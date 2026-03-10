from __future__ import annotations

import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
CLOUD_STATE_DIR = PROJECT_ROOT / "cloud_state"
CLOUD_AI_DECISION_LATEST = CLOUD_STATE_DIR / "ai_decision_latest.csv"
CLOUD_POSITIONS_LATEST = CLOUD_STATE_DIR / "positions_latest.csv"
CLOUD_EXECUTION_LATEST = CLOUD_STATE_DIR / "execution_trade_latest.csv"


def preferred_runtime_path(cloud_path: Path, local_path: Path) -> Path:
    return cloud_path if cloud_path.exists() else local_path


def _sync_file(source_path: Path, target_path: Path) -> Path | None:
    source = Path(source_path)
    if not source.exists():
        return None
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def sync_ai_decision_latest(source_path: Path) -> Path | None:
    return _sync_file(source_path, CLOUD_AI_DECISION_LATEST)


def sync_positions_latest(source_path: Path) -> Path | None:
    return _sync_file(source_path, CLOUD_POSITIONS_LATEST)


def sync_execution_latest(source_path: Path) -> Path | None:
    return _sync_file(source_path, CLOUD_EXECUTION_LATEST)