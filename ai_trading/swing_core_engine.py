"""
Swing Core Engine — 多日 Swing 策略引擎

掃描 core list + saved watchlist 裡被分類為 swing_core 的標的，
使用日線 Dynamic AVWAP + SQZMOM 計算持倉建議。

訊號類型：
  swing_entry  — 日線突破 AVWAP，SQZMOM 轉正且上升
  swing_add    — 既有部位浮盈超過門檻，動能持續走強
  swing_reduce — 動能衰退，先保主倉
  swing_exit   — 跌破 AVWAP 且 SQZMOM 轉負，結構失效

用法：
  python scripts/run_swing_core_engine.py [--dry-run]
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from .utils.yfinance_ssl import ensure_ascii_cert_bundle

_YFINANCE_CA_BUNDLE = ensure_ascii_cert_bundle()

import yfinance as yf

from turso_state import (
    append_execution_log_rows,
    load_all_saved_watchlist_states,
    sync_runtime_df,
)
from config import (
    DISCORD_WEBHOOK_URL,
    SWING_ENGINE_ENABLED,
    SWING_CORE_MAX_SYMBOLS,
    SWING_ENTRY_SIZE_FRACTION,
    SWING_ADD_SIZE_FRACTION,
    SWING_REDUCE_SIZE_FRACTION,
    SWING_ADD_MIN_PROFIT_PCT,
    SWING_ADD_MAX_COUNT,
    SWING_PULLBACK_AVWAP_PCT,
    SWING_ENABLE_NEW_ENTRY_IN_RISK_OFF,
    SWING_VOLUME_GATE_PREV_DAY_WEAK,
    SWING_VOLUME_GATE_PREV_DAY_STRONG,
    SWING_VOLUME_GATE_REQUIRE_FOR_ENTRY,
    SWING_VOLUME_GATE_REQUIRE_FOR_ADD,
)
from .intraday_indicators import calc_sqzmom_lb, calc_dynamic_swing_avwap
from .position_state import get_position_by_profile, load_positions
from .strategy_context import (
    HORIZON_SWING_CORE,
    REGIME_RISK_OFF,
    SIGNAL_SWING_ENTRY,
    SIGNAL_SWING_ADD,
    SIGNAL_SWING_REDUCE,
    SIGNAL_SWING_EXIT,
    STRATEGY_SWING_TREND,
    core_list_tickers,
    detect_regime_tag,
    normalize_horizon_tag,
)
from .canonical_event_log import (
    append_canonical_action_events,
    build_source_event_id,
    default_event_ts_and_trade_date,
    dispatch_outcome_payload,
    normalize_action_type,
    now_created_at_utc,
    update_canonical_dispatch_outcomes,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
SWING_DIR = BACKTEST_DIR / "swing"
SWING_SNAPSHOT_FILE = SWING_DIR / "swing_signal_latest.csv"
SWING_ACTION_LOG_FILE = SWING_DIR / "swing_action_log.csv"
SWING_STATE_FILE = SWING_DIR / "swing_engine_state.json"

STATE_KEY_SWING_SNAPSHOT = "swing_engine_snapshot"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _safe_float(value: object, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(parsed) else float(parsed)


def _load_state() -> Dict[str, str]:
    if not SWING_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SWING_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_state(state: Dict[str, str]) -> None:
    SWING_DIR.mkdir(parents=True, exist_ok=True)
    SWING_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_signature(
    ticker: str, action: str, signal_type: str,
    close_val: float, avwap: float, hist: float,
) -> str:
    return "|".join([ticker, action, signal_type, f"{close_val:.4f}", f"{avwap:.4f}", f"{hist:.4f}"])


def _position_effect(action: str) -> str:
    return {
        "swing_entry": "open",
        "swing_add": "increase",
        "swing_reduce": "reduce",
        "swing_exit": "close",
    }.get(action, "update")


def _build_reason(signal_type: str) -> str:
    return {
        SIGNAL_SWING_ENTRY: "Swing 進場：日線突破 Dynamic AVWAP，SQZMOM 轉正且上升，趨勢確立。",
        SIGNAL_SWING_ADD: "Swing 加碼：部位已浮盈，動能持續走強，允許小幅加倉。",
        SIGNAL_SWING_REDUCE: "Swing 減碼：動能開始衰退，先保住主倉，等待方向確認。",
        SIGNAL_SWING_EXIT: "Swing 出場：日線跌破 AVWAP 且 SQZMOM 轉負，趨勢結構失效。",
    }.get(signal_type, "")


def _normalize_date_time(ts_str: str, fallback: str) -> tuple[str, str]:
    dt = pd.to_datetime(ts_str, errors="coerce")
    if pd.isna(dt):
        dt = pd.to_datetime(fallback, errors="coerce")
    if pd.isna(dt):
        return "", ""
    return str(dt.date()), str(dt.time())


def _append_action_log(rows: List[dict]) -> None:
    SWING_DIR.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(rows)
    if SWING_ACTION_LOG_FILE.exists():
        try:
            existing = pd.read_csv(SWING_ACTION_LOG_FILE, encoding="utf-8-sig")
            new_df = pd.concat([existing, new_df], ignore_index=True)
        except (OSError, ValueError):
            pass
    new_df.to_csv(SWING_ACTION_LOG_FILE, index=False, encoding="utf-8-sig")


def _sanitize_webhook_url(value: str) -> str:
    return str(value or "").replace("\ufeff", "").strip().strip('"').strip("'").strip("[]<>")


def _http_post_json(url: str, payload: dict) -> tuple[bool, str]:
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AlphaFinder/1.0"},
            timeout=20,
        )
        if response.ok:
            return True, f"{response.status_code} {response.text[:200]}"
        return False, f"HTTP {response.status_code}: {response.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


def _send_discord(message: str) -> tuple[bool, str]:
    webhook_url = _sanitize_webhook_url(DISCORD_WEBHOOK_URL)
    if not webhook_url:
        return False, "DISCORD_WEBHOOK_URL missing"
    return _http_post_json(webhook_url, {"content": message[:1900]})


def _canonical_priority(action: str) -> int:
    return {
        "swing_exit": 1,
        "swing_reduce": 2,
        "swing_add": 3,
        "swing_entry": 4,
    }.get(str(action or "").strip().lower(), 9)


def _invalidation_rule_for_swing(signal_type: str, action: str) -> str:
    action_norm = str(action or "").strip().lower()
    signal_norm = str(signal_type or "").strip().lower()
    if action_norm in {"swing_exit", "swing_reduce"}:
        return "daily_close_below_dynamic_avwap_and_negative_sqzmom"
    if signal_norm in {SIGNAL_SWING_ENTRY, SIGNAL_SWING_ADD}:
        return "daily_close_below_dynamic_avwap_or_sqzmom_turn_negative"
    return "signal_invalidated"


def _position_state_snapshot(position: Optional[pd.Series]) -> dict:
    if position is None:
        return {"has_position": False, "quantity": 0.0, "avg_cost": 0.0}
    quantity = float(pd.to_numeric(position.get("quantity"), errors="coerce") or 0.0)
    avg_cost = float(pd.to_numeric(position.get("avg_cost"), errors="coerce") or 0.0)
    return {
        "has_position": bool(quantity > 0),
        "quantity": quantity,
        "avg_cost": avg_cost,
    }


def _position_state_after_action(before: dict, action: str) -> dict:
    out = dict(before or {})
    action_norm = str(action or "").strip().lower()
    if action_norm in {"swing_entry", "swing_add"}:
        out["has_position"] = True
        out["quantity"] = "planned_increase"
        return out
    if action_norm == "swing_reduce":
        out["has_position"] = bool(before.get("has_position", False))
        out["quantity"] = "planned_reduce"
        return out
    if action_norm == "swing_exit":
        out["has_position"] = False
        out["quantity"] = 0.0
        return out
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Data fetching
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_daily_bars(ticker: str) -> pd.DataFrame:
    """Fetch 3 month daily OHLCV bars. Falls back to yfinance library on direct API failure."""
    try:
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"range": "3mo", "interval": "1d", "events": "div,splits"},
            headers={"User-Agent": "AlphaFinder/1.0"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        result_list = (payload.get("chart") or {}).get("result") or []
        if result_list:
            result = result_list[0]
            timestamps = result.get("timestamp") or []
            quotes = (result.get("indicators") or {}).get("quote") or []
            if timestamps and quotes:
                q = quotes[0] or {}
                df = pd.DataFrame({
                    "Date": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None).date,
                    "Open": q.get("open", []),
                    "High": q.get("high", []),
                    "Low": q.get("low", []),
                    "Close": q.get("close", []),
                    "Volume": q.get("volume", []),
                })
                df = df.dropna(subset=["Close"]).copy()
                if len(df) >= 20:
                    return df.sort_values("Date").reset_index(drop=True)
    except (requests.RequestException, ValueError, TypeError, KeyError):
        pass

    # Fallback to yfinance library
    try:
        hist = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=False)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TypeError, KeyError, AttributeError):
        return pd.DataFrame()
    if hist is None or hist.empty:
        return pd.DataFrame()
    out = hist.copy().reset_index()
    date_col = out.columns[0]
    out = out.rename(columns={date_col: "Date"})
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in out.columns:
            return pd.DataFrame()
    return out.dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)


def add_swing_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply SQZMOM + Dynamic AVWAP on daily bars with daily-appropriate parameters."""
    out = calc_sqzmom_lb(df, bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5)
    # swing_period=3: detect swing highs/lows over ±3 days (appropriate for daily bars)
    out = calc_dynamic_swing_avwap(out, swing_period=3, apt_period=14, atr_period=14, atr_baseline_period=40)
    out["above_avwap"] = out["Close"] > out["dynamic_avwap"]
    out["below_avwap"] = out["Close"] < out["dynamic_avwap"]
    out["sqzmom_positive"] = out["sqzmom_hist"] > 0
    out["sqzmom_rising"] = out["sqzmom_delta"] > 0
    out["long_trigger"] = out["sqz_release"] & out["sqzmom_positive"] & out["above_avwap"]
    out["ema20"] = out["Close"].ewm(span=20, adjust=False).mean()
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Universe loading
# ──────────────────────────────────────────────────────────────────────────────

