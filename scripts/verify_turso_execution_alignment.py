from __future__ import annotations

import json
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import turso_state
from config import TURSO_AUTH_TOKEN, TURSO_DATABASE_URL


REQUIRED_TABLE_COLUMNS = {
    "event_id",
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
    "horizon_tag",
    "strategy_profile",
    "signal_type",
    "regime_tag",
    "entry_reason",
    "exit_reason",
    "position_size_fraction",
    "entry_price",
    "exit_price",
    "holding_minutes",
    "holding_days",
    "mfe",
    "mae",
    "realized_R",
    "realized_pct",
    "slippage_bps",
    "source_decision_rank",
    "source_confidence",
    "source_api_final_score",
    "snapshot_json",
    "close",
    "vwap",
    "sqzmom_color",
    "sqzmom_value",
    "signal_signature",
}


def _table_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    names: set[str] = set()
    for row in rows:
        if isinstance(row, tuple):
            names.add(str(row[1]))
        else:
            names.add(str(row["name"]))
    return names


def main() -> int:
    status = turso_state.turso_status()
    if status != "ready":
        report = {
            "ok": False,
            "reason": "turso_not_ready",
            "turso_status": status,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    module = importlib.import_module("libsql")
    connect = getattr(module, "connect", None)
    if connect is None:
        report = {
            "ok": False,
            "reason": "libsql_connect_missing",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    conn = connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)

    try:
        cols = _table_columns(conn, "execution_trade_log")
    finally:
        conn.close()

    missing = sorted(REQUIRED_TABLE_COLUMNS - cols)
    if missing:
        report = {
            "ok": False,
            "reason": "missing_execution_trade_log_columns",
            "missing_columns": missing,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    probe_row = {
        "recorded_at": now,
        "execution_date": now[:10],
        "execution_time": now[11:19],
        "decision_date": now[:10],
        "ticker": "ZZTURSOCHK",
        "rank": 9999,
        "action": "entry",
        "position_effect": "open",
        "decision_tag": "probe",
        "risk_level": "low",
        "tech_status": "probe",
        "theme": "probe",
        "reason_summary": "turso alignment probe",
        "signal_source": "canonical_probe",
        "exchange": "",
        "timeframe": "1m",
        "tv_event": "entry",
        "signal_ts": now,
        "horizon_tag": "intraday_monster",
        "strategy_profile": "monster_swing",
        "signal_type": "probe",
        "regime_tag": "neutral",
        "entry_reason": "probe",
        "exit_reason": "",
        "position_size_fraction": 0.01,
        "entry_price": 1.0,
        "exit_price": None,
        "holding_minutes": None,
        "holding_days": None,
        "mfe": None,
        "mae": None,
        "realized_R": None,
        "realized_pct": None,
        "slippage_bps": None,
        "source_decision_rank": 9999,
        "source_confidence": 0.0,
        "source_api_final_score": 0.0,
        "snapshot_json": "{}",
        "close": 1.0,
        "vwap": 1.0,
        "sqzmom_color": "green",
        "sqzmom_value": 0.1,
        "signal_signature": f"probe|{now}",
    }
    probe_event_id = ""
    append_source = turso_state.append_execution_log_rows([probe_row])
    if not append_source:
        report = {
            "ok": False,
            "reason": "append_execution_log_rows_failed",
            "probe_event_id": probe_event_id,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    conn = connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    if conn is None:
        report = {
            "ok": False,
            "reason": "reconnect_failed_after_append",
            "probe_event_id": probe_event_id,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    found = False
    try:
        row = conn.execute(
            "SELECT event_id FROM execution_trade_log WHERE ticker = ? AND signal_signature = ? ORDER BY recorded_at DESC LIMIT 1",
            ("ZZTURSOCHK", str(probe_row.get("signal_signature", ""))),
        ).fetchone()
        found = row is not None
        if row is not None:
            probe_event_id = str(row[0] if isinstance(row, tuple) else row["event_id"])
            conn.execute("DELETE FROM execution_trade_log WHERE event_id = ?", (probe_event_id,))
        conn.commit()
    finally:
        conn.close()

    report = {
        "ok": bool(found),
        "turso_status": status,
        "probe_event_id": probe_event_id,
        "probe_written": bool(found),
        "append_source": append_source,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
