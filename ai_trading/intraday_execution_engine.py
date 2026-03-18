from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from .utils.yfinance_ssl import ensure_ascii_cert_bundle

_YFINANCE_CA_BUNDLE = ensure_ascii_cert_bundle()

import yfinance as yf

from turso_state import (
    STATE_KEY_INTRADAY_SNAPSHOT,
    append_execution_log_rows,
    load_recent_trade_ledger,
    sync_runtime_df,
    sync_execution_latest as sync_execution_latest_to_turso,
)

from config import (
    DISCORD_WEBHOOK_URL,
    FINNHUB_API_KEY,
    INTRADAY_ADD_MIN_AVWAP_BUFFER_PCT,
    INTRADAY_ADD_SIZE_FRACTION,
    INTRADAY_AUTOMATION_MODE,
    INTRADAY_DATA_PROVIDER,
    INTRADAY_ENTRY_MAX_RANK,
    INTRADAY_ENTRY_MIN_API_SCORE,
    INTRADAY_ENTRY_MIN_AVWAP_BUFFER_PCT,
    INTRADAY_ENTRY_MIN_CONFIDENCE,
    INTRADAY_ENGINE_ENABLED,
    INTRADAY_ENTRY_WINDOW_END_MINUTES,
    INTRADAY_ENTRY_WINDOW_START_MINUTES,
    INTRADAY_ENTRY_SIZE_FRACTION,
    INTRADAY_ENTRY_WINDOW_MINUTES,
    INTRADAY_FINNHUB_TIMEOUT_SEC,
    INTRADAY_HARD_STOP_LOSS_PCT,
    INTRADAY_INTERVAL,
    INTRADAY_MAX_ADD_COUNT,
    INTRADAY_MAX_NEW_ENTRIES_PER_DAY,
    INTRADAY_MAX_TOTAL_POSITIONS,
    INTRADAY_MAX_SYMBOLS,
    INTRADAY_MIN_ADD_PROFIT_PCT,
    INTRADAY_IMPULSE_MIN_MOVE_PCT,
    INTRADAY_NOISE_EXIT_GRACE_MINUTES,
    INTRADAY_NO_REENTRY_SAME_DAY,
    INTRADAY_PERIOD,
    INTRADAY_PREPOST,
    INTRADAY_PULLBACK_AVWAP_MAX_PCT,
    INTRADAY_PULLBACK_AVWAP_MIN_PCT,
    INTRADAY_PULLBACK_MAX_VOL_RATIO,
    INTRADAY_PULLBACK_MIN_HIST,
    INTRADAY_REDUCE_SIZE_FRACTION,
    INTRADAY_TAKE_PROFIT_PCT,
    INTRADAY_TOP_N,
    PORTFOLIO_DAILY_MAX_STRATEGY_LOSS,
    PORTFOLIO_DAILY_MAX_TOTAL_LOSS,
    PORTFOLIO_MAX_NEW_ENTRIES_PER_STRATEGY,
    PORTFOLIO_MAX_THEME_EXPOSURE,
)
from .intraday_indicators import add_intraday_indicators
from .market_session import get_intraday_active_window
from .position_state import get_position_by_profile, load_positions
from .shadow_watchlist import load_shadow_decision_df
from .strategy_context import (
    HORIZON_INTRADAY_MONSTER,
    REGIME_RISK_OFF,
    SIGNAL_ADD,
    SIGNAL_ENTRY_IGNITION,
    SIGNAL_ENTRY_PULLBACK,
    SIGNAL_STOP_LOSS,
    SIGNAL_TAKE_PROFIT,
    STRATEGY_MONSTER_SWING,
    default_strategy_for_horizon,
    detect_regime_tag,
    normalize_horizon_tag,
)
from .ticker_mapping import resolve_underlying_ticker
from .trade_command_contract import (
    ACTION_BUY_AGGRESSIVE,
    ACTION_BUY_SCALE_IN,
    USER_VISIBLE,
    build_user_visible_command_df,
    enrich_trade_command_fields,
)
from .trade_memory import get_trade_memory


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
# Legacy export for modules that still import this constant; intraday seed source no longer reads it.
AI_DECISION_LATEST = BACKTEST_DIR / "ai_decision_latest.csv"
AI_TRADING_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_trading" / "latest"
AI_READY_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready" / "latest"
DAILY_REFRESH_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh" / "latest"
MATERIALIZED_CONTRACT_CSV = "ai_decision_contract_v2_materialized.csv"
MATERIALIZED_CONTRACT_SHEET_ALIASES = {
    "ai_decision_contract_v2_material",
    "ai_decision_contract_v2_materialized",
    "aidecisioncontractv2material",
    "aidecisioncontractv2materialized",
}
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
    "underlying_ticker",
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
    "horizon_tag",
    "strategy_profile",
    "signal_type",
    "regime_tag",
    "entry_reason",
    "exit_reason",
    "position_size_fraction",
    "entry_price",
    "exit_price",
    "holding_minutes",
    "holding_days",
    "mfe",
    "mae",
    "realized_R",
    "realized_pct",
    "slippage_bps",
    "source_decision_rank",
    "source_confidence",
    "source_api_final_score",
    "snapshot_json",
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
    cleaned = str(value or "").replace("\ufeff", "").strip().strip('"').strip("'").strip()
    cleaned = cleaned.strip("[]")
    cleaned = cleaned.strip("<>")
    return cleaned


def _normalize_sheet_name(value: object) -> str:
    return str(value or "").strip().lower().replace("_", "")


def _materialized_to_intraday_df(df: pd.DataFrame) -> pd.DataFrame:
    required = {"as_of_date", "ticker", "final_priority", "decision_status"}
    if df is None or len(df) == 0 or not required.issubset(set(df.columns)):
        return pd.DataFrame()

    normalized = enrich_trade_command_fields(df)
    visible = build_user_visible_command_df(normalized)
    out = visible[
        (visible["ticker"].astype(str).str.upper() != "NO_TRADE")
        & (visible["user_visibility"].astype(str).str.lower() == USER_VISIBLE)
        & (visible["execution_action"].astype(str).str.upper().isin({ACTION_BUY_SCALE_IN, ACTION_BUY_AGGRESSIVE}))
        & (visible["decision_status"].astype(str).str.lower() == "keep")
    ].copy()
    if len(out) == 0:
        return pd.DataFrame()

    out["decision_date"] = out.get("as_of_date", "")
    out["rank"] = pd.to_numeric(out.get("final_priority"), errors="coerce").fillna(9999).astype(int)
    out["decision_tag"] = out.get("decision_status", "watch").astype(str).str.strip().str.lower()
    out["risk_level"] = out.get("risk_level", "")
    out["tech_status"] = out.get("preferred_entry_type", "")
    out["theme"] = out.get("primary_event_type", "")
    out["reason_summary"] = out.get("decision_reason", "")
    out["confidence"] = pd.to_numeric(out.get("tomorrow_continuation_prob"), errors="coerce")
    out["explosion_probability"] = pd.to_numeric(out.get("tomorrow_continuation_prob"), errors="coerce")
    out["api_final_score"] = pd.to_numeric(out.get("trigger_score"), errors="coerce")
    out["research_mode"] = out.get("protocol_version", "materialized_contract")
    out["horizon_tag"] = HORIZON_INTRADAY_MONSTER
    out["strategy_profile"] = STRATEGY_MONSTER_SWING
    out["signal_type"] = out.get("preferred_entry_type", "pullback_entry")
    out["regime_tag"] = ""

    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out = out[out["ticker"] != ""].copy()
    out = out.sort_values(["rank", "ticker"], ascending=[True, True]).reset_index(drop=True)
    return out