def _load_saved_watchlist_tickers() -> list[str]:
    try:
        states = load_all_saved_watchlist_states()
    except Exception:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for _, user_tickers in states.items():
        for t in user_tickers:
            t_norm = str(t or "").strip().upper()
            if t_norm and t_norm not in seen:
                seen.add(t_norm)
                result.append(t_norm)
    return result


def _load_swing_universe() -> pd.DataFrame:
    """Build the swing scan universe: core list + watchlist tickers classified as swing_core."""
    from .strategy_context import classify_watch_horizon

    seen: set[str] = set()
    rows: list[dict] = []

    def _add(ticker: str, source: str) -> None:
        t = ticker.strip().upper()
        if not t or t in seen or len(rows) >= max(1, int(SWING_CORE_MAX_SYMBOLS)):
            return
        seen.add(t)
        rows.append({
            "ticker": t,
            "rank": 9999 if source == "saved_watchlist" else 999,
            "decision_date": "",
            "decision_tag": "",
            "risk_level": "",
            "theme": "",
            "reason_summary": "",
            "confidence": 0.0,
            "api_final_score": 0.0,
            "horizon_tag": HORIZON_SWING_CORE,
            "strategy_profile": STRATEGY_SWING_TREND,
            "source": source,
        })

    # 1. Core list — always swing
    for t in sorted(core_list_tickers()):
        _add(t, "core_list")

    # 2. Saved watchlist — only if classified as swing_core
    for t in _load_saved_watchlist_tickers():
        if normalize_horizon_tag(classify_watch_horizon(t)) == HORIZON_SWING_CORE:
            _add(t, "saved_watchlist")

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ──────────────────────────────────────────────────────────────────────────────
# Signal classification
# ──────────────────────────────────────────────────────────────────────────────

