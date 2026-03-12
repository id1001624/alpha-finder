"""Runner script for the Swing Core Engine."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import install_builtin_print_logging
from ai_trading.swing_core_engine import run_swing_core_engine

install_builtin_print_logging()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Swing Core Engine (daily AVWAP+SQZMOM)")
    parser.add_argument("--dry-run", action="store_true", help="計算訊號但不寫出檔案也不推送 Discord")
    args = parser.parse_args()

    result = run_swing_core_engine(dry_run=args.dry_run)
    if result.get("ok"):
        print(
            f"[OK] universe={result.get('universe_count')} "
            f"snapshot={result.get('snapshot_count')} "
            f"actions={result.get('action_count')} "
            f"regime={result.get('regime_tag')}"
        )
        if result.get("message"):
            print(result["message"])
    else:
        print(f"[SKIP] {result.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
