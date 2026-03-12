"""
Push decision alerts from ai_decision CSV to Discord (and optional LINE Messaging API),
then persist alert logs for later review.

Examples:
  python scripts/push_alerts_from_ai_decision.py --auto-latest --dry-run
  python scripts/push_alerts_from_ai_decision.py --auto-latest --top-n 5
  python scripts/push_alerts_from_ai_decision.py --csv-file repo_outputs/backtest/inbox/ai_decision_2026-03-05.csv

Env vars:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  LINE_CHANNEL_ACCESS_TOKEN=...
  LINE_TO_USER_ID=...
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import get_logger
from prompt_safety import sanitize_prompt_payload

from ai_trading.position_state import load_positions
from ai_trading.strategy_context import (
    HORIZON_SWING_CORE,
    classify_watch_horizon,
    default_strategy_for_horizon,
)
from config import (
    DISCORD_WEBHOOK_URL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    INTRADAY_ADD_SIZE_FRACTION,
    INTRADAY_ACTIVE_TIMEZONE,
    INTRADAY_ENTRY_MAX_RANK,
    INTRADAY_ENTRY_MIN_API_SCORE,
    INTRADAY_ENTRY_MIN_CONFIDENCE,
    INTRADAY_ENTRY_SIZE_FRACTION,
    INTRADAY_ENTRY_WINDOW_MINUTES,
    INTRADAY_MAX_NEW_ENTRIES_PER_DAY,
    INTRADAY_MAX_TOTAL_POSITIONS,
    INTRADAY_REDUCE_SIZE_FRACTION,
    RECAP_BEDTIME_UTC_START_HOUR,
    RECAP_CONFLICT_NEWS_MAX_TICKERS,
    RECAP_EXECUTION_LOOKBACK_LIMIT,
    RECAP_GEMINI_ENABLED,
    RECAP_GEMINI_TIMEOUT_SEC,
    RECAP_INTRADAY_PLAN_MAX_TICKERS,
    RECAP_MARKET_OPEN_TIME,
    RECAP_MARKET_TIMEZONE,
    RECAP_MORNING_FULL_ENGINE_ENABLED,
    RECAP_MORNING_FULL_ENGINE_MAX_TICKERS,
    RECAP_MORNING_GEMINI_REWRITE_ONLY,
    RECAP_MORNING_LOOKBACK_HOURS,
    RECAP_OPENING_LOOKBACK_MINUTES,
    RECAP_OPENING_RUN_AFTER_MINUTES,
    RECAP_OPENING_RUN_GRACE_MINUTES,
    RECAP_SWING_PLAN_MAX_TICKERS,
    RECAP_TAVILY_ENABLED,
    RECAP_TAVILY_MAX_RESULTS,
    RECAP_TRACKED_NEWS_ENABLED,
    RECAP_TRACKED_NEWS_MAX_TICKERS,
    SIGNAL_MAX_AGE_MINUTES,
    SIGNAL_REQUIRE_SAME_DAY,
    SIGNAL_STORE_PATH,
    TAVILY_API_KEY,
)
from signal_store import get_latest_signals
from turso_state import STATE_KEY_AI_DECISION_LATEST, STATE_KEY_INTRADAY_SNAPSHOT, load_recent_execution_log, load_runtime_df, load_runtime_df_with_fallback, sync_runtime_df

logger = get_logger(__name__)

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
INBOX_DIR = BACKTEST_DIR / "inbox"
AI_READY_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready" / "latest"
DAILY_REFRESH_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh" / "latest"
ALERT_DIR = BACKTEST_DIR / "alerts"
ALERT_LOG_CSV = ALERT_DIR / "alert_log.csv"
ALERT_MESSAGE_TXT = ALERT_DIR / "latest_alert_message.txt"
ALERT_MARKER_DIR = ALERT_DIR / "markers"
AI_DECISION_LOG_CSV = BACKTEST_DIR / "ai_decision_log.csv"
EXECUTION_LOG_CSV = BACKTEST_DIR / "execution_trade_log.csv"
BEDTIME_PLAN_STATE_KEY = "recap_bedtime_plan_latest"
BEDTIME_PLAN_FILE = ALERT_DIR / "bedtime_plan_latest.json"
MORNING_PLAN_STATE_KEY = "recap_morning_plan_latest"
MORNING_PLAN_FILE = ALERT_DIR / "morning_plan_latest.json"

REQUIRED_COLS = [
    "decision_date",
    "rank",
    "ticker",
    "short_score_final",
    "confidence",
    "api_final_score",
    "risk_level",
    "tech_status",
    "decision_tag",
    "reason_summary",
    "catalyst_summary",
    "catalyst_type",
    "catalyst_sentiment",
    "source_ref",
]

EXECUTION_COLS = [
    "recorded_at",
    "execution_date",
    "execution_time",
    "ticker",
    "action",
    "position_effect",
    "rank",
    "decision_tag",
    "close",
    "vwap",
    "sqzmom_color",
    "sqzmom_value",
    "signal_source",
    "timeframe",
    "reason_summary",
    "signal_ts",
]

ACTION_LABELS = {
    "entry": "適合買",
    "add": "可加碼",
    "take_profit": "先減碼",
    "stop_loss": "先降風險",
}

ACTION_DIRECTION = {
    "entry": "buy",
    "add": "buy",
    "take_profit": "sell",
    "stop_loss": "sell",
}


def _find_latest_decision_csv() -> Optional[Path]:
    found: List[tuple[float, Path]] = []
    static_candidates = [BACKTEST_DIR / "ai_decision_latest.csv"]
    for file in static_candidates:
        if not file.exists():
            continue
        try:
            found.append((file.stat().st_mtime, file))
        except OSError:
            continue
    for folder in [INBOX_DIR, AI_READY_LATEST_DIR, DAILY_REFRESH_LATEST_DIR]:
        if not folder.exists():
            continue
        for file in folder.glob("ai_decision_*.csv"):
            try:
                found.append((file.stat().st_mtime, file))
            except OSError:
                continue
    if not found:
        return None
    found.sort(key=lambda x: x[0], reverse=True)
    return found[0][1]


def _load_latest_decision_df() -> tuple[pd.DataFrame, str | None]:
    df, source = load_runtime_df_with_fallback(
        STATE_KEY_AI_DECISION_LATEST,
        [BACKTEST_DIR / "ai_decision_latest.csv"],
    )
    if source is not None:
        return df, source

    latest_csv = _find_latest_decision_csv()
    if latest_csv is None:
        return pd.DataFrame(), None
    return _load_decision_df(latest_csv), str(latest_csv)


def _load_decision_df(csv_path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path)

    for col in REQUIRED_COLS:
        if col not in df.columns:
            df[col] = ""

    out = df.copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["short_score_final"] = pd.to_numeric(out["short_score_final"], errors="coerce")
    out["confidence"] = pd.to_numeric(out["confidence"], errors="coerce")
    out["api_final_score"] = pd.to_numeric(out["api_final_score"], errors="coerce")
    out = out[out["ticker"] != ""].copy()
    out = out.dropna(subset=["rank"]).copy()
    out["rank"] = out["rank"].astype(int)
    out = out.sort_values(["rank", "ticker"], ascending=[True, True])
    return out


def _load_tv_map() -> Dict[str, object]:
    try:
        return get_latest_signals(
            SIGNAL_STORE_PATH,
            asof=datetime.now(timezone.utc),
            max_age_minutes=SIGNAL_MAX_AGE_MINUTES,
            require_same_day=SIGNAL_REQUIRE_SAME_DAY,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return {}


def _fmt_tv_line(ticker: str, tv_map: Dict[str, object]) -> str:
    event = tv_map.get(ticker)
    if not event:
        return "TV:NA"

    vwap = "NA" if event.vwap is None else f"{float(event.vwap):.2f}"
    sqz = "NA" if event.sqzmom_color in (None, "") else str(event.sqzmom_color)
    sqzv = "NA" if event.sqzmom_value is None else f"{float(event.sqzmom_value):.2f}"
    return f"TV:vwap={vwap},sqz={sqz}/{sqzv}"


def _clip_text(value: object, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _safe_float(value: object, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return default
    return float(parsed)


def _safe_int(value: object, default: int = 0) -> int:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return int(default)
    return int(parsed)


def _sanitize_webhook_url(value: str) -> str:
    cleaned = str(value or "").replace("\ufeff", "").strip().strip('"').strip("'").strip()
    cleaned = cleaned.strip("[]")
    cleaned = cleaned.strip("<>")
    return cleaned


def _marker_file_for(decision_date: str, mode: str, channel: str) -> Path:
    safe_date = str(decision_date or "unknown").strip().replace("/", "-")
    safe_mode = str(mode or "full").strip().lower()
    safe_channel = str(channel or "unknown").strip().lower()
    return ALERT_MARKER_DIR / f"{safe_date}_{safe_mode}_{safe_channel}.json"


def _already_sent(decision_date: str, mode: str, channel: str, source_csv: object) -> bool:
    if mode not in {"bedtime", "morning", "opening"}:
        return False
    marker_file = _marker_file_for(decision_date, mode, channel)
    if not marker_file.exists():
        return False
    try:
        marker = pd.read_json(marker_file, typ="series")
    except (ValueError, OSError):
        return False
    return str(marker.get("source_csv", "")).strip() == str(source_csv)


def _write_sent_marker(decision_date: str, mode: str, channel: str, source_csv: object) -> None:
    if mode not in {"bedtime", "morning", "opening"}:
        return
    ALERT_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker_file = _marker_file_for(decision_date, mode, channel)
    payload = {
        "decision_date": decision_date,
        "mode": mode,
        "channel": channel,
        "source_csv": str(source_csv),
        "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    marker_file.write_text(pd.Series(payload).to_json(force_ascii=False, indent=2), encoding="utf-8")


def _serialize_lines(values: List[str]) -> str:
    cleaned = [str(item).strip() for item in values if str(item or "").strip()]
    return json.dumps(cleaned, ensure_ascii=False)


def _deserialize_lines(value: object) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item or "").strip()]


def _plan_storage_for(mode: str) -> tuple[str, Path]:
    normalized = str(mode or "").strip().lower()
    if normalized == "bedtime":
        return BEDTIME_PLAN_STATE_KEY, BEDTIME_PLAN_FILE
    return MORNING_PLAN_STATE_KEY, MORNING_PLAN_FILE


def _merge_lines(*groups: List[str], limit: int = 5) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(_clip_text(text, 90))
            if len(out) >= max(1, int(limit)):
                return out
    return out


def _persist_recap_plan(mode: str, decision_date: str, recap_context: dict, message: str, source_id: object) -> None:
    state_key, plan_file = _plan_storage_for(mode)
    normalized = str(mode or "").strip().lower()
    ai_summary = recap_context.get("ai_summary", {}) if isinstance(recap_context, dict) else {}
    opening_plan = ai_summary.get("opening_plan", []) if isinstance(ai_summary.get("opening_plan", []), list) else []
    focus = ai_summary.get("focus", []) if isinstance(ai_summary.get("focus", []), list) else []
    risk_flags = ai_summary.get("risk_flags", []) if isinstance(ai_summary.get("risk_flags", []), list) else []
    payload = {
        "decision_date": str(decision_date or "").strip(),
        "mode": normalized,
        "opening_plan_json": _serialize_lines(opening_plan),
        "focus_json": _serialize_lines(focus),
        "risk_flags_json": _serialize_lines(risk_flags),
        "message": str(message or "").strip(),
        "source_csv": str(source_id or "").strip(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_runtime_df(state_key, pd.DataFrame([payload]), source_name=f"recap_{normalized}_plan")


def _persist_bedtime_plan(decision_date: str, recap_context: dict, message: str, source_id: object) -> None:
    _persist_recap_plan("bedtime", decision_date, recap_context, message, source_id)


def _persist_morning_plan(decision_date: str, recap_context: dict, message: str, source_id: object) -> None:
    _persist_recap_plan("morning", decision_date, recap_context, message, source_id)


def _load_persisted_recap_plan(mode: str, decision_date: str) -> dict:
    target_date = str(decision_date or "").strip()
    state_key, plan_file = _plan_storage_for(mode)
    normalized = str(mode or "").strip().lower()
    candidates: List[dict] = []
    df, _ = load_runtime_df(state_key)
    if len(df) > 0:
        candidates.append(df.iloc[0].to_dict())
    if plan_file.exists():
        try:
            candidates.append(json.loads(plan_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    for item in candidates:
        if not isinstance(item, dict):
            continue
        if str(item.get("mode", "")).strip().lower() != normalized:
            continue
        if str(item.get("decision_date", "")).strip() != target_date:
            continue
        return {
            "opening_plan": _deserialize_lines(item.get("opening_plan_json")),
            "focus": _deserialize_lines(item.get("focus_json")),
            "risk_flags": _deserialize_lines(item.get("risk_flags_json")),
            "message": str(item.get("message", "")).strip(),
            "source_csv": str(item.get("source_csv", "")).strip(),
        }
    return {}


def _load_persisted_bedtime_plan(decision_date: str) -> dict:
    return _load_persisted_recap_plan("bedtime", decision_date)


def _load_persisted_morning_plan(decision_date: str) -> dict:
    return _load_persisted_recap_plan("morning", decision_date)


def _load_previous_top1(current_date: str) -> str:
    if not AI_DECISION_LOG_CSV.exists():
        return ""
    try:
        history = pd.read_csv(AI_DECISION_LOG_CSV, encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return ""

    if "decision_date" not in history.columns or "ticker" not in history.columns or "rank" not in history.columns:
        return ""

    history = history.copy()
    history["decision_date"] = history["decision_date"].astype(str).str.strip()
    history["ticker"] = history["ticker"].astype(str).str.strip().str.upper()
    history["rank"] = pd.to_numeric(history["rank"], errors="coerce")
    history = history[(history["decision_date"] != current_date) & (history["rank"] == 1)].copy()
    if len(history) == 0:
        return ""
    history = history.sort_values(["decision_date"], ascending=[False])
    return str(history.iloc[0].get("ticker", "")).strip().upper()


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_hhmm(value: str, fallback: str) -> tuple[int, int]:
    raw = str(value or fallback).strip()
    try:
        hour_str, minute_str = raw.split(":", 1)
        return int(hour_str), int(minute_str)
    except (ValueError, TypeError):
        fallback_hour, fallback_minute = fallback.split(":", 1)
        return int(fallback_hour), int(fallback_minute)


def _get_zoneinfo(name: str, fallback: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or fallback).strip() or fallback)
    except ZoneInfoNotFoundError:
        return ZoneInfo(fallback)


def _market_open_utc_for(now_dt: datetime) -> datetime:
    market_tz = _get_zoneinfo(RECAP_MARKET_TIMEZONE, "America/New_York")
    now_market = now_dt.replace(tzinfo=timezone.utc).astimezone(market_tz)
    open_hour, open_minute = _parse_hhmm(RECAP_MARKET_OPEN_TIME, "09:30")
    market_open = now_market.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
    return market_open.astimezone(timezone.utc).replace(tzinfo=None)


def _opening_window_bounds(now_dt: datetime) -> tuple[datetime, datetime]:
    start_dt = _market_open_utc_for(now_dt)
    end_dt = start_dt + timedelta(minutes=max(1, int(RECAP_OPENING_LOOKBACK_MINUTES)))
    if now_dt < start_dt:
        return start_dt, start_dt
    return start_dt, min(now_dt, end_dt)


def _opening_dispatch_bounds(now_dt: datetime) -> tuple[datetime, datetime]:
    start_dt = _market_open_utc_for(now_dt) + timedelta(minutes=max(0, int(RECAP_OPENING_RUN_AFTER_MINUTES)))
    end_dt = _market_open_utc_for(now_dt) + timedelta(minutes=max(int(RECAP_OPENING_RUN_AFTER_MINUTES), int(RECAP_OPENING_RUN_GRACE_MINUTES)))
    return start_dt, end_dt


def _is_in_opening_dispatch_window(now_dt: datetime) -> bool:
    window_start, window_end = _opening_dispatch_bounds(now_dt)
    return window_start <= now_dt <= window_end


def _format_utc_to_active_local(value: datetime) -> str:
    active_tz = _get_zoneinfo(INTRADAY_ACTIVE_TIMEZONE, "Asia/Taipei")
    return value.replace(tzinfo=timezone.utc).astimezone(active_tz).strftime("%m-%d %H:%M")


def _normalize_ts(value: object) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    if getattr(parsed, "tzinfo", None) is not None:
        return parsed.tz_convert(None)
    return parsed


def _load_execution_df(limit: int) -> pd.DataFrame:
    out = load_recent_execution_log(limit=max(1, int(limit)))
    if len(out) == 0 and EXECUTION_LOG_CSV.exists():
        try:
            out = pd.read_csv(EXECUTION_LOG_CSV, encoding="utf-8-sig")
        except UnicodeDecodeError:
            out = pd.read_csv(EXECUTION_LOG_CSV)
        except (OSError, pd.errors.EmptyDataError):
            out = pd.DataFrame()
    if len(out) == 0:
        return pd.DataFrame(columns=EXECUTION_COLS + ["recorded_at_ts"])

    normalized = out.copy()
    for col in EXECUTION_COLS:
        if col not in normalized.columns:
            normalized[col] = ""
    normalized["ticker"] = normalized["ticker"].astype(str).str.strip().str.upper()
    normalized["action"] = normalized["action"].astype(str).str.strip().str.lower()
    normalized["decision_tag"] = normalized["decision_tag"].astype(str).str.strip().str.lower()
    normalized["rank"] = pd.to_numeric(normalized["rank"], errors="coerce").fillna(9999).astype(int)
    normalized["recorded_at_ts"] = normalized.apply(
        lambda row: _normalize_ts(
            row.get("recorded_at")
            or f"{str(row.get('execution_date', '')).strip()} {str(row.get('execution_time', '')).strip()}".strip()
            or row.get("signal_ts")
        ),
        axis=1,
    )
    normalized = normalized[normalized["ticker"] != ""].copy()
    normalized = normalized.sort_values(["recorded_at_ts", "rank", "ticker"], ascending=[True, True, True], na_position="last")
    return normalized.reset_index(drop=True)


def _window_start(mode: str, end_dt: datetime) -> datetime:
    if mode == "bedtime":
        start_dt = end_dt.replace(hour=int(RECAP_BEDTIME_UTC_START_HOUR), minute=0, second=0, microsecond=0)
        if start_dt > end_dt:
            start_dt -= timedelta(days=1)
        return start_dt
    return end_dt - timedelta(hours=max(float(RECAP_MORNING_LOOKBACK_HOURS), 1.0))


def _filter_execution_window(df: pd.DataFrame, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    if len(df) == 0:
        return df.copy()
    out = df.copy()
    mask = out["recorded_at_ts"].notna()
    mask &= out["recorded_at_ts"] >= pd.Timestamp(start_dt)
    mask &= out["recorded_at_ts"] <= pd.Timestamp(end_dt)
    return out[mask].copy().reset_index(drop=True)


def _action_priority(action: str) -> int:
    return {"stop_loss": 0, "take_profit": 1, "add": 2, "entry": 3}.get(str(action or ""), 9)


def _derive_status_and_guidance(latest_action: str, has_conflict: bool, reversal_count: int, has_position: bool) -> tuple[str, str]:
    if reversal_count >= 2:
        if has_position:
            return "高噪音衝突", "同檔來回翻向，開盤先看風險，不急著加碼。"
        return "高噪音衝突", "同檔來回翻向，先等方向重新乾淨再處理。"

    if latest_action == "stop_loss":
        if has_position:
            return "風險翻空", "你目前有持倉，開盤先驗證是否需要降風險。"
        return "風險翻空", "最新訊號已轉弱，先不要追價。"

    if latest_action == "take_profit":
        if has_position:
            return "獲利保守", "若部位還在，先看是否延續轉弱。"
        return "獲利保守", "這檔偏向先收斂，不適合急追。"

    if latest_action == "add":
        if has_conflict:
            return "先強後亂", "雖然出現加碼訊號，但中間有反轉，先等下一輪確認。"
        if has_position:
            return "續強加碼", "既有部位仍偏強，開盤可優先驗證續強性。"
        return "續強觀察", "列入優先觀察，但先確認不是短暫噴出。"

    if latest_action == "entry":
        if has_conflict:
            return "轉強待證實", "有新進場訊號但不夠乾淨，先等開盤確認。"
        return "初次轉強", "這檔有重新轉強跡象，可列為先開圖觀察。"

    return "待確認", "目前資料不足，先以原始 ai_decision 為主。"


def _summarize_execution_window(
    execution_df: pd.DataFrame,
    positions_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    include_all_tickers: bool = False,
) -> List[dict]:
    if len(execution_df) == 0:
        return []

    position_map = {
        str(row.get("ticker", "")).strip().upper(): row
        for _, row in positions_df.iterrows()
        if str(row.get("ticker", "")).strip()
    }
    decision_map = {
        str(row.get("ticker", "")).strip().upper(): row
        for _, row in decision_df.iterrows()
        if str(row.get("ticker", "")).strip()
    }
    tracked_tickers = set(position_map.keys()) | set(decision_map.keys())

    out: List[dict] = []
    grouped = execution_df.sort_values(["recorded_at_ts", "rank", "ticker"], ascending=[True, True, True]).groupby("ticker", sort=False)
    for ticker, group in grouped:
        if not ticker:
            continue
        if not include_all_tickers and tracked_tickers and ticker not in tracked_tickers:
            continue
        ordered = group.copy().reset_index(drop=True)
        actions = [str(value).strip().lower() for value in ordered["action"].tolist() if str(value).strip()]
        if not actions:
            continue
        directions = [ACTION_DIRECTION.get(action, "") for action in actions if ACTION_DIRECTION.get(action, "")]
        reversal_count = sum(1 for idx in range(1, len(directions)) if directions[idx] != directions[idx - 1])
        action_set = set(actions)
        has_conflict = reversal_count >= 1 or (bool({"entry", "add"} & action_set) and bool({"take_profit", "stop_loss"} & action_set))
        latest = ordered.iloc[-1]
        latest_action = str(latest.get("action", "")).strip().lower()
        latest_ts = latest.get("recorded_at_ts")
        latest_ts_text = latest_ts.strftime("%m-%d %H:%M") if isinstance(latest_ts, pd.Timestamp) and not pd.isna(latest_ts) else "NA"
        position = position_map.get(ticker)
        has_position = position is not None and _safe_float(position.get("quantity", 0.0), 0.0) > 0
        status_label, guidance = _derive_status_and_guidance(latest_action, has_conflict, reversal_count, has_position)
        decision_row = decision_map.get(ticker)
        rank_value = _safe_int(latest.get("rank"), 9999)
        if decision_row is not None:
            rank_value = _safe_int(decision_row.get("rank"), rank_value)
        horizon = str(latest.get("horizon_tag", "")).strip() or (
            str((decision_row.get("horizon_tag") if decision_row is not None else "")).strip()
            or classify_watch_horizon(ticker, decision_row.to_dict() if decision_row is not None else {})
        )
        strategy = str(latest.get("strategy_profile", "")).strip() or (
            str((decision_row.get("strategy_profile") if decision_row is not None else "")).strip()
            or default_strategy_for_horizon(horizon)
        )
        sequence_text = " -> ".join(action.upper() for action in actions[:4])
        if len(actions) > 4:
            sequence_text += " -> ..."
        out.append(
            {
                "ticker": ticker,
                "latest_action": latest_action,
                "latest_label": ACTION_LABELS.get(latest_action, latest_action or "NA"),
                "latest_time": latest_ts_text,
                "rank": rank_value,
                "horizon_tag": horizon,
                "strategy_profile": strategy,
                "decision_tag": str((decision_row.get("decision_tag") if decision_row is not None else latest.get("decision_tag")) or "").strip().lower(),
                "has_position": has_position,
                "position_qty": _safe_float(position.get("quantity", 0.0), 0.0) if position is not None else 0.0,
                "avg_cost": _safe_float(position.get("avg_cost", 0.0), 0.0) if position is not None else 0.0,
                "action_count": len(actions),
                "reversal_count": reversal_count,
                "has_conflict": has_conflict,
                "status_label": status_label,
                "guidance": guidance,
                "action_sequence": sequence_text,
                "close": _safe_float(latest.get("close"), 0.0),
                "vwap": _safe_float(latest.get("vwap"), 0.0),
                "sqzmom_color": str(latest.get("sqzmom_color", "")).strip(),
                "reason_summary": _clip_text(latest.get("reason_summary") or "", 120),
                "sort_ts": latest_ts,
            }
        )

    out.sort(
        key=lambda item: (
            -int(bool(item.get("has_position"))),
            -int(bool(item.get("has_conflict"))),
            _action_priority(str(item.get("latest_action", ""))),
            int(item.get("rank", 9999)),
            -(item.get("sort_ts").value if isinstance(item.get("sort_ts"), pd.Timestamp) and not pd.isna(item.get("sort_ts")) else 0),
        )
    )
    return out


def _summary_sort_ts_value(item: dict) -> int:
    ts = item.get("sort_ts")
    if isinstance(ts, pd.Timestamp) and not pd.isna(ts):
        return int(ts.value)
    return 0


def _morning_priority_bucket(item: dict) -> int:
    latest_action = str(item.get("latest_action", "")).strip().lower()
    has_position = bool(item.get("has_position"))
    has_conflict = bool(item.get("has_conflict"))

    if has_position and latest_action in {"stop_loss", "take_profit"}:
        return 0
    if has_position and has_conflict:
        return 1
    if latest_action in {"stop_loss", "take_profit"}:
        return 2
    if has_conflict:
        return 3
    if has_position and latest_action in {"entry", "add"}:
        return 4
    if latest_action in {"entry", "add"}:
        return 5
    return 6


def _rank_morning_full_engine(execution_summaries: List[dict]) -> List[dict]:
    ranked = execution_summaries.copy()
    ranked.sort(
        key=lambda item: (
            _morning_priority_bucket(item),
            int(item.get("rank", 9999)),
            -_summary_sort_ts_value(item),
            str(item.get("ticker", "")),
        )
    )
    return ranked


def _build_morning_rule_ai_summary(execution_summaries: List[dict], prior_bedtime_lines: List[str]) -> dict:
    if not execution_summaries:
        return {}

    focus_items: List[str] = []
    risk_items: List[str] = []
    plan_items: List[str] = []
    seen_plan_items: set[str] = set()

    for item in execution_summaries[:max(1, int(RECAP_MORNING_FULL_ENGINE_MAX_TICKERS))]:
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        status = str(item.get("status_label", "待確認")).strip() or "待確認"
        bucket = _morning_priority_bucket(item)
        if len(focus_items) < 3:
            focus_items.append(f"{ticker}({status})")
        if bucket <= 3 and len(risk_items) < 3:
            risk_items.append(f"{ticker}({status})")

        guidance = _clip_text(item.get("guidance") or "", 72)
        if guidance and guidance not in seen_plan_items and len(plan_items) < 3:
            seen_plan_items.add(guidance)
            plan_items.append(guidance)

    for line in prior_bedtime_lines:
        text = _clip_text(line or "", 72)
        if text and text not in seen_plan_items and len(plan_items) < 3:
            seen_plan_items.add(text)
            plan_items.append(text)

    risk_count = sum(1 for item in execution_summaries if _morning_priority_bucket(item) <= 3)
    if risk_count > 0:
        headline = "隔夜重點已整理，先控風險再看機會"
    else:
        headline = "隔夜重點已整理，盤前先驗證續強"

    summary = f"隔夜 engine 監控 {len(execution_summaries)} 檔，風險優先 {risk_count} 檔。"
    return {
        "headline": _clip_text(headline, 80),
        "summary": _clip_text(summary, 110),
        "focus": focus_items[:3],
        "risk_flags": risk_items[:3],
        "opening_plan": plan_items[:3],
    }


def _extract_json_block(text: str) -> Dict[str, object]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _tavily_search(query: str, api_key: str, max_results: int, timeout_sec: float) -> List[Dict[str, str]]:
    if not api_key:
        return []
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": int(max_results),
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        response = requests.post("https://api.tavily.com/search", json=payload, timeout=timeout_sec)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []
    results = data.get("results", []) if isinstance(data, dict) else []
    out: List[Dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "content": str(item.get("content", "")).strip(),
            }
        )
    return out


def _fetch_conflict_news(execution_summaries: List[dict]) -> List[dict]:
    if not RECAP_TAVILY_ENABLED or not TAVILY_API_KEY:
        return []
    out: List[dict] = []
    for summary in execution_summaries:
        if not bool(summary.get("has_conflict")):
            continue
        ticker = str(summary.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        snippets = _tavily_search(
            query=f"{ticker} stock news catalyst after hours",
            api_key=TAVILY_API_KEY,
            max_results=max(1, int(RECAP_TAVILY_MAX_RESULTS)),
            timeout_sec=max(float(RECAP_GEMINI_TIMEOUT_SEC), 10.0),
        )
        if not snippets:
            continue
        title_bits = [item.get("title", "") for item in snippets if str(item.get("title", "")).strip()]
        if not title_bits:
            continue
        out.append(
            {
                "ticker": ticker,
                "brief": _clip_text(" | ".join(title_bits[:2]), 150),
                "count": len(snippets),
            }
        )
        if len(out) >= max(1, int(RECAP_CONFLICT_NEWS_MAX_TICKERS)):
            break
    return out


def _fetch_tracked_news(tickers: List[str], mode: str) -> List[dict]:
    if not RECAP_TRACKED_NEWS_ENABLED or not RECAP_TAVILY_ENABLED or not TAVILY_API_KEY:
        return []
    query_suffix = "stock news catalyst premarket" if mode in {"morning", "opening"} else "stock news catalyst after hours"
    out: List[dict] = []
    for ticker in tickers[:max(1, int(RECAP_TRACKED_NEWS_MAX_TICKERS))]:
        clean_ticker = str(ticker or "").strip().upper()
        if not clean_ticker:
            continue
        snippets = _tavily_search(
            query=f"{clean_ticker} {query_suffix}",
            api_key=TAVILY_API_KEY,
            max_results=max(1, min(int(RECAP_TAVILY_MAX_RESULTS), 2)),
            timeout_sec=max(float(RECAP_GEMINI_TIMEOUT_SEC), 10.0),
        )
        if not snippets:
            continue
        news_items = [
            {"title": _clip_text(item.get("title") or "", 90), "content": _clip_text(item.get("content") or "", 110)}
            for item in snippets[:2]
            if str(item.get("title", "")).strip()
        ]
        if not news_items:
            continue
        out.append({"ticker": clean_ticker, "news": news_items})
    return out


def _load_engine_snapshot() -> List[dict]:
    df, _ = load_runtime_df(STATE_KEY_INTRADAY_SNAPSHOT)
    if len(df) == 0:
        return []
    out: List[dict] = []
    for _, row in df.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        action = str(row.get("tv_event") or row.get("action") or "").strip().lower()
        out.append({
            "ticker": ticker,
            "action": action,
            "close": _safe_float(row.get("close"), 0.0),
            "vwap": _safe_float(row.get("vwap"), 0.0),
            "sqzmom_color": str(row.get("sqzmom_color", "")).strip(),
            "sqzmom_value": _safe_float(row.get("sqzmom_value"), 0.0),
            "signal_ts": str(row.get("signal_ts", "")).strip(),
        })
    return out


def _top_candidate_payload(df: pd.DataFrame, tv_map: Dict[str, object]) -> List[dict]:
    out: List[dict] = []
    for _, row in df.head(3).iterrows():
        out.append(
            {
                "ticker": str(row.get("ticker", "")).strip().upper(),
                "rank": _safe_int(row.get("rank"), 0),
                "decision_tag": str(row.get("decision_tag", "")).strip().lower(),
                "risk_level": str(row.get("risk_level", "")).strip(),
                "confidence": _safe_float(row.get("confidence"), 0.0),
                "api_final_score": _safe_float(row.get("api_final_score"), 0.0),
                "reason_summary": _clip_text(row.get("reason_summary") or "", 120),
                "catalyst_summary": _clip_text(row.get("catalyst_summary") or row.get("catalyst_type") or "", 120),
                "tv": _fmt_tv_line(str(row.get("ticker", "")), tv_map),
            }
        )
    return out


def _summaries_to_payload(items: List[dict], limit: int = 6) -> List[dict]:
    out: List[dict] = []
    for item in items[:limit]:
        out.append(
            {
                "ticker": item.get("ticker", ""),
                "latest_action": item.get("latest_action", ""),
                "status_label": item.get("status_label", ""),
                "latest_time": item.get("latest_time", ""),
                "has_position": bool(item.get("has_position")),
                "rank": _safe_int(item.get("rank"), 9999),
                "horizon_tag": item.get("horizon_tag", ""),
                "strategy_profile": item.get("strategy_profile", ""),
                "has_conflict": bool(item.get("has_conflict")),
                "action_sequence": item.get("action_sequence", ""),
                "guidance": item.get("guidance", ""),
            }
        )
    return out


def _position_payload(positions_df: pd.DataFrame) -> List[dict]:
    out: List[dict] = []
    for _, row in positions_df.sort_values(["ticker"]).head(6).iterrows():
        out.append(
            {
                "ticker": str(row.get("ticker", "")).strip().upper(),
                "quantity": _safe_float(row.get("quantity", 0.0), 0.0),
                "avg_cost": _safe_float(row.get("avg_cost", 0.0), 0.0),
                "add_count": _safe_int(row.get("add_count"), 0),
                "horizon_tag": str(row.get("horizon_tag", "")).strip(),
                "strategy_profile": str(row.get("strategy_profile", "")).strip(),
            }
        )
    return out


def _decision_allows_auto_entry(row: pd.Series | dict) -> bool:
    row_data = row if isinstance(row, (pd.Series, dict)) else {}
    rank_value = _safe_int(row_data.get("rank"), 9999)
    if rank_value > max(1, int(INTRADAY_ENTRY_MAX_RANK)):
        return False
    if str(row_data.get("decision_tag", "")).strip().lower() != "keep":
        return False
    if str(row_data.get("risk_level", "")).strip() == "高":
        return False
    confidence = _safe_float(row_data.get("confidence"), 0.0)
    api_score = _safe_float(row_data.get("api_final_score"), 0.0)
    if float(INTRADAY_ENTRY_MIN_CONFIDENCE) > 0 and confidence > 0 and confidence < float(INTRADAY_ENTRY_MIN_CONFIDENCE):
        return False
    if float(INTRADAY_ENTRY_MIN_API_SCORE) > 0 and api_score > 0 and api_score < float(INTRADAY_ENTRY_MIN_API_SCORE):
        return False
    return True


def _find_auto_entry_candidate(df: pd.DataFrame, positions_df: pd.DataFrame) -> Optional[dict]:
    open_tickers = {
        str(row.get("ticker", "")).strip().upper()
        for _, row in positions_df.iterrows()
        if _safe_float(row.get("quantity"), 0.0) > 0
    }
    for _, row in df.sort_values(["rank", "ticker"], ascending=[True, True]).iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in open_tickers:
            continue
        if _decision_allows_auto_entry(row):
            return row.to_dict()
    return None


def _strategy_conclusion_for_summary(item: dict) -> str:
    ticker = str(item.get("ticker", "")).strip().upper() or "NA"
    latest_action = str(item.get("latest_action", "")).strip().lower()
    has_position = bool(item.get("has_position"))
    horizon = str(item.get("horizon_tag", "")).strip()
    tag = "[Swing]" if horizon == HORIZON_SWING_CORE else "[Monster]"
    if latest_action == "stop_loss" and has_position:
        return f"{ticker}{tag}: 直接全出，這不是雜訊等級，屬於硬風控。"
    if latest_action == "take_profit" and has_position:
        return f"{ticker}{tag}: 先減碼 {int(round(float(INTRADAY_REDUCE_SIZE_FRACTION) * 100))}% ，主倉保留。"
    if latest_action == "add" and has_position:
        return f"{ticker}{tag}: 續抱為主，若首輪仍強才允許再加碼 {int(round(float(INTRADAY_ADD_SIZE_FRACTION) * 100))}% 。"
    if has_position:
        return f"{ticker}{tag}: 預設續抱，不因第一段分鐘雜訊直接出場。"
    if latest_action in {"entry", "add"}:
        return f"{ticker}{tag}: 僅在開盤前 {int(INTRADAY_ENTRY_WINDOW_MINUTES)} 分鐘內確認續強才開 {int(round(float(INTRADAY_ENTRY_SIZE_FRACTION) * 100))}% 新倉。"
    return f"{ticker}{tag}: 先觀察，不主動追價。"


def _load_swing_watchlist_top1() -> Optional[str]:
    """讀取 swing_signal_latest.csv 快照，回傳最強 swing 候選的格式化文字行；找不到則回傳 None。"""
    snapshot_path = BACKTEST_DIR / "swing" / "swing_signal_latest.csv"
    if not snapshot_path.exists():
        return None
    try:
        df = pd.read_csv(snapshot_path, encoding="utf-8-sig")
    except (OSError, ValueError):
        return None
    if len(df) == 0 or "ticker" not in df.columns:
        return None

    df = df.copy()
    df["_above"] = df.get("above_avwap", False).astype(str).str.lower().isin({"true", "1"})
    df["_sqzmom"] = pd.to_numeric(df.get("sqzmom_hist", 0), errors="coerce").fillna(0.0)
    df["_action"] = df.get("action", "").fillna("").astype(str)
    df["_source"] = df.get("source", "core_list").fillna("core_list").astype(str)

    # 優先: 有 action > above_avwap + 正動能 > saved_watchlist 來源
    candidates = df[df["_above"] & (df["_sqzmom"] > 0)].copy()
    if len(candidates) == 0:
        candidates = df.copy()
    if len(candidates) == 0:
        return None

    candidates["_priority"] = (
        (candidates["_action"] != "").astype(int) * 4
        + (candidates["_source"] == "saved_watchlist").astype(int) * 2
        + candidates["_above"].astype(int)
    )
    top = candidates.sort_values(["_priority", "_sqzmom"], ascending=[False, False]).iloc[0]

    ticker = str(top.get("ticker", "")).strip().upper()
    if not ticker:
        return None

    action = str(top.get("action", "")).strip()
    signal_type = str(top.get("signal_type", "")).strip()
    close_val = pd.to_numeric(top.get("close"), errors="coerce")
    avwap = pd.to_numeric(top.get("dynamic_avwap"), errors="coerce")

    price_part = ""
    if pd.notna(close_val) and pd.notna(avwap) and float(avwap) > 0:
        price_part = f" | 收 {float(close_val):.2f} vs AVWAP {float(avwap):.2f}"

    if action and signal_type:
        size_raw = pd.to_numeric(top.get("size_fraction", 0.25), errors="coerce")
        size_pct = int(round(float(size_raw) * 100)) if pd.notna(size_raw) else 25
        return f"Swing 關注 Top1: {ticker}{price_part} | {signal_type} 候選 ({size_pct}%)"

    reason = str(top.get("reason_summary", "")).strip()
    if reason:
        reason_short = reason[:50]
        return f"Swing 關注 Top1: {ticker}{price_part} | {reason_short}"
    return f"Swing 關注 Top1: {ticker}{price_part} | 監控中"


def _build_morning_strategy_lines(df: pd.DataFrame, recap_context: dict) -> List[str]:
    positions_df = recap_context.get("positions_df") if isinstance(recap_context.get("positions_df"), pd.DataFrame) else pd.DataFrame()
    execution_summaries = recap_context.get("execution_summaries_full", []) if isinstance(recap_context, dict) else []
    lines = ["自動策略結論:"]
    used: set[str] = set()

    intraday_items = [s for s in execution_summaries if str(s.get("horizon_tag", "")).strip() != HORIZON_SWING_CORE]
    swing_items = [s for s in execution_summaries if str(s.get("horizon_tag", "")).strip() == HORIZON_SWING_CORE]

    for item in intraday_items[:max(1, int(RECAP_INTRADAY_PLAN_MAX_TICKERS))]:
        if not bool(item.get("has_position")):
            continue
        text = _strategy_conclusion_for_summary(item)
        if text not in used:
            used.add(text)
            lines.append(f"- {text}")

    for item in swing_items[:max(1, int(RECAP_SWING_PLAN_MAX_TICKERS))]:
        if not bool(item.get("has_position")):
            continue
        text = _strategy_conclusion_for_summary(item)
        if text not in used:
            used.add(text)
            lines.append(f"- {text}")

    if len(positions_df) < max(1, int(INTRADAY_MAX_TOTAL_POSITIONS)):
        candidate = _find_auto_entry_candidate(df, positions_df)
        if candidate is not None and len(lines) < 5:
            ticker = str(candidate.get("ticker", "")).strip().upper()
            lines.append(
                f"- 今日新倉只看 {ticker}；若開盤前 {int(INTRADAY_ENTRY_WINDOW_MINUTES)} 分鐘內確認續強，最多開 {int(round(float(INTRADAY_ENTRY_SIZE_FRACTION) * 100))}% 基礎倉。"
            )

    # 加入 swing 關注清單 Top1（來自 swing_signal_latest.csv）
    if len(lines) < 6:
        swing_top1 = _load_swing_watchlist_top1()
        if swing_top1:
            line_text = f"- {swing_top1}"
            if line_text not in used:
                used.add(line_text)
                lines.append(line_text)

    if len(lines) == 1:
        lines.append("- 今天預設空手，先等 opening recap 驗證。")
    return lines[:7]


def _build_opening_strategy_lines(df: pd.DataFrame, recap_context: dict) -> List[str]:
    if not isinstance(recap_context, dict):
        return []
    if not recap_context.get("opening_has_data"):
        return ["自動策略結論:", "- 開盤資料還不夠，現在先不動作，等下一輪 validation。"]

    positions_df = recap_context.get("positions_df") if isinstance(recap_context.get("positions_df"), pd.DataFrame) else pd.DataFrame()
    opening_summaries = recap_context.get("execution_summaries_full", [])
    lines = ["自動策略結論:"]
    used: set[str] = set()

    for item in opening_summaries:
        if not bool(item.get("has_position")):
            continue
        text = _strategy_conclusion_for_summary(item)
        if text not in used:
            used.add(text)
            lines.append(f"- {text}")
        if len(lines) >= 3:
            break

    if len(positions_df) < max(1, int(INTRADAY_MAX_TOTAL_POSITIONS)) and len(lines) < 4:
        candidate = _find_auto_entry_candidate(df, positions_df)
        if candidate is not None:
            ticker = str(candidate.get("ticker", "")).strip().upper()
            candidate_summary = next(
                (item for item in opening_summaries if str(item.get("ticker", "")).strip().upper() == ticker),
                None,
            )
            if candidate_summary is not None and str(candidate_summary.get("latest_action", "")).strip().lower() in {"entry", "add"}:
                lines.append(
                    f"- {ticker}: 開盤已確認續強，現在可開 {int(round(float(INTRADAY_ENTRY_SIZE_FRACTION) * 100))}% 新倉；今日最多新增 {int(INTRADAY_MAX_NEW_ENTRIES_PER_DAY)} 檔。"
                )

    if len(lines) == 1:
        lines.append("- 現在沒有新的自動執行條件，維持原部位與觀察清單。")
    return lines[:4]


def _bedtime_unique_candidate_plan_line(df: pd.DataFrame, positions_df: pd.DataFrame) -> str:
    if len(positions_df) >= max(1, int(INTRADAY_MAX_TOTAL_POSITIONS)):
        return "明天不開新倉，先處理既有持倉。"
    candidate = _find_auto_entry_candidate(df, positions_df)
    if candidate is None:
        return "明天預設空手，沒有合格的唯一候選新倉。"
    ticker = str(candidate.get("ticker", "")).strip().upper() or "NA"
    return (
        f"明天唯一候選新倉是 {ticker}；只有開盤前 {int(INTRADAY_ENTRY_WINDOW_MINUTES)} 分鐘內確認續強，才開 "
        f"{int(round(float(INTRADAY_ENTRY_SIZE_FRACTION) * 100))}% 基礎倉。"
    )


def _build_bedtime_strategy_lines(df: pd.DataFrame, recap_context: dict) -> List[str]:
    positions_df = recap_context.get("positions_df") if isinstance(recap_context.get("positions_df"), pd.DataFrame) else pd.DataFrame()
    lines = ["自動策略結論:"]
    if len(positions_df) > 0:
        lines.append("- 既有部位明天先交給 opening recap 驗證，不因隔夜雜訊預設全出。")
    lines.append(f"- {_bedtime_unique_candidate_plan_line(df, positions_df)}")
    return lines[:3]


def build_morning_strategy_lines(df: pd.DataFrame, recap_context: dict) -> List[str]:
    return _build_morning_strategy_lines(df, recap_context)


def _expected_bias_from_summary(summary: Optional[dict]) -> str:
    latest_action = str((summary or {}).get("latest_action", "")).strip().lower()
    if latest_action in {"stop_loss", "take_profit"}:
        return "sell"
    if latest_action in {"entry", "add"}:
        return "buy"
    return "watch"


def _validate_opening_ticker(reference_summary: Optional[dict], opening_summary: Optional[dict]) -> tuple[str, str]:
    expected_bias = _expected_bias_from_summary(reference_summary)
    if opening_summary is None:
        if expected_bias == "sell":
            return "待確認", "開盤首輪還沒確認轉弱，先別急著照昨晚劇本直接降風險。"
        if expected_bias == "buy":
            return "待確認", "開盤首輪還沒確認續強，先等下一輪再決定。"
        return "待確認", "開盤首輪暫時沒有明確 execution 訊號。"

    if bool(opening_summary.get("has_conflict")):
        return "開盤雜訊", "開盤訊號偏亂，先等下一輪，不要急著追單。"

    latest_action = str(opening_summary.get("latest_action", "")).strip().lower()
    if expected_bias == "sell":
        if latest_action in {"stop_loss", "take_profit"}:
            return "確認風險", "開盤延續轉弱，昨晚降風險計畫成立。"
        if latest_action in {"entry", "add"}:
            return "反向轉強", "開盤沒有延續轉弱，先別照昨晚劇本直接賣。"
        return "待確認", "昨晚偏空，但開盤首輪還沒給你乾淨結論。"

    if expected_bias == "buy":
        if latest_action in {"entry", "add"}:
            return "確認續強", "開盤延續轉強，昨晚的續強劇本成立。"
        if latest_action in {"stop_loss", "take_profit"}:
            return "開盤失敗", "開盤沒有延續昨晚強勢，先收斂風險。"
        return "待確認", "昨晚偏多，但開盤首輪還沒完全確認。"

    if latest_action in {"entry", "add"}:
        return "開盤轉強", "開盤出現新強勢，可以排進第一輪觀察。"
    if latest_action in {"stop_loss", "take_profit"}:
        return "開盤轉弱", "開盤偏弱，先不要把它當成新機會。"
    return "待確認", "先等下一輪 execution 訊號再決定。"


def _extract_reference_plan(reference_context: dict) -> List[str]:
    ai_summary = reference_context.get("ai_summary", {}) if isinstance(reference_context, dict) else {}
    plan_items = ai_summary.get("opening_plan", []) if isinstance(ai_summary.get("opening_plan", []), list) else []
    cleaned = [_clip_text(item, 90) for item in plan_items if str(item or "").strip()]
    if cleaned:
        return cleaned[:3]

    fallback: List[str] = []
    for item in reference_context.get("execution_summaries_full", [])[:3]:
        guidance = _clip_text(item.get("guidance") or "", 90)
        if guidance:
            fallback.append(guidance)
    return fallback[:3]


def _build_opening_validation(reference_context: dict, opening_summaries: List[dict]) -> List[dict]:
    reference_map = {
        str(item.get("ticker", "")).strip().upper(): item
        for item in reference_context.get("execution_summaries_full", [])
        if str(item.get("ticker", "")).strip()
    }
    opening_map = {
        str(item.get("ticker", "")).strip().upper(): item
        for item in opening_summaries
        if str(item.get("ticker", "")).strip()
    }
    tickers = list(reference_map.keys())
    for ticker in opening_map:
        if ticker not in reference_map:
            tickers.append(ticker)

    rows: List[dict] = []
    for ticker in tickers:
        reference_summary = reference_map.get(ticker)
        opening_summary = opening_map.get(ticker)
        validation_label, next_step = _validate_opening_ticker(reference_summary, opening_summary)
        rank_value = 9999
        if reference_summary is not None:
            rank_value = _safe_int(reference_summary.get("rank"), rank_value)
        if opening_summary is not None:
            rank_value = min(rank_value, _safe_int(opening_summary.get("rank"), rank_value))
        rows.append(
            {
                "ticker": ticker,
                "rank": rank_value,
                "reference_status": str((reference_summary or {}).get("status_label", "")).strip(),
                "reference_action": str((reference_summary or {}).get("latest_action", "")).strip().lower(),
                "opening_status": str((opening_summary or {}).get("status_label", "")).strip(),
                "opening_action": str((opening_summary or {}).get("latest_action", "")).strip().lower(),
                "validation_label": validation_label,
                "next_step": _clip_text(next_step, 90),
            }
        )

    priority_order = {
        "確認風險": 0,
        "開盤失敗": 1,
        "反向轉強": 2,
        "開盤雜訊": 3,
        "待確認": 4,
        "確認續強": 5,
        "開盤轉強": 6,
    }
    rows.sort(key=lambda item: (priority_order.get(str(item.get("validation_label", "")), 9), int(item.get("rank", 9999)), str(item.get("ticker", ""))))
    return rows[:6]


def _generate_recap_ai_summary(mode: str, recap_payload: dict) -> dict:
    morning_rule_summary = recap_payload.get("morning_rule_ai_summary", {}) if mode == "morning" else {}
    rewrite_only = bool(mode == "morning" and RECAP_MORNING_GEMINI_REWRITE_ONLY and morning_rule_summary)

    if not RECAP_GEMINI_ENABLED or not GEMINI_API_KEY:
        return morning_rule_summary if isinstance(morning_rule_summary, dict) and morning_rule_summary else {}

    if mode == "bedtime":
        mode_instruction = "Focus on what changed before sleep and which names change tomorrow's first action."
    elif mode == "morning":
        mode_instruction = (
            "This is the overnight recap. The operator was asleep and needs to know what happened. "
            "First, summarize any material overnight changes: earnings, after-hours moves, news catalysts, or execution events that occurred while asleep. "
            "Second, re-evaluate the bedtime plan in light of overnight facts — what still holds, what is invalidated, what is new. "
            "Third, produce a concrete pre-market plan for the coming session. "
            "Do not claim that any opening validation has already happened. "
            "Use focus for the few names whose overnight change demands attention. "
            "Use risk_flags for positions or watchlist names where overnight news created new risk. "
            "Use opening_plan for executable if-then instructions for the open, not broad commentary. "
            "If prior_bedtime_plan or prior_bedtime_risk_flags exists, carry those items forward unless overnight facts clearly override them."
        )
    else:
        mode_instruction = (
            "Treat this as opening validation, not a fresh idea scan. "
            "Validate whether the prior opening plan was confirmed in the first minutes after the opening bell. "
            "Prioritize sell-risk and failed setups before upside continuation. "
            "State clearly which plans are executable now, which are invalidated, and what to do in the next few minutes. "
            "If the first opening window has no clean validation yet, say that the plan is still unconfirmed and stay conservative. "
            "If prior_bedtime_plan exists, keep those pending validation names visible until the opening data confirms or invalidates them."
        )

    rewrite_instruction = ""
    rule_summary_block = ""
    if rewrite_only:
        rewrite_instruction = (
            "Morning mode is rewrite-only. You must preserve the same ticker coverage and priority in morning_rule_ai_summary. "
            "Do not introduce new tickers, new trade ideas, or any claims outside the provided rule summary. "
            "Rewrite for readability only."
        )
        rule_summary_block = f"Morning rule summary JSON:\n{json.dumps(sanitize_prompt_payload(morning_rule_summary), ensure_ascii=False)}\n\n"

    prompt = (
        "You are editing a trading recap for a human operator. "
        "Use only the provided facts. "
        "Do not invent positions, fills, prices, or news. "
        "Always distinguish open positions from engine suggestions. "
        "Prefer synthesis over inventory. "
        "Do not output holdings lists, raw execution logs, Top 3 recaps, UTC windows, or raw news snippets. "
        "Avoid repeating the same ticker across focus, risk_flags, and opening_plan unless the repetition is necessary to prevent an action mistake. "
        "For morning and opening modes, think in this order: risk first, plan validity second, upside opportunity last. "
        "Use focus for the few charts worth opening first, risk_flags for true risks, and opening_plan for concrete first actions. "
        "Keep focus narrow and action-relevant, not a broad inventory. "
        "Keep opening_plan imperative and executable, not analytical. "
        "If tracked_news exists, use it to judge catalyst quality for each ticker. "
        "If engine_snapshot exists, use VWAP and SQZMOM state to validate or invalidate prior plans. "
        "Rank tickers by actionable strength: risk-first, then continuation strength, then new opportunity. "
        "This is a trading conclusion card, not a market commentary. Do not output analysis process. "
        "If the mode is opening, opening_plan means what to do now in the next few minutes. "
        "If the mode is opening and opening_has_data is false, do not claim that last night's plan was confirmed. "
        "If the mode is opening and both sell-risk validation and buy-strength validation exist, prioritize sell-risk validation first. "
        "Return strict JSON only with keys: headline, summary, focus, risk_flags, opening_plan. "
        "headline and summary must be short Traditional Chinese strings. "
        "focus, risk_flags, opening_plan must each be arrays with 0 to 3 short Traditional Chinese strings. "
        f"{rewrite_instruction}\n"
        f"{mode_instruction}\n\n"
        f"{rule_summary_block}"
        f"Data JSON:\n{json.dumps(sanitize_prompt_payload(recap_payload), ensure_ascii=False)}"
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(
            endpoint,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=payload,
            timeout=max(float(RECAP_GEMINI_TIMEOUT_SEC), 5.0),
        )
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return morning_rule_summary if isinstance(morning_rule_summary, dict) and morning_rule_summary else {}

    parsed = _extract_json_block(text)
    if not parsed:
        return morning_rule_summary if isinstance(morning_rule_summary, dict) and morning_rule_summary else {}

    def _list_field(name: str) -> List[str]:
        value = parsed.get(name, [])
        if not isinstance(value, list):
            return []
        return [_clip_text(item, 90) for item in value if str(item or "").strip()][:3]

    out = {
        "headline": _clip_text(parsed.get("headline") or "", 80),
        "summary": _clip_text(parsed.get("summary") or "", 110),
        "focus": _list_field("focus"),
        "risk_flags": _list_field("risk_flags"),
        "opening_plan": _list_field("opening_plan"),
    }

    if rewrite_only and isinstance(morning_rule_summary, dict):
        if not str(out.get("headline", "")).strip():
            out["headline"] = str(morning_rule_summary.get("headline", "")).strip()
        if not str(out.get("summary", "")).strip():
            out["summary"] = str(morning_rule_summary.get("summary", "")).strip()
        if not out.get("focus"):
            out["focus"] = [str(item).strip() for item in morning_rule_summary.get("focus", []) if str(item).strip()][:3]
        if not out.get("risk_flags"):
            out["risk_flags"] = [str(item).strip() for item in morning_rule_summary.get("risk_flags", []) if str(item).strip()][:3]
        if not out.get("opening_plan"):
            out["opening_plan"] = [str(item).strip() for item in morning_rule_summary.get("opening_plan", []) if str(item).strip()][:3]
    return out


def _build_recap_context(df: pd.DataFrame, tv_map: Dict[str, object], title_date: str, mode: str, end_dt: Optional[datetime] = None) -> dict:
    now_dt = end_dt or _utc_now_naive()
    start_dt = _window_start(mode, now_dt)
    positions_df = load_positions()
    execution_df = _load_execution_df(RECAP_EXECUTION_LOOKBACK_LIMIT)
    execution_window_df = _filter_execution_window(execution_df, start_dt, now_dt)
    bedtime_reference = _load_persisted_bedtime_plan(title_date) if mode == "morning" else {}
    prior_bedtime_lines = _merge_lines(
        bedtime_reference.get("opening_plan", []),
        bedtime_reference.get("risk_flags", []),
        bedtime_reference.get("focus", []),
    )
    execution_summaries = _summarize_execution_window(execution_window_df, positions_df, df)
    morning_rule_ai_summary: dict = {}
    if mode == "morning" and RECAP_MORNING_FULL_ENGINE_ENABLED:
        full_engine_summaries = _summarize_execution_window(
            execution_window_df,
            positions_df,
            df,
            include_all_tickers=True,
        )
        ranked_full_engine = _rank_morning_full_engine(full_engine_summaries)
        execution_summaries = ranked_full_engine[:max(1, int(RECAP_MORNING_FULL_ENGINE_MAX_TICKERS))]
        morning_rule_ai_summary = _build_morning_rule_ai_summary(execution_summaries, prior_bedtime_lines)

    conflict_news = _fetch_conflict_news(execution_summaries)
    tracked_tickers = [str(row.get("ticker", "")).strip().upper() for _, row in df.head(3).iterrows() if str(row.get("ticker", "")).strip()]
    for _, row in positions_df.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker and ticker not in tracked_tickers:
            tracked_tickers.append(ticker)
    for item in execution_summaries[: max(1, int(RECAP_TRACKED_NEWS_MAX_TICKERS))]:
        ticker = str(item.get("ticker", "")).strip().upper()
        if ticker and ticker not in tracked_tickers:
            tracked_tickers.append(ticker)
    tracked_news = _fetch_tracked_news(tracked_tickers, mode)
    payload = {
        "mode": mode,
        "decision_date": title_date,
        "window_start_utc": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end_utc": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "positions": _position_payload(positions_df),
        "execution_summary": _summaries_to_payload(execution_summaries),
        "top_candidates": _top_candidate_payload(df, tv_map),
        "conflict_news": conflict_news,
        "tracked_news": tracked_news,
        "prior_bedtime_plan": bedtime_reference.get("opening_plan", []) if isinstance(bedtime_reference, dict) else [],
        "prior_bedtime_focus": bedtime_reference.get("focus", []) if isinstance(bedtime_reference, dict) else [],
        "prior_bedtime_risk_flags": bedtime_reference.get("risk_flags", []) if isinstance(bedtime_reference, dict) else [],
        "prior_bedtime_lines": prior_bedtime_lines,
        "execution_summary_source": "morning_full_engine" if mode == "morning" and RECAP_MORNING_FULL_ENGINE_ENABLED else "tracked",
        "morning_full_engine_enabled": bool(mode == "morning" and RECAP_MORNING_FULL_ENGINE_ENABLED),
        "morning_rule_ai_summary": morning_rule_ai_summary,
    }

    ai_summary = _generate_recap_ai_summary(mode, payload)
    if mode == "bedtime":
        candidate_plan_line = _bedtime_unique_candidate_plan_line(df, positions_df)
        if ai_summary:
            existing_plan = ai_summary.get("opening_plan", []) if isinstance(ai_summary.get("opening_plan", []), list) else []
            merged_plan = [candidate_plan_line]
            merged_plan.extend(str(item).strip() for item in existing_plan if str(item).strip() and str(item).strip() != candidate_plan_line)
            ai_summary["opening_plan"] = merged_plan[:3]
    if mode == "morning" and isinstance(morning_rule_ai_summary, dict) and morning_rule_ai_summary:
        if not ai_summary:
            ai_summary = morning_rule_ai_summary
        else:
            if not str(ai_summary.get("headline", "")).strip():
                ai_summary["headline"] = str(morning_rule_ai_summary.get("headline", "")).strip()
            if not str(ai_summary.get("summary", "")).strip():
                ai_summary["summary"] = str(morning_rule_ai_summary.get("summary", "")).strip()
            if not ai_summary.get("focus"):
                ai_summary["focus"] = [str(item).strip() for item in morning_rule_ai_summary.get("focus", []) if str(item).strip()][:3]
            if not ai_summary.get("risk_flags"):
                ai_summary["risk_flags"] = [str(item).strip() for item in morning_rule_ai_summary.get("risk_flags", []) if str(item).strip()][:3]
            if not ai_summary.get("opening_plan"):
                ai_summary["opening_plan"] = [str(item).strip() for item in morning_rule_ai_summary.get("opening_plan", []) if str(item).strip()][:3]

    payload["ai_summary"] = ai_summary
    payload["positions_df"] = positions_df
    payload["execution_summaries_full"] = execution_summaries
    return payload


def _build_opening_context(df: pd.DataFrame, tv_map: Dict[str, object], title_date: str) -> dict:
    now_dt = _utc_now_naive()
    opening_start_dt, opening_end_dt = _opening_window_bounds(now_dt)
    reference_context = _build_recap_context(df=df, tv_map=tv_map, title_date=title_date, mode="morning", end_dt=opening_start_dt)
    bedtime_reference = _load_persisted_bedtime_plan(title_date)
    persisted_morning = _load_persisted_morning_plan(title_date)

    positions_df = load_positions()
    execution_df = _load_execution_df(RECAP_EXECUTION_LOOKBACK_LIMIT)
    opening_window_df = _filter_execution_window(execution_df, opening_start_dt, opening_end_dt)
    opening_summaries = _summarize_execution_window(opening_window_df, positions_df, df)
    validation_rows = _build_opening_validation(reference_context, opening_summaries)
    engine_snapshot = _load_engine_snapshot()
    tracked_tickers = [str(row.get("ticker", "")).strip().upper() for _, row in df.head(3).iterrows() if str(row.get("ticker", "")).strip()]
    for _, row in positions_df.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker and ticker not in tracked_tickers:
            tracked_tickers.append(ticker)
    tracked_news = _fetch_tracked_news(tracked_tickers, "opening")
    reference_plan = _merge_lines(
        bedtime_reference.get("opening_plan", []) if isinstance(bedtime_reference, dict) else [],
        persisted_morning.get("opening_plan", []) if isinstance(persisted_morning, dict) else [],
    )
    if not reference_plan:
        reference_plan = _extract_reference_plan(reference_context)

    payload = {
        "mode": "opening",
        "decision_date": title_date,
        "opening_window_utc_start": opening_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "opening_window_utc_end": opening_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "opening_window_local": f"{_format_utc_to_active_local(opening_start_dt)} -> {_format_utc_to_active_local(opening_end_dt)}",
        "opening_has_data": len(opening_summaries) > 0,
        "reference_plan": reference_plan,
        "prior_bedtime_plan": bedtime_reference.get("opening_plan", []) if isinstance(bedtime_reference, dict) else [],
        "prior_bedtime_focus": bedtime_reference.get("focus", []) if isinstance(bedtime_reference, dict) else [],
        "prior_bedtime_risk_flags": bedtime_reference.get("risk_flags", []) if isinstance(bedtime_reference, dict) else [],
        "reference_summary": _summaries_to_payload(reference_context.get("execution_summaries_full", [])),
        "opening_execution_summary": _summaries_to_payload(opening_summaries),
        "opening_validation": validation_rows,
        "engine_snapshot": engine_snapshot,
        "tracked_news": tracked_news,
        "positions": _position_payload(positions_df),
        "top_candidates": _top_candidate_payload(df, tv_map),
        "reference_plan_source": "persisted_morning" if reference_plan and persisted_morning else "regenerated_morning",
    }
    payload["ai_summary"] = _generate_recap_ai_summary("opening", payload)
    payload["reference_context"] = reference_context
    payload["reference_plan_lines"] = reference_plan
    payload["validation_rows"] = validation_rows
    payload["positions_df"] = positions_df
    payload["execution_summaries_full"] = opening_summaries
    return payload


def _build_position_lines(positions_df: pd.DataFrame) -> List[str]:
    if len(positions_df) == 0:
        return ["- 目前沒有開倉部位。"]
    lines: List[str] = []
    trimmed = positions_df.sort_values(["ticker"]).head(5)
    for _, row in trimmed.iterrows():
        horizon = str(row.get("horizon_tag", "")).strip()
        tag = "[S]" if horizon == HORIZON_SWING_CORE else "[M]"
        lines.append(
            f"- {row.get('ticker', 'NA')}{tag} | qty={_safe_float(row.get('quantity', 0.0), 0.0):g} | avg={_safe_float(row.get('avg_cost', 0.0), 0.0):.2f} | add_count={_safe_int(row.get('add_count'), 0)}"
        )
    if len(positions_df) > len(trimmed):
        lines.append(f"- 其餘 {len(positions_df) - len(trimmed)} 檔持倉略。")
    return lines


def _build_execution_lines(execution_summaries: List[dict]) -> List[str]:
    if not execution_summaries:
        return ["- 這個時間窗沒有新的 execution 訊號。"]
    lines: List[str] = []
    for item in execution_summaries[:5]:
        horizon = str(item.get("horizon_tag", "")).strip()
        tag = "[S]" if horizon == HORIZON_SWING_CORE else "[M]"
        base = (
            f"- {item.get('ticker', 'NA')}{tag} | {item.get('status_label', '待確認')} | 最新={item.get('latest_label', 'NA')} {item.get('latest_time', 'NA')}"
        )
        if bool(item.get("has_position")):
            base += f" | 持倉={float(item.get('position_qty', 0.0)):g}@{float(item.get('avg_cost', 0.0)):.2f}"
        else:
            base += " | 目前未持有"
        if bool(item.get("has_conflict")):
            base += f" | 序列={item.get('action_sequence', 'NA')}"
        guidance = str(item.get("guidance", "")).strip()
        if guidance:
            base += f" | {guidance}"
        lines.append(base)
    return lines


def _build_conflict_news_lines(conflict_news: List[dict]) -> List[str]:
    if not conflict_news:
        return []
    lines = ["衝突查證:"]
    for item in conflict_news:
        lines.append(f"- {item.get('ticker', 'NA')} | {_clip_text(item.get('brief') or '查不到新聞摘要', 150)}")
    return lines


def _build_ai_summary_lines(
    ai_summary: dict,
    plan_label: str = "開盤先做",
    focus_label: str = "焦點",
    risk_first: bool = False,
    include_summary: bool = False,
) -> List[str]:
    if not ai_summary:
        return []
    lines: List[str] = []
    headline = str(ai_summary.get("headline", "")).strip()
    summary = str(ai_summary.get("summary", "")).strip()
    if headline:
        lines.append(headline)
    if include_summary and summary:
        lines.append(summary)
    focus_items = ai_summary.get("focus", []) if isinstance(ai_summary.get("focus", []), list) else []
    risk_items = ai_summary.get("risk_flags", []) if isinstance(ai_summary.get("risk_flags", []), list) else []
    summary_sections: List[str] = []
    if risk_first:
        if risk_items:
            summary_sections.append(f"風險: {' | '.join(str(item).strip() for item in risk_items if str(item).strip())}")
        if focus_items:
            summary_sections.append(f"{focus_label}: {' | '.join(str(item).strip() for item in focus_items if str(item).strip())}")
    else:
        if focus_items:
            summary_sections.append(f"{focus_label}: {' | '.join(str(item).strip() for item in focus_items if str(item).strip())}")
        if risk_items:
            summary_sections.append(f"風險: {' | '.join(str(item).strip() for item in risk_items if str(item).strip())}")
    lines.extend(summary_sections)
    plan_items = ai_summary.get("opening_plan", []) if isinstance(ai_summary.get("opening_plan", []), list) else []
    if plan_items:
        lines.append(f"{plan_label}:")
        for item in plan_items:
            text = str(item).strip()
            if text:
                lines.append(f"- {text}")
    return lines


def _build_reference_lines(reference_lines: List[str], label: str) -> List[str]:
    cleaned = [str(item).strip() for item in reference_lines if str(item or "").strip()]
    if not cleaned:
        return []
    lines = [f"{label}:"]
    for item in cleaned[:5]:
        lines.append(f"- {item}")
    return lines


def _build_recap_fallback_lines(recap_context: dict, plan_label: str = "開盤先做") -> List[str]:
    execution_summaries = recap_context.get("execution_summaries_full", [])
    if not execution_summaries:
        return ["Gemini 暫時無回應，這個觀測窗也沒有新的 execution 變化。"]

    lines = ["Gemini 暫時無回應，改用規則摘要。"]
    focus_items: List[str] = []
    risk_items: List[str] = []
    plan_items: List[str] = []
    seen_plan_items: set[str] = set()

    for item in execution_summaries[:3]:
        ticker = str(item.get("ticker", "NA")).strip() or "NA"
        status = str(item.get("status_label", "待確認")).strip() or "待確認"
        latest_action = str(item.get("latest_action", "")).strip().lower()
        guidance = _clip_text(item.get("guidance") or "", 72)

        focus_items.append(f"{ticker}({status})")
        if bool(item.get("has_conflict")) or latest_action in {"stop_loss", "take_profit"}:
            risk_items.append(f"{ticker}({status})")
        if guidance:
            if guidance not in seen_plan_items:
                seen_plan_items.add(guidance)
                plan_items.append(guidance)

    if focus_items:
        lines.append(f"焦點: {' | '.join(focus_items[:3])}")
    if risk_items:
        lines.append(f"風險: {' | '.join(risk_items[:3])}")
    if plan_items:
        lines.append(f"{plan_label}:")
        for item in plan_items[:3]:
            lines.append(f"- {item}")
    return lines


def _build_opening_fallback_lines(recap_context: dict) -> List[str]:
    validation_rows = recap_context.get("validation_rows", []) if isinstance(recap_context, dict) else []
    if not validation_rows:
        return ["Gemini 暫時無回應，開盤首輪還沒有足夠的 execution 訊號可驗證昨晚計畫；這在第一輪 opening workflow 屬正常情況。"]

    lines = ["Gemini 暫時無回應，改用開盤驗證摘要。"]
    focus_items = [f"{row.get('ticker', 'NA')}({row.get('validation_label', '待確認')})" for row in validation_rows[:3]]
    risk_items = [
        f"{row.get('ticker', 'NA')}({row.get('validation_label', '待確認')})"
        for row in validation_rows
        if str(row.get("validation_label", "")) in {"確認風險", "開盤失敗", "開盤雜訊", "反向轉強"}
    ][:3]
    if risk_items:
        lines.append(f"風險: {' | '.join(risk_items)}")
    if focus_items:
        lines.append(f"劇本驗證: {' | '.join(focus_items)}")
    lines.append("現在先做:")
    for row in validation_rows[:3]:
        next_step = str(row.get("next_step", "")).strip()
        if next_step:
            lines.append(f"- {next_step}")
    return lines


def _build_top3_lines(df: pd.DataFrame) -> List[str]:
    if len(df) == 0:
        return ["- No candidates available."]
    lines = ["Top 3:"]
    for _, row in df.head(3).iterrows():
        lines.append(
            f"- {row.get('rank', 'NA')}. {row.get('ticker', 'NA')} | {row.get('decision_tag', 'NA')} | {_clip_text(row.get('reason_summary') or row.get('catalyst_summary') or '', 90)}"
        )
    return lines


def _build_bedtime_message(df: pd.DataFrame, _tv_map: Dict[str, object], title_date: str, recap_context: dict) -> str:
    del _tv_map
    strategy_lines = _build_bedtime_strategy_lines(df, recap_context)
    selected = df.head(3).copy()
    top1 = selected.iloc[0] if len(selected) > 0 else None
    prev_top1 = _load_previous_top1(title_date)
    changed = "NA"
    if top1 is not None:
        changed = "是" if prev_top1 and str(top1.get("ticker", "")).upper() != prev_top1 else "否"

    lines = [
        f"[Alpha Finder] 睡前結論卡 {title_date}",
        "",
    ]
    if top1 is not None:
        lines.append(
            f"Top 1: {top1.get('ticker', 'NA')} | tag={top1.get('decision_tag', 'NA')} | risk={top1.get('risk_level', 'NA')} | 變動={changed}"
        )
        lines.append("")
    else:
        lines.append("No candidates available.")
        lines.append("")

    if strategy_lines:
        lines.extend(strategy_lines)
        lines.append("")

    ai_lines = _build_ai_summary_lines(recap_context.get("ai_summary", {}), plan_label="明早先做")
    if ai_lines:
        lines.extend(ai_lines)
    else:
        lines.extend(_build_recap_fallback_lines(recap_context, plan_label="明早先做"))
    return "\n".join(lines)


def _build_morning_message(_df: pd.DataFrame, _tv_map: Dict[str, object], title_date: str, recap_context: dict) -> str:
    strategy_lines = _build_morning_strategy_lines(_df, recap_context)
    del _tv_map
    lines = [
        f"[Alpha Finder] 隔夜結論卡 {title_date}",
        "",
    ]

    prior_bedtime_lines = recap_context.get("prior_bedtime_lines", []) if isinstance(recap_context, dict) else []
    if prior_bedtime_lines:
        lines.extend(_build_reference_lines(prior_bedtime_lines, "延續昨晚待驗證"))
        lines.append("")

    if strategy_lines:
        lines.extend(strategy_lines)
        lines.append("")

    ai_lines = _build_ai_summary_lines(recap_context.get("ai_summary", {}), plan_label="今日盤前計畫")
    if ai_lines:
        lines.extend(ai_lines)
    else:
        lines.extend(_build_recap_fallback_lines(recap_context, plan_label="今日盤前計畫"))
    return "\n".join(lines)


def _build_opening_message(_df: pd.DataFrame, _tv_map: Dict[str, object], title_date: str, recap_context: dict) -> str:
    strategy_lines = _build_opening_strategy_lines(_df, recap_context)
    del _tv_map
    lines = [
        f"[Alpha Finder] 開盤驗證結論卡 {title_date}",
        "",
    ]

    reference_plan = recap_context.get("reference_plan_lines", []) if isinstance(recap_context, dict) else []
    if reference_plan:
        lines.extend(_build_reference_lines(reference_plan, "待驗證計畫"))
        lines.append("")

    if strategy_lines:
        lines.extend(strategy_lines)
        lines.append("")

    ai_lines = _build_ai_summary_lines(
        recap_context.get("ai_summary", {}),
        plan_label="現在先做",
        focus_label="劇本驗證",
        risk_first=True,
    )
    if ai_lines:
        lines.extend(ai_lines)
    else:
        lines.extend(_build_opening_fallback_lines(recap_context))
    return "\n".join(lines)


def _build_message(df: pd.DataFrame, tv_map: Dict[str, object], top_n: int, tags: set[str], title_date: str) -> str:
    selected = df[df["decision_tag"].isin(tags)].copy()
    selected = selected.head(top_n)

    lines = [
        f"[Alpha Finder] AI Decision Alert {title_date}",
        f"Candidates: {len(selected)}",
        "",
    ]

    if len(selected) == 0:
        lines.append("No candidates matched current filters.")
    else:
        for _, row in selected.iterrows():
            ticker = str(row.get("ticker", ""))
            rank = int(row.get("rank", 0))
            score = row.get("short_score_final")
            score_str = "NA" if pd.isna(score) else f"{float(score):.1f}"
            tag = str(row.get("decision_tag", ""))
            risk = str(row.get("risk_level", "")) or "NA"
            tech = str(row.get("tech_status", "")) or "NA"
            tv_text = _fmt_tv_line(ticker, tv_map)
            lines.append(f"{rank}. {ticker} | tag={tag} | score={score_str} | risk={risk} | tech={tech} | {tv_text}")

    lines.append("")
    lines.append("Action: review in TradingView and follow your stop rules.")
    return "\n".join(lines)


def _render_message(
    df: pd.DataFrame,
    tv_map: Dict[str, object],
    top_n: int,
    tags: set[str],
    title_date: str,
    mode: str,
    recap_context: Optional[dict] = None,
) -> str:
    selected = df[df["decision_tag"].isin(tags)].copy().sort_values(["rank", "ticker"], ascending=[True, True])
    if mode == "bedtime":
        return _build_bedtime_message(selected, tv_map, title_date, recap_context or {})
    if mode == "morning":
        return _build_morning_message(selected, tv_map, title_date, recap_context or {})
    if mode == "opening":
        return _build_opening_message(selected, tv_map, title_date, recap_context or {})
    return _build_message(selected, tv_map, top_n=top_n, tags=tags, title_date=title_date)


def _post_json(url: str, payload: dict, headers: Optional[dict] = None, timeout: int = 15) -> tuple[bool, str]:
    req_headers = {"Content-Type": "application/json", "User-Agent": "AlphaFinder/1.0"}
    if headers:
        req_headers.update(headers)
    try:
        response = requests.post(url, json=payload, headers=req_headers, timeout=timeout)
        if response.ok:
            return True, f"{response.status_code} {response.text[:200]}"
        return False, f"HTTP {response.status_code}: {response.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


def _send_discord(message: str, webhook_url: str) -> tuple[bool, str]:
    if not webhook_url:
        return False, "discord webhook url missing"

    chunks: List[str] = []
    text = message
    while len(text) > 1900:
        split_at = text.rfind("\n", 0, 1900)
        if split_at <= 0:
            split_at = 1900
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)

    for chunk in chunks:
        ok, detail = _post_json(webhook_url, {"content": chunk})
        if not ok:
            return False, detail
    return True, f"sent {len(chunks)} discord message(s)"


def _send_line(message: str, channel_access_token: str, to_user_id: str) -> tuple[bool, str]:
    if not channel_access_token or not to_user_id:
        return False, "line token or to-user-id missing"

    payload = {
        "to": to_user_id,
        "messages": [{"type": "text", "text": message[:5000]}],
    }
    headers = {"Authorization": f"Bearer {channel_access_token}"}
    return _post_json("https://api.line.me/v2/bot/message/push", payload, headers=headers)


def _append_alert_log(rows: List[dict]) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = ALERT_LOG_CSV.exists()

    with ALERT_LOG_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "alert_ts",
                "decision_date",
                "channel",
                "ticker",
                "rank",
                "decision_tag",
                "short_score_final",
                "risk_level",
                "tech_status",
                "source_csv",
            ],
        )
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Push alerts from ai_decision CSV to Discord/LINE")
    parser.add_argument("--csv-file", default="", help="Path to ai_decision_YYYY-MM-DD.csv")
    parser.add_argument("--auto-latest", action="store_true", help="Find latest ai_decision_*.csv automatically")
    parser.add_argument("--top-n", type=int, default=5, help="Top N rows to send")
    parser.add_argument("--tags", default="keep,watch", help="Comma separated tags to include, e.g. keep or keep,watch")
    parser.add_argument("--channel", default="discord", choices=["discord", "line", "both"], help="Notification channel")
    parser.add_argument("--mode", default="full", choices=["full", "bedtime", "morning", "opening"], help="Discord/LINE message style")
    parser.add_argument("--respect-mode-window", action="store_true", help="Skip sending when the selected recap mode is outside its intended dispatch window")
    parser.add_argument("--dry-run", action="store_true", help="Print message only, do not send")
    args = parser.parse_args()

    csv_path = Path(args.csv_file).resolve() if args.csv_file.strip() else None
    source_id: str | None = None
    if args.auto_latest or csv_path is None:
        df, source_id = _load_latest_decision_df()
        if source_id is None:
            logger.error("No ai_decision latest state found in Turso / backtest latest / inbox / ai_ready/latest / daily_refresh/latest")
            return 1
    else:
        if not csv_path.exists():
            logger.error("CSV not found: %s", csv_path)
            return 2
        df = _load_decision_df(csv_path)
        source_id = str(csv_path)

    tags = {x.strip().lower() for x in str(args.tags).split(",") if x.strip()}
    if not tags:
        tags = {"keep", "watch"}

    decision_date = "unknown"
    if "decision_date" in df.columns and df["decision_date"].notna().any():
        decision_date = str(df["decision_date"].dropna().iloc[0])

    if args.mode == "opening" and args.respect_mode_window and not args.dry_run:
        now_dt = _utc_now_naive()
        if not _is_in_opening_dispatch_window(now_dt):
            open_start, open_end = _opening_dispatch_bounds(now_dt)
            logger.warning(
                "[SKIP] opening mode outside dispatch window: %s -> %s",
                _format_utc_to_active_local(open_start),
                _format_utc_to_active_local(open_end),
            )
            return 0

    tv_map = _load_tv_map()
    recap_context = None
    if str(args.mode) in {"bedtime", "morning"}:
        recap_context = _build_recap_context(df=df[df["decision_tag"].isin(tags)].copy(), tv_map=tv_map, title_date=decision_date, mode=str(args.mode))
    elif str(args.mode) == "opening":
        recap_context = _build_opening_context(df=df[df["decision_tag"].isin(tags)].copy(), tv_map=tv_map, title_date=decision_date)
    message = _render_message(
        df=df,
        tv_map=tv_map,
        top_n=max(1, int(args.top_n)),
        tags=tags,
        title_date=decision_date,
        mode=str(args.mode),
        recap_context=recap_context,
    )

    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_MESSAGE_TXT.write_text(message, encoding="utf-8")

    logger.info("=== Alert Preview ===\n%s", message)

    sent_channels: List[str] = []
    send_failed = False
    if not args.dry_run:
        if args.channel in {"discord", "both"}:
            if _already_sent(decision_date, args.mode, "discord", source_id):
                logger.warning("[SKIP] discord %s already sent for %s -> %s", args.mode, decision_date, source_id)
            else:
                discord_url = _sanitize_webhook_url(os.getenv("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL))
                ok, detail = _send_discord(message, discord_url)
                logger.info("[DISCORD] ok=%s detail=%s", ok, detail)
                if ok:
                    sent_channels.append("discord")
                    _write_sent_marker(decision_date, args.mode, "discord", source_id)
                else:
                    send_failed = True

        if args.channel in {"line", "both"}:
            if _already_sent(decision_date, args.mode, "line", source_id):
                logger.warning("[SKIP] line %s already sent for %s -> %s", args.mode, decision_date, source_id)
            else:
                line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
                line_to = os.getenv("LINE_TO_USER_ID", "").strip()
                ok, detail = _send_line(message, line_token, line_to)
                logger.info("[LINE] ok=%s detail=%s", ok, detail)
                if ok:
                    sent_channels.append("line")
                    _write_sent_marker(decision_date, args.mode, "line", source_id)
                else:
                    send_failed = True
    else:
        sent_channels.append("dry_run")

    if recap_context is not None and sent_channels and not args.dry_run:
        if str(args.mode) == "bedtime":
            _persist_bedtime_plan(decision_date, recap_context, message, source_id)
        elif str(args.mode) == "morning":
            _persist_morning_plan(decision_date, recap_context, message, source_id)

    log_df = df[df["decision_tag"].isin(tags)].head(max(1, int(args.top_n))).copy()
    log_rows: List[dict] = []
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for _, row in log_df.iterrows():
        for ch in sent_channels:
            log_rows.append(
                {
                    "alert_ts": ts_now,
                    "decision_date": decision_date,
                    "channel": ch,
                    "ticker": str(row.get("ticker", "")),
                    "rank": int(row.get("rank", 0)),
                    "decision_tag": str(row.get("decision_tag", "")),
                    "short_score_final": "" if pd.isna(row.get("short_score_final")) else float(row.get("short_score_final")),
                    "risk_level": str(row.get("risk_level", "")),
                    "tech_status": str(row.get("tech_status", "")),
                    "source_csv": str(source_id),
                }
            )

    if log_rows and not args.dry_run:
        _append_alert_log(log_rows)
        logger.info("[ALERT_LOG] appended %s rows -> %s", len(log_rows), ALERT_LOG_CSV)

    logger.info("[ALERT_MSG] %s", ALERT_MESSAGE_TXT)
    return 4 if send_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