def _classify_swing_action(
    latest: pd.Series,
    previous: pd.Series,
    position: Optional[pd.Series],
    _meta: pd.Series,
    volume_gate: Dict[str, object],
    regime_tag: str,
) -> tuple[str, float, str, str]:
    """Classify swing action. Returns (action, size_fraction, reason, signal_type)."""
    close_val = float(pd.to_numeric(latest.get("Close"), errors="coerce"))
    avwap = float(pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"))
    hist = float(pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"))
    prev_hist = float(pd.to_numeric(previous.get("sqzmom_hist"), errors="coerce"))
    color = str(latest.get("sqzmom_color", "")).strip().lower()
    ema20 = float(pd.to_numeric(latest.get("ema20"), errors="coerce"))

    if pd.isna(close_val) or pd.isna(avwap) or pd.isna(hist) or avwap <= 0:
        return "", 0.0, "", ""

    avwap_gap_pct = ((close_val / avwap) - 1.0) * 100.0
    has_position = position is not None and _safe_float(position.get("quantity", 0.0)) > 0

    # ── EXIT: daily close breaks below AVWAP with negative momentum ────────
    if has_position and close_val < avwap and hist < 0 and color in {"red", "maroon"}:
        return "swing_exit", 1.0, _build_reason(SIGNAL_SWING_EXIT), SIGNAL_SWING_EXIT

    # ── REDUCE: momentum fading while in profit and still near AVWAP ───────
    if has_position and 0 < hist < prev_hist and color == "green":
        avg_cost = _safe_float(position.get("avg_cost", 0.0))
        unrealized_pct = ((close_val / avg_cost) - 1.0) * 100.0 if avg_cost > 0 else 0.0
        if unrealized_pct > 0 and avwap_gap_pct < float(SWING_PULLBACK_AVWAP_PCT):
            return "swing_reduce", float(SWING_REDUCE_SIZE_FRACTION), _build_reason(SIGNAL_SWING_REDUCE), SIGNAL_SWING_REDUCE

    # ── NEW ENTRY ───────────────────────────────────────────────────────────
    if not has_position:
        if regime_tag == REGIME_RISK_OFF and not bool(SWING_ENABLE_NEW_ENTRY_IN_RISK_OFF):
            return "", 0.0, "", ""
        if bool(SWING_VOLUME_GATE_REQUIRE_FOR_ENTRY) and str(volume_gate.get("volume_gate_status", "")).strip().lower() != "passed":
            return "", 0.0, "", ""

        # Breakout entry: clear break above AVWAP + rising momentum
        if (
            close_val > avwap
            and avwap_gap_pct > 0
            and hist > 0
            and hist > prev_hist
            and color in {"lime", "green"}
        ):
            return "swing_entry", float(SWING_ENTRY_SIZE_FRACTION), _build_reason(SIGNAL_SWING_ENTRY), SIGNAL_SWING_ENTRY

        # Pullback entry: price consolidating near AVWAP, momentum still intact
        if (
            pd.notna(ema20) and ema20 > 0
            and 0.0 <= avwap_gap_pct <= float(SWING_PULLBACK_AVWAP_PCT)
            and hist > 0
            and color in {"lime", "green", "maroon"}
        ):
            size = float(SWING_ENTRY_SIZE_FRACTION) * 0.8
            return "swing_entry", size, _build_reason(SIGNAL_SWING_ENTRY), SIGNAL_SWING_ENTRY

        return "", 0.0, "", ""

    # ── ADD: floating profit above threshold, momentum still strengthening ─
    avg_cost = _safe_float(position.get("avg_cost", 0.0))
    add_count = int(pd.to_numeric(position.get("add_count", 0), errors="coerce") or 0)
    unrealized_pct = ((close_val / avg_cost) - 1.0) * 100.0 if avg_cost > 0 else 0.0
    if (
        add_count < int(SWING_ADD_MAX_COUNT)
        and unrealized_pct >= float(SWING_ADD_MIN_PROFIT_PCT)
        and hist > 0
        and hist > prev_hist
        and close_val > avwap
        and color in {"lime", "green"}
        and (
            (not bool(SWING_VOLUME_GATE_REQUIRE_FOR_ADD))
            or str(volume_gate.get("volume_gate_status", "")).strip().lower() == "passed"
        )
    ):
        return "swing_add", float(SWING_ADD_SIZE_FRACTION), _build_reason(SIGNAL_SWING_ADD), SIGNAL_SWING_ADD

    return "", 0.0, "", ""


def _build_swing_volume_gate(valid_history: pd.DataFrame, meta: pd.Series) -> Dict[str, object]:
    out: Dict[str, object] = {
        "prev_day_relvolume": float("nan"),
        "prev_day_volume_vs_20d": float("nan"),
        "open_5m_relvolume": float("nan"),
        "open_15m_relvolume": float("nan"),
        "volume_gate_status": "blocked_missing_volume_data",
        "volume_gate_reason": "日線量能資料不足，先不發 swing entry/add。",
    }
    if len(valid_history) < 2 or "Volume" not in valid_history.columns:
        return out

    history = valid_history.copy().sort_values("Datetime") if "Datetime" in valid_history.columns else valid_history.copy()
    volumes = pd.to_numeric(history.get("Volume"), errors="coerce")
    if len(volumes) < 2:
        return out

    prev_day_volume = float(volumes.iloc[-2]) if pd.notna(volumes.iloc[-2]) else float("nan")
    baseline = volumes.iloc[:-1].tail(20)
    baseline = baseline[pd.notna(baseline)]
    avg_20d_volume = float(baseline.mean()) if len(baseline) > 0 else float("nan")
    prev_day_vs_20d = (prev_day_volume / avg_20d_volume) if avg_20d_volume > 0 else float("nan")

    meta_prev_day_rel = pd.to_numeric(meta.get("prev_day_relvolume", float("nan")), errors="coerce")
    if pd.isna(meta_prev_day_rel) or float(meta_prev_day_rel) <= 0:
        meta_prev_day_rel = pd.to_numeric(meta.get("rel_volume", float("nan")), errors="coerce")
    prev_day_relvolume = float(meta_prev_day_rel) if pd.notna(meta_prev_day_rel) and float(meta_prev_day_rel) > 0 else prev_day_vs_20d

    status = "passed"
    reason = "前日量能達標，允許 swing entry/add 條件判斷。"
    if not pd.notna(prev_day_relvolume):
        status = "blocked_prev_day_relvolume_missing"
        reason = "昨日相對量缺失，先不發 swing entry/add。"
    elif float(prev_day_relvolume) < float(SWING_VOLUME_GATE_PREV_DAY_WEAK):
        status = "blocked_prev_day_relvolume_low"
        reason = f"昨日相對量 {float(prev_day_relvolume):.2f}x < {float(SWING_VOLUME_GATE_PREV_DAY_WEAK):.2f}x。"
    elif float(prev_day_relvolume) < float(SWING_VOLUME_GATE_PREV_DAY_STRONG):
        status = "blocked_prev_day_relvolume_neutral"
        reason = (
            f"昨日相對量 {float(prev_day_relvolume):.2f}x 介於 {float(SWING_VOLUME_GATE_PREV_DAY_WEAK):.2f}-{float(SWING_VOLUME_GATE_PREV_DAY_STRONG):.2f}x，"
            "僅觀察不直接發 swing entry/add。"
        )

    out.update(
        {
            "prev_day_relvolume": float(prev_day_relvolume) if pd.notna(prev_day_relvolume) else float("nan"),
            "prev_day_volume_vs_20d": float(prev_day_vs_20d) if pd.notna(prev_day_vs_20d) else float("nan"),
            "volume_gate_status": status,
            "volume_gate_reason": reason,
        }
    )
    return out


def _format_action_line(ticker: str, action: str, size_fraction: float, signal_type: str, reason: str) -> str:
    label = {
        "swing_entry": "Swing 進場",
        "swing_add": "Swing 加碼",
        "swing_reduce": "Swing 減碼",
        "swing_exit": "Swing 出場",
    }.get(action, action)
    pct = f"{int(round(size_fraction * 100))}%"
    return f"- {ticker} | {label} | 比例={pct} | {signal_type} | {reason}"


# ──────────────────────────────────────────────────────────────────────────────
# Main engine run
# ──────────────────────────────────────────────────────────────────────────────

def run_swing_core_engine(dry_run: bool = False) -> dict:
    """Scan the swing universe and produce daily AVWAP+SQZMOM signals."""
    if not SWING_ENGINE_ENABLED:
        return {"ok": False, "reason": "swing_engine_disabled"}

    universe_df = _load_swing_universe()
    if len(universe_df) == 0:
        return {"ok": False, "reason": "no_swing_universe"}

    positions_all_df = load_positions()
    positions_df = positions_all_df[
        (positions_all_df["horizon_tag"].astype(str).str.strip().str.lower() == HORIZON_SWING_CORE)
        & (positions_all_df["strategy_profile"].astype(str).str.strip().str.lower() == STRATEGY_SWING_TREND)
    ].copy() if len(positions_all_df) > 0 else pd.DataFrame()

    regime_tag = detect_regime_tag()
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = _load_state()

    snapshot_rows: List[dict] = []
    action_rows: List[dict] = []
    canonical_rows: List[dict] = []
    execution_rows: List[dict] = []
    state_updates: Dict[str, str] = {}

    for _, meta in universe_df.iterrows():
        ticker = str(meta.get("ticker", "")).strip().upper()
        if not ticker:
            continue

        bars = _fetch_daily_bars(ticker)
        if len(bars) < 25:
            continue

        enriched = add_swing_daily_indicators(bars)
        valid = enriched.dropna(subset=["dynamic_avwap", "sqzmom_hist"]).copy()
        if len(valid) < 2:
            continue

        latest = valid.iloc[-1]
        previous = valid.iloc[-2]
        position = get_position_by_profile(
            positions_df, ticker,
            horizon_tag=HORIZON_SWING_CORE,
            strategy_profile=STRATEGY_SWING_TREND,
        )
        volume_gate = _build_swing_volume_gate(valid, meta)

        action, size_fraction, reason, signal_type = _classify_swing_action(
            latest, previous, position, meta, volume_gate, regime_tag,
        )

        close_val = _safe_float(latest.get("Close"))
        avwap = _safe_float(latest.get("dynamic_avwap"))
        hist = _safe_float(latest.get("sqzmom_hist"))
        ema20 = _safe_float(latest.get("ema20"))

        snapshot_rows.append({
            "generated_at": now_ts,
            "ticker": ticker,
            "source": str(meta.get("source", "core_list")),
            "close": close_val,
            "dynamic_avwap": avwap,
            "sqzmom_hist": hist,
            "sqzmom_color": str(latest.get("sqzmom_color", "")),
            "ema20": ema20,
            "above_avwap": bool(latest.get("above_avwap", False)),
            "sqz_release": bool(latest.get("sqz_release", False)),
            "has_position": position is not None and _safe_float(position.get("quantity", 0.0)) > 0,
            "position_qty": _safe_float(position.get("quantity", 0.0)) if position is not None else 0.0,
            "avg_cost": _safe_float(position.get("avg_cost", 0.0)) if position is not None else 0.0,
            "horizon_tag": HORIZON_SWING_CORE,
            "strategy_profile": STRATEGY_SWING_TREND,
            "regime_tag": regime_tag,
            "action": action,
            "signal_type": signal_type,
            "size_fraction": size_fraction,
            "reason_summary": reason,
            "prev_day_relvolume": pd.to_numeric(volume_gate.get("prev_day_relvolume"), errors="coerce"),
            "prev_day_volume_vs_20d": pd.to_numeric(volume_gate.get("prev_day_volume_vs_20d"), errors="coerce"),
            "open_5m_relvolume": pd.to_numeric(volume_gate.get("open_5m_relvolume"), errors="coerce"),
            "open_15m_relvolume": pd.to_numeric(volume_gate.get("open_15m_relvolume"), errors="coerce"),
            "volume_gate_status": str(volume_gate.get("volume_gate_status", "")),
            "volume_gate_reason": str(volume_gate.get("volume_gate_reason", "")),
        })

        if not action:
            continue

        signature = _build_signature(ticker, action, signal_type, close_val, avwap, hist)
        if state.get(ticker) == signature:
            continue

        action_rows.append({
            "alert_ts": now_ts,
            "ticker": ticker,
            "action": action,
            "signal_type": signal_type,
            "size_fraction": size_fraction,
            "close": close_val,
            "dynamic_avwap": avwap,
            "sqzmom_hist": hist,
            "sqzmom_color": str(latest.get("sqzmom_color", "")),
            "reason_summary": reason,
            "prev_day_relvolume": pd.to_numeric(volume_gate.get("prev_day_relvolume"), errors="coerce"),
            "prev_day_volume_vs_20d": pd.to_numeric(volume_gate.get("prev_day_volume_vs_20d"), errors="coerce"),
            "open_5m_relvolume": pd.to_numeric(volume_gate.get("open_5m_relvolume"), errors="coerce"),
            "open_15m_relvolume": pd.to_numeric(volume_gate.get("open_15m_relvolume"), errors="coerce"),
            "volume_gate_status": str(volume_gate.get("volume_gate_status", "")),
            "volume_gate_reason": str(volume_gate.get("volume_gate_reason", "")),
        })
        event_ts, trade_date = default_event_ts_and_trade_date(now_ts)
        action_type = normalize_action_type(action)
        source_event_id = build_source_event_id("swing", ticker, action_type, event_ts, signature)
        position_before = _position_state_snapshot(position)
        position_after = _position_state_after_action(position_before, action)
        action_rows[-1]["source_event_id"] = source_event_id

        canonical_rows.append(
            {
                "event_ts": event_ts,
                "trade_date": trade_date,
                "engine": "swing",
                "ticker": ticker,
                "action_type": action_type,
                "strategy_tag": STRATEGY_SWING_TREND,
                "reason_code": signal_type,
                "reason_text": reason,
                "price_ref": close_val,
                "size_ref": float(size_fraction),
                "risk_unit": "fraction",
                "priority": _canonical_priority(action),
                "dispatch_mode": "realtime" if action in {"swing_exit", "swing_reduce"} else "recap",
                "dispatch_status": "pending_realtime" if action in {"swing_exit", "swing_reduce"} else "pending_recap",
                "source_event_id": source_event_id,
                "source_log_id": signature,
                "position_state_before": json.dumps(position_before, ensure_ascii=False),
                "position_state_after": json.dumps(position_after, ensure_ascii=False),
                "invalidation_rule": _invalidation_rule_for_swing(signal_type, action),
                "created_at": now_created_at_utc(),
                "dispatch_detail": "",
                "prev_day_relvolume": float(pd.to_numeric(volume_gate.get("prev_day_relvolume"), errors="coerce")) if pd.notna(pd.to_numeric(volume_gate.get("prev_day_relvolume"), errors="coerce")) else float("nan"),
                "prev_day_volume_vs_20d": float(pd.to_numeric(volume_gate.get("prev_day_volume_vs_20d"), errors="coerce")) if pd.notna(pd.to_numeric(volume_gate.get("prev_day_volume_vs_20d"), errors="coerce")) else float("nan"),
                "open_5m_relvolume": float(pd.to_numeric(volume_gate.get("open_5m_relvolume"), errors="coerce")) if pd.notna(pd.to_numeric(volume_gate.get("open_5m_relvolume"), errors="coerce")) else float("nan"),
                "open_15m_relvolume": float(pd.to_numeric(volume_gate.get("open_15m_relvolume"), errors="coerce")) if pd.notna(pd.to_numeric(volume_gate.get("open_15m_relvolume"), errors="coerce")) else float("nan"),
                "volume_gate_status": str(volume_gate.get("volume_gate_status", "")),
                "volume_gate_reason": str(volume_gate.get("volume_gate_reason", "")),
            }
        )
        state_updates[ticker] = signature

        # Build execution log row
        date_str, time_str = _normalize_date_time(str(latest.get("Date", "")), now_ts)
        entry_price = close_val
        exit_price = float("nan")
        holding_days = float("nan")
        realized_pct = float("nan")

        if position is not None and _safe_float(position.get("quantity", 0.0)) > 0:
            avg_cost = _safe_float(position.get("avg_cost", 0.0))
            if avg_cost > 0:
                realized_pct = ((close_val / avg_cost) - 1.0) * 100.0
            if action in {"swing_exit", "swing_reduce"}:
                exit_price = close_val
            opened_at = pd.to_datetime(position.get("opened_at"), errors="coerce")
            signal_dt = pd.to_datetime(now_ts, errors="coerce")
            if pd.notna(opened_at) and pd.notna(signal_dt):
                holding_days = max(0.0, (signal_dt - opened_at).total_seconds() / (86400.0))

        execution_rows.append({
            "recorded_at": now_ts,
            "execution_date": date_str,
            "execution_time": time_str,
            "decision_date": str(meta.get("decision_date", "")),
            "ticker": ticker,
            "rank": int(_safe_float(meta.get("rank"), 9999)),
            "action": action,
            "position_effect": _position_effect(action),
            "decision_tag": str(meta.get("decision_tag", "")),
            "risk_level": str(meta.get("risk_level", "")),
            "tech_status": "repo_swing_engine",
            "theme": str(meta.get("theme", "")),
            "reason_summary": reason,
            "signal_source": "swing_core_engine",
            "exchange": "",
            "timeframe": "1d",
            "tv_event": action,
            "signal_ts": now_ts,
            "horizon_tag": HORIZON_SWING_CORE,
            "strategy_profile": STRATEGY_SWING_TREND,
            "signal_type": signal_type,
            "regime_tag": regime_tag,
            "entry_reason": reason if action in {"swing_entry", "swing_add"} else "",
            "exit_reason": reason if action in {"swing_exit", "swing_reduce"} else "",
            "position_size_fraction": size_fraction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "holding_minutes": float("nan"),
            "holding_days": holding_days,
            "mfe": float("nan"),
            "mae": float("nan"),
            "realized_R": float("nan"),
            "realized_pct": realized_pct,
            "slippage_bps": float("nan"),
            "source_decision_rank": int(_safe_float(meta.get("rank"), 9999)),
            "source_confidence": _safe_float(meta.get("confidence"), 0.0),
            "source_api_final_score": _safe_float(meta.get("api_final_score"), 0.0),
            "snapshot_json": json.dumps({
                "close": close_val,
                "dynamic_avwap": avwap,
                "sqzmom_hist": hist,
                "sqzmom_color": str(latest.get("sqzmom_color", "")),
                "ema20": ema20,
                "regime_tag": regime_tag,
            }, ensure_ascii=False),
            "close": close_val,
            "vwap": avwap,
            "sqzmom_color": str(latest.get("sqzmom_color", "")),
            "sqzmom_value": hist,
            "signal_signature": signature,
        })

    # ── Persist snapshot (always, even in dry-run) ─────────────────────────
    if snapshot_rows:
        SWING_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_df = pd.DataFrame(snapshot_rows)
        snapshot_df.to_csv(SWING_SNAPSHOT_FILE, index=False, encoding="utf-8-sig")
        sync_runtime_df(STATE_KEY_SWING_SNAPSHOT, snapshot_df, source_name="swing_engine")

    # ── Count actions by type ───────────────────────────────────────────────
    entry_count = sum(1 for r in action_rows if r.get("action") == "swing_entry")
    add_count = sum(1 for r in action_rows if r.get("action") == "swing_add")
    reduce_count = sum(1 for r in action_rows if r.get("action") == "swing_reduce")
    exit_count = sum(1 for r in action_rows if r.get("action") == "swing_exit")

    message = ""
    discord_ok: Optional[bool] = None
    discord_detail = ""
    discord_attempt_count = 0
    discord_success_count = 0

    if action_rows and not dry_run:
        if canonical_rows:
            append_canonical_action_events(canonical_rows)
        _append_action_log(action_rows)
        if execution_rows:
            try:
                append_execution_log_rows(execution_rows)
            except Exception:
                pass
        state.update(state_updates)
        _save_state(state)

        lines = [f"[Alpha Finder] Swing Core Engine {now_ts}", ""]
        lines.extend(
            _format_action_line(
                ticker=r["ticker"],
                action=r["action"],
                size_fraction=r["size_fraction"],
                signal_type=r["signal_type"],
                reason=r["reason_summary"],
            )
            for r in action_rows
        )
        lines.append("\n提醒: Swing 建議不是自動下單。成交請用 Discord /buy /sell profile=swing 回報。")
        message = "\n".join(lines)
        
        # ── Two-tier notification ───────────────────────────────────────────
        # Immediate: exit/reduce
        exit_rows = [r for r in action_rows if r.get("action") in {"swing_exit", "swing_reduce"}]
        if exit_rows:
            exit_lines = [f"[Alpha Finder] ⚡ Swing 風控 {now_ts}", ""]
            exit_lines.extend(
                _format_action_line(r["ticker"], r["action"], r["size_fraction"], r["signal_type"], r["reason_summary"])
                for r in exit_rows
            )
            exit_lines.append("\n提醒: 成交請用 Discord /sell profile=swing 回報。")
            discord_attempt_count += 1
            discord_ok, discord_detail = _send_discord("\n".join(exit_lines))
            if discord_ok:
                discord_success_count += 1
            exit_event_ids = [
                str(r.get("source_event_id", "")).strip()
                for r in exit_rows
                if str(r.get("source_event_id", "")).strip()
            ]
            if exit_event_ids:
                update_canonical_dispatch_outcomes(
                    dispatch_outcome_payload(
                        exit_event_ids,
                        "sent_realtime" if bool(discord_ok) else "failed_realtime",
                        str(discord_detail or ""),
                    )
                )

    return {
        "ok": True,
        "universe_count": len(universe_df),
        "snapshot_count": len(snapshot_rows),
        "action_count": len(action_rows),
        "canonical_action_count": len(canonical_rows),
        "entry_count": entry_count,
        "add_count": add_count,
        "reduce_count": reduce_count,
        "exit_count": exit_count,
        "discord_attempt_count": discord_attempt_count,
        "discord_success_count": discord_success_count,
        "discord_ok": discord_ok,
        "discord_detail": discord_detail,
        "message": message,
        "regime_tag": regime_tag,
    }
