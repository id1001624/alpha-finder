from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests
import yfinance as yf

from config import (
    DISCORD_WEBHOOK_URL,
    FINNHUB_API_KEY,
    INTRADAY_ADD_SIZE_FRACTION,
    INTRADAY_DATA_PROVIDER,
    INTRADAY_ENGINE_ENABLED,
    INTRADAY_ENTRY_SIZE_FRACTION,
    INTRADAY_FINNHUB_TIMEOUT_SEC,
    INTRADAY_INTERVAL,
    INTRADAY_MAX_ADD_COUNT,
    INTRADAY_MAX_SYMBOLS,
    INTRADAY_MIN_ADD_PROFIT_PCT,
    INTRADAY_PERIOD,
    INTRADAY_PREPOST,
    INTRADAY_REDUCE_SIZE_FRACTION,
    INTRADAY_STOP_LOSS_PCT,
    INTRADAY_TAKE_PROFIT_PCT,
    INTRADAY_TOP_N,
)
from .intraday_indicators import add_intraday_indicators
from .position_state import POSITIONS_FILE, get_position, load_positions


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
AI_DECISION_LATEST = BACKTEST_DIR / "ai_decision_latest.csv"
ALERT_DIR = BACKTEST_DIR / "alerts"
INTRADAY_DIR = BACKTEST_DIR / "intraday"
STATE_FILE = ALERT_DIR / "intraday_engine_state.json"
SNAPSHOT_FILE = INTRADAY_DIR / "intraday_signal_latest.csv"
ACTION_LOG_FILE = INTRADAY_DIR / "intraday_action_log.csv"
EXECUTION_LOG = BACKTEST_DIR / "execution_trade_log.csv"
EXECUTION_LATEST = BACKTEST_DIR / "execution_trade_latest.csv"
EXECUTION_DAILY_DIR = BACKTEST_DIR / "daily_execution_trades"

EXECUTION_LOG_FIELDS = [
    "recorded_at",
    "execution_date",
    "execution_time",
    "decision_date",
    "ticker",
    "rank",
    "action",
    "position_effect",
    "decision_tag",
    "risk_level",
    "tech_status",
    "theme",
    "reason_summary",
    "signal_source",
    "exchange",
    "timeframe",
    "tv_event",
    "signal_ts",
    "close",
    "vwap",
    "sqzmom_color",
    "sqzmom_value",
    "signal_signature",
]

_FINNHUB_RESOLUTION_MAP = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
    "1h": "60",
    "1d": "D",
    "1wk": "W",
    "1mo": "M",
}


def _safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)


def _sanitize_webhook_url(value: str) -> str:
    cleaned = str(value or "").strip().strip('"').strip("'").strip()
    cleaned = cleaned.strip("[]")
    cleaned = cleaned.strip("<>")
    return cleaned


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


def _load_decision_df() -> pd.DataFrame:
    df = _safe_read_csv(AI_DECISION_LATEST)
    if len(df) == 0 or "ticker" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    for col in ["decision_date", "rank", "ticker", "decision_tag", "risk_level", "tech_status", "theme", "reason_summary"]:
        if col not in out.columns:
            out[col] = ""
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out = out[out["ticker"] != ""].dropna(subset=["rank"]).copy()
    out["rank"] = out["rank"].astype(int)
    return out.sort_values(["rank", "ticker"], ascending=[True, True]).reset_index(drop=True)


def _load_state() -> Dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_state(state: Dict[str, str]) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _position_effect(action: str) -> str:
    if action == "entry":
        return "open"
    if action == "add":
        return "increase"
    if action == "take_profit":
        return "reduce"
    if action == "stop_loss":
        return "close"
    return "update"


def _normalize_date_time(ts_value: object, fallback_ts: str) -> tuple[str, str]:
    parsed = pd.to_datetime(ts_value, errors="coerce", utc=True)
    if pd.isna(parsed):
        fallback = pd.to_datetime(fallback_ts, errors="coerce")
        if pd.isna(fallback):
            return "", ""
        return fallback.strftime("%Y-%m-%d"), fallback.strftime("%H:%M:%S")
    local_dt = parsed.tz_convert(None)
    return local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M:%S")


