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

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.position_state import load_positions
from config import (
    DISCORD_WEBHOOK_URL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    RECAP_BEDTIME_UTC_START_HOUR,
    RECAP_CONFLICT_NEWS_MAX_TICKERS,
    RECAP_EXECUTION_LOOKBACK_LIMIT,
    RECAP_GEMINI_ENABLED,
    RECAP_GEMINI_TIMEOUT_SEC,
    RECAP_MORNING_LOOKBACK_HOURS,
    RECAP_TAVILY_ENABLED,
    RECAP_TAVILY_MAX_RESULTS,
    SIGNAL_MAX_AGE_MINUTES,
    SIGNAL_REQUIRE_SAME_DAY,
    SIGNAL_STORE_PATH,
    TAVILY_API_KEY,
)
from signal_store import get_latest_signals
from turso_state import STATE_KEY_AI_DECISION_LATEST, load_recent_execution_log, load_runtime_df_with_fallback

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

REQUIRED_COLS = [
    "decision_date",
    "rank",
    "ticker",
    "short_score_final",
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
    if mode not in {"bedtime", "morning"}:
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
    if mode not in {"bedtime", "morning"}:
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


def _summarize_execution_window(execution_df: pd.DataFrame, positions_df: pd.DataFrame, decision_df: pd.DataFrame) -> List[dict]:
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

    out: List[dict] = []
    grouped = execution_df.sort_values(["recorded_at_ts", "rank", "ticker"], ascending=[True, True, True]).groupby("ticker", sort=False)
    for ticker, group in grouped:
        if not ticker:
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


def _top_candidate_payload(df: pd.DataFrame, tv_map: Dict[str, object]) -> List[dict]:
    out: List[dict] = []
    for _, row in df.head(3).iterrows():
        out.append(
            {
                "ticker": str(row.get("ticker", "")).strip().upper(),
                "rank": _safe_int(row.get("rank"), 0),
                "decision_tag": str(row.get("decision_tag", "")).strip().lower(),
                "risk_level": str(row.get("risk_level", "")).strip(),
                "reason_summary": _clip_text(row.get("reason_summary") or "", 120),
                "catalyst_summary": _clip_text(row.get("catalyst_summary") or row.get("catalyst_type") or "", 120),
                "tv": _fmt_tv_line(str(row.get("ticker", "")), tv_map),
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
            }
        )
    return out


def _generate_recap_ai_summary(mode: str, recap_payload: dict) -> dict:
    if not RECAP_GEMINI_ENABLED or not GEMINI_API_KEY:
        return {}

    mode_instruction = (
        "Focus on what changed before sleep and which tickers need extra caution tomorrow."
        if mode == "bedtime"
        else "Focus on what happened overnight and which charts should be opened first at the next session."
    )
    prompt = (
        "You are editing a trading recap for a human operator. "
        "Use only the provided facts. "
        "Do not invent positions, fills, prices, or news. "
        "Always distinguish open positions from engine suggestions. "
        "Return strict JSON only with keys: headline, summary, focus, risk_flags, opening_plan. "
        "headline and summary must be short Traditional Chinese strings. "
        "focus, risk_flags, opening_plan must each be arrays with 0 to 3 short Traditional Chinese strings. "
        f"{mode_instruction}\n\n"
        f"Data JSON:\n{json.dumps(recap_payload, ensure_ascii=False)}"
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(endpoint, json=payload, timeout=max(float(RECAP_GEMINI_TIMEOUT_SEC), 5.0))
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return {}

    parsed = _extract_json_block(text)
    if not parsed:
        return {}

    def _list_field(name: str) -> List[str]:
        value = parsed.get(name, [])
        if not isinstance(value, list):
            return []
        return [_clip_text(item, 90) for item in value if str(item or "").strip()][:3]

    return {
        "headline": _clip_text(parsed.get("headline") or "", 80),
        "summary": _clip_text(parsed.get("summary") or "", 140),
        "focus": _list_field("focus"),
        "risk_flags": _list_field("risk_flags"),
        "opening_plan": _list_field("opening_plan"),
    }


def _build_recap_context(df: pd.DataFrame, tv_map: Dict[str, object], title_date: str, mode: str) -> dict:
    now_dt = _utc_now_naive()
    start_dt = _window_start(mode, now_dt)
    positions_df = load_positions()
    execution_df = _load_execution_df(RECAP_EXECUTION_LOOKBACK_LIMIT)
    execution_window_df = _filter_execution_window(execution_df, start_dt, now_dt)
    execution_summaries = _summarize_execution_window(execution_window_df, positions_df, df)
    conflict_news = _fetch_conflict_news(execution_summaries)
    payload = {
        "mode": mode,
        "decision_date": title_date,
        "window_start_utc": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end_utc": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "positions": _position_payload(positions_df),
        "execution_summary": [
            {
                "ticker": item.get("ticker", ""),
                "latest_action": item.get("latest_action", ""),
                "status_label": item.get("status_label", ""),
                "latest_time": item.get("latest_time", ""),
                "has_position": bool(item.get("has_position")),
                "rank": _safe_int(item.get("rank"), 9999),
                "has_conflict": bool(item.get("has_conflict")),
                "action_sequence": item.get("action_sequence", ""),
                "guidance": item.get("guidance", ""),
            }
            for item in execution_summaries[:6]
        ],
        "top_candidates": _top_candidate_payload(df, tv_map),
        "conflict_news": conflict_news,
    }
    payload["ai_summary"] = _generate_recap_ai_summary(mode, payload)
    payload["positions_df"] = positions_df
    payload["execution_summaries_full"] = execution_summaries
    return payload


def _build_position_lines(positions_df: pd.DataFrame) -> List[str]:
    if len(positions_df) == 0:
        return ["- 目前沒有開倉部位。"]
    lines: List[str] = []
    trimmed = positions_df.sort_values(["ticker"]).head(5)
    for _, row in trimmed.iterrows():
        lines.append(
            f"- {row.get('ticker', 'NA')} | qty={_safe_float(row.get('quantity', 0.0), 0.0):g} | avg={_safe_float(row.get('avg_cost', 0.0), 0.0):.2f} | add_count={_safe_int(row.get('add_count'), 0)}"
        )
    if len(positions_df) > len(trimmed):
        lines.append(f"- 其餘 {len(positions_df) - len(trimmed)} 檔持倉略。")
    return lines


def _build_execution_lines(execution_summaries: List[dict]) -> List[str]:
    if not execution_summaries:
        return ["- 這個時間窗沒有新的 execution 訊號。"]
    lines: List[str] = []
    for item in execution_summaries[:5]:
        base = (
            f"- {item.get('ticker', 'NA')} | {item.get('status_label', '待確認')} | 最新={item.get('latest_label', 'NA')} {item.get('latest_time', 'NA')}"
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


def _build_ai_summary_lines(ai_summary: dict) -> List[str]:
    if not ai_summary:
        return []
    lines: List[str] = ["Gemini 摘要:"]
    headline = str(ai_summary.get("headline", "")).strip()
    summary = str(ai_summary.get("summary", "")).strip()
    if headline:
        lines.append(headline)
    if summary:
        lines.append(summary)
    for label, key in [("焦點", "focus"), ("風險", "risk_flags"), ("開盤先做", "opening_plan")]:
        items = ai_summary.get(key, []) if isinstance(ai_summary.get(key, []), list) else []
        if not items:
            continue
        lines.append(f"{label}:")
        for item in items:
            lines.append(f"- {item}")
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


def _build_bedtime_message(df: pd.DataFrame, tv_map: Dict[str, object], title_date: str, recap_context: dict) -> str:
    selected = df.head(3).copy()
    top1 = selected.iloc[0] if len(selected) > 0 else None
    prev_top1 = _load_previous_top1(title_date)
    changed = "NA"
    if top1 is not None:
        changed = "是" if prev_top1 and str(top1.get("ticker", "")).upper() != prev_top1 else "否"

    lines = [
        f"[Alpha Finder] 睡前執行摘要 {title_date}",
        "",
    ]
    if top1 is not None:
        lines.extend(
            [
                f"Top 1: {top1.get('ticker', 'NA')} | tag={top1.get('decision_tag', 'NA')} | risk={top1.get('risk_level', 'NA')} | Top1是否改變={changed}",
                f"盤中確認: {_fmt_tv_line(str(top1.get('ticker', '')), tv_map)}",
                f"主催化: {_clip_text(top1.get('catalyst_summary') or top1.get('catalyst_type') or '待確認', 140)}",
                f"決策摘要: {_clip_text(top1.get('reason_summary') or '待確認', 140)}",
                "",
            ]
        )
    else:
        lines.append("No candidates available.")
        lines.append("")

    lines.append(
        f"睡前窗口(UTC): {recap_context.get('window_start_utc', 'NA')} -> {recap_context.get('window_end_utc', 'NA')}"
    )
    lines.append("目前持倉:")
    lines.extend(_build_position_lines(recap_context.get("positions_df", pd.DataFrame())))
    lines.append("")
    lines.append("睡前 engine 摘要:")
    lines.extend(_build_execution_lines(recap_context.get("execution_summaries_full", [])))
    if recap_context.get("conflict_news"):
        lines.append("")
        lines.extend(_build_conflict_news_lines(recap_context.get("conflict_news", [])))
    if recap_context.get("ai_summary"):
        lines.append("")
        lines.extend(_build_ai_summary_lines(recap_context.get("ai_summary", {})))
    lines.append("")
    lines.extend(_build_top3_lines(selected))
    lines.append("")
    lines.append("用途: 睡前先知道手上部位、今晚第一段 engine 怎麼看、明早先盯哪幾檔。")
    return "\n".join(lines)


def _build_morning_message(df: pd.DataFrame, tv_map: Dict[str, object], title_date: str, recap_context: dict) -> str:
    selected = df.head(3).copy()
    top1 = selected.iloc[0] if len(selected) > 0 else None
    lines = [
        f"[Alpha Finder] Overnight Recap {title_date}",
        "",
    ]
    if top1 is not None:
        lines.append(
            f"今日先看: {top1.get('ticker', 'NA')} | tag={top1.get('decision_tag', 'NA')} | {_fmt_tv_line(str(top1.get('ticker', '')), tv_map)}"
        )
        lines.append(f"原因: {_clip_text(top1.get('catalyst_summary') or top1.get('reason_summary'), 160)}")
        lines.append("")
    else:
        lines.append("No candidates available.")
        lines.append("")

    lines.append(
        f"隔夜窗口(UTC): {recap_context.get('window_start_utc', 'NA')} -> {recap_context.get('window_end_utc', 'NA')}"
    )
    lines.append("目前持倉:")
    lines.extend(_build_position_lines(recap_context.get("positions_df", pd.DataFrame())))
    lines.append("")
    lines.append("隔夜 engine 摘要:")
    lines.extend(_build_execution_lines(recap_context.get("execution_summaries_full", [])))
    if recap_context.get("conflict_news"):
        lines.append("")
        lines.extend(_build_conflict_news_lines(recap_context.get("conflict_news", [])))
    if recap_context.get("ai_summary"):
        lines.append("")
        lines.extend(_build_ai_summary_lines(recap_context.get("ai_summary", {})))
    lines.append("")
    lines.extend(_build_top3_lines(selected))
    lines.append("")
    lines.append("用途: 早上先分清楚你手上有什麼、隔夜 engine 怎麼翻、開盤第一輪先打開哪幾檔。")
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
    parser.add_argument("--mode", default="full", choices=["full", "bedtime", "morning"], help="Discord/LINE message style")
    parser.add_argument("--dry-run", action="store_true", help="Print message only, do not send")
    args = parser.parse_args()

    csv_path = Path(args.csv_file).resolve() if args.csv_file.strip() else None
    source_id: str | None = None
    if args.auto_latest or csv_path is None:
        df, source_id = _load_latest_decision_df()
        if source_id is None:
            print("No ai_decision latest state found in Turso / backtest latest / inbox / ai_ready/latest / daily_refresh/latest")
            return 1
    else:
        if not csv_path.exists():
            print(f"CSV not found: {csv_path}")
            return 2
        df = _load_decision_df(csv_path)
        source_id = str(csv_path)

    tags = {x.strip().lower() for x in str(args.tags).split(",") if x.strip()}
    if not tags:
        tags = {"keep", "watch"}

    decision_date = "unknown"
    if "decision_date" in df.columns and df["decision_date"].notna().any():
        decision_date = str(df["decision_date"].dropna().iloc[0])

    tv_map = _load_tv_map()
    recap_context = None
    if str(args.mode) in {"bedtime", "morning"}:
        recap_context = _build_recap_context(df=df[df["decision_tag"].isin(tags)].copy(), tv_map=tv_map, title_date=decision_date, mode=str(args.mode))
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

    print("\n=== Alert Preview ===")
    print(message)

    sent_channels: List[str] = []
    send_failed = False
    if not args.dry_run:
        if args.channel in {"discord", "both"}:
            if _already_sent(decision_date, args.mode, "discord", source_id):
                print(f"[SKIP] discord {args.mode} already sent for {decision_date} -> {source_id}")
            else:
                discord_url = _sanitize_webhook_url(os.getenv("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL))
                ok, detail = _send_discord(message, discord_url)
                print(f"[DISCORD] ok={ok} detail={detail}")
                if ok:
                    sent_channels.append("discord")
                    _write_sent_marker(decision_date, args.mode, "discord", source_id)
                else:
                    send_failed = True

        if args.channel in {"line", "both"}:
            if _already_sent(decision_date, args.mode, "line", source_id):
                print(f"[SKIP] line {args.mode} already sent for {decision_date} -> {source_id}")
            else:
                line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
                line_to = os.getenv("LINE_TO_USER_ID", "").strip()
                ok, detail = _send_line(message, line_token, line_to)
                print(f"[LINE] ok={ok} detail={detail}")
                if ok:
                    sent_channels.append("line")
                    _write_sent_marker(decision_date, args.mode, "line", source_id)
                else:
                    send_failed = True
    else:
        sent_channels.append("dry_run")

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
        print(f"[ALERT_LOG] appended {len(log_rows)} rows -> {ALERT_LOG_CSV}")

    print(f"[ALERT_MSG] {ALERT_MESSAGE_TXT}")
    return 4 if send_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
