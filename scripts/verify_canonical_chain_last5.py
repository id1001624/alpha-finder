from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.canonical_event_log import CANONICAL_ACTION_EVENT_LOG_FILE, load_canonical_action_events


ALLOWED_ACTIONS = {"entry", "add", "reduce", "take_profit", "stop_loss", "exit"}
ALLOWED_ENGINES = {"monster", "swing"}
REQUIRED_COLUMNS = {
    "event_ts",
    "trade_date",
    "engine",
    "ticker",
    "action_type",
    "strategy_tag",
    "reason_code",
    "reason_text",
    "price_ref",
    "size_ref",
    "priority",
    "dispatch_mode",
    "dispatch_status",
    "source_event_id",
    "source_log_id",
    "position_state_before",
    "position_state_after",
    "invalidation_rule",
    "created_at",
}


def _safe_text(value: object) -> str:
    return str(value or "").strip()


def main() -> int:
    if not CANONICAL_ACTION_EVENT_LOG_FILE.exists():
        print(json.dumps({"ok": False, "reason": "canonical_log_missing", "path": str(CANONICAL_ACTION_EVENT_LOG_FILE)}, ensure_ascii=False))
        return 1

    df = load_canonical_action_events(limit=200000)
    if len(df) == 0:
        print(json.dumps({"ok": False, "reason": "canonical_log_empty", "path": str(CANONICAL_ACTION_EVENT_LOG_FILE)}, ensure_ascii=False))
        return 1

    columns = set(df.columns)
    missing_columns = sorted(REQUIRED_COLUMNS - columns)
    if missing_columns:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "missing_required_columns",
                    "missing_columns": missing_columns,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str).str.strip()
    df["engine"] = df["engine"].astype(str).str.strip().str.lower()
    df["action_type"] = df["action_type"].astype(str).str.strip().str.lower()
    df["dispatch_mode"] = df["dispatch_mode"].astype(str).str.strip().str.lower()
    df["dispatch_status"] = df["dispatch_status"].astype(str).str.strip().str.lower()

    latest_dates = sorted([d for d in df["trade_date"].unique() if d], reverse=True)[:5]
    if len(latest_dates) < 5:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "insufficient_trade_days",
                    "required_days": 5,
                    "available_days": len(latest_dates),
                    "trade_dates": latest_dates,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    scope_df = df[df["trade_date"].isin(latest_dates)].copy()

    invalid_actions = scope_df[~scope_df["action_type"].isin(ALLOWED_ACTIONS)]
    invalid_engines = scope_df[~scope_df["engine"].isin(ALLOWED_ENGINES)]
    missing_core = scope_df[
        (scope_df["source_event_id"].astype(str).str.strip() == "")
        | (scope_df["source_log_id"].astype(str).str.strip() == "")
        | (scope_df["reason_text"].astype(str).str.strip() == "")
        | (scope_df["dispatch_status"].astype(str).str.strip() == "")
    ]

    per_day = []
    for trade_date in sorted(latest_dates):
        day_df = scope_df[scope_df["trade_date"] == trade_date].copy()
        by_engine = Counter(day_df["engine"].tolist())
        by_action = Counter(day_df["action_type"].tolist())
        by_dispatch = Counter(day_df["dispatch_status"].tolist())

        per_day.append(
            {
                "trade_date": trade_date,
                "total_events": int(len(day_df)),
                "by_engine": dict(by_engine),
                "by_action_type": dict(by_action),
                "by_dispatch_status": dict(by_dispatch),
            }
        )

    window_engine_counts = Counter(scope_df["engine"].tolist())
    window_engine_failures = []
    for engine in sorted(ALLOWED_ENGINES):
        if int(window_engine_counts.get(engine, 0)) == 0:
            window_engine_failures.append(
                {
                    "engine": engine,
                    "reason": "missing_engine_coverage_in_window",
                }
            )

    recap_pending = scope_df[
        (scope_df["dispatch_mode"] == "recap")
        & (~scope_df["dispatch_status"].isin({"pending_recap", "sent_recap", "failed_recap"}))
    ]
    realtime_invalid = scope_df[
        (scope_df["dispatch_mode"] == "realtime")
        & (~scope_df["dispatch_status"].isin({"pending_realtime", "sent_realtime", "failed_realtime"}))
    ]
    invalid_dispatch_mode = scope_df[
        ~scope_df["dispatch_mode"].isin({"realtime", "recap"})
    ]

    ok = True
    reasons = []
    if len(invalid_actions) > 0:
        ok = False
        reasons.append("invalid_action_type")
    if len(invalid_engines) > 0:
        ok = False
        reasons.append("invalid_engine")
    if len(missing_core) > 0:
        ok = False
        reasons.append("missing_core_fields")
    if len(recap_pending) > 0:
        ok = False
        reasons.append("invalid_recap_dispatch_status")
    if len(realtime_invalid) > 0:
        ok = False
        reasons.append("invalid_realtime_dispatch_status")
    if len(invalid_dispatch_mode) > 0:
        ok = False
        reasons.append("invalid_dispatch_mode")
    if window_engine_failures:
        ok = False
        reasons.append("window_engine_coverage_failed")

    report = {
        "ok": ok,
        "checked_trade_dates": latest_dates,
        "canonical_log_path": str(CANONICAL_ACTION_EVENT_LOG_FILE),
        "total_checked_events": int(len(scope_df)),
        "per_day": per_day,
        "failure_reasons": reasons,
        "invalid_action_rows": int(len(invalid_actions)),
        "invalid_engine_rows": int(len(invalid_engines)),
        "missing_core_rows": int(len(missing_core)),
        "invalid_recap_dispatch_rows": int(len(recap_pending)),
        "invalid_realtime_dispatch_rows": int(len(realtime_invalid)),
        "invalid_dispatch_mode_rows": int(len(invalid_dispatch_mode)),
        "window_engine_counts": dict(window_engine_counts),
        "window_engine_failures": window_engine_failures,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
