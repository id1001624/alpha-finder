from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import install_builtin_print_logging

from ai_trading.intraday_execution_engine import run_intraday_execution_engine
from ai_trading.market_session import get_intraday_active_window
from config import (
    INTRADAY_IDLE_POLL_SECONDS,
    INTRADAY_POLL_SECONDS,
    INTRADAY_TOP_N,
)

install_builtin_print_logging()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repo-native intraday execution engine")
    parser.add_argument("--top-n", type=int, default=INTRADAY_TOP_N, help="Top N ai_decision tickers to monitor")
    parser.add_argument("--dry-run", action="store_true", help="Compute signals without writing logs or sending Discord")
    parser.add_argument("--loop", action="store_true", help="Keep polling intraday data continuously")
    parser.add_argument("--respect-active-window", action="store_true", help="Skip execution outside configured active window")
    parser.add_argument("--poll-seconds", type=int, default=INTRADAY_POLL_SECONDS, help="Polling interval for --loop mode")
    args = parser.parse_args()

    idle_poll_seconds = max(60, int(INTRADAY_IDLE_POLL_SECONDS))

    while True:
        if (args.loop or args.respect_active_window) and not args.dry_run:
            session = get_intraday_active_window()
            if not bool(session.get("is_active", False)):
                result = {
                    "ok": True,
                    "reason": "outside_active_window",
                    "now_local": session.get("now_local").strftime("%Y-%m-%d %H:%M:%S"),
                    "active_start": session.get("active_start_local").strftime("%m-%d %H:%M"),
                    "active_end": session.get("active_end_local").strftime("%m-%d %H:%M"),
                    "active_timezone": str(session.get("active_timezone", "Asia/Taipei")),
                    "market_timezone": str(session.get("market_timezone", "America/New_York")),
                    "market_date": str(session.get("market_date", "")),
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