def _read_materialized_csv(path: Path) -> pd.DataFrame:
    try:
        raw = pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = pd.read_csv(path)
    out = _materialized_to_intraday_df(raw)
    if len(out) > 0:
        out["source_contract"] = str(path)
    return out


def _load_materialized_from_bundle(bundle_path: Path) -> pd.DataFrame:
    if not bundle_path.exists():
        return pd.DataFrame()
    try:
        workbook = pd.ExcelFile(bundle_path)
    except (OSError, ValueError, ImportError):
        return pd.DataFrame()

    normalized_targets = {_normalize_sheet_name(name) for name in MATERIALIZED_CONTRACT_SHEET_ALIASES}
    chosen_sheet = ""
    for sheet in workbook.sheet_names:
        if _normalize_sheet_name(sheet) in normalized_targets:
            chosen_sheet = sheet
            break
    if not chosen_sheet:
        return pd.DataFrame()

    try:
        raw = pd.read_excel(workbook, sheet_name=chosen_sheet)
    except (OSError, ValueError):
        return pd.DataFrame()
    out = _materialized_to_intraday_df(raw)
    if len(out) > 0:
        out["source_contract"] = f"{bundle_path}#sheet={chosen_sheet}"
    return out


def _find_latest_materialized_csv() -> Optional[Path]:
    candidates = [
        AI_TRADING_LATEST_DIR / MATERIALIZED_CONTRACT_CSV,
        AI_READY_LATEST_DIR / MATERIALIZED_CONTRACT_CSV,
        DAILY_REFRESH_LATEST_DIR / MATERIALIZED_CONTRACT_CSV,
    ]
    found: List[tuple[float, Path]] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not found:
        return None
    found.sort(key=lambda item: item[0], reverse=True)
    return found[0][1]


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
    latest_csv = _find_latest_materialized_csv()
    if latest_csv is not None:
        out = _read_materialized_csv(latest_csv)
        if len(out) > 0:
            return out

    bundle_path = AI_READY_LATEST_DIR / "ai_ready_bundle.xlsx"
    out = _load_materialized_from_bundle(bundle_path)
    if len(out) > 0:
        return out

    return pd.DataFrame()


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


