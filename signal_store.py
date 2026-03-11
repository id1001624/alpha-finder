import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Union

from app_logging import get_logger
from config import SIGNAL_RAW_LOG_RETENTION_DAYS, SIGNAL_STORE_RETENTION_DAYS


logger = get_logger(__name__)


@dataclass
class SignalEvent:
    schema_version: int
    source: str = "tradingview"
    symbol: str = ""
    exchange: Optional[str] = None
    timeframe: str = "1D"
    ts: str = ""
    close: Optional[float] = None
    vwap: Optional[float] = None
    sqz_on: Optional[bool] = None
    sqzmom_value: Optional[float] = None
    sqzmom_color: Optional[str] = None
    event: str = "update"
    signature: Optional[str] = None
    raw: Optional[Union[dict, str]] = None
    received_at: str = ""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "on"}:
            return True
        if v in {"false", "0", "no", "off"}:
            return False
    return None


def init_signal_store(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                source TEXT NOT NULL,
                exchange TEXT,
                close REAL,
                vwap REAL,
                sqz_on INTEGER,
                sqzmom_value REAL,
                sqzmom_color TEXT,
                event TEXT NOT NULL,
                signature TEXT,
                raw_text TEXT,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(symbol, timeframe, ts)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_lookup ON signals(symbol, received_at DESC, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_received_at ON signals(received_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_webhook_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT NOT NULL,
                content_type TEXT,
                received_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_webhook_logs_received_at ON raw_webhook_logs(received_at)"
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_signal_store(
    db_path: str,
    signal_retention_days: int = SIGNAL_STORE_RETENTION_DAYS,
    raw_log_retention_days: int = SIGNAL_RAW_LOG_RETENTION_DAYS,
) -> None:
    init_signal_store(db_path)
    conn = sqlite3.connect(db_path)
    try:
        signal_cutoff = (datetime.now(timezone.utc) - timedelta(days=max(int(signal_retention_days), 1))).isoformat()
        raw_cutoff = (datetime.now(timezone.utc) - timedelta(days=max(int(raw_log_retention_days), 1))).isoformat()
        conn.execute(
            "DELETE FROM signals WHERE COALESCE(updated_at, received_at) < ?",
            (signal_cutoff,),
        )
        conn.execute(
            "DELETE FROM raw_webhook_logs WHERE received_at < ?",
            (raw_cutoff,),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("signal store cleanup failed: %s", exc)
    finally:
        conn.close()


def log_raw_webhook(db_path: str, raw_text: str, content_type: Optional[str]) -> None:
    init_signal_store(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO raw_webhook_logs(raw_text, content_type, received_at) VALUES (?, ?, ?)",
            (raw_text, content_type or "", utcnow_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    cleanup_signal_store(db_path)


def build_signal_event(payload: dict, signature: Optional[str], received_at: Optional[str] = None) -> SignalEvent:
    received_ts = received_at or utcnow_iso()
    symbol = str(payload.get("symbol", "")).strip().upper()
    timeframe = str(payload.get("timeframe", "1D")).strip() or "1D"
    ts = str(payload.get("ts", "")).strip()
    sqzmom_color = payload.get("sqzmom_color")
    if isinstance(sqzmom_color, str):
        sqzmom_color = sqzmom_color.strip().lower()

    return SignalEvent(
        schema_version=int(payload.get("schema_version", 1)),
        source=str(payload.get("source", "tradingview")),
        symbol=symbol,
        exchange=payload.get("exchange"),
        timeframe=timeframe,
        ts=ts,
        close=_parse_float(payload.get("close")),
        vwap=_parse_float(payload.get("vwap")),
        sqz_on=_parse_bool(payload.get("sqz_on")),
        sqzmom_value=_parse_float(payload.get("sqzmom_value")),
        sqzmom_color=sqzmom_color,
        event=str(payload.get("event", "update")),
        signature=signature,
        raw=payload,
        received_at=received_ts,
    )


def upsert_signal_event(db_path: str, event: SignalEvent) -> None:
    init_signal_store(db_path)
    now_iso = utcnow_iso()
    raw_text = json.dumps(event.raw, ensure_ascii=False) if isinstance(event.raw, dict) else str(event.raw or "")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO signals (
                symbol, timeframe, ts, schema_version, source, exchange, close, vwap, sqz_on,
                sqzmom_value, sqzmom_color, event, signature, raw_text, received_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                schema_version=excluded.schema_version,
                source=excluded.source,
                exchange=excluded.exchange,
                close=excluded.close,
                vwap=excluded.vwap,
                sqz_on=excluded.sqz_on,
                sqzmom_value=excluded.sqzmom_value,
                sqzmom_color=excluded.sqzmom_color,
                event=excluded.event,
                signature=excluded.signature,
                raw_text=excluded.raw_text,
                received_at=excluded.received_at,
                updated_at=excluded.updated_at
            """,
            (
                event.symbol,
                event.timeframe,
                event.ts,
                event.schema_version,
                event.source,
                event.exchange,
                event.close,
                event.vwap,
                int(event.sqz_on) if event.sqz_on is not None else None,
                event.sqzmom_value,
                event.sqzmom_color,
                event.event,
                event.signature,
                raw_text,
                event.received_at,
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    cleanup_signal_store(db_path)


def _parse_iso_or_epoch(ts_value: str) -> Optional[datetime]:
    if not ts_value:
        return None
    try:
        if ts_value.isdigit():
            sec = int(ts_value)
            if len(ts_value) >= 13:
                sec = sec / 1000
            return datetime.fromtimestamp(sec, tz=timezone.utc)
    except Exception:
        return None

    clean = ts_value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def get_latest_signals(
    db_path: str,
    asof: Optional[datetime] = None,
    max_age_minutes: int = 240,
    require_same_day: bool = True,
) -> Dict[str, SignalEvent]:
    init_signal_store(db_path)
    now = asof or datetime.now(timezone.utc)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT * FROM signals
            ORDER BY symbol ASC, received_at DESC, updated_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    latest: Dict[str, SignalEvent] = {}
    for row in rows:
        symbol = row["symbol"]
        if symbol in latest:
            continue

        received_dt = _parse_iso_or_epoch(str(row["received_at"]))
        ts_dt = _parse_iso_or_epoch(str(row["ts"]))
        if not received_dt or not ts_dt:
            continue

        age_minutes = (now - received_dt).total_seconds() / 60.0
        if age_minutes > max_age_minutes:
            continue
        if require_same_day and ts_dt.date() != now.date():
            continue

        raw_val: Union[dict, str, None]
        try:
            raw_val = json.loads(row["raw_text"]) if row["raw_text"] else None
        except Exception:
            raw_val = row["raw_text"]

        latest[symbol] = SignalEvent(
            schema_version=int(row["schema_version"]),
            source=row["source"],
            symbol=symbol,
            exchange=row["exchange"],
            timeframe=row["timeframe"],
            ts=row["ts"],
            close=row["close"],
            vwap=row["vwap"],
            sqz_on=None if row["sqz_on"] is None else bool(row["sqz_on"]),
            sqzmom_value=row["sqzmom_value"],
            sqzmom_color=row["sqzmom_color"],
            event=row["event"],
            signature=row["signature"],
            raw=raw_val,
            received_at=row["received_at"],
        )

    return latest


def signal_event_to_dict(event: SignalEvent) -> dict:
    return asdict(event)