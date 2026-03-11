from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd
import requests

from config import (
    CATALYST_TAVILY_MAX_RESULTS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    INTRADAY_INTERVAL,
    INTRADAY_PERIOD,
    RECAP_GEMINI_TIMEOUT_SEC,
    SIGNAL_MAX_AGE_MINUTES,
    SIGNAL_REQUIRE_SAME_DAY,
    SIGNAL_STORE_PATH,
    TAVILY_API_KEY,
)
from signal_store import get_latest_signals
from turso_state import STATE_KEY_AI_DECISION_LATEST, load_recent_execution_log, load_runtime_df_with_fallback

from .intraday_execution_engine import AI_DECISION_LATEST, SNAPSHOT_FILE, _classify_action, _fetch_intraday_bars
from .intraday_indicators import add_intraday_indicators
from .position_state import get_position, load_positions
from .shadow_watchlist import build_decision_universe_df, load_shadow_decision_df


ACTION_LABELS = {
    "entry": "適合買",
    "add": "可加碼",
    "take_profit": "先減碼",
    "stop_loss": "先降風險",
    "": "待確認",
}
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHLIST_STORE_PATH = PROJECT_ROOT / "repo_outputs" / "backtest" / "discord_watchlists.json"
DEFAULT_AI_DECISION_TOP_N = 5
MAX_TRACKED_TICKERS = 12


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


def _parse_tickers(raw: str, limit: int = MAX_TRACKED_TICKERS) -> List[str]:
    parts = re.split(r"[\s,，;；]+", str(raw or "").strip().upper())
    out: List[str] = []
    for part in parts:
        cleaned = re.sub(r"[^A-Z0-9._-]", "", part)
        if not cleaned or cleaned in out:
            continue
        out.append(cleaned)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _load_watchlist_store() -> dict:
    if not WATCHLIST_STORE_PATH.exists():
        return {"users": {}}
    try:
        data = json.loads(WATCHLIST_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"users": {}}
    if not isinstance(data, dict):
        return {"users": {}}
    users = data.get("users")
    if not isinstance(users, dict):
        data["users"] = {}
    return data


def _save_watchlist_store(data: dict) -> None:
    WATCHLIST_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_STORE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_saved_watchlist(user_id: int | str) -> List[str]:
    store = _load_watchlist_store()
    entry = store.get("users", {}).get(str(user_id), {})
    tickers = entry.get("tickers", []) if isinstance(entry, dict) else []
    if not isinstance(tickers, list):
        return []
    return _parse_tickers(" ".join(str(item) for item in tickers), limit=MAX_TRACKED_TICKERS)


