from __future__ import annotations

from typing import Tuple

import pandas as pd
import config as settings_module

from .contracts import parse_probability_mid


def _to_float_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors='coerce').fillna(default)


def _resolve_feature_config() -> dict[str, float]:
    def _get(name: str, default: float) -> float:
        return float(getattr(settings_module, name, default))

    return {
        'grade_a': _get('FEATURE_GRADE_A_SCORE', 8.0),
        'grade_b': _get('FEATURE_GRADE_B_SCORE', 5.5),
        'grade_c': _get('FEATURE_GRADE_C_SCORE', 3.0),
        'grade_d': _get('FEATURE_GRADE_D_SCORE', 1.0),
        'trend_prob_weight': _get('FEATURE_TREND_PROB_WEIGHT', 0.08),
        'vol_daily_weight': _get('FEATURE_VOLATILITY_DAILY_WEIGHT', 0.9),
        'vol_xq3d_weight': _get('FEATURE_VOLATILITY_XQ3D_WEIGHT', 0.45),
        'vol_xq5d_weight': _get('FEATURE_VOLATILITY_XQ5D_WEIGHT', 0.3),
        'float_base_weight': _get('FEATURE_FLOAT_BASE_WEIGHT', 18.0),
        'float_liquidity_weight': _get('FEATURE_FLOAT_LIQUIDITY_WEIGHT', 0.28),
        'squeeze_rel_vol_weight': _get('FEATURE_SQUEEZE_REL_VOL_WEIGHT', 2.6),
        'squeeze_momentum_weight': _get('FEATURE_SQUEEZE_MOMENTUM_WEIGHT', 0.45),
        'squeeze_setup_weight': _get('FEATURE_SQUEEZE_SETUP_WEIGHT', 0.35),
        'earnings_near_score': _get('FEATURE_EARNINGS_NEAR_SCORE', 10.0),
        'earnings_mid_score': _get('FEATURE_EARNINGS_MID_SCORE', 6.0),
        'earnings_far_score': _get('FEATURE_EARNINGS_FAR_SCORE', 3.0),
        'earnings_post_score': _get('FEATURE_EARNINGS_POST_SCORE', 5.0),
        'analyst_upside_weight': _get('FEATURE_ANALYST_UPSIDE_WEIGHT', 0.2),
        'analyst_count_weight': _get('FEATURE_ANALYST_COUNT_WEIGHT', 1.1),
        'analyst_core_weight': _get('FEATURE_ANALYST_CORE_WEIGHT', 0.12),
        'news_attention_weight': _get('FEATURE_NEWS_ATTENTION_WEIGHT', 4.2),
        'news_rel_vol_weight': _get('FEATURE_NEWS_REL_VOL_WEIGHT', 0.8),
        'news_daily_weight': _get('FEATURE_NEWS_DAILY_WEIGHT', 0.18),
        'alpha_trend_weight': _get('FEATURE_ALPHA_TREND_WEIGHT', 0.9),
        'alpha_accel_1d3d_weight': _get('FEATURE_ALPHA_ACCEL_1D3D_WEIGHT', 0.7),
        'alpha_accel_3d5d_weight': _get('FEATURE_ALPHA_ACCEL_3D5D_WEIGHT', 0.5),
        'alpha_squeeze_weight': _get('FEATURE_ALPHA_SQUEEZE_WEIGHT', 0.55),
        'alpha_monster_weight': _get('FEATURE_ALPHA_MONSTER_WEIGHT', 0.12),
        'alpha_float_weight': _get('FEATURE_ALPHA_FLOAT_WEIGHT', 0.35),
        'alpha_analyst_weight': _get('FEATURE_ALPHA_ANALYST_WEIGHT', 0.5),
        'alpha_earnings_weight': _get('FEATURE_ALPHA_EARNINGS_WEIGHT', 0.8),
        'alpha_news_weight': _get('FEATURE_ALPHA_NEWS_WEIGHT', 0.45),
        'alpha_volatility_penalty': _get('FEATURE_ALPHA_VOLATILITY_PENALTY', 0.3),
        'priority_a_min': _get('FEATURE_PRIORITY_A_MIN', 40.0),
        'priority_b_min': _get('FEATURE_PRIORITY_B_MIN', 27.0),
        'hit_min': _get('FEATURE_HIT_MIN', 27.0),
        'hit_trend_min': _get('FEATURE_HIT_TREND_MIN', 10.0),
        'hit_squeeze_min': _get('FEATURE_HIT_SQUEEZE_MIN', 18.0),
    }