def _dedupe_and_sort_execution_df(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) == 0:
        return df
    out = df.copy()
    out = out.drop_duplicates(subset=["ticker", "action", "signal_ts", "timeframe"], keep="last")
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out = out.sort_values(
        ["execution_date", "execution_time", "rank", "ticker", "signal_ts"],
        ascending=[False, False, True, True, False],
        na_position="last",
    )
    out["rank"] = out["rank"].fillna(0).astype(int)
    return out


def _write_execution_outputs(rows: List[dict]) -> None:
    if not rows:
        return
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    EXECUTION_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(rows, columns=EXECUTION_LOG_FIELDS)
    existing = _safe_read_csv(EXECUTION_LOG)
    merged = pd.concat([existing, new_df], ignore_index=True) if len(existing) > 0 else new_df
    merged = _dedupe_and_sort_execution_df(merged)
    merged.to_csv(EXECUTION_LOG, index=False, encoding="utf-8-sig")

    latest_df = _dedupe_and_sort_execution_df(new_df)
    latest_df.to_csv(EXECUTION_LATEST, index=False, encoding="utf-8-sig")

    for execution_date, daily_df in latest_df.groupby("execution_date", dropna=False):
        if not execution_date:
            continue
        daily_path = EXECUTION_DAILY_DIR / f"{execution_date}_execution_trade.csv"
        existing_daily = _safe_read_csv(daily_path)
        daily_out = pd.concat([existing_daily, daily_df], ignore_index=True) if len(existing_daily) > 0 else daily_df
        daily_out = _dedupe_and_sort_execution_df(daily_out)
        daily_out.to_csv(daily_path, index=False, encoding="utf-8-sig")


def _append_action_log(rows: List[dict]) -> None:
    if not rows:
        return
    INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
    exists = ACTION_LOG_FILE.exists()
    pd.DataFrame(rows).to_csv(ACTION_LOG_FILE, mode="a", header=not exists, index=False, encoding="utf-8-sig")


def _load_watchlist(top_n: int) -> pd.DataFrame:
    decision_df = _load_decision_df()
    positions_df = load_positions(POSITIONS_FILE)

    watch = decision_df.head(max(1, top_n)).copy() if len(decision_df) > 0 else pd.DataFrame(columns=["ticker"])
    if len(positions_df) > 0:
        pos_rows = positions_df[["ticker"]].copy()
        pos_rows["decision_date"] = ""
        pos_rows["rank"] = 9999
        pos_rows["decision_tag"] = "position_open"
        pos_rows["risk_level"] = ""
        pos_rows["tech_status"] = ""
        pos_rows["theme"] = ""
        pos_rows["reason_summary"] = "open position"
        watch = pd.concat([watch, pos_rows], ignore_index=True)

    if len(watch) == 0:
        return pd.DataFrame()
    watch = watch.drop_duplicates(subset=["ticker"], keep="first")
    watch = watch.head(max(1, INTRADAY_MAX_SYMBOLS)).reset_index(drop=True)
    return watch


def _finnhub_resolution_for_interval(interval: str) -> str:
    return _FINNHUB_RESOLUTION_MAP.get(str(interval or "").strip().lower(), "")


def _parse_period_to_seconds(period: str) -> int:
    raw = str(period or "").strip().lower()
    if not raw:
        return 5 * 24 * 3600
    normalized = raw
    if raw.endswith("d") and raw[:-1].isdigit():
        normalized = f"{raw[:-1]}D"
    try:
        delta = pd.to_timedelta(normalized)
        return max(int(delta.total_seconds()), 24 * 3600)
    except (TypeError, ValueError):
        pass
    if raw.endswith("mo") and raw[:-2].isdigit():
        return max(int(raw[:-2]) * 30 * 24 * 3600, 24 * 3600)
    if raw.endswith("wk") and raw[:-2].isdigit():
        return max(int(raw[:-2]) * 7 * 24 * 3600, 24 * 3600)
    return 5 * 24 * 3600


