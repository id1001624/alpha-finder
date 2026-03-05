from __future__ import annotations

from typing import Dict, Tuple

import pandas as pd
import config as settings_module


def _to_float_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors='coerce').fillna(default)


def _resolve_rank_config() -> Dict[str, float]:
    def _get(name: str, default: float) -> float:
        return float(getattr(settings_module, name, default))

    return {
        'top_k': _get('AI_RANK_TOP_K', 80),
        'base_w': _get('AI_RANK_BASE_WEIGHT', 0.42),
        'feature_w': _get('AI_RANK_FEATURE_WEIGHT', 0.28),
        'radar_w': _get('AI_RANK_RADAR_WEIGHT', 0.20),
        'event_w': _get('AI_RANK_EVENT_WEIGHT', 0.10),
        'monster_bonus_w': _get('AI_RANK_MONSTER_BONUS_WEIGHT', 0.08),
        'focus_bonus': _get('AI_RANK_FOCUS_BONUS', 1.5),
        'fusion_bonus': _get('AI_RANK_FUSION_BONUS', 1.0),
        'bull_min': _get('AI_RANK_REGIME_BULL_MIN_BREADTH', 0.22),
        'bear_max': _get('AI_RANK_REGIME_BEAR_MAX_BREADTH', 0.10),
        'bull_base_mult': _get('AI_RANK_BULL_BASE_MULT', 1.15),
        'bull_feature_mult': _get('AI_RANK_BULL_FEATURE_MULT', 1.10),
        'bull_radar_mult': _get('AI_RANK_BULL_RADAR_MULT', 0.90),
        'bull_event_mult': _get('AI_RANK_BULL_EVENT_MULT', 0.85),
        'bear_base_mult': _get('AI_RANK_BEAR_BASE_MULT', 0.80),
        'bear_feature_mult': _get('AI_RANK_BEAR_FEATURE_MULT', 0.90),
        'bear_radar_mult': _get('AI_RANK_BEAR_RADAR_MULT', 1.15),
        'bear_event_mult': _get('AI_RANK_BEAR_EVENT_MULT', 1.25),
        'tier_a_min': _get('AI_RANK_TIER_A_MIN', 42.0),
        'tier_b_min': _get('AI_RANK_TIER_B_MIN', 30.0),
    }


def _detect_regime(dataset: pd.DataFrame, bull_min: float, bear_max: float) -> Tuple[str, float]:
    if dataset is None or len(dataset) == 0:
        return 'neutral', 0.0

    daily = _to_float_series(dataset, 'daily_change_pct')
    rel_volume = _to_float_series(dataset, 'rel_volume')
    strong_mask = (daily >= 3.0) & (rel_volume >= 1.8)
    breadth = float(strong_mask.mean()) if len(strong_mask) > 0 else 0.0

    if breadth >= bull_min:
        return 'bull', breadth
    if breadth <= bear_max:
        return 'bear', breadth
    return 'neutral', breadth


def _normalize_weights(base_w: float, feature_w: float, radar_w: float, event_w: float) -> Dict[str, float]:
    total = base_w + feature_w + radar_w + event_w
    if total <= 0:
        return {'base': 0.4, 'feature': 0.3, 'radar': 0.2, 'event': 0.1}
    return {
        'base': base_w / total,
        'feature': feature_w / total,
        'radar': radar_w / total,
        'event': event_w / total,
    }


def _apply_regime_multiplier(regime: str, settings: Dict[str, float]) -> Dict[str, float]:
    if regime == 'bull':
        return _normalize_weights(
            settings['base_w'] * settings['bull_base_mult'],
            settings['feature_w'] * settings['bull_feature_mult'],
            settings['radar_w'] * settings['bull_radar_mult'],
            settings['event_w'] * settings['bull_event_mult'],
        )
    if regime == 'bear':
        return _normalize_weights(
            settings['base_w'] * settings['bear_base_mult'],
            settings['feature_w'] * settings['bear_feature_mult'],
            settings['radar_w'] * settings['bear_radar_mult'],
            settings['event_w'] * settings['bear_event_mult'],
        )
    return _normalize_weights(settings['base_w'], settings['feature_w'], settings['radar_w'], settings['event_w'])


def _event_score_map(event_signals: pd.DataFrame) -> Dict[str, float]:
    if event_signals is None or len(event_signals) == 0 or 'ticker' not in event_signals.columns:
        return {}

    temp = event_signals.copy()
    temp['ticker'] = temp['ticker'].astype(str).str.strip().str.upper()
    temp['event_score'] = pd.to_numeric(temp.get('event_score'), errors='coerce').fillna(0.0)
    grouped = temp.groupby('ticker', as_index=False)['event_score'].max()
    return dict(zip(grouped['ticker'], grouped['event_score']))


