from __future__ import annotations

from typing import Tuple

import pandas as pd

from .contracts import parse_probability_mid


def _to_float_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors='coerce').fillna(default)


def apply_feature_engineering(dataset: pd.DataFrame, top_k_signals: int = 120) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame(), pd.DataFrame()

    out = dataset.copy()

    daily = _to_float_series(out, 'daily_change_pct')
    rel_volume = _to_float_series(out, 'rel_volume')
    market_cap_raw = _to_float_series(out, 'market_cap_raw')
    dollar_volume_m = _to_float_series(out, 'xq_dollar_volume_m')
    xq_chg_1d = _to_float_series(out, 'xq_chg_1d_pct')
    xq_chg_3d = _to_float_series(out, 'xq_chg_3d_pct')
    xq_chg_5d = _to_float_series(out, 'xq_chg_5d_pct')
    xq_momentum = _to_float_series(out, 'xq_momentum_mix')
    core_score = _to_float_series(out, 'core_score_v81')
    monster_score = _to_float_series(out, 'monster_score')
    upside = _to_float_series(out, 'upside_pct')
    analysts = _to_float_series(out, 'num_analysts')
    days_to_earnings = pd.to_numeric(out.get('days_to_earnings'), errors='coerce')
    earnings_status = out.get('earnings_status', '').fillna('').astype(str).str.lower()

    out['momentum_accel_1d3d'] = (xq_chg_1d - (xq_chg_3d / 3.0)).round(2)
    out['momentum_accel_3d5d'] = ((xq_chg_3d / 3.0) - (xq_chg_5d / 5.0)).round(2)

    trend_grade = out.get('continuation_grade', '').fillna('').astype(str).str.upper()
    grade_score = trend_grade.map({'A': 8.0, 'B': 5.5, 'C': 3.0, 'D': 1.0}).fillna(0.0)
    prob_mid = out.get('prob_next_day', pd.Series('', index=out.index)).apply(parse_probability_mid)
    out['trend_persistence_score'] = (grade_score + prob_mid * 0.08).round(2)

    volatility_proxy = (
        daily.abs().clip(upper=35) * 0.9 +
        xq_chg_3d.abs().clip(upper=45) * 0.45 +
        xq_chg_5d.abs().clip(upper=65) * 0.3
    )
    out['volatility_proxy_pct'] = volatility_proxy.round(2)

    out['liquidity_turnover_proxy_pct'] = (
        (dollar_volume_m * 1_000_000.0) / market_cap_raw.replace(0, pd.NA)
    ).fillna(0).clip(lower=0, upper=1.5).mul(100).round(2)

    out['float_tightness_proxy'] = (
        (1_000_000_000.0 / market_cap_raw.replace(0, pd.NA)).fillna(0).clip(upper=2.5) * 18.0 +
        out['liquidity_turnover_proxy_pct'].clip(upper=60) * 0.28
    ).round(2)

    out['squeeze_pressure_score'] = (
        rel_volume.clip(upper=15) * 2.6 +
        xq_momentum.clip(lower=-10, upper=45) * 0.45 +
        out.get('squeeze_setup_score', pd.Series(0, index=out.index)).fillna(0) * 0.35
    ).round(2)

    earnings_catalyst = pd.Series(0.0, index=out.index)
    upcoming_mask = earnings_status.eq('upcoming') & days_to_earnings.notna()
    earnings_catalyst.loc[upcoming_mask & days_to_earnings.between(0, 3, inclusive='both')] = 10.0
    earnings_catalyst.loc[upcoming_mask & days_to_earnings.between(4, 7, inclusive='both')] = 6.0
    earnings_catalyst.loc[upcoming_mask & days_to_earnings.between(8, 14, inclusive='both')] = 3.0
    post_mask = earnings_status.eq('past') & days_to_earnings.notna() & days_to_earnings.between(-3, -1, inclusive='both')
    earnings_catalyst.loc[post_mask] = 5.0
    out['earnings_catalyst_score'] = earnings_catalyst

    out['analyst_conviction_score'] = (
        upside.clip(lower=0, upper=120) * 0.2 +
        analysts.clip(lower=0, upper=20) * 1.1 +
        core_score.clip(lower=0, upper=60) * 0.12
    ).round(2)

    attention_count = (
        out.get('is_in_ai_focus', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int) +
        out.get('is_in_monster_radar', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int) +
        out.get('is_in_xq', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int) +
        out.get('is_in_fusion', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int)
    )
    out['news_velocity_proxy'] = (
        attention_count * 4.2 +
        rel_volume.clip(upper=12) * 0.8 +
        daily.clip(lower=-3, upper=20) * 0.18
    ).round(2)

    out['feature_alpha_score_v1'] = (
        out['trend_persistence_score'] * 0.9 +
        out['momentum_accel_1d3d'].clip(lower=-8, upper=20) * 0.7 +
        out['momentum_accel_3d5d'].clip(lower=-8, upper=20) * 0.5 +
        out['squeeze_pressure_score'] * 0.55 +
        monster_score.clip(lower=0, upper=100) * 0.12 +
        out['float_tightness_proxy'] * 0.35 +
        out['analyst_conviction_score'] * 0.5 +
        out['earnings_catalyst_score'] * 0.8 +
        out['news_velocity_proxy'] * 0.45 -
        out['volatility_proxy_pct'].clip(upper=40) * 0.3
    ).round(2)

    out['feature_priority_tier'] = 'C'
    out.loc[out['feature_alpha_score_v1'] >= 40, 'feature_priority_tier'] = 'A'
    out.loc[(out['feature_alpha_score_v1'] >= 27) & (out['feature_alpha_score_v1'] < 40), 'feature_priority_tier'] = 'B'
    out['feature_hit'] = (
        (out['feature_alpha_score_v1'] >= 27) |
        ((out['trend_persistence_score'] >= 10) & (out['squeeze_pressure_score'] >= 18))
    )

    feature_cols = [
        'ticker', 'feature_priority_tier', 'feature_alpha_score_v1',
        'trend_persistence_score', 'momentum_accel_1d3d', 'momentum_accel_3d5d',
        'squeeze_pressure_score', 'float_tightness_proxy', 'liquidity_turnover_proxy_pct',
        'earnings_catalyst_score', 'analyst_conviction_score', 'news_velocity_proxy',
        'volatility_proxy_pct', 'daily_change_pct', 'rel_volume', 'monster_score',
        'is_in_monster_radar', 'is_in_ai_focus', 'is_in_xq', 'is_in_fusion',
    ]
    feature_cols = [c for c in feature_cols if c in out.columns]

    feature_signals = out[out['feature_hit']].copy()
    feature_signals = feature_signals.sort_values(
        ['feature_alpha_score_v1', 'trend_persistence_score', 'squeeze_pressure_score', 'ticker'],
        ascending=[False, False, False, True],
    ).head(max(top_k_signals, 1))
    feature_signals = feature_signals[feature_cols].reset_index(drop=True)

    return out, feature_signals
