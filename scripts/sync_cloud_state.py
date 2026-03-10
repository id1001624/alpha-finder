from __future__ import annotations

import argparse
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cloud_state import sync_ai_decision_latest, sync_execution_latest, sync_positions_latest

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
AI_DECISION_LATEST = BACKTEST_DIR / "ai_decision_latest.csv"
POSITIONS_LATEST = BACKTEST_DIR / "positions_latest.csv"
EXECUTION_LATEST = BACKTEST_DIR / "execution_trade_latest.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="同步本機 runtime 狀態到 cloud_state")
    parser.parse_args()

    decision_target = sync_ai_decision_latest(AI_DECISION_LATEST)
    positions_target = sync_positions_latest(POSITIONS_LATEST)
    execution_target = sync_execution_latest(EXECUTION_LATEST)

    print("=== cloud_state 同步完成 ===")
    print(f"ai_decision_latest: {decision_target or '缺少來源檔'}")
    print(f"positions_latest: {positions_target or '缺少來源檔'}")
    print(f"execution_trade_latest: {execution_target or '缺少來源檔'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())