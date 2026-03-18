from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SCAN_PATTERNS = {
    "swing_dispatch_log.csv": ["**/ai_trading/*.py", "**/scripts/*.py", "**/.github/workflows/*.yml"],
    "_append_dispatch_log": ["**/ai_trading/*.py", "**/scripts/*.py"],
    "pending_entry_add": ["**/ai_trading/*.py", "**/scripts/*.py"],
    "resend_pending_entry_add": ["**/ai_trading/*.py", "**/scripts/*.py"],
}

ALLOWLIST = {
    str((PROJECT_ROOT / "scripts" / "verify_no_parallel_dispatch_paths.py").resolve()).lower(),
}


def _collect_files(glob_pattern: str) -> list[Path]:
    return [p for p in PROJECT_ROOT.glob(glob_pattern) if p.is_file()]


def _find_hits() -> list[dict]:
    hits: list[dict] = []
    for needle, globs in SCAN_PATTERNS.items():
        for glob_pattern in globs:
            for file_path in _collect_files(glob_pattern):
                resolved = str(file_path.resolve()).lower()
                if resolved in ALLOWLIST:
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    text = file_path.read_text(encoding="utf-8-sig")
                except OSError:
                    continue
                if needle not in text:
                    continue
                lines = text.splitlines()
                for idx, line in enumerate(lines, start=1):
                    if needle in line:
                        hits.append(
                            {
                                "needle": needle,
                                "file": str(file_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                                "line": idx,
                                "text": line.strip(),
                            }
                        )
    return hits


def main() -> int:
    hits = _find_hits()
    report = {
        "ok": len(hits) == 0,
        "scan_root": str(PROJECT_ROOT),
        "patterns": list(SCAN_PATTERNS.keys()),
        "hit_count": len(hits),
        "hits": hits[:100],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