def apply_feature_engineering(dataset: pd.DataFrame, top_k_signals: int = 120) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame(), pd.DataFrame()

    cfg = _resolve_feature_config()
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
    grade_score = trend_grade.map({'A': cfg['grade_a'], 'B': cfg['grade_b'], 'C': cfg['grade_c'], 'D': cfg['grade_d']}).fillna(0.0)
    prob_mid = out.get('prob_next_day', pd.Series('', index=out.index)).apply(parse_probability_mid)
    out['trend_persistence_score'] = (grade_score + prob_mid * cfg['trend_prob_weight']).round(2)

    volatility_proxy = (
        daily.abs().clip(upper=35) * cfg['vol_daily_weight'] +
        xq_chg_3d.abs().clip(upper=45) * cfg['vol_xq3d_weight'] +
        xq_chg_5d.abs().clip(upper=65) * cfg['vol_xq5d_weight']
    )
    out['volatility_proxy_pct'] = volatility_proxy.round(2)

    out['liquidity_turnover_proxy_pct'] = (
        (dollar_volume_m * 1_000_000.0) / market_cap_raw.replace(0, pd.NA)
    ).fillna(0).clip(lower=0, upper=1.5).mul(100).round(2)

    out['float_tightness_proxy'] = (
        (1_000_000_000.0 / market_cap_raw.replace(0, pd.NA)).fillna(0).clip(upper=2.5) * cfg['float_base_weight'] +
        out['liquidity_turnover_proxy_pct'].clip(upper=60) * cfg['float_liquidity_weight']
    ).round(2)

    out['squeeze_pressure_score'] = (
        rel_volume.clip(upper=15) * cfg['squeeze_rel_vol_weight'] +
        xq_momentum.clip(lower=-10, upper=45) * cfg['squeeze_momentum_weight'] +
        out.get('squeeze_setup_score', pd.Series(0, index=out.index)).fillna(0) * cfg['squeeze_setup_weight']
    ).round(2)

    earnings_catalyst = pd.Series(0.0, index=out.index)
    upcoming_mask = earnings_status.eq('upcoming') & days_to_earnings.notna()
    earnings_catalyst.loc[upcoming_mask & days_to_earnings.between(0, 3, inclusive='both')] = cfg['earnings_near_score']
    earnings_catalyst.loc[upcoming_mask & days_to_earnings.between(4, 7, inclusive='both')] = cfg['earnings_mid_score']
    earnings_catalyst.loc[upcoming_mask & days_to_earnings.between(8, 14, inclusive='both')] = cfg['earnings_far_score']
    post_mask = earnings_status.eq('past') & days_to_earnings.notna() & days_to_earnings.between(-3, -1, inclusive='both')
    earnings_catalyst.loc[post_mask] = cfg['earnings_post_score']
    out['earnings_catalyst_score'] = earnings_catalyst

    out['analyst_conviction_score'] = (
        upside.clip(lower=0, upper=120) * cfg['analyst_upside_weight'] +
        analysts.clip(lower=0, upper=20) * cfg['analyst_count_weight'] +
        core_score.clip(lower=0, upper=60) * cfg['analyst_core_weight']
    ).round(2)

    attention_count = (
        out.get('is_in_ai_focus', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int) +
        out.get('is_in_monster_radar', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int) +
        out.get('is_in_xq', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int) +
        out.get('is_in_fusion', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int)
    )
    out['news_velocity_proxy'] = (
        attention_count * cfg['news_attention_weight'] +
        rel_volume.clip(upper=12) * cfg['news_rel_vol_weight'] +
        daily.clip(lower=-3, upper=20) * cfg['news_daily_weight']
    ).round(2)

    out['feature_alpha_score_v1'] = (
        out['trend_persistence_score'] * cfg['alpha_trend_weight'] +
        out['momentum_accel_1d3d'].clip(lower=-8, upper=20) * cfg['alpha_accel_1d3d_weight'] +
        out['momentum_accel_3d5d'].clip(lower=-8, upper=20) * cfg['alpha_accel_3d5d_weight'] +
        out['squeeze_pressure_score'] * cfg['alpha_squeeze_weight'] +
        monster_score.clip(lower=0, upper=100) * cfg['alpha_monster_weight'] +
        out['float_tightness_proxy'] * cfg['alpha_float_weight'] +
        out['analyst_conviction_score'] * cfg['alpha_analyst_weight'] +
        out['earnings_catalyst_score'] * cfg['alpha_earnings_weight'] +
        out['news_velocity_proxy'] * cfg['alpha_news_weight'] -
        out['volatility_proxy_pct'].clip(upper=40) * cfg['alpha_volatility_penalty']
    ).round(2)

    out['feature_priority_tier'] = 'C'
    out.loc[out['feature_alpha_score_v1'] >= cfg['priority_a_min'], 'feature_priority_tier'] = 'A'
    out.loc[(out['feature_alpha_score_v1'] >= cfg['priority_b_min']) & (out['feature_alpha_score_v1'] < cfg['priority_a_min']), 'feature_priority_tier'] = 'B'
    out['feature_hit'] = (
        (out['feature_alpha_score_v1'] >= cfg['hit_min']) |
        ((out['trend_persistence_score'] >= cfg['hit_trend_min']) & (out['squeeze_pressure_score'] >= cfg['hit_squeeze_min']))
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
