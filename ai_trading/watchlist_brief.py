from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
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

from .intraday_execution_engine import AI_DECISION_LATEST, _classify_action, _fetch_intraday_bars
from .intraday_indicators import add_intraday_indicators
from .position_state import get_position, load_positions


ACTION_LABELS = {
    "entry": "適合買",
    "add": "可加碼",
    "take_profit": "先減碼",
    "stop_loss": "先降風險",
    "": "待確認",
}


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


def _parse_tickers(raw: str, limit: int = 6) -> List[str]:
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


def _load_decision_map() -> Dict[str, dict]:
    df, _ = load_runtime_df_with_fallback(STATE_KEY_AI_DECISION_LATEST, [AI_DECISION_LATEST])
    if len(df) == 0 or "ticker" not in df.columns:
        return {}
    out = df.copy()
    for col in ["decision_date", "rank", "ticker", "decision_tag", "risk_level", "tech_status", "theme", "reason_summary", "catalyst_summary"]:
        if col not in out.columns:
            out[col] = ""
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out = out[out["ticker"] != ""].copy()
    out = out.sort_values(["rank", "ticker"], ascending=[True, True], na_position="last")
    return {str(row.get("ticker", "")).strip().upper(): row.to_dict() for _, row in out.iterrows() if str(row.get("ticker", "")).strip()}


def _load_signal_map() -> Dict[str, object]:
    try:
        return get_latest_signals(
            SIGNAL_STORE_PATH,
            asof=datetime.now(timezone.utc),
            max_age_minutes=SIGNAL_MAX_AGE_MINUTES,
            require_same_day=SIGNAL_REQUIRE_SAME_DAY,
        )
    except Exception:
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


def _build_engine_payload(ticker: str, positions_df: pd.DataFrame) -> dict:
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


def _build_watch_payload(tickers: List[str]) -> dict:
    decision_map = _load_decision_map()
    positions_df = load_positions()
    signal_map = _load_signal_map()

    items: List[dict] = []
    for ticker in tickers:
        decision = decision_map.get(ticker, {})
        position = get_position(positions_df, ticker)
        news = _tavily_search(f"{ticker} stock news premarket catalyst", max_results=max(1, min(int(CATALYST_TAVILY_MAX_RESULTS), 3)))
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
                },
                "position": {
                    "has_position": bool(position is not None and _safe_float(position.get("quantity", 0.0), 0.0) > 0),
                    "quantity": _safe_float(position.get("quantity", 0.0), 0.0) if position is not None else 0.0,
                    "avg_cost": _safe_float(position.get("avg_cost", 0.0), 0.0) if position is not None else 0.0,
                },
                "tv_signal": _tv_payload_for(ticker, signal_map),
                "engine": _build_engine_payload(ticker, positions_df),
                "recent_execution": _load_recent_execution_rows(ticker, limit=3),
                "news": [
                    {
                        "title": _clip_text(item.get("title") or "", 100),
                        "content": _clip_text(item.get("content") or "", 120),
                    }
                    for item in news[:3]
                ],
            }
        )

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": tickers,
        "items": items,
    }


def _gemini_watchlist_summary(payload: dict) -> dict:
    if not GEMINI_API_KEY:
        return {}
    prompt = (
        "You are a premarket trading assistant for a human operator. "
        "Use only the provided facts. "
        "Do not invent news, prices, signals, or rankings. "
        "Risk control comes before fresh buying. "
        "If a ticker shows stop-loss, take-profit, or weak opening confirmation, prioritize that in risk_flags and action_plan before new long ideas. "
        "Only put clear buy candidates in priority_order. "
        "If many names are bullish, rank them from highest to lowest priority using cleaner engine continuation, stronger news, cleaner momentum, and better existing ai_decision rank. "
        "Do not recommend buying every bullish ticker. "
        "Return strict JSON only with keys: headline, summary, priority_order, risk_flags, action_plan. "
        "headline and summary must be short Traditional Chinese strings. "
        "priority_order, risk_flags, action_plan must each be arrays with 0 to 5 short Traditional Chinese strings. "
        "priority_order items should begin with the ticker symbol.\n\n"
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
        "summary": _clip_text(parsed.get("summary") or "", 120),
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
        score += min(len(news), 3) * 3
        if str(item.get("tv_signal", {}).get("event", "")).strip().lower() in {"entry", "add", "buy", "breakout"}:
            score += 8
        scored.append((score, item))

        if action in {"stop_loss", "take_profit"}:
            risk_flags.append(f"{ticker}: {engine.get('action_label', '先處理風險')}")
            action_plan.append(f"先處理 {ticker}，{engine.get('reason') or '開盤前先看是否要降風險'}")

    scored.sort(key=lambda pair: pair[0], reverse=True)
    priority_order = []
    for score, item in scored:
        ticker = str(item.get("ticker", "")).strip().upper()
        engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
        if str(engine.get("action", "")).strip().lower() not in {"add", "entry"}:
            continue
        priority_order.append(f"{ticker}: {engine.get('action_label', '可觀察')}，{engine.get('reason') or '優先開圖確認'}")
        if len(priority_order) >= 5:
            break

    if not action_plan:
        for _, item in scored[:3]:
            ticker = str(item.get("ticker", "")).strip().upper()
            engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
            action_plan.append(f"{ticker}: {engine.get('reason') or '盤前先看訊號是否延續'}")

    headline = "自選股盤前整理完成"
    summary = "已根據 engine、近期 execution 與新聞整理出風險與優先順序。"
    return {
        "headline": headline,
        "summary": summary,
        "priority_order": priority_order[:5],
        "risk_flags": risk_flags[:5],
        "action_plan": action_plan[:5],
    }


def build_watchlist_brief_message(raw_tickers: str) -> str:
    tickers = _parse_tickers(raw_tickers)
    if not tickers:
        raise ValueError("請提供至少一個股票代號，例如 AAPL NVDA TSLA")

    payload = _build_watch_payload(tickers)
    summary = _gemini_watchlist_summary(payload) or _fallback_watchlist_summary(payload)

    lines = [
        f"[Alpha Finder] 自選關注股盤前分析 {payload.get('generated_at', '')}",
        "",
        f"輸入: {' | '.join(tickers)}",
        "",
    ]
    headline = str(summary.get("headline", "")).strip()
    message_summary = str(summary.get("summary", "")).strip()
    if headline:
        lines.append(headline)
    if message_summary:
        lines.append(message_summary)
    priority_order = summary.get("priority_order", []) if isinstance(summary.get("priority_order", []), list) else []
    risk_flags = summary.get("risk_flags", []) if isinstance(summary.get("risk_flags", []), list) else []
    action_plan = summary.get("action_plan", []) if isinstance(summary.get("action_plan", []), list) else []
    if priority_order:
        lines.append("優先順序:")
        for item in priority_order:
            lines.append(f"- {item}")
    if risk_flags:
        lines.append("風險:")
        for item in risk_flags:
            lines.append(f"- {item}")
    if action_plan:
        lines.append("盤前先做:")
        for item in action_plan:
            lines.append(f"- {item}")
    return "\n".join(lines)