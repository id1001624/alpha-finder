from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.canonical_event_log import load_canonical_action_events
from scripts.push_alerts_from_ai_decision import _load_execution_df


INTRADAY_ACTION_LOG = PROJECT_ROOT / "repo_outputs" / "backtest" / "intraday" / "intraday_action_log.csv"
SWING_ACTION_LOG = PROJECT_ROOT / "repo_outputs" / "backtest" / "swing" / "swing_action_log.csv"
OUTPUT_DIR = PROJECT_ROOT / "repo_outputs" / "backtest" / "canonical"


def _safe_read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def _valid_dispatch(mode: str, status: str) -> bool:
    mode_text = str(mode or "").strip().lower()
    status_text = str(status or "").strip().lower()
    if mode_text == "realtime":
        return status_text in {"pending_realtime", "sent_realtime", "failed_realtime"}
    if mode_text == "recap":
        return status_text in {"pending_recap", "sent_recap", "failed_recap"}
    return False


def _event_in_engine_log(row: pd.Series, intraday_df: pd.DataFrame, swing_df: pd.DataFrame) -> bool:
    event_id = str(row.get("source_event_id", "")).strip()
    engine = str(row.get("engine", "")).strip().lower()
    target = swing_df if engine == "swing" else intraday_df
    if len(target) == 0:
        return False
    if "source_event_id" in target.columns:
        return bool((target["source_event_id"].astype(str).str.strip() == event_id).any())

    ticker = str(row.get("ticker", "")).strip().upper()
    action = str(row.get("action_type", "")).strip().lower()
    raw_action = action
    if engine == "swing":
        raw_action = {
            "entry": "swing_entry",
            "add": "swing_add",
            "reduce": "swing_reduce",
            "exit": "swing_exit",
        }.get(action, action)
    if "ticker" not in target.columns or "action" not in target.columns:
        return False
    mask = (
        target["ticker"].astype(str).str.strip().str.upper() == ticker
    ) & (
        target["action"].astype(str).str.strip().str.lower() == raw_action
    )
    return bool(mask.any())


def build_report(trade_days: int = 5) -> Dict[str, object]:
    canonical = load_canonical_action_events(limit=200000)
    if len(canonical) == 0:
        return {
            "ok": False,
            "reason": "canonical_log_empty",
            "total_events": 0,
            "broken_events": 0,
            "trace_sample": [],
            "trade_dates": [],
        }

    canonical = canonical.copy()
    canonical["trade_date"] = canonical["trade_date"].astype(str).str.strip()
    canonical["engine"] = canonical["engine"].astype(str).str.strip().str.lower()
    canonical["source_event_id"] = canonical["source_event_id"].astype(str).str.strip()

    trade_dates = sorted([d for d in canonical["trade_date"].unique() if d], reverse=True)[: max(1, int(trade_days))]
    scoped = canonical[canonical["trade_date"].isin(trade_dates)].copy()

    intraday_df = _safe_read(INTRADAY_ACTION_LOG)
    swing_df = _safe_read(SWING_ACTION_LOG)
    recap_df = _load_execution_df(limit=max(2000, len(scoped) * 2))
    recap_event_ids = set(recap_df.get("source_event_id", pd.Series(dtype=str)).astype(str).str.strip().tolist())

    traces: List[dict] = []
    for _, row in scoped.iterrows():
        event_id = str(row.get("source_event_id", "")).strip()
        chain = {
            "source_event_id": event_id,
            "trade_date": str(row.get("trade_date", "")),
            "event_ts": str(row.get("event_ts", "")),
            "engine": str(row.get("engine", "")),
            "ticker": str(row.get("ticker", "")),
            "action_type": str(row.get("action_type", "")),
            "dispatch_mode": str(row.get("dispatch_mode", "")),
            "dispatch_status": str(row.get("dispatch_status", "")),
            "engine_logged": _event_in_engine_log(row, intraday_df, swing_df),
            "canonical_logged": bool(event_id),
            "dispatch_valid": _valid_dispatch(str(row.get("dispatch_mode", "")), str(row.get("dispatch_status", ""))),
            "recap_visible": event_id in recap_event_ids,
        }
        chain["chain_ok"] = bool(
            chain["engine_logged"]
            and chain["canonical_logged"]
            and chain["dispatch_valid"]
            and chain["recap_visible"]
        )
        traces.append(chain)

    broken = [item for item in traces if not item["chain_ok"]]
    report = {
        "ok": len(broken) == 0,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "trade_dates": sorted(trade_dates),
        "total_events": len(traces),
        "broken_events": len(broken),
        "broken_examples": broken[:30],
        "trace_sample": traces[:30],
        "stats": {
            "engine_logged_ok": sum(1 for t in traces if t["engine_logged"]),
            "dispatch_valid_ok": sum(1 for t in traces if t["dispatch_valid"]),
            "recap_visible_ok": sum(1 for t in traces if t["recap_visible"]),
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate canonical notification chain reconciliation report")
    parser.add_argument("--trade-days", type=int, default=5)
    parser.add_argument("--output", type=str, default="")
    parser.add_argument("--fail-on-broken", action="store_true")
    args = parser.parse_args()

    report = build_report(trade_days=max(1, int(args.trade_days)))
    output_path = Path(args.output).resolve() if str(args.output).strip() else OUTPUT_DIR / "notification_reconciliation_latest.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.fail_on_broken and not bool(report.get("ok")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
