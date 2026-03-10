from __future__ import annotations

import argparse
from datetime import datetime, time as dt_time
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.intraday_execution_engine import run_intraday_execution_engine
from config import (
    INTRADAY_ACTIVE_END_LOCAL,
    INTRADAY_ACTIVE_START_LOCAL,
    INTRADAY_ACTIVE_TIMEZONE,
    INTRADAY_IDLE_POLL_SECONDS,
    INTRADAY_POLL_SECONDS,
    INTRADAY_TOP_N,
)


def _parse_hhmm(value: str, fallback: str) -> dt_time:
    raw = str(value or fallback).strip()
    try:
        hour_str, minute_str = raw.split(":", 1)
        return dt_time(hour=int(hour_str), minute=int(minute_str))
    except (ValueError, TypeError):
        hour_str, minute_str = fallback.split(":", 1)
        return dt_time(hour=int(hour_str), minute=int(minute_str))


def _is_in_active_window(now_dt: datetime, start_local: dt_time, end_local: dt_time) -> bool:
    current = now_dt.time().replace(second=0, microsecond=0)
    if start_local <= end_local:
        return start_local <= current <= end_local
    return current >= start_local or current <= end_local


def _now_in_active_timezone() -> datetime:
    timezone_name = str(INTRADAY_ACTIVE_TIMEZONE or "Asia/Taipei").strip() or "Asia/Taipei"
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        return datetime.now()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repo-native intraday execution engine")
    parser.add_argument("--top-n", type=int, default=INTRADAY_TOP_N, help="Top N ai_decision tickers to monitor")
    parser.add_argument("--dry-run", action="store_true", help="Compute signals without writing logs or sending Discord")
    parser.add_argument("--loop", action="store_true", help="Keep polling intraday data continuously")
    parser.add_argument("--respect-active-window", action="store_true", help="Skip execution outside configured active window")
    parser.add_argument("--poll-seconds", type=int, default=INTRADAY_POLL_SECONDS, help="Polling interval for --loop mode")
    args = parser.parse_args()

    session_start = _parse_hhmm(INTRADAY_ACTIVE_START_LOCAL, "21:20")
    session_end = _parse_hhmm(INTRADAY_ACTIVE_END_LOCAL, "05:10")
    idle_poll_seconds = max(60, int(INTRADAY_IDLE_POLL_SECONDS))

    while True:
        if (args.loop or args.respect_active_window) and not args.dry_run:
            now_dt = _now_in_active_timezone()
            if not _is_in_active_window(now_dt, session_start, session_end):
                result = {
                    "ok": True,
                    "reason": "outside_active_window",
                    "now_local": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "active_start": session_start.strftime("%H:%M"),
                    "active_end": session_end.strftime("%H:%M"),
                    "active_timezone": str(INTRADAY_ACTIVE_TIMEZONE or "Asia/Taipei"),
                }
                print(result)
                if not args.loop:
                    return 0
                time.sleep(idle_poll_seconds)
                continue

        result = run_intraday_execution_engine(top_n=max(1, int(args.top_n)), dry_run=args.dry_run)
        print(result.get("message") or result)
        if result.get("discord_ok") is not None:
            print(f"[DISCORD] ok={result.get('discord_ok')} detail={result.get('discord_detail', '')}")
        if not args.loop:
            return 0 if result.get("ok") else 1
        time.sleep(max(30, int(args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())