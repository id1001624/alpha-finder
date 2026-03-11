from __future__ import annotations

from datetime import datetime
import hashlib
import importlib
from io import StringIO
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from config import TURSO_AUTH_TOKEN, TURSO_DATABASE_URL, TURSO_ENABLED


STATE_KEY_AI_DECISION_LATEST = "ai_decision_latest"
STATE_KEY_POSITIONS_LATEST = "positions_latest"
STATE_KEY_EXECUTION_LATEST = "execution_trade_latest"
STATE_KEY_INTRADAY_HEARTBEAT = "intraday_engine_heartbeat"
STATE_SOURCE_PREFIX = "turso://runtime_state_latest/"
TRADE_LEDGER_SOURCE_PREFIX = "turso://position_trade_log/"
EXECUTION_LOG_SOURCE_PREFIX = "turso://execution_trade_log/"

TRADE_LEDGER_FIELDS = [
    "recorded_at",
    "ticker",
    "side",
    "quantity",
    "price",
    "position_effect",
    "before_qty",
    "after_qty",
    "avg_cost_after",
    "realized_pnl_delta",
    "source",
    "note",
]

EXECUTION_LOG_FIELDS = [
    "recorded_at",
    "execution_date",
    "execution_time",
    "decision_date",
    "ticker",
    "rank",
    "action",
    "position_effect",
    "decision_tag",
    "risk_level",
    "tech_status",
    "theme",
    "reason_summary",
    "signal_source",
    "exchange",
    "timeframe",
    "tv_event",
    "signal_ts",
    "close",
    "vwap",
    "sqzmom_color",
    "sqzmom_value",
    "signal_signature",
]


def turso_configured() -> bool:
    return bool(TURSO_ENABLED and str(TURSO_DATABASE_URL).strip() and str(TURSO_AUTH_TOKEN).strip())


def turso_status() -> str:
    if not TURSO_ENABLED:
        return "disabled"
    if not str(TURSO_DATABASE_URL).strip() or not str(TURSO_AUTH_TOKEN).strip():
        return "missing_credentials"
    try:
        module = importlib.import_module("libsql")
    except ImportError:
        return "missing_libsql_package"
    if getattr(module, "connect", None) is None:
        return "missing_connect_api"
    return "ready"


def turso_source_label(state_key: str) -> str:
    return f"{STATE_SOURCE_PREFIX}{state_key}"


def _connect():
    if not turso_configured():
        return None
    try:
        module = importlib.import_module("libsql")
    except ImportError:
        return None
    connect = getattr(module, "connect", None)
    if connect is None:
        return None
    try:
        conn = connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        _ensure_schema(conn)
        return conn
    except Exception:  # noqa: BLE001
        return None