def _safe_float(value: object, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return default
    return float(parsed)


def _coerce_utc_timestamp(value: object) -> Optional[pd.Timestamp]:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return parsed


def _load_recent_trade_df(limit: int = 240) -> pd.DataFrame:
    df = load_recent_trade_ledger(limit=max(20, int(limit)))
    if len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["side"] = out["side"].astype(str).str.strip().str.lower()
    out["position_effect"] = out["position_effect"].astype(str).str.strip().str.lower()
    if "strategy_profile" in out.columns:
        out["strategy_profile"] = out["strategy_profile"].astype(str).str.strip().str.lower()
    else:
        out["strategy_profile"] = ""
    if "horizon_tag" in out.columns:
        out["horizon_tag"] = out["horizon_tag"].astype(str).str.strip().str.lower()
    else:
        out["horizon_tag"] = ""
    if "regime_tag" in out.columns:
        out["regime_tag"] = out["regime_tag"].astype(str).str.strip().str.lower()
    else:
        out["regime_tag"] = ""
    out["realized_pnl_delta"] = pd.to_numeric(out.get("realized_pnl_delta", 0.0), errors="coerce").fillna(0.0)
    out["recorded_at_ts"] = pd.to_datetime(out["recorded_at"], errors="coerce", utc=True)
    return out


def _filter_trade_df_for_session(df: pd.DataFrame, start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> pd.DataFrame:
    if len(df) == 0:
        return df.copy()
    out = df.copy()
    mask = out["recorded_at_ts"].notna()
    mask &= out["recorded_at_ts"] >= start_utc
    mask &= out["recorded_at_ts"] <= end_utc
    return out[mask].copy().reset_index(drop=True)


def _count_open_positions(positions_df: pd.DataFrame) -> int:
    if len(positions_df) == 0:
        return 0
    return int((pd.to_numeric(positions_df["quantity"], errors="coerce").fillna(0.0) > 0).sum())


def _count_new_entries_today(trade_df: pd.DataFrame) -> int:
    if len(trade_df) == 0:
        return 0
    scoped = trade_df[trade_df["position_effect"] == "open"].copy()
    if "strategy_profile" in scoped.columns:
        scoped = scoped[scoped["strategy_profile"].astype(str).str.strip().str.lower().isin({"", STRATEGY_MONSTER_SWING})]
    if "horizon_tag" in scoped.columns:
        scoped = scoped[scoped["horizon_tag"].astype(str).str.strip().str.lower().isin({"", HORIZON_INTRADAY_MONSTER})]
    return int(scoped["ticker"].nunique())


def _ticker_has_trade_today(
    trade_df: pd.DataFrame,
    ticker: str,
    strategy_profile: str = STRATEGY_MONSTER_SWING,
    horizon_tag: str = HORIZON_INTRADAY_MONSTER,
) -> bool:
    if len(trade_df) == 0:
        return False
    scoped = trade_df[trade_df["ticker"] == str(ticker).strip().upper()].copy()
    if "strategy_profile" in scoped.columns:
        scoped = scoped[scoped["strategy_profile"].astype(str).str.strip().str.lower().isin({"", strategy_profile})]
    if "horizon_tag" in scoped.columns:
        scoped = scoped[scoped["horizon_tag"].astype(str).str.strip().str.lower().isin({"", horizon_tag})]
    return len(scoped) > 0


def _latest_buy_fill_ts(
    trade_df: pd.DataFrame,
    ticker: str,
    strategy_profile: str = STRATEGY_MONSTER_SWING,
    horizon_tag: str = HORIZON_INTRADAY_MONSTER,
) -> Optional[pd.Timestamp]:
    if len(trade_df) == 0:
        return None
    scoped = trade_df.copy()
    if "strategy_profile" in scoped.columns:
        scoped = scoped[scoped["strategy_profile"].astype(str).str.strip().str.lower().isin({"", strategy_profile})]
    if "horizon_tag" in scoped.columns:
        scoped = scoped[scoped["horizon_tag"].astype(str).str.strip().str.lower().isin({"", horizon_tag})]
    rows = scoped[
        (scoped["ticker"] == str(ticker).strip().upper())
        & scoped["side"].isin({"buy", "add"})
    ]["recorded_at_ts"]
    if len(rows) == 0:
        return None
    latest = rows.max()
    return latest if isinstance(latest, pd.Timestamp) and not pd.isna(latest) else None


def _decision_allows_entry(meta: pd.Series) -> bool:
    if str(INTRADAY_AUTOMATION_MODE or "").strip().lower() != "monster_swing":
        return True
    if normalize_horizon_tag(meta.get("horizon_tag", HORIZON_INTRADAY_MONSTER), HORIZON_INTRADAY_MONSTER) != HORIZON_INTRADAY_MONSTER:
        return False
    if str(meta.get("strategy_profile", default_strategy_for_horizon(HORIZON_INTRADAY_MONSTER))).strip().lower() not in {"", STRATEGY_MONSTER_SWING}:
        return False
    if str(meta.get("decision_tag", "")).strip().lower() != "keep":
        return False

    execution_action = str(meta.get("execution_action", "")).strip().upper()
    user_visibility = str(meta.get("user_visibility", "")).strip().lower()

    rank_gate = max(1, int(INTRADAY_ENTRY_MAX_RANK))
    if execution_action:
        if execution_action not in {ACTION_BUY_SCALE_IN, ACTION_BUY_AGGRESSIVE}:
            return False
        if user_visibility and user_visibility != USER_VISIBLE:
            return False
        # Command-layer rows must at least allow priority 1-2 to prevent rank=2 pullback plans from being silently blocked.
        rank_gate = max(rank_gate, 2)

    rank_value = int(pd.to_numeric(meta.get("rank"), errors="coerce") or 9999)
    if rank_value > rank_gate:
        return False
    if str(meta.get("risk_level", "")).strip() == "高":
        return False
    confidence = _safe_float(meta.get("confidence"), 0.0)
    api_score = _safe_float(meta.get("api_final_score"), 0.0)
    if float(INTRADAY_ENTRY_MIN_CONFIDENCE) > 0 and confidence > 0 and confidence < float(INTRADAY_ENTRY_MIN_CONFIDENCE):
        return False
    if float(INTRADAY_ENTRY_MIN_API_SCORE) > 0 and api_score > 0 and api_score < float(INTRADAY_ENTRY_MIN_API_SCORE):
        return False
    return True


def _is_within_entry_window(signal_ts: Optional[pd.Timestamp], session_context: Dict[str, object]) -> bool:
    if signal_ts is None:
        return False
    market_open_utc = session_context.get("market_open_utc")
    if not isinstance(market_open_utc, datetime):
        return False
    start_minutes = max(0, int(INTRADAY_ENTRY_WINDOW_START_MINUTES))
    end_minutes = max(start_minutes + 1, int(INTRADAY_ENTRY_WINDOW_END_MINUTES or INTRADAY_ENTRY_WINDOW_MINUTES))
    entry_start_utc = pd.Timestamp(market_open_utc + pd.Timedelta(minutes=start_minutes))
    entry_deadline_utc = pd.Timestamp(market_open_utc + pd.Timedelta(minutes=end_minutes))
    return entry_start_utc <= signal_ts <= entry_deadline_utc


def _is_in_noise_exit_grace(signal_ts: Optional[pd.Timestamp], last_fill_ts: Optional[pd.Timestamp]) -> bool:
    if signal_ts is None or last_fill_ts is None:
        return False
    elapsed_minutes = (signal_ts - last_fill_ts).total_seconds() / 60.0
    return 0 <= elapsed_minutes < max(0, int(INTRADAY_NOISE_EXIT_GRACE_MINUTES))


def decision_allows_entry(meta: pd.Series) -> bool:
    return _decision_allows_entry(meta)


def is_in_noise_exit_grace(signal_ts: Optional[pd.Timestamp], last_fill_ts: Optional[pd.Timestamp]) -> bool:
    return _is_in_noise_exit_grace(signal_ts, last_fill_ts)


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
    sync_execution_latest_to_turso(EXECUTION_LATEST)
    append_execution_log_rows(rows)

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
    if len(decision_df) > 0:
        decision_df["horizon_tag"] = decision_df["horizon_tag"].apply(lambda value: normalize_horizon_tag(value, HORIZON_INTRADAY_MONSTER))
        decision_df = decision_df[decision_df["horizon_tag"] == HORIZON_INTRADAY_MONSTER].copy()
    shadow_df = load_shadow_decision_df(decision_df)
    if len(shadow_df) > 0 and "horizon_tag" in shadow_df.columns:
        shadow_df["horizon_tag"] = shadow_df["horizon_tag"].apply(lambda value: normalize_horizon_tag(value, HORIZON_INTRADAY_MONSTER))
        shadow_df = shadow_df[shadow_df["horizon_tag"] == HORIZON_INTRADAY_MONSTER].copy()
    positions_df = load_positions()
    if len(positions_df) > 0:
        positions_df = positions_df[
            (positions_df["horizon_tag"].astype(str).str.strip().str.lower() == HORIZON_INTRADAY_MONSTER)
            & (positions_df["strategy_profile"].astype(str).str.strip().str.lower() == STRATEGY_MONSTER_SWING)
        ].copy()

    watch_seed_n = max(2, int(top_n))
    watch = decision_df.head(watch_seed_n).copy() if len(decision_df) > 0 else pd.DataFrame(columns=["ticker"])
    if len(watch) > 0:
        watch["monitor_priority"] = "今天主監控"
        watch["shadow_age_days"] = 0
    if len(shadow_df) > 0:
        watch = pd.concat([watch, shadow_df], ignore_index=True)
    if len(positions_df) > 0:
        pos_rows = positions_df[["ticker"]].copy()
        pos_rows["decision_date"] = ""
        pos_rows["rank"] = 9999
        pos_rows["decision_tag"] = "position_open"
        pos_rows["risk_level"] = ""
        pos_rows["tech_status"] = ""
        pos_rows["theme"] = ""
        pos_rows["reason_summary"] = "open position"
        pos_rows["horizon_tag"] = HORIZON_INTRADAY_MONSTER
        pos_rows["strategy_profile"] = STRATEGY_MONSTER_SWING
        pos_rows["signal_type"] = "carry_position"
        pos_rows["regime_tag"] = ""
        pos_rows["monitor_priority"] = "今天主監控"
        pos_rows["shadow_age_days"] = 0
        watch = pd.concat([watch, pos_rows], ignore_index=True)

    if len(watch) == 0:
        return pd.DataFrame()
    watch["underlying_ticker"] = watch["ticker"].astype(str).str.strip().str.upper().apply(resolve_underlying_ticker)
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


def _fetch_intraday_bars_from_yahoo_chart(ticker: str, period: str, interval: str, prepost: bool) -> pd.DataFrame:
    try:
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={
                "range": period,
                "interval": interval,
                "includePrePost": str(bool(prepost)).lower(),
                "events": "div,splits",
            },
            headers={"User-Agent": "AlphaFinder/1.0"},
            timeout=max(10.0, float(INTRADAY_FINNHUB_TIMEOUT_SEC)),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return pd.DataFrame()

    chart = (payload.get("chart") or {})
    result_list = chart.get("result") or []
    if not result_list:
        return pd.DataFrame()

    result = result_list[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    if not timestamps or not quotes:
        return pd.DataFrame()

    quote = quotes[0] or {}
    try:
        out = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None),
                "Open": quote.get("open", []),
                "High": quote.get("high", []),
                "Low": quote.get("low", []),
                "Close": quote.get("close", []),
                "Volume": quote.get("volume", []),
            }
        )
    except (TypeError, ValueError):
        return pd.DataFrame()

    if len(out) == 0:
        return out
    out = out.dropna(subset=["Datetime", "Open", "High", "Low", "Close"]).copy()
    return out.sort_values(["Datetime"]).reset_index(drop=True)


