"""Runner script for the Swing Core Engine."""
from __future__ import annotations

import argparse
import json
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
    
    # Structured output for GitHub Actions + logging
    # Format: JSON on one line for CI consumption
    structured_log = {
        "engine": "swing_core_engine",
        "status": "ok" if result.get("ok") else "skipped",
        "reason": result.get("reason", ""),
        "universe_count": result.get("universe_count", 0),
        "snapshot_count": result.get("snapshot_count", 0),
        "action_count": result.get("action_count", 0),
        "canonical_action_count": result.get("canonical_action_count", 0),
        "entry_count": result.get("entry_count", 0),
        "add_count": result.get("add_count", 0),
        "reduce_count": result.get("reduce_count", 0),
        "exit_count": result.get("exit_count", 0),
        "discord_attempt_count": result.get("discord_attempt_count", 0),
        "discord_success_count": result.get("discord_success_count", 0),
        "discord_ok": result.get("discord_ok"),
        "regime_tag": result.get("regime_tag", ""),
    }
    
    # Print structured JSON for CI capture
    print("::group::Swing Engine Execution Log")
    print(json.dumps(structured_log, ensure_ascii=False, indent=2))
    print("::endgroup::")
    
    # Print human-readable summary
    if result.get("ok"):
        print(
            f"[OK] universe={result.get('universe_count')} "
            f"snapshot={result.get('snapshot_count')} "
            f"actions={result.get('action_count')} "
            f"(entry={result.get('entry_count')} add={result.get('add_count')} "
            f"reduce={result.get('reduce_count')} exit={result.get('exit_count')}) "
            f"discord_ok={result.get('discord_ok')} "
            f"regime={result.get('regime_tag')}"
        )
        if result.get("message"):
            print(result["message"])
    else:
        print(f"[SKIP] {result.get('reason')}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