def _resolve_intraday_provider(interval: str, prepost: bool) -> str:
    configured = str(INTRADAY_DATA_PROVIDER or "auto").strip().lower()
    finnhub_ready = bool(FINNHUB_API_KEY) and bool(_finnhub_resolution_for_interval(interval)) and not prepost
    if configured in {"yfinance", "yf"}:
        return "yfinance"
    if configured == "finnhub":
        return "finnhub" if finnhub_ready else "yfinance"
    if configured == "auto":
        return "finnhub" if finnhub_ready else "yfinance"
    return "yfinance"


def _fetch_intraday_bars_from_finnhub(ticker: str, period: str, interval: str) -> pd.DataFrame:
    if not FINNHUB_API_KEY:
        return pd.DataFrame()

    resolution = _finnhub_resolution_for_interval(interval)
    if not resolution:
        return pd.DataFrame()

    now_ts = int(datetime.now(timezone.utc).timestamp())
    from_ts = max(now_ts - _parse_period_to_seconds(period), 0)

    try:
        response = requests.get(
            "https://finnhub.io/api/v1/stock/candle",
            params={
                "symbol": ticker,
                "resolution": resolution,
                "from": from_ts,
                "to": now_ts,
                "token": FINNHUB_API_KEY,
            },
            timeout=INTRADAY_FINNHUB_TIMEOUT_SEC,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return pd.DataFrame()

    if data.get("s") != "ok":
        return pd.DataFrame()

    timestamps = data.get("t") or []
    if not timestamps:
        return pd.DataFrame()

    try:
        out = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None),
                "Open": data.get("o", []),
                "High": data.get("h", []),
                "Low": data.get("l", []),
                "Close": data.get("c", []),
                "Volume": data.get("v", []),
            }
        )
    except (TypeError, ValueError):
        return pd.DataFrame()

    if len(out) == 0:
        return out
    out = out.dropna(subset=["Datetime", "Open", "High", "Low", "Close"]).copy()
    return out.sort_values(["Datetime"]).reset_index(drop=True)


def _fetch_intraday_bars_from_yfinance(ticker: str, period: str, interval: str, prepost: bool) -> pd.DataFrame:
    try:
        hist = yf.Ticker(ticker).history(period=period, interval=interval, prepost=prepost, auto_adjust=False)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TypeError, KeyError, AttributeError):
        return pd.DataFrame()
    if hist is None or hist.empty:
        return pd.DataFrame()
    out = hist.copy().reset_index()
    datetime_col = out.columns[0]
    out = out.rename(columns={datetime_col: "Datetime"})
    return out


def _fetch_intraday_bars(ticker: str, period: str, interval: str, prepost: bool) -> pd.DataFrame:
    provider = _resolve_intraday_provider(interval, prepost)
    if provider == "finnhub":
        finnhub_bars = _fetch_intraday_bars_from_finnhub(ticker, period, interval)
        if len(finnhub_bars) > 0:
            return finnhub_bars
    return _fetch_intraday_bars_from_yfinance(ticker, period, interval, prepost)


def _build_reason(action: str) -> str:
    reasons = {
        "entry": "動能剛轉強並重新站上主力防線，適合買進。",
        "add": "既有部位續強且動能抬升，適合小幅加碼。",
        "take_profit": "部位已有利潤且動能開始放緩，適合先賣一部分。",
        "stop_loss": "價格失守主力防線且動能轉弱，適合全出或快速降風險。",
    }
    return reasons.get(action, "")


