from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
import yfinance as yf

from config import (
    CORE_LIST_TICKERS,
    REGIME_BENCHMARK_TICKER,
    REGIME_RISK_OFF_DAILY_CHANGE_PCT,
    REGIME_RISK_ON_DAILY_CHANGE_PCT,
)

HORIZON_INTRADAY_MONSTER = "intraday_monster"
HORIZON_SWING_CORE = "swing_core"

STRATEGY_MONSTER_SWING = "monster_swing"
STRATEGY_SWING_TREND = "swing_trend"

REGIME_RISK_ON = "risk_on"
REGIME_NEUTRAL = "neutral"
REGIME_RISK_OFF = "risk_off"

SIGNAL_ENTRY_IGNITION = "entry_ignition"
SIGNAL_ENTRY_PULLBACK = "entry_pullback"
SIGNAL_ADD = "add"
SIGNAL_REDUCE = "reduce"
SIGNAL_TAKE_PROFIT = "take_profit"
SIGNAL_STOP_LOSS = "stop_loss"
SIGNAL_SWING_ENTRY = "swing_entry"
SIGNAL_SWING_ADD = "swing_add"
SIGNAL_SWING_REDUCE = "swing_reduce"
SIGNAL_SWING_EXIT = "swing_exit"

ALL_HORIZON_TAGS = {HORIZON_INTRADAY_MONSTER, HORIZON_SWING_CORE}
ALL_STRATEGY_PROFILES = {STRATEGY_MONSTER_SWING, STRATEGY_SWING_TREND}
ALL_REGIME_TAGS = {REGIME_RISK_ON, REGIME_NEUTRAL, REGIME_RISK_OFF}

DECISION_REQUIRED_COLUMNS = [
    "horizon_tag",
    "strategy_profile",
    "signal_type",
    "regime_tag",
]


def parse_symbol_csv(raw: str) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        ticker = str(part or "").strip().upper()
        if ticker and ticker not in out:
            out.append(ticker)
    return out


def core_list_tickers() -> set[str]:
    return set(parse_symbol_csv(CORE_LIST_TICKERS))


def normalize_horizon_tag(value: object, default: str = HORIZON_INTRADAY_MONSTER) -> str:
    text = str(value or "").strip().lower()
    if text in ALL_HORIZON_TAGS:
        return text
    return default


def normalize_strategy_profile(value: object, default: str = STRATEGY_MONSTER_SWING) -> str:
    text = str(value or "").strip().lower()
    if text in ALL_STRATEGY_PROFILES:
        return text
    return default


def normalize_regime_tag(value: object, default: str = REGIME_NEUTRAL) -> str:
    text = str(value or "").strip().lower()
    if text in ALL_REGIME_TAGS:
        return text
    return default


def default_strategy_for_horizon(horizon_tag: str) -> str:
    if normalize_horizon_tag(horizon_tag) == HORIZON_SWING_CORE:
        return STRATEGY_SWING_TREND
    return STRATEGY_MONSTER_SWING


def ensure_decision_strategy_columns(df: pd.DataFrame, default_regime: str = REGIME_NEUTRAL) -> pd.DataFrame:
    out = df.copy()
    for col in DECISION_REQUIRED_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    core_tickers = core_list_tickers()
    if "ticker" not in out.columns:
        out["ticker"] = ""
    tickers = out["ticker"].astype(str).str.strip().str.upper()

    inferred_horizon = []
    for ticker, existing in zip(tickers.tolist(), out["horizon_tag"].tolist()):
        normalized = normalize_horizon_tag(existing, default="")
        if normalized:
            inferred_horizon.append(normalized)
            continue
        inferred_horizon.append(HORIZON_SWING_CORE if ticker in core_tickers else HORIZON_INTRADAY_MONSTER)

    out["horizon_tag"] = inferred_horizon
    out["strategy_profile"] = [
        normalize_strategy_profile(profile, default_strategy_for_horizon(horizon))
        for profile, horizon in zip(out["strategy_profile"].tolist(), out["horizon_tag"].tolist())
    ]
    out["signal_type"] = out["signal_type"].astype(str).str.strip().str.lower()
    out.loc[out["signal_type"] == "", "signal_type"] = "watch"
    out["regime_tag"] = [normalize_regime_tag(value, default_regime) for value in out["regime_tag"].tolist()]
    return out


