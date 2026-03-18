from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_DIR = PROJECT_ROOT / "repo_outputs" / "backtest" / "canonical"
DAILY_DIR = CANONICAL_DIR / "daily"
CANONICAL_LOG = CANONICAL_DIR / "canonical_action_event_log.csv"
RECONCILIATION_LATEST = CANONICAL_DIR / "notification_reconciliation_latest.json"
GATE_LATEST = CANONICAL_DIR / "acceptance_gate_latest.json"


def _run_step(title: str, cmd: list[str]) -> dict[str, Any]:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False)
    ended = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "title": title,
        "command": " ".join(cmd),
        "return_code": int(proc.returncode),
        "ok": bool(proc.returncode == 0),
        "started_utc": started,
        "ended_utc": ended,
        "stdout": str(proc.stdout or "")[-12000:],
        "stderr": str(proc.stderr or "")[-8000:],
    }


def _safe_copy(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run daily production chain and archive canonical artifacts")
    parser.add_argument("--python", type=str, default=sys.executable)
    parser.add_argument("--trade-days", type=int, default=5)
    parser.add_argument("--recap-mode", type=str, default="bedtime", choices=["full", "bedtime", "morning", "opening"])
    parser.add_argument("--recap-channel", type=str, default="discord", choices=["discord", "line", "both"])
    parser.add_argument("--date-tag", type=str, default="", help="Override artifact date tag (YYYY-MM-DD)")
    parser.add_argument("--strict-gate", action="store_true", help="Return non-zero when gate result is not ok")
    args = parser.parse_args()

    py = str(args.python or sys.executable)
    now_utc = datetime.now(timezone.utc)
    date_tag = str(args.date_tag or "").strip() or now_utc.strftime("%Y-%m-%d")

    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, Any]] = []
    steps.append(
        _run_step(
            "run intraday engine non-dry-run",
            [py, "scripts/run_intraday_execution_engine.py"],
        )
    )
    steps.append(
        _run_step(
            "run swing engine non-dry-run",
            [py, "scripts/run_swing_core_engine.py"],
        )
    )
    steps.append(
        _run_step(
            "run recap non-dry-run",
            [
                py,
                "scripts/push_alerts_from_ai_decision.py",
                "--auto-latest",
                "--mode",
                str(args.recap_mode),
                "--channel",
                str(args.recap_channel),
            ],
        )
    )
    steps.append(
        _run_step(
            "generate reconciliation report",
            [
                py,
                "scripts/generate_notification_reconciliation_report.py",
                "--trade-days",
                str(max(1, int(args.trade_days))),
                "--fail-on-broken",
            ],
        )
    )

    gate_step = _run_step(
        "run canonical acceptance gate",
        [py, "scripts/run_canonical_acceptance_gate.py"],
    )
    steps.append(gate_step)

    daily_canonical_snapshot = DAILY_DIR / f"canonical_action_event_log_{date_tag}.csv"
    daily_reconciliation = DAILY_DIR / f"notification_reconciliation_{date_tag}.json"
    daily_gate = DAILY_DIR / f"acceptance_gate_{date_tag}.json"
    daily_summary = DAILY_DIR / f"production_chain_summary_{date_tag}.json"
    latest_summary = CANONICAL_DIR / "production_chain_summary_latest.json"

    canonical_saved = _safe_copy(CANONICAL_LOG, daily_canonical_snapshot)
    reconciliation_saved = _safe_copy(RECONCILIATION_LATEST, daily_reconciliation)

    daily_gate.write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "date_tag": date_tag,
                "gate_step": gate_step,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _safe_copy(daily_gate, GATE_LATEST)

    gate_ok = bool(gate_step.get("ok"))
    runtime_ok = all(bool(step.get("ok")) for step in steps[:4])
    status = "acceptance_ready" if gate_ok else "acceptance_pending"

    summary = {
        "ok": bool(runtime_ok and (gate_ok or not bool(args.strict_gate))),
        "status": status,
        "strict_gate": bool(args.strict_gate),
        "date_tag": date_tag,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "artifacts": {
            "canonical_log": str(CANONICAL_LOG),
            "canonical_snapshot": str(daily_canonical_snapshot),
            "canonical_snapshot_saved": canonical_saved,
            "reconciliation_latest": str(RECONCILIATION_LATEST),
            "reconciliation_daily": str(daily_reconciliation),
            "reconciliation_saved": reconciliation_saved,
            "gate_latest": str(GATE_LATEST),
            "gate_daily": str(daily_gate),
        },
        "steps": steps,
    }

    daily_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _safe_copy(daily_summary, latest_summary)

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not runtime_ok:
        return 2
    if bool(args.strict_gate) and not gate_ok:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