def _classify_action(latest: pd.Series, previous: pd.Series, position: Optional[pd.Series]) -> tuple[str, float, str]:
    close_val = float(pd.to_numeric(latest.get("Close"), errors="coerce"))
    avwap = float(pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"))
    hist = float(pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"))
    prev_hist = float(pd.to_numeric(previous.get("sqzmom_hist"), errors="coerce")) if previous is not None else hist
    sqz_release = bool(latest.get("sqz_release", False))
    color = str(latest.get("sqzmom_color", "")).strip().lower()

    if pd.isna(close_val) or pd.isna(avwap) or pd.isna(hist):
        return "", 0.0, ""

    if position is None or float(position.get("quantity", 0.0)) <= 0:
        if sqz_release and hist > 0 and close_val > avwap:
            return "entry", INTRADAY_ENTRY_SIZE_FRACTION, _build_reason("entry")
        return "", 0.0, ""

    avg_cost = float(position.get("avg_cost", 0.0))
    add_count = int(pd.to_numeric(position.get("add_count", 0), errors="coerce"))
    unrealized_pct = ((close_val / avg_cost) - 1.0) * 100.0 if avg_cost > 0 else 0.0

    if unrealized_pct <= INTRADAY_STOP_LOSS_PCT or (close_val < avwap and hist < 0 and color in {"red", "maroon"}):
        return "stop_loss", 1.0, _build_reason("stop_loss")

    if unrealized_pct >= INTRADAY_TAKE_PROFIT_PCT and hist < prev_hist:
        return "take_profit", INTRADAY_REDUCE_SIZE_FRACTION, _build_reason("take_profit")

    if (
        add_count < INTRADAY_MAX_ADD_COUNT
        and unrealized_pct >= INTRADAY_MIN_ADD_PROFIT_PCT
        and close_val >= avwap * 1.003
        and hist > 0
        and hist > prev_hist
        and color in {"lime", "green"}
    ):
        return "add", INTRADAY_ADD_SIZE_FRACTION, _build_reason("add")

    return "", 0.0, ""


def _build_signature(ticker: str, action: str, ts_value: object, close_val: object, avwap: object, hist: object) -> str:
    return "|".join([ticker, action, str(ts_value), str(close_val), str(avwap), str(hist)])


def _format_user_line(row: dict) -> str:
    action_map = {
        "entry": "適合買",
        "add": "可加碼",
        "take_profit": "適合先賣一部分",
        "stop_loss": "適合全出",
    }
    qty_part = ""
    fraction = pd.to_numeric(row.get("size_fraction"), errors="coerce")
    if pd.notna(fraction) and float(fraction) > 0:
        qty_part = f" | 建議比例={int(round(float(fraction) * 100))}%"
    return (
        f"- {row.get('ticker')} | {action_map.get(str(row.get('action')), str(row.get('action')))}"
        f" | rank={row.get('rank', 'NA')}{qty_part} | {row.get('reason_summary', '')}"
    )


def _send_discord(message: str) -> tuple[bool, str]:
    webhook_url = _sanitize_webhook_url(DISCORD_WEBHOOK_URL)
    if not webhook_url:
        return False, "DISCORD_WEBHOOK_URL missing"
    return _http_post_json(webhook_url, {"content": message[:1900]})


def run_intraday_execution_engine(top_n: int | None = None, dry_run: bool = False) -> dict:
    if not INTRADAY_ENGINE_ENABLED:
        return {"ok": False, "reason": "engine_disabled"}

    watch_df = _load_watchlist(top_n or INTRADAY_TOP_N)
    if len(watch_df) == 0:
        return {"ok": False, "reason": "no_watchlist"}

    positions_df = load_positions(POSITIONS_FILE)
    state = _load_state()
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    snapshot_rows: List[dict] = []
    action_rows: List[dict] = []
    execution_rows: List[dict] = []
    state_updates: Dict[str, str] = {}

    for _, meta in watch_df.iterrows():
        ticker = str(meta.get("ticker", "")).strip().upper()
        if not ticker:
            continue

        bars = _fetch_intraday_bars(ticker, INTRADAY_PERIOD, INTRADAY_INTERVAL, INTRADAY_PREPOST)
        if len(bars) < 60:
            continue

        enriched = add_intraday_indicators(bars)
        valid = enriched.dropna(subset=["dynamic_avwap", "sqzmom_hist"]).copy()
        if len(valid) < 2:
            continue
        latest = valid.iloc[-1]
        previous = valid.iloc[-2]
        position = get_position(positions_df, ticker)
        action, size_fraction, reason = _classify_action(latest, previous, position)

        snapshot_rows.append(
            {
                "generated_at": now_ts,
                "ticker": ticker,
                "rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(meta.get("rank"), errors="coerce")) else 0),
                "decision_tag": str(meta.get("decision_tag", "")),
                "close": pd.to_numeric(latest.get("Close"), errors="coerce"),
                "dynamic_avwap": pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"),
                "sqzmom_hist": pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"),
                "sqzmom_color": str(latest.get("sqzmom_color", "")),
                "sqz_on": bool(latest.get("sqz_on", False)),
                "sqz_release": bool(latest.get("sqz_release", False)),
                "has_position": bool(position is not None and float(position.get("quantity", 0.0)) > 0),
                "position_qty": float(position.get("quantity", 0.0)) if position is not None else 0.0,
                "avg_cost": float(position.get("avg_cost", 0.0)) if position is not None else 0.0,
                "action": action,
                "size_fraction": size_fraction,
                "reason_summary": reason,
                "signal_ts": str(latest.get("Datetime", "")),
            }
        )

        if not action:
            continue

        signature = _build_signature(
            ticker,
            action,
            latest.get("Datetime", ""),
            latest.get("Close", ""),
            latest.get("dynamic_avwap", ""),
            latest.get("sqzmom_hist", ""),
        )
        if state.get(ticker) == signature:
            continue

        signal_ts = str(latest.get("Datetime", ""))
        execution_date, execution_time = _normalize_date_time(signal_ts, now_ts)
        action_row = {
            "alert_ts": now_ts,
            "ticker": ticker,
            "rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(meta.get("rank"), errors="coerce")) else 0),
            "action": action,
            "size_fraction": size_fraction,
            "decision_tag": str(meta.get("decision_tag", "")),
            "close": pd.to_numeric(latest.get("Close"), errors="coerce"),
            "dynamic_avwap": pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"),
            "sqzmom_color": str(latest.get("sqzmom_color", "")),
            "sqzmom_hist": pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"),
            "signal_ts": signal_ts,
            "reason_summary": reason,
        }
        action_rows.append(action_row)
        state_updates[ticker] = signature
        execution_rows.append(
            {
                "recorded_at": now_ts,
                "execution_date": execution_date,
                "execution_time": execution_time,
                "decision_date": str(meta.get("decision_date", "")),
                "ticker": ticker,
                "rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(meta.get("rank"), errors="coerce")) else 0),
                "action": action,
                "position_effect": _position_effect(action),
                "decision_tag": str(meta.get("decision_tag", "")),
                "risk_level": str(meta.get("risk_level", "")),
                "tech_status": str(meta.get("tech_status", "repo_intraday_engine")),
                "theme": str(meta.get("theme", "")),
                "reason_summary": reason,
                "signal_source": "intraday_engine",
                "exchange": "",
                "timeframe": INTRADAY_INTERVAL,
                "tv_event": action,
                "signal_ts": signal_ts,
                "close": pd.to_numeric(latest.get("Close"), errors="coerce"),
                "vwap": pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"),
                "sqzmom_color": str(latest.get("sqzmom_color", "")),
                "sqzmom_value": pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"),
                "signal_signature": signature,
            }
        )

    if snapshot_rows:
        INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(snapshot_rows).to_csv(SNAPSHOT_FILE, index=False, encoding="utf-8-sig")

    if action_rows and not dry_run:
        _append_action_log(action_rows)
        _write_execution_outputs(execution_rows)
        state.update(state_updates)
        _save_state(state)

    message = ""
    if action_rows:
        lines = [f"[Alpha Finder] Repo Intraday Engine {now_ts}", ""]
        lines.extend(_format_user_line(row) for row in action_rows)
        lines.extend(["", "提醒: 這是系統計算出的執行建議，不是自動下單。實際成交請用 Discord Bot 回報。"])
        message = "\n".join(lines)
        if not dry_run:
            _send_discord(message)

    return {
        "ok": True,
        "watch_count": len(watch_df),
        "snapshot_count": len(snapshot_rows),
        "action_count": len(action_rows),
        "snapshot_file": str(SNAPSHOT_FILE),
        "message": message,
    }