def classify_watch_horizon(
    ticker: str,
    decision: dict | None = None,
    source_flags: dict | None = None,
) -> str:
    ticker_norm = str(ticker or "").strip().upper()
    if ticker_norm in core_list_tickers():
        return HORIZON_SWING_CORE

    decision_data = decision or {}
    source_data = source_flags or {}
    if bool(source_data.get("saved_watchlist", False)) and not bool(source_data.get("ai_decision", False)):
        return HORIZON_SWING_CORE

    monster_score = pd.to_numeric(decision_data.get("monster_score"), errors="coerce")
    day_change = pd.to_numeric(decision_data.get("daily_change_pct"), errors="coerce")
    catalyst = str(decision_data.get("catalyst_type", "")).strip().lower()

    catalyst_hint = any(key in catalyst for key in ["earnings", "fda", "ai", "crypto", "event"])
    explosive_move = bool(pd.notna(day_change) and abs(float(day_change)) >= 6.0)
    high_monster_score = bool(pd.notna(monster_score) and float(monster_score) >= 35.0)

    if catalyst_hint or explosive_move or high_monster_score:
        return HORIZON_INTRADAY_MONSTER
    return HORIZON_SWING_CORE


def classify_watch_stance(horizon_tag: str, engine: dict, decision: dict) -> str:
    action = str((engine or {}).get("action", "")).strip().lower()
    has_data = bool((engine or {}).get("has_data", False))
    close_val = float(pd.to_numeric((engine or {}).get("close"), errors="coerce") or 0.0)
    avwap = float(pd.to_numeric((engine or {}).get("dynamic_avwap"), errors="coerce") or 0.0)
    hist = float(pd.to_numeric((engine or {}).get("sqzmom_hist"), errors="coerce") or 0.0)

    if action in {"stop_loss", "take_profit"}:
        return "avoid_for_now"
    if action in {"entry", "add"}:
        return "ready_to_fire"

    if normalize_horizon_tag(horizon_tag) == HORIZON_INTRADAY_MONSTER:
        if has_data and close_val > 0 and avwap > 0 and close_val >= avwap and hist > 0:
            return "ready_to_fire"
        if has_data and close_val > 0 and avwap > 0 and close_val >= avwap * 1.03:
            return "watch_for_pullback"
        return "avoid_for_now" if has_data and hist < 0 else "watch_for_pullback"

    if has_data and close_val > 0 and avwap > 0 and close_val >= avwap and hist >= 0:
        return "watch_for_pullback"
    if has_data and hist < 0:
        return "avoid_for_now"

    risk_level = str((decision or {}).get("risk_level", "")).strip()
    if risk_level == "高":
        return "avoid_for_now"
    return "watch_for_pullback"


def detect_regime_tag(now: datetime | None = None) -> str:
    del now
    ticker = str(REGIME_BENCHMARK_TICKER or "SPY").strip().upper() or "SPY"
    try:
        bars = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=False)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
        return REGIME_NEUTRAL

    if bars is None or bars.empty or len(bars) < 55:
        return REGIME_NEUTRAL

    close = pd.to_numeric(bars["Close"], errors="coerce").dropna()
    if len(close) < 55:
        return REGIME_NEUTRAL

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    latest = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    change_pct = ((latest / prev) - 1.0) * 100.0 if prev > 0 else 0.0
    latest_ema20 = float(ema20.iloc[-1])
    latest_ema50 = float(ema50.iloc[-1])

    if latest < latest_ema20 and latest_ema20 < latest_ema50:
        return REGIME_RISK_OFF
    if change_pct <= float(REGIME_RISK_OFF_DAILY_CHANGE_PCT):
        return REGIME_RISK_OFF
    if latest > latest_ema20 and latest_ema20 > latest_ema50 and change_pct >= float(REGIME_RISK_ON_DAILY_CHANGE_PCT):
        return REGIME_RISK_ON
    return REGIME_NEUTRAL


def today_utc_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def unique_preserve_order(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        text = str(item or "").strip().upper()
        if not text or text in out:
            continue
        out.append(text)
    return out