def _fetch_intraday_bars_from_yfinance(ticker: str, period: str, interval: str, prepost: bool) -> pd.DataFrame:
    chart_bars = _fetch_intraday_bars_from_yahoo_chart(ticker, period, interval, prepost)
    if len(chart_bars) > 0:
        return chart_bars
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


def _latest_close_for_ticker(ticker: str, period: str, interval: str, prepost: bool) -> float:
    bars = _fetch_intraday_bars(ticker, period, interval, prepost)
    if len(bars) == 0:
        return float("nan")
    return float(pd.to_numeric(bars.iloc[-1].get("Close"), errors="coerce"))


def _build_reason(signal_type: str) -> str:
    reasons = {
        SIGNAL_ENTRY_IGNITION: "Ignition 模式：突破 AVWAP 且 SQZMOM 放量轉強，先開小倉抓第一波。",
        SIGNAL_ENTRY_PULLBACK: "Pullback 模式：先有衝刺後回踩 AVWAP 附近止跌，啟動二買。",
        SIGNAL_ADD: "既有部位已浮盈且續強，允許單次小幅加碼。",
        SIGNAL_TAKE_PROFIT: "先減碼保住主倉，避免被分鐘級噪音整筆洗掉。",
        SIGNAL_STOP_LOSS: "已觸發硬停損或結構失效，直接執行風控退出。",
    }
    return reasons.get(signal_type, "")


def _build_trade_memory_context(latest: pd.Series, meta: pd.Series, signal_type: str = "") -> dict:
    return {
        "ticker": str(meta.get("ticker", "")).strip().upper(),
        "decision_tag": str(meta.get("decision_tag", "")).strip().lower(),
        "regime_tag": str(meta.get("regime_tag", "")).strip().lower(),
        "signal_type": str(signal_type or "").strip().lower(),
        "sqzmom_color": str(latest.get("sqzmom_color", "")).strip().lower(),
        "reason_summary": _build_reason(signal_type),
    }


def _build_trade_memory_hint(similar_past_trades: List[dict]) -> str:
    if not similar_past_trades:
        return ""
    top = similar_past_trades[0]
    outcome = str(top.get("outcome", "")).strip().lower()
    similarity = float(pd.to_numeric(top.get("similarity", 0.0), errors="coerce") or 0.0)
    if similarity < 0.55:
        return ""
    if outcome == "loss":
        return "近似歷史偏弱，務必嚴格風控。"
    if outcome == "win":
        return "近似歷史偏強，可按紀律執行。"
    return ""


def _merge_reason_with_hint(reason: str, hint: str) -> str:
    base = str(reason or "").strip()
    memo = str(hint or "").strip()
    if not memo:
        return base
    if not base:
        return memo
    return f"{base} {memo}".strip()


def _theme_exposure_count(positions_df: pd.DataFrame, theme: str) -> int:
    if len(positions_df) == 0:
        return 0
    normalized_theme = str(theme or "").strip().lower()
    if not normalized_theme:
        return 0
    open_df = positions_df[pd.to_numeric(positions_df.get("quantity", 0.0), errors="coerce").fillna(0.0) > 0].copy()
    if len(open_df) == 0 or "theme" not in open_df.columns:
        return 0
    return int((open_df["theme"].astype(str).str.strip().str.lower() == normalized_theme).sum())


def _daily_realized_pnl(trade_df: pd.DataFrame, strategy_profile: str = "") -> float:
    if len(trade_df) == 0 or "realized_pnl_delta" not in trade_df.columns:
        return 0.0
    out = trade_df.copy()
    if strategy_profile:
        out = out[out["strategy_profile"].astype(str).str.strip().str.lower() == str(strategy_profile).strip().lower()]
    return float(pd.to_numeric(out["realized_pnl_delta"], errors="coerce").fillna(0.0).sum())


def _portfolio_blocks_new_entry(
    meta: pd.Series,
    trade_df: pd.DataFrame,
    positions_df: pd.DataFrame,
    regime_tag: str,
    planned_entry_count: int,
    new_entries_today: int,
) -> bool:
    rank_value = int(pd.to_numeric(meta.get("rank"), errors="coerce") or 9999)
    if regime_tag == REGIME_RISK_OFF and rank_value > 1:
        return True

    strategy_realized = _daily_realized_pnl(trade_df, STRATEGY_MONSTER_SWING)
    total_realized = _daily_realized_pnl(trade_df)
    if strategy_realized <= float(PORTFOLIO_DAILY_MAX_STRATEGY_LOSS):
        return True
    if total_realized <= float(PORTFOLIO_DAILY_MAX_TOTAL_LOSS):
        return True
    if new_entries_today + planned_entry_count >= max(1, int(PORTFOLIO_MAX_NEW_ENTRIES_PER_STRATEGY)):
        return True

    theme = str(meta.get("theme", "")).strip()
    if theme and _theme_exposure_count(positions_df, theme) >= max(1, int(PORTFOLIO_MAX_THEME_EXPOSURE)):
        return True
    return False


