from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.intraday_execution_engine import run_intraday_execution_engine
from config import INTRADAY_POLL_SECONDS, INTRADAY_TOP_N


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repo-native intraday execution engine")
    parser.add_argument("--top-n", type=int, default=INTRADAY_TOP_N, help="Top N ai_decision tickers to monitor")
    parser.add_argument("--dry-run", action="store_true", help="Compute signals without writing logs or sending Discord")
    parser.add_argument("--loop", action="store_true", help="Keep polling intraday data continuously")
    parser.add_argument("--poll-seconds", type=int, default=INTRADAY_POLL_SECONDS, help="Polling interval for --loop mode")
    args = parser.parse_args()

    while True:
        result = run_intraday_execution_engine(top_n=max(1, int(args.top_n)), dry_run=args.dry_run)
        print(result.get("message") or result)
        if not args.loop:
            return 0 if result.get("ok") else 1
        time.sleep(max(30, int(args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())