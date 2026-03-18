from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
CANONICAL_DIR = BACKTEST_DIR / "canonical"
CANONICAL_ACTION_EVENT_LOG_FILE = CANONICAL_DIR / "canonical_action_event_log.csv"


CANONICAL_ACTION_EVENT_COLUMNS = [
    "event_ts",
    "trade_date",
    "engine",
    "ticker",
    "action_type",
    "strategy_tag",
    "reason_code",
    "reason_text",
    "price_ref",
    "size_ref",
    "risk_unit",
    "priority",
    "dispatch_mode",
    "dispatch_status",
    "source_event_id",
    "source_log_id",
    "position_state_before",
    "position_state_after",
    "invalidation_rule",
    "created_at",
    "dispatch_detail",
]


_ACTION_TYPE_MAP = {
    "entry": "entry",
    "add": "add",
    "reduce": "reduce",
    "take_profit": "take_profit",
    "stop_loss": "stop_loss",
    "exit": "exit",
    "swing_entry": "entry",
    "swing_add": "add",
    "swing_reduce": "reduce",
    "swing_exit": "exit",
}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_action_type(raw_action: object) -> str:
    action = str(raw_action or "").strip().lower()
    return _ACTION_TYPE_MAP.get(action, action)


def build_source_event_id(engine: str, ticker: str, action_type: str, event_ts: str, source_log_id: str = "") -> str:
    raw = "|".join([
        str(engine or "").strip().lower(),
        str(ticker or "").strip().upper(),
        str(action_type or "").strip().lower(),
        str(event_ts or "").strip(),
        str(source_log_id or "").strip(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def ensure_canonical_schema(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in CANONICAL_ACTION_EVENT_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    return out[CANONICAL_ACTION_EVENT_COLUMNS]


def append_canonical_action_events(rows: List[dict]) -> None:
    if not rows:
        return
    CANONICAL_DIR.mkdir(parents=True, exist_ok=True)
    new_df = ensure_canonical_schema(pd.DataFrame(rows))
    exists = CANONICAL_ACTION_EVENT_LOG_FILE.exists()
    new_df.to_csv(
        CANONICAL_ACTION_EVENT_LOG_FILE,
        mode="a",
        header=not exists,
        index=False,
        encoding="utf-8-sig",
    )


def load_canonical_action_events(limit: int | None = None) -> pd.DataFrame:
    if not CANONICAL_ACTION_EVENT_LOG_FILE.exists():
        return pd.DataFrame(columns=CANONICAL_ACTION_EVENT_COLUMNS)
    try:
        out = pd.read_csv(CANONICAL_ACTION_EVENT_LOG_FILE, encoding="utf-8-sig")
    except UnicodeDecodeError:
        out = pd.read_csv(CANONICAL_ACTION_EVENT_LOG_FILE)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=CANONICAL_ACTION_EVENT_COLUMNS)

    out = ensure_canonical_schema(out)
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["engine"] = out["engine"].astype(str).str.strip().str.lower()
    out["action_type"] = out["action_type"].apply(normalize_action_type)
    out["event_ts"] = out["event_ts"].astype(str).str.strip()
    out["trade_date"] = out["trade_date"].astype(str).str.strip()
    out["event_ts_parsed"] = pd.to_datetime(out["event_ts"], errors="coerce", utc=True)
    out = out.sort_values(["event_ts_parsed", "created_at", "ticker"], ascending=[True, True, True], na_position="last")
    if limit is not None and int(limit) > 0:
        out = out.tail(int(limit)).copy()
    return out.reset_index(drop=True)


def update_canonical_dispatch_outcomes(outcomes: Dict[str, Dict[str, str]]) -> int:
    if not outcomes or not CANONICAL_ACTION_EVENT_LOG_FILE.exists():
        return 0
    try:
        out = pd.read_csv(CANONICAL_ACTION_EVENT_LOG_FILE, encoding="utf-8-sig")
    except UnicodeDecodeError:
        out = pd.read_csv(CANONICAL_ACTION_EVENT_LOG_FILE)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return 0

    out = ensure_canonical_schema(out)
    out["dispatch_status"] = out["dispatch_status"].astype(str)
    out["dispatch_detail"] = out["dispatch_detail"].astype(str)
    updated_count = 0
    out["source_event_id"] = out["source_event_id"].astype(str)
    for source_event_id, status_payload in outcomes.items():
        key = str(source_event_id or "").strip()
        if not key:
            continue
        mask = out["source_event_id"] == key
        if not bool(mask.any()):
            continue
        status = str((status_payload or {}).get("dispatch_status", "")).strip()
        detail = str((status_payload or {}).get("dispatch_detail", "")).strip()
        if status:
            out.loc[mask, "dispatch_status"] = status
        out.loc[mask, "dispatch_detail"] = detail
        updated_count += int(mask.sum())

    if updated_count > 0:
        out.to_csv(CANONICAL_ACTION_EVENT_LOG_FILE, index=False, encoding="utf-8-sig")
    return updated_count


def dispatch_outcome_payload(source_event_ids: Iterable[str], dispatch_status: str, dispatch_detail: str = "") -> Dict[str, Dict[str, str]]:
    payload: Dict[str, Dict[str, str]] = {}
    for event_id in source_event_ids:
        key = str(event_id or "").strip()
        if not key:
            continue
        payload[key] = {
            "dispatch_status": str(dispatch_status or "").strip(),
            "dispatch_detail": str(dispatch_detail or "").strip(),
        }
    return payload


def default_event_ts_and_trade_date(raw_event_ts: object) -> tuple[str, str]:
    parsed = pd.to_datetime(raw_event_ts, errors="coerce", utc=True)
    if pd.isna(parsed):
        now = _now_utc_iso()
        return now, now[:10]
    event_ts = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    trade_date = parsed.strftime("%Y-%m-%d")
    return event_ts, trade_date


def now_created_at_utc() -> str:
    return _now_utc_iso()