def _is_ignition_entry(
    close_val: float,
    avwap: float,
    hist: float,
    prev_hist: float,
    sqz_release: bool,
    color: str,
) -> bool:
    return bool(
        sqz_release
        and hist > 0
        and hist > prev_hist
        and close_val >= avwap * (1.0 + (float(INTRADAY_ENTRY_MIN_AVWAP_BUFFER_PCT) / 100.0))
        and color in {"lime", "green"}
    )


def _is_pullback_entry(
    latest: pd.Series,
    previous: pd.Series,
    avwap: float,
    hist: float,
    prev_hist: float,
    color: str,
    valid_history: pd.DataFrame,
) -> bool:
    if avwap <= 0:
        return False

    close_val = float(pd.to_numeric(latest.get("Close"), errors="coerce") or 0.0)
    open_ref = float(pd.to_numeric(valid_history.iloc[0].get("Open"), errors="coerce") or 0.0) if len(valid_history) > 0 else 0.0
    max_close = float(pd.to_numeric(valid_history.get("Close", pd.Series(dtype=float)), errors="coerce").max() or 0.0) if len(valid_history) > 0 else 0.0
    impulse_move_pct = ((max_close / open_ref) - 1.0) * 100.0 if open_ref > 0 and max_close > 0 else 0.0
    recent_release = False
    if "sqz_release" in valid_history.columns and len(valid_history) > 0:
        recent_release = bool(valid_history["sqz_release"].tail(12).astype(bool).any())

    avwap_gap_pct = ((close_val / avwap) - 1.0) * 100.0
    if avwap_gap_pct < float(INTRADAY_PULLBACK_AVWAP_MIN_PCT) or avwap_gap_pct > float(INTRADAY_PULLBACK_AVWAP_MAX_PCT):
        return False

    latest_vol = float(pd.to_numeric(latest.get("Volume"), errors="coerce") or 0.0)
    prev_vol = float(pd.to_numeric(previous.get("Volume"), errors="coerce") or 0.0)
    if prev_vol > 0 and latest_vol > prev_vol * float(INTRADAY_PULLBACK_MAX_VOL_RATIO):
        return False

    if hist < float(INTRADAY_PULLBACK_MIN_HIST):
        return False
    if hist < prev_hist and color in {"red"}:
        return False

    if not recent_release and impulse_move_pct < float(INTRADAY_IMPULSE_MIN_MOVE_PCT):
        return False
    return bool(hist >= prev_hist or color in {"maroon", "green", "lime"})