def add_saved_watchlist_tickers(user_id: int | str, raw_tickers: str) -> List[str]:
    additions = _parse_tickers(raw_tickers)
    if not additions:
        raise ValueError("請提供至少一個股票代號，例如 AAPL NVDA")
    current = load_saved_watchlist(user_id)
    merged = current[:]
    for ticker in additions:
        if ticker not in merged:
            merged.append(ticker)
    merged = merged[:MAX_TRACKED_TICKERS]
    store = _load_watchlist_store()
    store.setdefault("users", {})[str(user_id)] = {
        "tickers": merged,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_watchlist_store(store)
    return merged


def remove_saved_watchlist_tickers(user_id: int | str, raw_tickers: str) -> List[str]:
    removals = set(_parse_tickers(raw_tickers))
    if not removals:
        raise ValueError("請提供至少一個要移除的股票代號，例如 AAPL")
    current = load_saved_watchlist(user_id)
    remaining = [ticker for ticker in current if ticker not in removals]
    store = _load_watchlist_store()
    store.setdefault("users", {})[str(user_id)] = {
        "tickers": remaining,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_watchlist_store(store)
    return remaining


def format_saved_watchlist_message(user_id: int | str) -> str:
    tickers = load_saved_watchlist(user_id)
    if not tickers:
        return "你目前沒有保存的關注股。可用 /watchadd AAPL NVDA 新增。"
    lines = ["你保存的關注股:"]
    for ticker in tickers:
        lines.append(f"- {ticker}")
    return "\n".join(lines)


def _load_decision_df() -> pd.DataFrame:
    df, _ = load_runtime_df_with_fallback(STATE_KEY_AI_DECISION_LATEST, [AI_DECISION_LATEST])
    if len(df) == 0 or "ticker" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    for col in ["decision_date", "rank", "ticker", "decision_tag", "risk_level", "tech_status", "theme", "reason_summary", "catalyst_summary"]:
        if col not in out.columns:
            out[col] = ""
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out = out[out["ticker"] != ""].copy()
    out = out[out["decision_tag"].isin(["keep", "watch", "replace", "trim"]) | (out["decision_tag"] == "")]
    out = out.sort_values(["rank", "ticker"], ascending=[True, True], na_position="last").reset_index(drop=True)
    return out


def _load_decision_map() -> Dict[str, dict]:
    out = _load_decision_universe_df()
    return {str(row.get("ticker", "")).strip().upper(): row.to_dict() for _, row in out.iterrows() if str(row.get("ticker", "")).strip()}


def _load_decision_universe_df() -> pd.DataFrame:
    latest = _load_decision_df()
    return build_decision_universe_df(latest)


def _load_signal_map() -> Dict[str, object]:
    try:
        return get_latest_signals(
            SIGNAL_STORE_PATH,
            asof=datetime.now(timezone.utc),
            max_age_minutes=SIGNAL_MAX_AGE_MINUTES,
            require_same_day=SIGNAL_REQUIRE_SAME_DAY,
        )
    except (OSError, ValueError, TypeError, sqlite3.Error, pd.errors.EmptyDataError):
        return {}


def _tavily_search(query: str, max_results: int) -> List[Dict[str, str]]:
    if not TAVILY_API_KEY:
        return []
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": int(max_results),
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        response = requests.post("https://api.tavily.com/search", json=payload, timeout=max(float(RECAP_GEMINI_TIMEOUT_SEC), 10.0))
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


def _load_recent_execution_rows(ticker: str, limit: int = 3) -> List[dict]:
    df = load_recent_execution_log(limit=max(1, int(limit)), ticker=ticker)
    out: List[dict] = []
    for _, row in df.head(limit).iterrows():
        out.append(
            {
                "recorded_at": str(row.get("recorded_at", "")).strip(),
                "action": str(row.get("action", "")).strip().lower(),
                "position_effect": str(row.get("position_effect", "")).strip().lower(),
                "timeframe": str(row.get("timeframe", "")).strip(),
                "close": _safe_float(row.get("close"), 0.0),
                "vwap": _safe_float(row.get("vwap"), 0.0),
                "sqzmom_color": str(row.get("sqzmom_color", "")).strip(),
                "reason_summary": _clip_text(row.get("reason_summary") or "", 90),
            }
        )
    return out


def _load_intraday_snapshot_map() -> Dict[str, dict]:
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        df = pd.read_csv(SNAPSHOT_FILE, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(SNAPSHOT_FILE)
    except (OSError, pd.errors.EmptyDataError):
        return {}
    if len(df) == 0 or "ticker" not in df.columns:
        return {}
    out = df.copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    return {str(row.get("ticker", "")).strip().upper(): row.to_dict() for _, row in out.iterrows() if str(row.get("ticker", "")).strip()}


def _build_engine_payload_from_snapshot(snapshot_row: dict) -> dict:
    action = str(snapshot_row.get("action", "")).strip().lower()
    return {
        "ticker": str(snapshot_row.get("ticker", "")).strip().upper(),
        "has_data": True,
        "action": action,
        "action_label": ACTION_LABELS.get(action, "待確認"),
        "size_fraction": _safe_float(snapshot_row.get("size_fraction"), 0.0),
        "reason": _clip_text(snapshot_row.get("reason_summary") or "", 90),
        "close": _safe_float(snapshot_row.get("close"), 0.0),
        "dynamic_avwap": _safe_float(snapshot_row.get("dynamic_avwap"), 0.0),
        "sqzmom_hist": _safe_float(snapshot_row.get("sqzmom_hist"), 0.0),
        "sqzmom_color": str(snapshot_row.get("sqzmom_color", "")).strip(),
        "sqz_release": bool(snapshot_row.get("sqz_release", False)),
        "signal_ts": str(snapshot_row.get("signal_ts", "")).strip(),
    }


def _build_engine_payload_live(ticker: str, positions_df: pd.DataFrame) -> dict:
    bars = _fetch_intraday_bars(ticker, INTRADAY_PERIOD, INTRADAY_INTERVAL, True)
    if len(bars) < 60:
        return {"ticker": ticker, "has_data": False, "action": "", "action_label": "待確認"}

    enriched = add_intraday_indicators(bars)
    valid = enriched.dropna(subset=["dynamic_avwap", "sqzmom_hist"]).copy()
    if len(valid) < 2:
        return {"ticker": ticker, "has_data": False, "action": "", "action_label": "待確認"}

    latest = valid.iloc[-1]
    previous = valid.iloc[-2]
    position = get_position(positions_df, ticker)
    action, size_fraction, reason = _classify_action(latest, previous, position)
    return {
        "ticker": ticker,
        "has_data": True,
        "action": action,
        "action_label": ACTION_LABELS.get(action, "待確認"),
        "size_fraction": float(size_fraction or 0.0),
        "reason": _clip_text(reason or "", 90),
        "close": _safe_float(latest.get("Close"), 0.0),
        "dynamic_avwap": _safe_float(latest.get("dynamic_avwap"), 0.0),
        "sqzmom_hist": _safe_float(latest.get("sqzmom_hist"), 0.0),
        "sqzmom_color": str(latest.get("sqzmom_color", "")).strip(),
        "sqz_release": bool(latest.get("sqz_release", False)),
        "signal_ts": str(latest.get("Datetime", "")).strip(),
    }


def _build_engine_payload(ticker: str, positions_df: pd.DataFrame, snapshot_map: Dict[str, dict]) -> dict:
    snapshot_row = snapshot_map.get(ticker)
    if snapshot_row is not None:
        return _build_engine_payload_from_snapshot(snapshot_row)
    return _build_engine_payload_live(ticker, positions_df)


def _tv_payload_for(ticker: str, signal_map: Dict[str, object]) -> dict:
    event = signal_map.get(ticker)
    if not event:
        return {}
    return {
        "source": str(getattr(event, "source", "")).strip(),
        "event": str(getattr(event, "event", "")).strip(),
        "timeframe": str(getattr(event, "timeframe", "")).strip(),
        "close": _safe_float(getattr(event, "close", None), 0.0),
        "vwap": _safe_float(getattr(event, "vwap", None), 0.0),
        "sqzmom_value": _safe_float(getattr(event, "sqzmom_value", None), 0.0),
        "sqzmom_color": str(getattr(event, "sqzmom_color", "")).strip(),
        "ts": str(getattr(event, "ts", "")).strip(),
    }


def _resolve_universe(saved_tickers: List[str], extra_tickers: List[str]) -> List[str]:
    decision_df = _load_decision_df()
    shadow_df = load_shadow_decision_df(decision_df)
    positions_df = load_positions()
    decision_tickers = [str(value).strip().upper() for value in decision_df["ticker"].head(DEFAULT_AI_DECISION_TOP_N).tolist()] if len(decision_df) > 0 else []
    shadow_tickers = [str(value).strip().upper() for value in shadow_df["ticker"].tolist()] if len(shadow_df) > 0 else []
    position_tickers = [str(value).strip().upper() for value in positions_df["ticker"].dropna().tolist()] if len(positions_df) > 0 else []
    ordered = decision_tickers + position_tickers + shadow_tickers + saved_tickers + extra_tickers
    out: List[str] = []
    for ticker in ordered:
        if not ticker or ticker in out:
            continue
        out.append(ticker)
        if len(out) >= MAX_TRACKED_TICKERS:
            break
    return out


def _build_watch_payload(tickers: List[str], saved_tickers: List[str], extra_tickers: List[str]) -> dict:
    decision_map = _load_decision_map()
    positions_df = load_positions()
    signal_map = _load_signal_map()
    snapshot_map = _load_intraday_snapshot_map()

    items: List[dict] = []
    for ticker in tickers:
        decision = decision_map.get(ticker, {})
        position = get_position(positions_df, ticker)
        news = _tavily_search(f"{ticker} stock news premarket catalyst", max_results=max(1, min(int(CATALYST_TAVILY_MAX_RESULTS), 2)))
        has_position = bool(position is not None and _safe_float(position.get("quantity", 0.0), 0.0) > 0)
        monitor_priority = str(decision.get("monitor_priority") or ("今天主監控" if (has_position or ticker in extra_tickers or ticker in saved_tickers) else "今天主監控")).strip()
        items.append(
            {
                "ticker": ticker,
                "decision": {
                    "in_ai_decision": bool(decision),
                    "rank": int(pd.to_numeric(decision.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(decision.get("rank"), errors="coerce")) else 9999),
                    "decision_tag": str(decision.get("decision_tag", "")).strip().lower(),
                    "risk_level": str(decision.get("risk_level", "")).strip(),
                    "reason_summary": _clip_text(decision.get("reason_summary") or "", 100),
                    "catalyst_summary": _clip_text(decision.get("catalyst_summary") or "", 100),
                    "monitor_priority": monitor_priority,
                    "shadow_age_days": int(pd.to_numeric(decision.get("shadow_age_days"), errors="coerce") if pd.notna(pd.to_numeric(decision.get("shadow_age_days"), errors="coerce")) else 0),
                    "shadow_decay_score": float(pd.to_numeric(decision.get("shadow_decay_score"), errors="coerce") if pd.notna(pd.to_numeric(decision.get("shadow_decay_score"), errors="coerce")) else 0.0),
                },
                "position": {
                    "has_position": has_position,
                    "quantity": _safe_float(position.get("quantity", 0.0), 0.0) if position is not None else 0.0,
                    "avg_cost": _safe_float(position.get("avg_cost", 0.0), 0.0) if position is not None else 0.0,
                },
                "tv_signal": _tv_payload_for(ticker, signal_map),
                "engine": _build_engine_payload(ticker, positions_df, snapshot_map),
                "recent_execution": _load_recent_execution_rows(ticker, limit=3),
                "news": [
                    {
                        "title": _clip_text(item.get("title") or "", 90),
                        "content": _clip_text(item.get("content") or "", 110),
                    }
                    for item in news[:2]
                ],
                "source_flags": {
                    "saved_watchlist": ticker in saved_tickers,
                    "ad_hoc_watchlist": ticker in extra_tickers,
                    "ai_decision": bool(decision),
                    "open_position": has_position,
                },
                "monitor_priority": monitor_priority,
            }
        )

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
    }


def _gemini_watchlist_summary(payload: dict) -> dict:
    if not GEMINI_API_KEY:
        return {}
    prompt = (
        "You are a premarket trading decision engine for a human operator. "
        "Use only the provided facts. "
        "Do not invent news, prices, signals, or rankings. "
        "Combine current ai_decision candidates, open positions, and saved watchlist names into one clean ranking. "
        "Each ticker also carries a monitor_priority of 今天主監控 or 延續觀察. Preserve that distinction in the final card. "
        "Do not mention whether a ticker came from ai_decision, positions, or watchlist. "
        "Do not mention raw news headlines, search process, data collection process, or comparison process. "
        "Only output the strongest actionable names. Omit weak or unclear names. "
        "Risk control comes before fresh buying. "
        "If a ticker shows stop-loss, take-profit, or weak opening confirmation, prioritize that in risk_flags and action_plan before new long ideas. "
        "Only put clear buy candidates in priority_order. "
        "If many names are bullish, rank them from highest to lowest priority using cleaner engine continuation, stronger news, cleaner momentum, and better existing ai_decision rank. "
        "Do not recommend buying every bullish ticker. "
        "This is not a market commentary. It is a trading conclusion card. "
        "Return strict JSON only with keys: headline, decision, priority_order, risk_flags, action_plan. "
        "headline must be a very short Traditional Chinese title of at most 12 characters. "
        "decision must be one short Traditional Chinese sentence that states the conclusion directly, without filler. "
        "priority_order, risk_flags, action_plan must each be arrays with 0 to 4 short Traditional Chinese strings. "
        "Each item must start with the ticker symbol when a ticker is involved. "
        "Use imperative, trade-ready wording. Avoid abstract summary language like 整理完成, 已彙整, 已分析. "
        "If there is no strong long candidate, leave priority_order empty instead of forcing one.\n\n"
        f"Data JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    request_payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(endpoint, json=request_payload, timeout=max(float(RECAP_GEMINI_TIMEOUT_SEC), 8.0))
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
        return [_clip_text(item, 90) for item in value if str(item or "").strip()][:5]

    return {
        "headline": _clip_text(parsed.get("headline") or "", 80),
        "summary": _clip_text(parsed.get("decision") or parsed.get("summary") or "", 120),
        "priority_order": _list_field("priority_order"),
        "risk_flags": _list_field("risk_flags"),
        "action_plan": _list_field("action_plan"),
    }


def _fallback_watchlist_summary(payload: dict) -> dict:
    scored: List[tuple[float, dict]] = []
    risk_flags: List[str] = []
    action_plan: List[str] = []

    for item in payload.get("items", []):
        ticker = str(item.get("ticker", "")).strip().upper()
        decision = item.get("decision", {}) if isinstance(item.get("decision"), dict) else {}
        engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
        news = item.get("news", []) if isinstance(item.get("news"), list) else []
        monitor_priority = str(decision.get("monitor_priority") or item.get("monitor_priority") or "今天主監控").strip()
        action = str(engine.get("action", "")).strip().lower()
        score = 0.0
        if action == "add":
            score += 40
        elif action == "entry":
            score += 30
        elif action == "take_profit":
            score -= 10
        elif action == "stop_loss":
            score -= 20
        rank_value = int(pd.to_numeric(decision.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(decision.get("rank"), errors="coerce")) else 9999)
        if rank_value < 9999:
            score += max(0, 20 - rank_value * 3)
        score += float(pd.to_numeric(decision.get("shadow_decay_score"), errors="coerce") if pd.notna(pd.to_numeric(decision.get("shadow_decay_score"), errors="coerce")) else 0.0)
        score += min(len(news), 2) * 2
        if str(item.get("tv_signal", {}).get("event", "")).strip().lower() in {"entry", "add", "buy", "breakout"}:
            score += 8
        scored.append((score, item))

        if action in {"stop_loss", "take_profit"}:
            risk_flags.append(f"{ticker}: {engine.get('action_label', '先處理風險')}")
            action_plan.append(f"{ticker}: {engine.get('reason') or '先處理風險'}")

    scored.sort(key=lambda pair: pair[0], reverse=True)
    priority_order = []
    for score, item in scored:
        ticker = str(item.get("ticker", "")).strip().upper()
        engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
        if score < 20:
            continue
        if str(engine.get("action", "")).strip().lower() not in {"add", "entry"}:
            continue
        priority_order.append(f"{ticker}: {monitor_priority}，{engine.get('action_label', '可觀察')}，{engine.get('reason') or '優先開圖確認'}")
        if len(priority_order) >= 5:
            break

    if not action_plan:
        for _, item in scored[:3]:
            ticker = str(item.get("ticker", "")).strip().upper()
            engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
            if str(engine.get("action", "")).strip().lower() in {"stop_loss", "take_profit"}:
                action_plan.append(f"{ticker}: {engine.get('reason') or '先看風險'}")
            elif str(engine.get("action", "")).strip().lower() in {"add", "entry"}:
                action_plan.append(f"{ticker}: {engine.get('reason') or '優先開圖確認'}")

    headline = "交易結論卡"
    summary = "只看先做什麼、先避開什麼、先盯哪幾檔。"
    return {
        "headline": headline,
        "summary": summary,
        "priority_order": priority_order[:5],
        "risk_flags": risk_flags[:5],
        "action_plan": action_plan[:5],
    }


def _priority_map(payload: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in payload.get("items", []):
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        label = str(item.get("monitor_priority") or item.get("decision", {}).get("monitor_priority") or "今天主監控").strip()
        out[ticker] = label or "今天主監控"
    return out


def _decorate_priority_item(text: str, priority_map: Dict[str, str]) -> str:
    raw = str(text or "").strip()
    if not raw:
        return raw
    for ticker, label in priority_map.items():
        if not raw.startswith(ticker):
            continue
        remainder = raw[len(ticker):].lstrip()
        if remainder.startswith(f"[{label}]"):
            return raw
        if remainder.startswith(":") or remainder.startswith("："):
            return f"{ticker} [{label}]{remainder}"
        if remainder:
            return f"{ticker} [{label}]: {remainder}"
        return f"{ticker} [{label}]"
    return raw


def build_watchlist_brief_message(raw_tickers: str = "", saved_tickers: List[str] | None = None) -> str:
    saved = _parse_tickers(" ".join(saved_tickers or []), limit=MAX_TRACKED_TICKERS)
    extra = _parse_tickers(raw_tickers, limit=MAX_TRACKED_TICKERS)
    tickers = _resolve_universe(saved, extra)
    if not tickers:
        raise ValueError("目前沒有可分析的股票。先用 /watchadd 新增關注股，或直接輸入 ticker。")

    payload = _build_watch_payload(tickers, saved, extra)
    summary = _gemini_watchlist_summary(payload) or _fallback_watchlist_summary(payload)
    priority_map = _priority_map(payload)

    lines = [
        f"[Alpha Finder] 交易結論卡 {payload.get('generated_at', '')}",
        "",
    ]
    headline = str(summary.get("headline", "")).strip()
    message_summary = str(summary.get("summary", "")).strip()
    if headline:
        lines.append(headline)
    if message_summary:
        lines.append(message_summary)
    priority_order = [_decorate_priority_item(item, priority_map) for item in (summary.get("priority_order", []) if isinstance(summary.get("priority_order", []), list) else [])]
    risk_flags = [_decorate_priority_item(item, priority_map) for item in (summary.get("risk_flags", []) if isinstance(summary.get("risk_flags", []), list) else [])]
    action_plan = [_decorate_priority_item(item, priority_map) for item in (summary.get("action_plan", []) if isinstance(summary.get("action_plan", []), list) else [])]
    if priority_order:
        lines.append("先看標的:")
        for idx, item in enumerate(priority_order, 1):
            lines.append(f"- {idx}. {item}")
    if risk_flags:
        lines.append("先避開 / 先處理:")
        for item in risk_flags:
            lines.append(f"- {item}")
    if action_plan:
        lines.append("執行動作:")
        for item in action_plan:
            lines.append(f"- {item}")
    return "\n".join(lines)