from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _run_step(title: str, cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False)
    output = (proc.stdout or "").strip()
    error = (proc.stderr or "").strip()
    return {
        "title": title,
        "command": " ".join(cmd),
        "ok": proc.returncode == 0,
        "return_code": proc.returncode,
        "stdout": output[-6000:],
        "stderr": error[-4000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run canonical acceptance gate (fail-fast)")
    parser.add_argument("--python", type=str, default=sys.executable)
    args = parser.parse_args()

    py = str(args.python or sys.executable)
    steps = [
        _run_step(
            "verify turso execution alignment",
            [py, "scripts/verify_turso_execution_alignment.py"],
        ),
        _run_step(
            "verify canonical chain last 5 trading days",
            [py, "scripts/verify_canonical_chain_last5.py"],
        ),
        _run_step(
            "verify no parallel swing dispatch paths",
            [py, "scripts/verify_no_parallel_dispatch_paths.py"],
        ),
        _run_step(
            "generate notification reconciliation report",
            [
                py,
                "scripts/generate_notification_reconciliation_report.py",
                "--trade-days",
                "5",
                "--fail-on-broken",
            ],
        ),
    ]

    ok = all(step["ok"] for step in steps)
    report = {
        "ok": ok,
        "steps": steps,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