def apply_ranking_engine(
    dataset: pd.DataFrame,
    event_signals: pd.DataFrame,
    top_k_signals: int | None = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object]]:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame(), pd.DataFrame(), {'regime': 'neutral', 'breadth': 0.0, 'weights': {}}

    rank_settings = _resolve_rank_config()
    out = dataset.copy()
    event_map = _event_score_map(event_signals)

    regime, breadth = _detect_regime(out, bull_min=rank_settings['bull_min'], bear_max=rank_settings['bear_max'])
    weights = _apply_regime_multiplier(regime, rank_settings)

    base_score = _to_float_series(out, 'base_alpha_score_v1').clip(lower=-20, upper=120)
    feature_score = _to_float_series(out, 'feature_alpha_score_v1').clip(lower=-20, upper=120)
    radar_score = _to_float_series(out, 'multi_radar_score').clip(lower=-20, upper=120)
    monster_score = _to_float_series(out, 'monster_score').clip(lower=0, upper=100)

    out['event_score_v1'] = out.get('ticker', pd.Series('', index=out.index)).astype(str).str.upper().map(event_map).fillna(0.0)

    signal_count = (
        (feature_score > 0).astype(int) +
        (radar_score > 0).astype(int) +
        (out['event_score_v1'] > 0).astype(int) +
        out.get('is_in_ai_focus', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(int)
    )
    out['rank_signal_count'] = signal_count

    focus_bonus = out.get('is_in_ai_focus', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(float) * rank_settings['focus_bonus']
    fusion_bonus = out.get('is_in_fusion', pd.Series(False, index=out.index)).fillna(False).astype(bool).astype(float) * rank_settings['fusion_bonus']
    drawdown_penalty = _to_float_series(out, 'daily_change_pct').clip(upper=0).abs() * 0.25

    out['rank_score_v1'] = (
        base_score * weights['base'] +
        feature_score * weights['feature'] +
        radar_score * weights['radar'] +
        out['event_score_v1'].clip(lower=0, upper=150) * weights['event'] +
        monster_score * rank_settings['monster_bonus_w'] +
        focus_bonus +
        fusion_bonus -
        drawdown_penalty
    ).round(2)

    out['rank_regime'] = regime
    out['rank_breadth'] = round(breadth, 4)
    out['rank_engine_tier'] = 'C'
    out.loc[out['rank_score_v1'] >= rank_settings['tier_a_min'], 'rank_engine_tier'] = 'A'
    out.loc[(out['rank_score_v1'] >= rank_settings['tier_b_min']) & (out['rank_score_v1'] < rank_settings['tier_a_min']), 'rank_engine_tier'] = 'B'

    out = out.sort_values(
        ['rank_score_v1', 'event_score_v1', 'multi_radar_score', 'feature_alpha_score_v1', 'monster_score', 'ticker'],
        ascending=[False, False, False, False, False, True],
    ).reset_index(drop=True)
    out['rank_engine_rank'] = range(1, len(out) + 1)

    signal_mask = (out['rank_signal_count'] >= 2) | out['rank_engine_tier'].isin(['A', 'B'])
    ranking_signals = out[signal_mask].copy()
    ranking_signals = ranking_signals.sort_values(
        ['rank_score_v1', 'rank_signal_count', 'event_score_v1', 'ticker'],
        ascending=[False, False, False, True],
    ).head(int(top_k_signals or rank_settings['top_k']))

    signal_cols = [
        'ticker', 'rank_engine_rank', 'rank_engine_tier', 'rank_score_v1',
        'rank_signal_count', 'rank_regime', 'rank_breadth',
        'event_score_v1', 'feature_alpha_score_v1', 'multi_radar_score',
        'base_alpha_score_v1', 'monster_score', 'daily_change_pct', 'rel_volume',
        'is_in_ai_focus', 'is_in_fusion', 'is_in_monster_radar', 'is_in_xq',
    ]
    signal_cols = [c for c in signal_cols if c in ranking_signals.columns]
    ranking_signals = ranking_signals[signal_cols].reset_index(drop=True)

    return out, ranking_signals, {
        'regime': regime,
        'breadth': round(breadth, 4),
        'weights': {k: round(v, 4) for k, v in weights.items()},
        'top_k': int(top_k_signals or rank_settings['top_k']),
        'signal_rows': int(len(ranking_signals)),
    }
