from __future__ import annotations

from typing import Dict, List, Tuple

import pandas as pd


def _to_float_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors='coerce').fillna(default)


def _to_bool_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)

    def _cast(value) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return text in {'1', 'true', 'yes', 'y'}

    return df[col].apply(_cast)


def _build_sector_rotation_scores(df: pd.DataFrame) -> pd.Series:
    if 'sector' not in df.columns:
        return pd.Series(0.0, index=df.index)

    temp = df.copy()
    temp['sector'] = temp['sector'].fillna('Unknown').astype(str)
    temp['daily_change_pct'] = _to_float_series(temp, 'daily_change_pct')
    temp['rel_volume'] = _to_float_series(temp, 'rel_volume')
    temp['is_strong'] = ((temp['daily_change_pct'] >= 3.0) & (temp['rel_volume'] >= 1.8)).astype(int)

    grouped = temp.groupby('sector', as_index=False).agg(
        sector_count=('ticker', 'count'),
        sector_median_change=('daily_change_pct', 'median'),
        sector_avg_rel_volume=('rel_volume', 'mean'),
        sector_strong_count=('is_strong', 'sum'),
    )
    grouped['sector_strong_ratio'] = grouped['sector_strong_count'] / grouped['sector_count'].clip(lower=1)
    grouped['sector_rotation_score'] = (
        grouped['sector_median_change'].clip(lower=-3, upper=12) * 1.0 +
        grouped['sector_avg_rel_volume'].clip(lower=0, upper=5) * 2.2 +
        grouped['sector_strong_ratio'].clip(lower=0, upper=1) * 9.0
    ).round(2)

    score_map = dict(zip(grouped['sector'], grouped['sector_rotation_score']))
    return temp['sector'].map(score_map).fillna(0.0)


def _top_radar_tag(row: pd.Series) -> Tuple[str, float]:
    score_map: Dict[str, float] = {
        'sector_rotation': float(row.get('sector_rotation_score', 0) or 0),
        'post_earnings_drift': float(row.get('post_earnings_drift_score', 0) or 0),
        'squeeze_setup': float(row.get('squeeze_setup_score', 0) or 0),
    }
    top_tag = max(score_map, key=score_map.get)
    return top_tag, score_map[top_tag]


def apply_multi_radars(dataset: pd.DataFrame, top_k_signals: int = 80) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame(), pd.DataFrame()

    out = dataset.copy()
    out['daily_change_pct'] = _to_float_series(out, 'daily_change_pct')
    out['rel_volume'] = _to_float_series(out, 'rel_volume')
    out['core_score_v81'] = _to_float_series(out, 'core_score_v81')
    out['monster_score'] = _to_float_series(out, 'monster_score')
    out['xq_momentum_mix'] = _to_float_series(out, 'xq_momentum_mix')
    out['days_to_earnings'] = pd.to_numeric(out.get('days_to_earnings'), errors='coerce')
    out['earnings_status'] = out.get('earnings_status', '').fillna('').astype(str).str.lower()

    out['sector_rotation_score'] = _build_sector_rotation_scores(out)
    out['sector_rotation_hit'] = (
        (out['sector_rotation_score'] >= 8.5) &
        (out['daily_change_pct'] >= 2.0) &
        (out['rel_volume'] >= 1.5)
    )

    post_score = pd.Series(0.0, index=out.index)
    past_mask = out['earnings_status'].eq('past') & out['days_to_earnings'].notna()
    drift_window = past_mask & out['days_to_earnings'].between(-5, -1, inclusive='both')
    post_score.loc[drift_window] = (
        (6 - out.loc[drift_window, 'days_to_earnings'].abs()) * 4.0 +
        out.loc[drift_window, 'daily_change_pct'].clip(lower=-5, upper=20) * 1.1 +
        out.loc[drift_window, 'rel_volume'].clip(lower=0, upper=8) * 3.4 +
        out.loc[drift_window, 'core_score_v81'].clip(lower=0, upper=60) * 0.18
    )
    out['post_earnings_drift_score'] = post_score.clip(lower=0, upper=40).round(2)
    out['post_earnings_drift_hit'] = (
        drift_window &
        (out['daily_change_pct'] > 0.5) &
        (out['rel_volume'] >= 1.2) &
        (out['post_earnings_drift_score'] >= 16.0)
    )

    tv_sqz_on = _to_bool_series(out, 'tv_sqz_on')
    squeeze_score = (
        tv_sqz_on.astype(float) * 12.0 +
        out['rel_volume'].clip(lower=0, upper=10) * 2.8 +
        out['daily_change_pct'].clip(lower=-6, upper=25) * 0.7 +
        out['xq_momentum_mix'].clip(lower=-10, upper=40) * 0.35 +
        out['monster_score'].clip(lower=0, upper=100) * 0.08
    )
    squeeze_penalty = ((out['daily_change_pct'] > 15) & (out['rel_volume'] < 1.5)).astype(float) * 7.0
    out['squeeze_setup_score'] = (squeeze_score - squeeze_penalty).round(2)
    out['squeeze_setup_hit'] = (
        ((tv_sqz_on) | (out['rel_volume'] >= 2.0)) &
        (out['squeeze_setup_score'] >= 15.0)
    )

    out['multi_radar_score'] = (
        out['sector_rotation_score'] * 0.34 +
        out['post_earnings_drift_score'] * 0.31 +
        out['squeeze_setup_score'] * 0.35
    ).round(2)

    out['multi_radar_hit'] = (
        out['sector_rotation_hit'] |
        out['post_earnings_drift_hit'] |
        out['squeeze_setup_hit']
    )

    radar_tags: List[str] = []
    radar_top_scores: List[float] = []
    for _, row in out.iterrows():
        tag, score = _top_radar_tag(row)
        radar_tags.append(tag)
        radar_top_scores.append(round(score, 2))
    out['radar_tag'] = radar_tags
    out['radar_top_score'] = radar_top_scores

    out['radar_priority_tier'] = 'C'
    out.loc[out['multi_radar_score'] >= 22, 'radar_priority_tier'] = 'A'
    out.loc[(out['multi_radar_score'] >= 16) & (out['multi_radar_score'] < 22), 'radar_priority_tier'] = 'B'

    signal_cols = [
        'ticker', 'radar_tag', 'radar_priority_tier', 'multi_radar_score', 'radar_top_score',
        'sector_rotation_score', 'post_earnings_drift_score', 'squeeze_setup_score',
        'daily_change_pct', 'rel_volume', 'monster_score', 'core_score_v81',
        'is_in_monster_radar', 'is_in_ai_focus', 'is_in_xq', 'is_in_fusion',
    ]
    signal_cols = [c for c in signal_cols if c in out.columns]

    radar_signals = out[out['multi_radar_hit']].copy()
    radar_signals = radar_signals.sort_values(
        ['multi_radar_score', 'radar_top_score', 'monster_score', 'rel_volume'],
        ascending=[False, False, False, False],
    ).head(max(top_k_signals, 1))
    radar_signals = radar_signals[signal_cols].reset_index(drop=True)

    return out, radar_signals