def _safe_close(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_state_latest (
            state_key TEXT PRIMARY KEY,
            csv_content TEXT NOT NULL,
            source_name TEXT NOT NULL DEFAULT '',
            row_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_state_latest_updated_at ON runtime_state_latest(updated_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_trade_log (
            event_id TEXT PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            position_effect TEXT NOT NULL DEFAULT '',
            before_qty REAL NOT NULL DEFAULT 0,
            after_qty REAL NOT NULL DEFAULT 0,
            avg_cost_after REAL NOT NULL DEFAULT 0,
            realized_pnl_delta REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_position_trade_log_recorded_at ON position_trade_log(recorded_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_trade_log (
            event_id TEXT PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            execution_date TEXT NOT NULL DEFAULT '',
            execution_time TEXT NOT NULL DEFAULT '',
            decision_date TEXT NOT NULL DEFAULT '',
            ticker TEXT NOT NULL,
            rank INTEGER NOT NULL DEFAULT 0,
            action TEXT NOT NULL DEFAULT '',
            position_effect TEXT NOT NULL DEFAULT '',
            decision_tag TEXT NOT NULL DEFAULT '',
            risk_level TEXT NOT NULL DEFAULT '',
            tech_status TEXT NOT NULL DEFAULT '',
            theme TEXT NOT NULL DEFAULT '',
            reason_summary TEXT NOT NULL DEFAULT '',
            signal_source TEXT NOT NULL DEFAULT '',
            exchange TEXT NOT NULL DEFAULT '',
            timeframe TEXT NOT NULL DEFAULT '',
            tv_event TEXT NOT NULL DEFAULT '',
            signal_ts TEXT NOT NULL DEFAULT '',
            close REAL,
            vwap REAL,
            sqzmom_color TEXT NOT NULL DEFAULT '',
            sqzmom_value REAL,
            signal_signature TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_trade_log_lookup ON execution_trade_log(execution_date, execution_time, ticker)"
    )
    conn.commit()


def _read_csv_file(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def _read_csv_text(csv_content: str) -> pd.DataFrame:
    if not str(csv_content or "").strip():
        return pd.DataFrame()
    try:
        return pd.read_csv(StringIO(csv_content))
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _df_to_csv_text(df: pd.DataFrame) -> str:
    buffer = StringIO()
    export_df = df.copy() if df is not None else pd.DataFrame()
    export_df.to_csv(buffer, index=False)
    return buffer.getvalue()


def _normalize_trade_row(row: dict) -> dict:
    payload = {field: row.get(field, "") for field in TRADE_LEDGER_FIELDS}
    payload["recorded_at"] = str(payload.get("recorded_at", "")).strip()
    payload["ticker"] = str(payload.get("ticker", "")).strip().upper()
    payload["side"] = str(payload.get("side", "")).strip().lower()
    payload["position_effect"] = str(payload.get("position_effect", "")).strip().lower()
    payload["source"] = str(payload.get("source", "")).strip()
    payload["note"] = str(payload.get("note", "")).strip()
    for field in ["quantity", "price", "before_qty", "after_qty", "avg_cost_after", "realized_pnl_delta"]:
        payload[field] = float(pd.to_numeric(payload.get(field), errors="coerce") or 0.0)
    return payload


def _trade_event_id(row: dict) -> str:
    normalized = _normalize_trade_row(row)
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def trade_ledger_source_label(event_id: str) -> str:
    return f"{TRADE_LEDGER_SOURCE_PREFIX}{event_id}"


def execution_log_source_label(event_id: str) -> str:
    return f"{EXECUTION_LOG_SOURCE_PREFIX}{event_id}"


def _normalize_execution_row(row: dict) -> dict:
    payload = {field: row.get(field, "") for field in EXECUTION_LOG_FIELDS}
    for field in [
        "recorded_at",
        "execution_date",
        "execution_time",
        "decision_date",
        "ticker",
        "action",
        "position_effect",
        "decision_tag",
        "risk_level",
        "tech_status",
        "theme",
        "reason_summary",
        "signal_source",
        "exchange",
        "timeframe",
        "tv_event",
        "signal_ts",
        "sqzmom_color",
        "signal_signature",
    ]:
        payload[field] = str(payload.get(field, "")).strip()
    payload["ticker"] = payload["ticker"].upper()
    payload["rank"] = int(pd.to_numeric(payload.get("rank"), errors="coerce") or 0)
    for field in ["close", "vwap", "sqzmom_value"]:
        parsed = pd.to_numeric(payload.get(field), errors="coerce")
        payload[field] = None if pd.isna(parsed) else float(parsed)
    return payload


def _execution_event_id(row: dict) -> str:
    normalized = _normalize_execution_row(row)
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def load_runtime_df(state_key: str) -> tuple[pd.DataFrame, str | None]:
    conn = _connect()
    if conn is None:
        return pd.DataFrame(), None
    try:
        row = conn.execute(
            "SELECT csv_content FROM runtime_state_latest WHERE state_key = ?",
            (state_key,),
        ).fetchone()
    except Exception:
        return pd.DataFrame(), None
    finally:
        _safe_close(conn)

    if not row:
        return pd.DataFrame(), None

    csv_content = row[0] if isinstance(row, tuple) else row["csv_content"]
    return _read_csv_text(str(csv_content or "")), turso_source_label(state_key)


def load_runtime_df_with_fallback(state_key: str, fallback_paths: Iterable[Path]) -> tuple[pd.DataFrame, str | None]:
    df, source = load_runtime_df(state_key)
    if source is not None:
        return df, source

    for raw_path in fallback_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        return _read_csv_file(path), str(path)
    return pd.DataFrame(), None


def sync_runtime_df(state_key: str, df: pd.DataFrame, source_name: str = "") -> str | None:
    conn = _connect()
    if conn is None:
        return None
    try:
        conn.execute(
            """
            INSERT INTO runtime_state_latest (state_key, csv_content, source_name, row_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                csv_content = excluded.csv_content,
                source_name = excluded.source_name,
                row_count = excluded.row_count,
                updated_at = excluded.updated_at
            """,
            (
                state_key,
                _df_to_csv_text(df),
                str(source_name or "").strip(),
                int(len(df.index)) if df is not None else 0,
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        return None
    finally:
        _safe_close(conn)
    return turso_source_label(state_key)


def sync_runtime_csv(state_key: str, source_path: Path) -> str | None:
    path = Path(source_path)
    if not path.exists():
        return None
    return sync_runtime_df(state_key, _read_csv_file(path), source_name=str(path))


def sync_ai_decision_latest(source_path: Path) -> str | None:
    return sync_runtime_csv(STATE_KEY_AI_DECISION_LATEST, source_path)


def sync_positions_latest(source_path: Path) -> str | None:
    return sync_runtime_csv(STATE_KEY_POSITIONS_LATEST, source_path)


def sync_execution_latest(source_path: Path) -> str | None:
    return sync_runtime_csv(STATE_KEY_EXECUTION_LATEST, source_path)


def append_execution_log_rows(rows: list[dict]) -> str | None:
    if not rows:
        return None
    conn = _connect()
    if conn is None:
        return None

    values = []
    for row in rows:
        payload = _normalize_execution_row(row)
        values.append(
            (
                _execution_event_id(payload),
                payload["recorded_at"],
                payload["execution_date"],
                payload["execution_time"],
                payload["decision_date"],
                payload["ticker"],
                payload["rank"],
                payload["action"],
                payload["position_effect"],
                payload["decision_tag"],
                payload["risk_level"],
                payload["tech_status"],
                payload["theme"],
                payload["reason_summary"],
                payload["signal_source"],
                payload["exchange"],
                payload["timeframe"],
                payload["tv_event"],
                payload["signal_ts"],
                payload["close"],
                payload["vwap"],
                payload["sqzmom_color"],
                payload["sqzmom_value"],
                payload["signal_signature"],
            )
        )

    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO execution_trade_log (
                event_id,
                recorded_at,
                execution_date,
                execution_time,
                decision_date,
                ticker,
                rank,
                action,
                position_effect,
                decision_tag,
                risk_level,
                tech_status,
                theme,
                reason_summary,
                signal_source,
                exchange,
                timeframe,
                tv_event,
                signal_ts,
                close,
                vwap,
                sqzmom_color,
                sqzmom_value,
                signal_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        return None
    finally:
        _safe_close(conn)
    return f"{EXECUTION_LOG_SOURCE_PREFIX}bulk"


def sync_execution_log_csv(source_path: Path) -> str | None:
    path = Path(source_path)
    if not path.exists():
        return None
    df = _read_csv_file(path)
    if len(df) == 0:
        return None
    return append_execution_log_rows(df.to_dict(orient="records"))


def append_trade_ledger_row(row: dict) -> str | None:
    conn = _connect()
    if conn is None:
        return None

    payload = _normalize_trade_row(row)
    event_id = _trade_event_id(payload)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO position_trade_log (
                event_id,
                recorded_at,
                ticker,
                side,
                quantity,
                price,
                position_effect,
                before_qty,
                after_qty,
                avg_cost_after,
                realized_pnl_delta,
                source,
                note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                payload["recorded_at"],
                payload["ticker"],
                payload["side"],
                payload["quantity"],
                payload["price"],
                payload["position_effect"],
                payload["before_qty"],
                payload["after_qty"],
                payload["avg_cost_after"],
                payload["realized_pnl_delta"],
                payload["source"],
                payload["note"],
            ),
        )
        conn.commit()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return trade_ledger_source_label(event_id)


def sync_trade_ledger_csv(source_path: Path) -> str | None:
    path = Path(source_path)
    if not path.exists():
        return None

    conn = _connect()
    if conn is None:
        return None

    df = _read_csv_file(path)
    if len(df) == 0:
        _safe_close(conn)
        return None

    rows = []
    for _, row in df.iterrows():
        payload = _normalize_trade_row(row.to_dict())
        rows.append(
            (
                _trade_event_id(payload),
                payload["recorded_at"],
                payload["ticker"],
                payload["side"],
                payload["quantity"],
                payload["price"],
                payload["position_effect"],
                payload["before_qty"],
                payload["after_qty"],
                payload["avg_cost_after"],
                payload["realized_pnl_delta"],
                payload["source"],
                payload["note"],
            )
        )

    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO position_trade_log (
                event_id,
                recorded_at,
                ticker,
                side,
                quantity,
                price,
                position_effect,
                before_qty,
                after_qty,
                avg_cost_after,
                realized_pnl_delta,
                source,
                note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        return None
    finally:
        _safe_close(conn)
    return f"{TRADE_LEDGER_SOURCE_PREFIX}bulk"


def _load_query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = _connect()
    if conn is None:
        return pd.DataFrame()
    try:
        rows = conn.execute(sql, params).fetchall()
        columns = [item[0] for item in conn.execute(sql, params).description]
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    finally:
        _safe_close(conn)
    if not rows:
        return pd.DataFrame(columns=columns if 'columns' in locals() else [])
    return pd.DataFrame(rows, columns=columns)


def load_recent_trade_ledger(limit: int = 5, ticker: str = "") -> pd.DataFrame:
    limit_value = max(1, int(limit))
    ticker_value = str(ticker or "").strip().upper()
    if ticker_value:
        return _load_query_df(
            """
            SELECT recorded_at, ticker, side, quantity, price, position_effect, after_qty, avg_cost_after, realized_pnl_delta, source, note
            FROM position_trade_log
            WHERE ticker = ?
            ORDER BY recorded_at DESC, event_id DESC
            LIMIT ?
            """,
            (ticker_value, limit_value),
        )
    return _load_query_df(
        """
        SELECT recorded_at, ticker, side, quantity, price, position_effect, after_qty, avg_cost_after, realized_pnl_delta, source, note
        FROM position_trade_log
        ORDER BY recorded_at DESC, event_id DESC
        LIMIT ?
        """,
        (limit_value,),
    )


def load_recent_execution_log(limit: int = 5, ticker: str = "") -> pd.DataFrame:
    limit_value = max(1, int(limit))
    ticker_value = str(ticker or "").strip().upper()
    if ticker_value:
        return _load_query_df(
            """
            SELECT recorded_at, execution_date, execution_time, ticker, action, position_effect, rank, decision_tag, close, vwap, sqzmom_color, sqzmom_value, signal_source, timeframe, reason_summary, signal_ts
            FROM execution_trade_log
            WHERE ticker = ?
            ORDER BY execution_date DESC, execution_time DESC, recorded_at DESC
            LIMIT ?
            """,
            (ticker_value, limit_value),
        )
    return _load_query_df(
        """
        SELECT recorded_at, execution_date, execution_time, ticker, action, position_effect, rank, decision_tag, close, vwap, sqzmom_color, sqzmom_value, signal_source, timeframe, reason_summary, signal_ts
        FROM execution_trade_log
        ORDER BY execution_date DESC, execution_time DESC, recorded_at DESC
        LIMIT ?
        """,
        (limit_value,),
    )