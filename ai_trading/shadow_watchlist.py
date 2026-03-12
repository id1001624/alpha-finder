from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from ai_trading.strategy_context import ensure_decision_strategy_columns

from config import (
    SHADOW_AI_DECISION_BASE_BONUS,
    SHADOW_AI_DECISION_DAY1_MAX_RANK,
    SHADOW_AI_DECISION_DECAY_PER_DAY,
    SHADOW_AI_DECISION_ENABLED,
    SHADOW_AI_DECISION_LOOKBACK_DAYS,
    SHADOW_AI_DECISION_OLDER_MAX_RANK,
    SHADOW_AI_DECISION_RANK_DECAY,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
AI_DECISION_LOG_CSV = BACKTEST_DIR / "ai_decision_log.csv"

DECISION_BASE_COLUMNS = [
    "decision_date",
    "rank",
    "ticker",
    "short_score_final",
    "risk_level",
    "tech_status",
    "theme",
    "decision_tag",
    "reason_summary",
    "catalyst_summary",
    "horizon_tag",
    "strategy_profile",
    "signal_type",
    "regime_tag",
]


def _read_csv_fallback(csv_path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(csv_path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(csv_path)


def normalize_decision_df(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) == 0:
        return pd.DataFrame(columns=DECISION_BASE_COLUMNS + ["monitor_priority", "shadow_age_days", "shadow_decay_score"])

    out = df.copy()
    for col in DECISION_BASE_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out["decision_date"] = out["decision_date"].astype(str).str.strip()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["short_score_final"] = pd.to_numeric(out["short_score_final"], errors="coerce")
    out = out[(out["ticker"] != "") & out["rank"].notna()].copy()
    out["rank"] = out["rank"].astype(int)
    out = ensure_decision_strategy_columns(out)
    return out.reset_index(drop=True)


def _decision_date_ts(df: pd.DataFrame) -> pd.Timestamp:
    if len(df) == 0 or "decision_date" not in df.columns:
        return pd.Timestamp(datetime.utcnow().date())
    parsed = pd.to_datetime(df["decision_date"], errors="coerce")
    parsed = parsed.dropna()
    if len(parsed) == 0:
        return pd.Timestamp(datetime.utcnow().date())
    return pd.Timestamp(parsed.iloc[0]).normalize()


def _max_shadow_rank(age_days: int) -> int:
    if age_days <= 1:
        return max(1, int(SHADOW_AI_DECISION_DAY1_MAX_RANK))
    return max(1, int(SHADOW_AI_DECISION_OLDER_MAX_RANK))


def _decay_score(age_days: int, rank_value: int) -> float:
    base_bonus = float(SHADOW_AI_DECISION_BASE_BONUS)
    day_decay = max(0, age_days - 1) * float(SHADOW_AI_DECISION_DECAY_PER_DAY)
    rank_decay = max(0, int(rank_value) - 1) * float(SHADOW_AI_DECISION_RANK_DECAY)
    return max(0.0, base_bonus - day_decay - rank_decay)


def load_shadow_decision_df(latest_df: pd.DataFrame, history_path: Path | None = None) -> pd.DataFrame:
    current = normalize_decision_df(latest_df)
    if not SHADOW_AI_DECISION_ENABLED or len(current) == 0:
        return pd.DataFrame(columns=current.columns.tolist() + ["monitor_priority", "shadow_age_days", "shadow_decay_score", "is_shadow"])

    history_file = Path(history_path or AI_DECISION_LOG_CSV)
    if not history_file.exists():
        return pd.DataFrame(columns=current.columns.tolist() + ["monitor_priority", "shadow_age_days", "shadow_decay_score", "is_shadow"])

    try:
        history = _read_csv_fallback(history_file)
    except (OSError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=current.columns.tolist() + ["monitor_priority", "shadow_age_days", "shadow_decay_score", "is_shadow"])

    history = normalize_decision_df(history)
    if len(history) == 0:
        return pd.DataFrame(columns=current.columns.tolist() + ["monitor_priority", "shadow_age_days", "shadow_decay_score", "is_shadow"])

    current_date = _decision_date_ts(current)
    current_tickers = set(current["ticker"].astype(str).str.upper().tolist())

    history["decision_date_ts"] = pd.to_datetime(history["decision_date"], errors="coerce").dt.normalize()
    history = history[history["decision_date_ts"].notna()].copy()
    history["shadow_age_days"] = (current_date - history["decision_date_ts"]).dt.days.astype(int)
    history = history[(history["shadow_age_days"] >= 1) & (history["shadow_age_days"] <= max(1, int(SHADOW_AI_DECISION_LOOKBACK_DAYS)))].copy()
    history = history[~history["ticker"].isin(current_tickers)].copy()
    if len(history) == 0:
        return pd.DataFrame(columns=current.columns.tolist() + ["monitor_priority", "shadow_age_days", "shadow_decay_score", "is_shadow"])

    history["shadow_max_rank"] = history["shadow_age_days"].apply(_max_shadow_rank)
    history = history[history["rank"] <= history["shadow_max_rank"]].copy()
    if len(history) == 0:
        return pd.DataFrame(columns=current.columns.tolist() + ["monitor_priority", "shadow_age_days", "shadow_decay_score", "is_shadow"])

    history["shadow_decay_score"] = history.apply(
        lambda row: _decay_score(int(row.get("shadow_age_days", 0)), int(row.get("rank", 9999))),
        axis=1,
    )
    history["monitor_priority"] = "延續觀察"
    history["is_shadow"] = True
    history = history.sort_values(["shadow_age_days", "rank", "short_score_final", "ticker"], ascending=[True, True, False, True], na_position="last")
    history = history.drop_duplicates(subset=["ticker"], keep="first")
    return history.reset_index(drop=True)


def build_decision_universe_df(latest_df: pd.DataFrame) -> pd.DataFrame:
    current = normalize_decision_df(latest_df)
    if len(current) == 0:
        return current

    current = current.copy()
    current["monitor_priority"] = "今天主監控"
    current["shadow_age_days"] = 0
    current["shadow_decay_score"] = 0.0
    current["is_shadow"] = False

    shadow = load_shadow_decision_df(current)
    if len(shadow) == 0:
        return current.reset_index(drop=True)

    combined = pd.concat([current, shadow], ignore_index=True)
    combined = combined.sort_values(["is_shadow", "shadow_age_days", "rank", "ticker"], ascending=[True, True, True, True], na_position="last")
    return combined.reset_index(drop=True)