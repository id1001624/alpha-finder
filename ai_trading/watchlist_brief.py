from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd
import requests

from prompt_safety import sanitize_prompt_payload

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
from turso_state import (
    STATE_KEY_AI_DECISION_LATEST,
    load_all_saved_watchlist_states,
    load_recent_execution_log,
    load_runtime_df_with_fallback,
    load_saved_watchlist_state,
    sync_saved_watchlist_state,
)

from .intraday_execution_engine import AI_DECISION_LATEST, SNAPSHOT_FILE, _classify_action, _fetch_intraday_bars
from .intraday_indicators import add_intraday_indicators
from .market_session import get_intraday_active_window
from .position_state import get_position_by_profile, load_positions
from .shadow_watchlist import build_decision_universe_df, load_shadow_decision_df
from .strategy_context import (
    classify_watch_horizon,
    classify_watch_stance,
    default_strategy_for_horizon,
    detect_regime_tag,
)


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
MAX_TRACKED_TICKERS = 20
ENGINE_SIGNAL_MAX_AGE_MINUTES = 90


def _clip_text(value: object, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _is_current_engine_signal(signal_ts: object) -> bool:
    text = str(signal_ts or "").strip()
    if not text:
        return False

    session = get_intraday_active_window()
    if not bool(session.get("is_active", False)):
        return False

    try:
        parsed = pd.to_datetime(text, utc=True)
    except (ValueError, TypeError):
        return False
    if pd.isna(parsed):
        return False

    parsed_dt = parsed.to_pydatetime()
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    signal_utc = parsed_dt.astimezone(timezone.utc)
    window_start_utc = session.get("active_start_utc")
    window_end_utc = session.get("active_end_utc")
    if not isinstance(window_start_utc, datetime) or not isinstance(window_end_utc, datetime):
        return False
    age = session.get("now_utc") - signal_utc
    if not isinstance(age, timedelta):
        return False
    return window_start_utc <= signal_utc <= window_end_utc and timedelta(0) <= age <= timedelta(minutes=ENGINE_SIGNAL_MAX_AGE_MINUTES)


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


def _sync_saved_watchlist_to_shared(user_id: int | str, tickers: List[str], updated_at: str = "") -> None:
    sync_saved_watchlist_state(user_id, tickers, updated_at=updated_at)


def _sync_local_store_to_shared_if_needed() -> None:
    shared_df = load_all_saved_watchlist_states()
    if len(shared_df) > 0:
        return
    store = _load_watchlist_store()
    users = store.get("users", {}) if isinstance(store, dict) else {}
    if not isinstance(users, dict):
        return
    for user_id, entry in users.items():
        if not isinstance(entry, dict):
            continue
        tickers = entry.get("tickers", [])
        if not isinstance(tickers, list):
            continue
        parsed = _parse_tickers(" ".join(str(item) for item in tickers), limit=MAX_TRACKED_TICKERS)
        updated_at = str(entry.get("updated_at", "")).strip()
        _sync_saved_watchlist_to_shared(user_id, parsed, updated_at=updated_at)


def load_saved_watchlist(user_id: int | str) -> List[str]:
    shared = load_saved_watchlist_state(user_id)
    if shared is not None:
        return _parse_tickers(" ".join(str(item) for item in shared), limit=MAX_TRACKED_TICKERS)

    store = _load_watchlist_store()
    entry = store.get("users", {}).get(str(user_id), {})
    tickers = entry.get("tickers", []) if isinstance(entry, dict) else []
    if not isinstance(tickers, list):
        return []
    parsed = _parse_tickers(" ".join(str(item) for item in tickers), limit=MAX_TRACKED_TICKERS)
    if parsed:
        _sync_saved_watchlist_to_shared(user_id, parsed, updated_at=str(entry.get("updated_at", "")).strip())
    return parsed


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
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store.setdefault("users", {})[str(user_id)] = {
        "tickers": merged,
        "updated_at": updated_at,
    }
    _save_watchlist_store(store)
    _sync_saved_watchlist_to_shared(user_id, merged, updated_at=updated_at)
    return merged


def remove_saved_watchlist_tickers(user_id: int | str, raw_tickers: str) -> List[str]:
    removals = set(_parse_tickers(raw_tickers))
    if not removals:
        raise ValueError("請提供至少一個要移除的股票代號，例如 AAPL")
    current = load_saved_watchlist(user_id)
    remaining = [ticker for ticker in current if ticker not in removals]
    store = _load_watchlist_store()
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store.setdefault("users", {})[str(user_id)] = {
        "tickers": remaining,
        "updated_at": updated_at,
    }
    _save_watchlist_store(store)
    _sync_saved_watchlist_to_shared(user_id, remaining, updated_at=updated_at)
    return remaining


def load_all_saved_watchlist_tickers(limit: int = MAX_TRACKED_TICKERS) -> List[str]:
    ordered_entries: List[tuple[str, List[str]]] = []
    shared_df = load_all_saved_watchlist_states()
    if len(shared_df) == 0:
        _sync_local_store_to_shared_if_needed()
        shared_df = load_all_saved_watchlist_states()

    if len(shared_df) > 0:
        for _, row in shared_df.iterrows():
            tickers_json = str(row.get("tickers_json", "[]"))
            try:
                tickers = json.loads(tickers_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                tickers = []
            if not isinstance(tickers, list):
                tickers = []
            ordered_entries.append(
                (
                    str(row.get("updated_at", "")).strip(),
                    [str(item or "").strip() for item in tickers],
                )
            )
    else:
        store = _load_watchlist_store()
        users = store.get("users", {})
        if not isinstance(users, dict):
            return []
        for entry in users.values():
            if not isinstance(entry, dict):
                continue
            tickers = entry.get("tickers", [])
            if not isinstance(tickers, list):
                continue
            ordered_entries.append(
                (
                    str(entry.get("updated_at", "")).strip(),
                    [str(item or "").strip() for item in tickers],
                )
            )

    ordered_entries.sort(key=lambda item: item[0], reverse=True)
    merged: List[str] = []
    for _, raw_tickers in ordered_entries:
        parsed = _parse_tickers(" ".join(raw_tickers), limit=limit)
        for ticker in parsed:
            if ticker in merged:
                continue
            merged.append(ticker)
            if len(merged) >= limit:
                return merged
    return merged


def format_saved_watchlist_message(user_id: int | str) -> str:
    tickers = load_saved_watchlist(user_id)
    if not tickers:
        return "你目前沒有保存的關注股。可用 /watchadd AAPL NVDA 新增。"
    lines = ["你保存的關注股:"]
    for ticker in tickers:
        lines.append(f"- {ticker}")
    return "\n".join(lines)


def _count_open_positions(positions_df: pd.DataFrame) -> int:
    if len(positions_df) == 0 or "quantity" not in positions_df.columns:
        return 0
    qty = pd.to_numeric(positions_df["quantity"], errors="coerce").fillna(0.0)
    return int((qty > 0).sum())


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
        "signal_type": str(snapshot_row.get("signal_type", "")).strip(),
        "size_fraction": _safe_float(snapshot_row.get("size_fraction"), 0.0),
        "reason": _clip_text(snapshot_row.get("reason_summary") or "", 90),
        "close": _safe_float(snapshot_row.get("close"), 0.0),
        "dynamic_avwap": _safe_float(snapshot_row.get("dynamic_avwap"), 0.0),
        "sqzmom_hist": _safe_float(snapshot_row.get("sqzmom_hist"), 0.0),
        "sqzmom_color": str(snapshot_row.get("sqzmom_color", "")).strip(),
        "sqz_release": bool(snapshot_row.get("sqz_release", False)),
        "signal_ts": str(snapshot_row.get("signal_ts", "")).strip(),
    }


def _build_engine_payload_live(ticker: str, positions_df: pd.DataFrame, decision: dict | None = None) -> dict:
    bars = _fetch_intraday_bars(ticker, INTRADAY_PERIOD, INTRADAY_INTERVAL, True)
    if len(bars) < 60:
        return {"ticker": ticker, "has_data": False, "action": "", "action_label": "待確認"}

    enriched = add_intraday_indicators(bars)
    valid = enriched.dropna(subset=["dynamic_avwap", "sqzmom_hist"]).copy()
    if len(valid) < 2:
        return {"ticker": ticker, "has_data": False, "action": "", "action_label": "待確認"}

    latest = valid.iloc[-1]
    previous = valid.iloc[-2]
    signal_ts = str(latest.get("Datetime", "")).strip()
    if not _is_current_engine_signal(signal_ts):
        return {"ticker": ticker, "has_data": False, "action": "", "action_label": "待確認", "signal_ts": signal_ts}

    horizon = classify_watch_horizon(ticker, decision)
    strategy = default_strategy_for_horizon(horizon)
    position = get_position_by_profile(positions_df, ticker, horizon_tag=horizon, strategy_profile=strategy)
    meta = pd.Series(
        {
            "ticker": ticker,
            "rank": (decision or {}).get("rank", 9999),
            "decision_tag": str((decision or {}).get("decision_tag", "")).strip().lower(),
            "risk_level": str((decision or {}).get("risk_level", "")).strip(),
            "confidence": pd.to_numeric((decision or {}).get("confidence"), errors="coerce"),
            "api_final_score": pd.to_numeric((decision or {}).get("api_final_score"), errors="coerce"),
            "horizon_tag": horizon,
            "strategy_profile": strategy,
        }
    )
    session_context = get_intraday_active_window(datetime.now(timezone.utc))
    action, size_fraction, reason, signal_type = _classify_action(
        latest,
        previous,
        position,
        meta,
        session_context,
        pd.DataFrame(),
        positions_df,
        _count_open_positions(positions_df),
        0,
        0,
        detect_regime_tag(),
        valid,
    )
    return {
        "ticker": ticker,
        "has_data": True,
        "action": action,
        "action_label": ACTION_LABELS.get(action, "待確認"),
        "signal_type": signal_type,
        "size_fraction": float(size_fraction or 0.0),
        "reason": _clip_text(reason or "", 90),
        "close": _safe_float(latest.get("Close"), 0.0),
        "dynamic_avwap": _safe_float(latest.get("dynamic_avwap"), 0.0),
        "sqzmom_hist": _safe_float(latest.get("sqzmom_hist"), 0.0),
        "sqzmom_color": str(latest.get("sqzmom_color", "")).strip(),
        "sqz_release": bool(latest.get("sqz_release", False)),
        "signal_ts": signal_ts,
    }


def _build_engine_payload(ticker: str, positions_df: pd.DataFrame, snapshot_map: Dict[str, dict], decision: dict | None = None) -> dict:
    snapshot_row = snapshot_map.get(ticker)
    if snapshot_row is not None and _is_current_engine_signal(snapshot_row.get("signal_ts")):
        return _build_engine_payload_from_snapshot(snapshot_row)
    return _build_engine_payload_live(ticker, positions_df, decision=decision)


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
    # Keep user-owned watch names in the first batch so they are not trimmed away by caps.
    ordered = decision_tickers + position_tickers + saved_tickers + extra_tickers + shadow_tickers
    out: List[str] = []
    for ticker in ordered:
        if not ticker or ticker in out:
            continue
        out.append(ticker)
        if len(out) >= MAX_TRACKED_TICKERS:
            break
    return out


def _build_universe_context_lines(payload: dict, saved_tickers: List[str], extra_tickers: List[str]) -> List[str]:
    items = payload.get("items", []) if isinstance(payload.get("items"), list) else []
    selected = [str(item.get("ticker", "")).strip().upper() for item in items if str(item.get("ticker", "")).strip()]
    if not selected:
        return []

    saved_clean = [str(ticker or "").strip().upper() for ticker in saved_tickers if str(ticker or "").strip()]
    extra_clean = [str(ticker or "").strip().upper() for ticker in extra_tickers if str(ticker or "").strip()]
    saved_included = [ticker for ticker in saved_clean if ticker in selected]
    saved_missing = [ticker for ticker in saved_clean if ticker not in selected]
    extra_included = [ticker for ticker in extra_clean if ticker in selected]

    today_main: List[str] = []
    continuity: List[str] = []
    for item in items:
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        label = str(item.get("monitor_priority") or item.get("decision", {}).get("monitor_priority") or "今天主監控").strip() or "今天主監控"
        if label == "延續觀察":
            continuity.append(ticker)
        else:
            today_main.append(ticker)

    def _join_limit(values: List[str], limit: int = 8) -> str:
        unique: List[str] = []
        for value in values:
            if value and value not in unique:
                unique.append(value)
        if not unique:
            return "-"
        text = ", ".join(unique[:limit])
        if len(unique) > limit:
            text += f" ...(+{len(unique) - limit})"
        return text

    lines = ["本輪整合清單:"]
    lines.append(f"- 今天主監控: {_join_limit(today_main)}")
    lines.append(f"- 延續觀察: {_join_limit(continuity)}")
    lines.append(f"- 保存關注股(已納入): {_join_limit(saved_included)}")
    if extra_clean:
        lines.append(f"- 臨時輸入(已納入): {_join_limit(extra_included)}")
    if saved_missing:
        lines.append(f"- 保存關注股(未納入，本輪上限 {MAX_TRACKED_TICKERS}): {_join_limit(saved_missing)}")
    return lines


def _build_watch_payload(tickers: List[str], saved_tickers: List[str], extra_tickers: List[str]) -> dict:
    decision_map = _load_decision_map()
    positions_df = load_positions()
    signal_map = _load_signal_map()
    snapshot_map = _load_intraday_snapshot_map()

    items: List[dict] = []
    for ticker in tickers:
        decision = decision_map.get(ticker, {})
        horizon = classify_watch_horizon(ticker, decision)
        strategy = default_strategy_for_horizon(horizon)
        position = get_position_by_profile(positions_df, ticker, horizon_tag=horizon, strategy_profile=strategy)
        news = _tavily_search(f"{ticker} stock news premarket catalyst", max_results=max(1, min(int(CATALYST_TAVILY_MAX_RESULTS), 2)))
        has_position = bool(position is not None and _safe_float(position.get("quantity", 0.0), 0.0) > 0)
        monitor_priority = str(decision.get("monitor_priority") or ("今天主監控" if (has_position or ticker in extra_tickers or ticker in saved_tickers) else "延續觀察")).strip()
        engine_payload = _build_engine_payload(ticker, positions_df, snapshot_map, decision=decision)
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
                "engine": engine_payload,
                "recent_execution": _load_recent_execution_rows(ticker, limit=3),
                "news": [
                    {
                        "title": _clip_text(item.get("title") or "", 90),
                        "content": _clip_text(item.get("content") or "", 110),
                    }
                    for item in news[:2]
                ],
                "horizon_tag": horizon,
                "strategy_profile": strategy,
                "stance": classify_watch_stance(horizon, engine_payload, decision),
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
        f"Data JSON:\n{json.dumps(sanitize_prompt_payload(payload), ensure_ascii=False)}"
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    request_payload = {
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
            json=request_payload,
            timeout=max(float(RECAP_GEMINI_TIMEOUT_SEC), 8.0),
        )
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
    """Tiered scoring: risk first > engine+catalyst > engine only > AI rank > continuity."""
    scored: List[tuple[float, dict]] = []
    risk_flags: List[str] = []
    action_plan: List[str] = []
    engine_data_count = 0

    for item in payload.get("items", []):
        ticker = str(item.get("ticker", "")).strip().upper()
        decision = item.get("decision", {}) if isinstance(item.get("decision"), dict) else {}
        engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
        news = item.get("news", []) if isinstance(item.get("news"), list) else []
        position = item.get("position", {}) if isinstance(item.get("position"), dict) else {}
        action = str(engine.get("action", "")).strip().lower()
        has_data = bool(engine.get("has_data", False))

        if has_data:
            engine_data_count += 1

        # --- Risk items: separate to risk_flags, do NOT compete for priority ---
        if action in {"stop_loss", "take_profit"}:
            risk_flags.append(f"{ticker}: {engine.get('action_label', '先處理風險')}")
            action_plan.append(f"{ticker}: {engine.get('reason') or '先處理風險'}")
            continue

        score = 0.0

        # Engine signal (strongest hard signal when available)
        if action == "add":
            score += 30
        elif action == "entry":
            score += 25

        # AI decision rank (tiered — Top 1 clearly wins)
        rank_value = int(pd.to_numeric(decision.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(decision.get("rank"), errors="coerce")) else 9999)
        rank_scores = {1: 20, 2: 16, 3: 12, 4: 8, 5: 4}
        score += rank_scores.get(rank_value, 0)

        # News catalyst (key pre-market factor, significant weight)
        score += min(len(news), 2) * 10

        # TV signal
        if str(item.get("tv_signal", {}).get("event", "")).strip().lower() in {"entry", "add", "buy", "breakout"}:
            score += 8

        # Has open position (already committed capital → pay more attention)
        if bool(position.get("has_position", False)):
            score += 5

        # Shadow decay (natural diminishing for aged tickers)
        score += float(pd.to_numeric(decision.get("shadow_decay_score"), errors="coerce") if pd.notna(pd.to_numeric(decision.get("shadow_decay_score"), errors="coerce")) else 0.0)

        scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    has_engine = engine_data_count > 0

    # --- Build priority_order ---
    priority_order = []
    for score, item in scored:
        ticker = str(item.get("ticker", "")).strip().upper()
        engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
        item_decision = item.get("decision", {}) if isinstance(item.get("decision"), dict) else {}
        item_priority = str(item_decision.get("monitor_priority") or item.get("monitor_priority") or "今天主監控").strip()
        item_rank = int(pd.to_numeric(item_decision.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(item_decision.get("rank"), errors="coerce")) else 9999)
        item_action = str(engine.get("action", "")).strip().lower()

        if has_engine:
            if score < 15:
                continue
            if item_action in {"add", "entry"}:
                label = engine.get("action_label", "可觀察")
                reason = engine.get("reason") or "優先開圖確認"
            else:
                label = "待確認"
                reason = "開圖確認指標"
        else:
            if score < 10:
                continue
            label = f"AI Rank {item_rank}" if item_rank < 9999 else "關注中"
            reason = "盤前待觀察"

        priority_order.append(f"{ticker}: {item_priority}，{label}，{reason}")
        if len(priority_order) >= 5:
            break

    # --- Build action_plan if not already populated by risk items ---
    if not action_plan:
        for _, item in scored[:3]:
            ticker = str(item.get("ticker", "")).strip().upper()
            engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
            item_action = str(engine.get("action", "")).strip().lower()
            if item_action in {"add", "entry"}:
                action_plan.append(f"{ticker}: {engine.get('reason') or '優先開圖確認'}")
            elif not has_engine:
                item_news = item.get("news", []) if isinstance(item.get("news"), list) else []
                if item_news:
                    action_plan.append(f"{ticker}: 有盤前新聞，開盤後優先確認")
                else:
                    action_plan.append(f"{ticker}: 開盤後確認指標")

    headline = "交易結論卡"
    if not has_engine:
        summary = "⚠️ 目前無盤中指標資料，排序依 AI 排名與新聞。開盤 30 分鐘後再看一次。"
    else:
        summary = "只看先做什麼、先避開什麼、先盯哪幾檔。"
    return {
        "headline": headline,
        "summary": summary,
        "priority_order": priority_order[:5],
        "risk_flags": risk_flags[:5],
        "action_plan": action_plan[:5],
    }


def _gemini_saved_watchlist_followup_summary(payload: dict) -> dict:
    if not GEMINI_API_KEY:
        return {}
    prompt = (
        "You are a watchlist follow-up assistant for a human swing trader. "
        "Use only the provided facts. "
        "These names come from the user's saved watchlist, not the formal auto-trade chain. "
        "Do not turn this into a market commentary or a full portfolio review. "
        "Do not say a name is a formal new-entry order. "
        "Focus only on which names are still continuing cleanly, which names are rebuilding and worth re-entry observation, and which names should not be chased yet. "
        "Do not mention search process, data collection process, or raw news headlines. "
        "Return strict JSON only with keys: headline, decision, priority_order, risk_flags, action_plan. "
        "headline must be a very short Traditional Chinese title of at most 12 characters. "
        "decision must be one short Traditional Chinese sentence that directly states the conclusion, and must imply this is follow-up only, not a formal buy order. "
        "priority_order, risk_flags, action_plan must each be arrays with 0 to 4 short Traditional Chinese strings. "
        "Each item must start with the ticker symbol when a ticker is involved. "
        "priority_order should only include names worth continuation or re-entry observation. "
        "risk_flags should include names that are still weak, failed, or should not be chased. "
        "action_plan should tell the user exactly what to do next, such as 留觀察、等站回 AVWAP、等開盤確認.\n\n"
        f"Data JSON:\n{json.dumps(sanitize_prompt_payload(payload), ensure_ascii=False)}"
    )
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    request_payload = {
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
            json=request_payload,
            timeout=max(float(RECAP_GEMINI_TIMEOUT_SEC), 8.0),
        )
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


def _fallback_saved_watchlist_followup_summary(payload: dict) -> dict:
    scored: List[tuple[float, str, str, dict]] = []
    risk_flags: List[str] = []
    action_plan: List[str] = []
    engine_data_count = 0

    for item in payload.get("items", []):
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            continue

        engine = item.get("engine", {}) if isinstance(item.get("engine"), dict) else {}
        news = item.get("news", []) if isinstance(item.get("news"), list) else []
        tv_signal = item.get("tv_signal", {}) if isinstance(item.get("tv_signal"), dict) else {}
        decision = item.get("decision", {}) if isinstance(item.get("decision"), dict) else {}
        position = item.get("position", {}) if isinstance(item.get("position"), dict) else {}
        has_position = bool(position.get("has_position", False))
        action = str(engine.get("action", "")).strip().lower()
        has_data = bool(engine.get("has_data", False))
        close_val = _safe_float(engine.get("close"), 0.0)
        avwap = _safe_float(engine.get("dynamic_avwap"), 0.0)
        hist = _safe_float(engine.get("sqzmom_hist"), 0.0)
        rank_num = pd.to_numeric(decision.get("rank"), errors="coerce")
        rank_value = int(rank_num) if pd.notna(rank_num) else 9999

        if has_data:
            engine_data_count += 1

        if action in {"stop_loss", "take_profit"}:
            risk_text = engine.get("reason") or "短線結構還沒修回來，先別追。"
            risk_flags.append(f"{ticker}: {risk_text}")
            if has_position:
                action_plan.append(f"{ticker}: 先照部位風控走，等重回 AVWAP 再考慮補回。")
            else:
                action_plan.append(f"{ticker}: 先留觀察，不要把反彈當成正式重啟。")
            continue

        score = 0.0
        label = "保留觀察"
        reason = "等下一次結構轉強再看。"

        if action in {"entry", "add"}:
            score += 30
            label = "重回站上，可列再進場觀察"
            reason = str(engine.get("reason") or "已回到 AVWAP 上方且動能轉強。")
        elif has_data and close_val > 0 and avwap > 0 and close_val >= avwap and hist > 0:
            score += 20
            label = "續強觀察"
            reason = "維持在 AVWAP 上方，動能仍偏正。"
        elif str(tv_signal.get("event", "")).strip().lower() in {"entry", "add", "buy", "breakout"}:
            score += 12
            label = "訊號轉強，待開盤確認"
            reason = "TV 訊號轉強，但仍只列觀察。"
        elif news:
            score += 8
            label = "催化觀察"
            reason = "有新催化，等開盤或下一次回站上再說。"

        if rank_value <= 5:
            score += {1: 10, 2: 8, 3: 6, 4: 4, 5: 2}.get(rank_value, 0)
        if has_position:
            score += 5
            if label == "保留觀察":
                label = "續抱觀察"
                reason = "已有部位，先盯是否維持強勢。"

        scored.append((score, label, reason, item))

    scored.sort(key=lambda row: row[0], reverse=True)

    priority_order: List[str] = []
    for score, label, reason, item in scored:
        if score < 8:
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        priority_order.append(f"{ticker}: {label}，{reason}")
        if len(priority_order) >= 5:
            break

    if not action_plan:
        if priority_order:
            for _, label, reason, item in scored[:3]:
                ticker = str(item.get("ticker", "")).strip().upper()
                if not ticker:
                    continue
                if label in {"重回站上，可列再進場觀察", "續強觀察", "訊號轉強，待開盤確認"}:
                    action_plan.append(f"{ticker}: 保留觀察名單，不直接當正式新倉指令。")
                else:
                    action_plan.append(f"{ticker}: {reason}")
                if len(action_plan) >= 3:
                    break
        else:
            action_plan.append("今天沒有明確重啟結構的關注股，先等下一次回站上。")

    headline = "Watchlist追蹤"
    if engine_data_count > 0:
        summary = "這份只追蹤你 watchadd 的票，供續強與再進場觀察，不等於正式買點指令。"
    else:
        summary = "⚠️ 目前無盤中指標資料；這份只追蹤關注股近況，不等於正式買點指令。"

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

    # Detect whether any ticker has live engine data
    _engine_data_available = any(
        bool((item.get("engine") or {}).get("has_data", False))
        for item in (payload.get("items", []) if isinstance(payload.get("items"), list) else [])
    )

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
    if not _engine_data_available and "⚠️" not in message_summary:
        lines.append("⚠️ 目前無盤中指標資料，排序依 AI 排名與新聞。開盤 30 分鐘後再看一次。")
    context_lines = _build_universe_context_lines(payload, saved, extra)
    if context_lines:
        lines.append("")
        lines.extend(context_lines)
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


def build_saved_watchlist_followup_message(saved_tickers: List[str] | None = None) -> str:
    saved = _parse_tickers(" ".join(saved_tickers or load_all_saved_watchlist_tickers()), limit=MAX_TRACKED_TICKERS)
    if not saved:
        raise ValueError("目前沒有保存的關注股。先用 /watchadd 新增。")

    payload = _build_watch_payload(saved, saved, [])
    summary = _gemini_saved_watchlist_followup_summary(payload) or _fallback_saved_watchlist_followup_summary(payload)
    engine_data_available = any(
        bool((item.get("engine") or {}).get("has_data", False))
        for item in (payload.get("items", []) if isinstance(payload.get("items"), list) else [])
    )

    lines = [
        f"[Alpha Finder] Watchlist Follow-up {payload.get('generated_at', '')}",
        "",
    ]
    headline = str(summary.get("headline", "")).strip()
    message_summary = str(summary.get("summary", "")).strip()
    if headline:
        lines.append(headline)
    if message_summary:
        lines.append(message_summary)
    lines.append(f"追蹤名單: {', '.join(saved)}")
    if not engine_data_available and "⚠️" not in message_summary:
        lines.append("⚠️ 目前無盤中指標資料；先把這張卡當 follow-up，不要當正式新倉指令。")

    priority_order = summary.get("priority_order", []) if isinstance(summary.get("priority_order", []), list) else []
    risk_flags = summary.get("risk_flags", []) if isinstance(summary.get("risk_flags", []), list) else []
    action_plan = summary.get("action_plan", []) if isinstance(summary.get("action_plan", []), list) else []

    if priority_order:
        lines.append("續強 / 再進場觀察:")
        for idx, item in enumerate(priority_order, 1):
            lines.append(f"- {idx}. {item}")
    if risk_flags:
        lines.append("先別追 / 先處理:")
        for item in risk_flags:
            lines.append(f"- {item}")
    if action_plan:
        lines.append("現在先做:")
        for item in action_plan:
            lines.append(f"- {item}")
    return "\n".join(lines)