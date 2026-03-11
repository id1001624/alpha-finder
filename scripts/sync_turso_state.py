from __future__ import annotations

import argparse
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import get_logger

from ai_trading.position_state import TRADE_LEDGER_FILE
from turso_state import (
    sync_ai_decision_latest,
    sync_execution_latest,
    sync_execution_log_csv,
    sync_positions_latest,
    sync_trade_ledger_csv,
    turso_status,
)

logger = get_logger(__name__)

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
AI_DECISION_LATEST = BACKTEST_DIR / "ai_decision_latest.csv"
POSITIONS_LATEST = BACKTEST_DIR / "positions_latest.csv"
EXECUTION_LATEST = BACKTEST_DIR / "execution_trade_latest.csv"
EXECUTION_DAILY_DIR = BACKTEST_DIR / "daily_execution_trades"
TRADE_LEDGER_LATEST = BACKTEST_DIR / "position_trade_log.csv"


def _sync_execution_history() -> str | None:
    main_log = BACKTEST_DIR / "execution_trade_log.csv"
    if main_log.exists():
        return sync_execution_log_csv(main_log)
    if not EXECUTION_DAILY_DIR.exists():
        return None

    synced = False
    for file in sorted(EXECUTION_DAILY_DIR.glob("*_execution_trade.csv")):
        result = sync_execution_log_csv(file)
        if result:
            synced = True
    return "turso://execution_trade_log/bulk" if synced else None


def main() -> int:
    parser = argparse.ArgumentParser(description="同步最新 runtime 狀態到 Turso")
    parser.parse_args()

    logger.info("turso_status: %s", turso_status())

    decision_source = AI_DECISION_LATEST
    positions_source = POSITIONS_LATEST
    execution_source = EXECUTION_LATEST

    decision_target = sync_ai_decision_latest(decision_source)
    positions_target = sync_positions_latest(positions_source)
    execution_target = sync_execution_latest(execution_source)
    execution_log_target = _sync_execution_history()
    trade_ledger_target = sync_trade_ledger_csv(TRADE_LEDGER_LATEST if TRADE_LEDGER_LATEST.exists() else TRADE_LEDGER_FILE)

    logger.info("=== Turso state 同步完成 ===")
    logger.info("ai_decision_latest: %s | source=%s", decision_target or '未同步', decision_source if decision_source.exists() else '缺少來源檔')
    logger.info("positions_latest: %s | source=%s", positions_target or '未同步', positions_source if positions_source.exists() else '缺少來源檔')
    logger.info("execution_trade_latest: %s | source=%s", execution_target or '未同步', execution_source if execution_source.exists() else '缺少來源檔')
    execution_history_source = BACKTEST_DIR / "execution_trade_log.csv"
    if not execution_history_source.exists():
        execution_history_source = EXECUTION_DAILY_DIR if EXECUTION_DAILY_DIR.exists() else Path("缺少來源檔")
    logger.info("execution_trade_log: %s | source=%s", execution_log_target or '未同步', execution_history_source)
    logger.info("position_trade_log: %s | source=%s", trade_ledger_target or '未同步', TRADE_LEDGER_LATEST if TRADE_LEDGER_LATEST.exists() else '缺少來源檔')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())