def _classify_action(
    latest: pd.Series,
    previous: pd.Series,
    position: Optional[pd.Series],
    meta: pd.Series,
    session_context: Dict[str, object],
    trade_df: pd.DataFrame,
    positions_df: pd.DataFrame,
    open_position_count: int,
    planned_entry_count: int,
    new_entries_today: int,
    regime_tag: str,
    valid_history: pd.DataFrame,
    similar_past_trades: Optional[List[dict]] = None,
    position_mark_price: Optional[float] = None,
) -> tuple[str, float, str, str]:
    close_val = float(pd.to_numeric(latest.get("Close"), errors="coerce"))
    avwap = float(pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"))
    hist = float(pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"))
    prev_hist = float(pd.to_numeric(previous.get("sqzmom_hist"), errors="coerce")) if previous is not None else hist
    sqz_release = bool(latest.get("sqz_release", False))
    color = str(latest.get("sqzmom_color", "")).strip().lower()
    signal_ts = _coerce_utc_timestamp(latest.get("Datetime"))
    memory_hint = _build_trade_memory_hint(similar_past_trades or [])

    if pd.isna(close_val) or pd.isna(avwap) or pd.isna(hist):
        return "", 0.0, "", ""

    if position is None or float(position.get("quantity", 0.0)) <= 0:
        if not _decision_allows_entry(meta):
            return "", 0.0, "", ""
        if open_position_count + planned_entry_count >= max(1, int(INTRADAY_MAX_TOTAL_POSITIONS)):
            return "", 0.0, "", ""
        if new_entries_today + planned_entry_count >= max(1, int(INTRADAY_MAX_NEW_ENTRIES_PER_DAY)):
            return "", 0.0, "", ""
        if not _is_within_entry_window(signal_ts, session_context):
            return "", 0.0, "", ""
        if INTRADAY_NO_REENTRY_SAME_DAY and _ticker_has_trade_today(
            trade_df,
            str(meta.get("ticker", "")),
            strategy_profile=STRATEGY_MONSTER_SWING,
            horizon_tag=HORIZON_INTRADAY_MONSTER,
        ):
            return "", 0.0, "", ""
        if _portfolio_blocks_new_entry(meta, trade_df, positions_df, regime_tag, planned_entry_count, new_entries_today):
            return "", 0.0, "", ""

        if _is_ignition_entry(close_val, avwap, hist, prev_hist, sqz_release, color):
            return (
                "entry",
                INTRADAY_ENTRY_SIZE_FRACTION,
                _merge_reason_with_hint(_build_reason(SIGNAL_ENTRY_IGNITION), memory_hint),
                SIGNAL_ENTRY_IGNITION,
            )

        if _is_pullback_entry(latest, previous, avwap, hist, prev_hist, color, valid_history):
            pullback_size = min(float(INTRADAY_ENTRY_SIZE_FRACTION), 0.30)
            return (
                "entry",
                pullback_size,
                _merge_reason_with_hint(_build_reason(SIGNAL_ENTRY_PULLBACK), memory_hint),
                SIGNAL_ENTRY_PULLBACK,
            )

        return "", 0.0, "", ""

    avg_cost = float(position.get("avg_cost", 0.0))
    add_count = int(pd.to_numeric(position.get("add_count", 0), errors="coerce"))
    mark_close = float(position_mark_price) if position_mark_price is not None and pd.notna(position_mark_price) else close_val
    unrealized_pct = ((mark_close / avg_cost) - 1.0) * 100.0 if avg_cost > 0 else 0.0
    last_fill_ts = _latest_buy_fill_ts(trade_df, str(meta.get("ticker", "")))
    in_noise_grace = _is_in_noise_exit_grace(signal_ts, last_fill_ts)

    if unrealized_pct <= float(INTRADAY_HARD_STOP_LOSS_PCT):
        return "stop_loss", 1.0, _merge_reason_with_hint(_build_reason(SIGNAL_STOP_LOSS), memory_hint), SIGNAL_STOP_LOSS

    if unrealized_pct >= INTRADAY_TAKE_PROFIT_PCT and hist < prev_hist:
        return (
            "take_profit",
            INTRADAY_REDUCE_SIZE_FRACTION,
            _merge_reason_with_hint(_build_reason(SIGNAL_TAKE_PROFIT), memory_hint),
            SIGNAL_TAKE_PROFIT,
        )

    if not in_noise_grace and close_val < avwap and hist < 0 and color in {"red", "maroon"}:
        return (
            "take_profit",
            INTRADAY_REDUCE_SIZE_FRACTION,
            _merge_reason_with_hint(_build_reason(SIGNAL_TAKE_PROFIT), memory_hint),
            SIGNAL_TAKE_PROFIT,
        )

    if (
        add_count < INTRADAY_MAX_ADD_COUNT
        and unrealized_pct >= INTRADAY_MIN_ADD_PROFIT_PCT
        and _is_within_entry_window(signal_ts, session_context)
        and close_val >= avwap * (1.0 + (float(INTRADAY_ADD_MIN_AVWAP_BUFFER_PCT) / 100.0))
        and hist > 0
        and hist > prev_hist
        and color in {"lime", "green"}
    ):
        return "add", INTRADAY_ADD_SIZE_FRACTION, _merge_reason_with_hint(_build_reason(SIGNAL_ADD), memory_hint), SIGNAL_ADD

    return "", 0.0, "", ""


def _build_signature(ticker: str, action: str, signal_type: str, ts_value: object, _close_val: object, _avwap: object, _hist: object) -> str:
    parsed = _coerce_utc_timestamp(ts_value)
    bucket = ""
    if parsed is not None:
        bucket = parsed.floor("min").strftime("%Y-%m-%dT%H:%MZ")
    return "|".join([ticker, action, signal_type, bucket])


def _format_user_line(row: dict) -> str:
    action_map = {
        "entry": "現在買入",
        "add": "現在加碼",
        "take_profit": "先減碼",
        "stop_loss": "全賣退場",
    }
    qty_part = ""
    fraction = pd.to_numeric(row.get("size_fraction"), errors="coerce")
    if pd.notna(fraction) and float(fraction) > 0:
        qty_part = f" | 建議比例={int(round(float(fraction) * 100))}%"
    display_ticker = str(row.get("ticker", "")).strip().upper()
    underlying_ticker = str(row.get("underlying_ticker", "")).strip().upper()
    if display_ticker and underlying_ticker and display_ticker != underlying_ticker:
        display_ticker = f"{display_ticker}(標的:{underlying_ticker})"
    return (
        f"- {display_ticker or row.get('ticker')} | {action_map.get(str(row.get('action')), str(row.get('action')))}"
        f" | rank={row.get('rank', 'NA')}{qty_part} | {row.get('signal_type', 'watch')} | {row.get('reason_summary', '')}"
    )


def _send_discord(message: str) -> tuple[bool, str]:
    webhook_url = _sanitize_webhook_url(DISCORD_WEBHOOK_URL)
    if not webhook_url:
        return False, "DISCORD_WEBHOOK_URL missing"
    return _http_post_json(webhook_url, {"content": message[:1900]})


def _state_key(ticker: str, channel: str) -> str:
    return f"{str(ticker or '').strip().upper()}::{channel}"


def _build_wait_signature(ticker: str, signal_ts: object) -> str:
    parsed = _coerce_utc_timestamp(signal_ts)
    bucket = ""
    if parsed is not None:
        bucket = parsed.floor("min").strftime("%Y-%m-%dT%H:%MZ")
    return f"WAIT_PULLBACK|{str(ticker or '').strip().upper()}|{bucket}"


def _format_wait_line(row: dict) -> str:
    ticker = str(row.get("ticker", "")).strip().upper()
    rank = int(pd.to_numeric(row.get("rank"), errors="coerce") or 0)
    signal_type = str(row.get("signal_type", "pullback_entry")).strip() or "pullback_entry"
    reason = str(row.get("reason_summary", "")).strip() or "尚未回踩到位，暫不買入。"
    return f"- {ticker} | 待命觀察 | rank={rank} | {signal_type} | {reason}"


def run_intraday_execution_engine(top_n: int | None = None, dry_run: bool = False) -> dict:
    if not INTRADAY_ENGINE_ENABLED:
        return {"ok": False, "reason": "engine_disabled"}

    watch_df = _load_watchlist(top_n or INTRADAY_TOP_N)
    if len(watch_df) == 0:
        return {"ok": False, "reason": "no_watchlist"}

    positions_all_df = load_positions()
    positions_df = positions_all_df[
        (positions_all_df["horizon_tag"].astype(str).str.strip().str.lower() == HORIZON_INTRADAY_MONSTER)
        & (positions_all_df["strategy_profile"].astype(str).str.strip().str.lower() == STRATEGY_MONSTER_SWING)
    ].copy()
    session_context = get_intraday_active_window(datetime.now(timezone.utc))
    trade_df = _load_recent_trade_df(limit=240)
    market_open_utc = pd.Timestamp(session_context.get("market_open_utc") or datetime.now(timezone.utc))
    active_end_utc = pd.Timestamp(session_context.get("active_end_utc") or datetime.now(timezone.utc))
    session_trade_df = _filter_trade_df_for_session(trade_df, market_open_utc, active_end_utc)
    open_position_count = _count_open_positions(positions_df)
    new_entries_today = _count_new_entries_today(session_trade_df)
    planned_entry_count = 0
    state = _load_state()
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    regime_tag = detect_regime_tag()
    trade_memory = get_trade_memory()
    try:
        memory_seed = _safe_read_csv(EXECUTION_LOG)
        if len(memory_seed) > 0:
            trade_memory.sync_completed_trades(memory_seed.tail(1200))
    except (OSError, ValueError, pd.errors.EmptyDataError):
        pass

    snapshot_rows: List[dict] = []
    action_rows: List[dict] = []
    wait_rows: List[dict] = []
    execution_rows: List[dict] = []
    state_updates: Dict[str, str] = {}

    for _, meta in watch_df.iterrows():
        ticker = str(meta.get("ticker", "")).strip().upper()
        underlying_ticker = str(meta.get("underlying_ticker", "")).strip().upper() or resolve_underlying_ticker(ticker)
        if not ticker:
            continue

        bars = _fetch_intraday_bars(underlying_ticker, INTRADAY_PERIOD, INTRADAY_INTERVAL, INTRADAY_PREPOST)
        if len(bars) < 60:
            continue

        enriched = add_intraday_indicators(bars)
        valid = enriched.dropna(subset=["dynamic_avwap", "sqzmom_hist"]).copy()
        if len(valid) < 2:
            continue
        latest = valid.iloc[-1]
        previous = valid.iloc[-2]
        position = get_position_by_profile(positions_df, ticker, horizon_tag=HORIZON_INTRADAY_MONSTER, strategy_profile=STRATEGY_MONSTER_SWING)
        memory_context = _build_trade_memory_context(latest=latest, meta=meta)
        similar_past_trades = trade_memory.find_similar_trades(
            ticker=ticker,
            context=memory_context,
            top_k=3,
        )
        position_mark_price = float(pd.to_numeric(latest.get("Close"), errors="coerce") or 0.0)
        if underlying_ticker != ticker and position is not None and float(position.get("quantity", 0.0)) > 0:
            mapped_close = _latest_close_for_ticker(ticker, INTRADAY_PERIOD, INTRADAY_INTERVAL, INTRADAY_PREPOST)
            if pd.notna(mapped_close):
                position_mark_price = float(mapped_close)
        action, size_fraction, reason, signal_type = _classify_action(
            latest,
            previous,
            position,
            meta,
            session_context,
            session_trade_df,
            positions_df,
            open_position_count,
            planned_entry_count,
            new_entries_today,
            regime_tag,
            valid,
            similar_past_trades,
            position_mark_price=position_mark_price,
        )

        snapshot_payload = {
            "ticker": ticker,
            "underlying_ticker": underlying_ticker,
            "close": float(pd.to_numeric(latest.get("Close"), errors="coerce") or 0.0),
            "dynamic_avwap": float(pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce") or 0.0),
            "sqzmom_hist": float(pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce") or 0.0),
            "sqzmom_color": str(latest.get("sqzmom_color", "")),
            "sqz_release": bool(latest.get("sqz_release", False)),
            "signal_ts": str(latest.get("Datetime", "")),
            "similar_past_trades": similar_past_trades,
            "source_contract": str(meta.get("source_contract", "")),
            "contract_rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") or 0),
            "contract_execution_action": str(meta.get("execution_action", "")),
            "contract_user_visibility": str(meta.get("user_visibility", "")),
            "contract_preferred_entry_type": str(meta.get("preferred_entry_type", "")),
            "contract_execution_window": str(meta.get("execution_window", "")),
        }

        snapshot_rows.append(
            {
                "generated_at": now_ts,
                "ticker": ticker,
                "underlying_ticker": underlying_ticker,
                "rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(meta.get("rank"), errors="coerce")) else 0),
                "decision_tag": str(meta.get("decision_tag", "")),
                "monitor_priority": str(meta.get("monitor_priority", "今天主監控")),
                "shadow_age_days": int(pd.to_numeric(meta.get("shadow_age_days"), errors="coerce") if pd.notna(pd.to_numeric(meta.get("shadow_age_days"), errors="coerce")) else 0),
                "close": pd.to_numeric(latest.get("Close"), errors="coerce"),
                "dynamic_avwap": pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"),
                "sqzmom_hist": pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"),
                "sqzmom_color": str(latest.get("sqzmom_color", "")),
                "sqz_on": bool(latest.get("sqz_on", False)),
                "sqz_release": bool(latest.get("sqz_release", False)),
                "has_position": bool(position is not None and float(position.get("quantity", 0.0)) > 0),
                "position_qty": float(position.get("quantity", 0.0)) if position is not None else 0.0,
                "avg_cost": float(position.get("avg_cost", 0.0)) if position is not None else 0.0,
                "confidence": _safe_float(meta.get("confidence"), 0.0),
                "api_final_score": _safe_float(meta.get("api_final_score"), 0.0),
                "horizon_tag": HORIZON_INTRADAY_MONSTER,
                "strategy_profile": STRATEGY_MONSTER_SWING,
                "regime_tag": regime_tag,
                "action": action,
                "signal_type": signal_type,
                "size_fraction": size_fraction,
                "reason_summary": reason,
                "signal_ts": str(latest.get("Datetime", "")),
                "source_contract": str(meta.get("source_contract", "")),
                "execution_action": str(meta.get("execution_action", "")),
                "user_visibility": str(meta.get("user_visibility", "")),
                "preferred_entry_type": str(meta.get("preferred_entry_type", "")),
                "execution_window": str(meta.get("execution_window", "")),
            }
        )

        if not action:
            has_position = bool(position is not None and float(position.get("quantity", 0.0)) > 0)
            preferred_entry_type = str(meta.get("preferred_entry_type", "")).strip().lower()
            execution_action = str(meta.get("execution_action", "")).strip().upper()
            user_visibility = str(meta.get("user_visibility", "")).strip().lower()
            signal_ts = latest.get("Datetime", "")
            if (
                not has_position
                and preferred_entry_type == "pullback_entry"
                and execution_action in {ACTION_BUY_SCALE_IN, ACTION_BUY_AGGRESSIVE}
                and user_visibility in {"", USER_VISIBLE}
                and _is_within_entry_window(_coerce_utc_timestamp(signal_ts), session_context)
            ):
                wait_signature = _build_wait_signature(ticker, signal_ts)
                wait_key = _state_key(ticker, "wait")
                if state.get(wait_key) != wait_signature:
                    wait_rows.append(
                        {
                            "alert_ts": now_ts,
                            "ticker": ticker,
                            "rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") or 0),
                            "signal_type": preferred_entry_type,
                            "reason_summary": "回踩條件尚未成立，先待命，暫不買入。",
                            "signal_ts": str(signal_ts),
                            "source_contract": str(meta.get("source_contract", "")),
                        }
                    )
                    state_updates[wait_key] = wait_signature
            continue

        signature = _build_signature(
            ticker,
            action,
            signal_type,
            latest.get("Datetime", ""),
            latest.get("Close", ""),
            latest.get("dynamic_avwap", ""),
            latest.get("sqzmom_hist", ""),
        )
        action_state_key = _state_key(ticker, "action")
        if state.get(action_state_key) == signature or state.get(ticker) == signature:
            continue

        signal_ts = str(latest.get("Datetime", ""))
        execution_date, execution_time = _normalize_date_time(signal_ts, now_ts)
        action_row = {
            "alert_ts": now_ts,
            "ticker": ticker,
            "underlying_ticker": underlying_ticker,
            "rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") if pd.notna(pd.to_numeric(meta.get("rank"), errors="coerce")) else 0),
            "action": action,
            "signal_type": signal_type,
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
        state_updates[action_state_key] = signature
        state_updates[ticker] = signature
        if action == "entry":
            planned_entry_count += 1

        holding_minutes = float("nan")
        holding_days = float("nan")
        realized_pct = float("nan")
        entry_price = float(pd.to_numeric(latest.get("Close"), errors="coerce") or 0.0)
        exit_price = float("nan")
        if position is not None and float(position.get("quantity", 0.0)) > 0:
            opened_at = pd.to_datetime(position.get("opened_at"), errors="coerce")
            signal_dt = pd.to_datetime(signal_ts, errors="coerce")
            if pd.notna(opened_at) and pd.notna(signal_dt):
                holding_minutes = max(0.0, (signal_dt - opened_at).total_seconds() / 60.0)
                holding_days = holding_minutes / (60.0 * 24.0)
            avg_cost = float(pd.to_numeric(position.get("avg_cost"), errors="coerce") or 0.0)
            if avg_cost > 0:
                realized_pct = ((entry_price / avg_cost) - 1.0) * 100.0
            if action in {"take_profit", "stop_loss"}:
                exit_price = entry_price

        execution_rows.append(
            {
                "recorded_at": now_ts,
                "execution_date": execution_date,
                "execution_time": execution_time,
                "decision_date": str(meta.get("decision_date", "")),
                "ticker": ticker,
                "underlying_ticker": underlying_ticker,
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
                "horizon_tag": HORIZON_INTRADAY_MONSTER,
                "strategy_profile": STRATEGY_MONSTER_SWING,
                "signal_type": signal_type,
                "regime_tag": regime_tag,
                "entry_reason": reason if action in {"entry", "add"} else "",
                "exit_reason": reason if action in {"take_profit", "stop_loss"} else "",
                "position_size_fraction": float(size_fraction),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "holding_minutes": holding_minutes,
                "holding_days": holding_days,
                "mfe": float("nan"),
                "mae": float("nan"),
                "realized_R": float("nan"),
                "realized_pct": realized_pct,
                "slippage_bps": float("nan"),
                "source_decision_rank": int(pd.to_numeric(meta.get("rank"), errors="coerce") or 0),
                "source_confidence": _safe_float(meta.get("confidence"), 0.0),
                "source_api_final_score": _safe_float(meta.get("api_final_score"), 0.0),
                "snapshot_json": json.dumps(snapshot_payload, ensure_ascii=False),
                "close": pd.to_numeric(latest.get("Close"), errors="coerce"),
                "vwap": pd.to_numeric(latest.get("dynamic_avwap"), errors="coerce"),
                "sqzmom_color": str(latest.get("sqzmom_color", "")),
                "sqzmom_value": pd.to_numeric(latest.get("sqzmom_hist"), errors="coerce"),
                "signal_signature": signature,
            }
        )

    if snapshot_rows:
        INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_df = pd.DataFrame(snapshot_rows)
        snapshot_df.to_csv(SNAPSHOT_FILE, index=False, encoding="utf-8-sig")
        sync_runtime_df(STATE_KEY_INTRADAY_SNAPSHOT, snapshot_df, source_name="intraday_snapshot")

    if action_rows and not dry_run:
        _append_action_log(action_rows)
        _write_execution_outputs(execution_rows)
        if execution_rows:
            trade_memory.sync_completed_trades(pd.DataFrame(execution_rows))

    if (action_rows or wait_rows) and not dry_run:
        state.update(state_updates)
        _save_state(state)

    message = ""
    wait_discord_ok = None
    wait_discord_detail = ""
    entry_discord_ok = None
    entry_discord_detail = ""
    exit_discord_ok = None
    exit_discord_detail = ""

    if wait_rows:
        wait_lines = [f"[Alpha Finder] ⏳ Pullback 待命 {now_ts}", ""]
        wait_lines.extend(_format_wait_line(row) for row in wait_rows)
        wait_lines.extend(["", "提醒: 這是待命訊號，尚未觸發買入。"])
        wait_message = "\n".join(wait_lines)
    else:
        wait_message = ""

    if action_rows:
        lines = [f"[Alpha Finder] Repo Intraday Engine {now_ts}", ""]
        lines.extend(_format_user_line(row) for row in action_rows)
        lines.extend(["", "提醒: 這是系統計算出的執行建議，不是自動下單。實際成交請用 Discord Bot 回報。"])
        message = "\n".join(lines)
        entry_rows = [r for r in action_rows if str(r.get("action", "")) in {"entry", "add"}]
        exit_rows = [r for r in action_rows if str(r.get("action", "")) in {"stop_loss", "take_profit"}]

        if entry_rows:
            entry_lines = [f"[Alpha Finder] ⚡ 即時進場指令 {now_ts}", ""]
            entry_lines.extend(_format_user_line(r) for r in entry_rows)
            entry_lines.extend(["", "提醒: 進場條件已成立，請依規劃比例執行。"])
            entry_message = "\n".join(entry_lines)
        else:
            entry_message = ""

        if exit_rows:
            exit_lines = [f"[Alpha Finder] ⚠️ 即時風控指令 {now_ts}", ""]
            exit_lines.extend(_format_user_line(r) for r in exit_rows)
            exit_lines.extend(["", "提醒: 已觸發失效條件，優先執行風控。"])
            exit_message = "\n".join(exit_lines)
        else:
            exit_message = ""
    else:
        entry_message = ""
        exit_message = ""

    if not dry_run:
        if wait_message:
            wait_discord_ok, wait_discord_detail = _send_discord(wait_message)
        if entry_message:
            entry_discord_ok, entry_discord_detail = _send_discord(entry_message)
        if exit_message:
            exit_discord_ok, exit_discord_detail = _send_discord(exit_message)

    return {
        "ok": True,
        "watch_count": len(watch_df),
        "snapshot_count": len(snapshot_rows),
        "wait_count": len(wait_rows),
        "action_count": len(action_rows),
        "snapshot_file": str(SNAPSHOT_FILE),
        "message": message,
        "wait_message": wait_message,
        "entry_message": entry_message,
        "exit_message": exit_message,
        "wait_discord_ok": wait_discord_ok,
        "wait_discord_detail": wait_discord_detail,
        "entry_discord_ok": entry_discord_ok,
        "entry_discord_detail": entry_discord_detail,
        "exit_discord_ok": exit_discord_ok,
        "exit_discord_detail": exit_discord_detail,
    }