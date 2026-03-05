from __future__ import annotations

from typing import Dict, Tuple

import pandas as pd
import config as settings_module


def _to_float_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors='coerce').fillna(default)


def _resolve_decision_config() -> Dict[str, float]:
    def _get(name: str, default: float) -> float:
        return float(getattr(settings_module, name, default))

    return {
        'top_k': _get('AI_DECISION_TOP_K', 80),
        'keep_min': _get('AI_DECISION_KEEP_MIN_SCORE', 42.0),
        'watch_min': _get('AI_DECISION_WATCH_MIN_SCORE', 30.0),
        'max_keep_risk': _get('AI_DECISION_MAX_KEEP_RISK_SCORE', 3.2),
        'entry_min_gain': _get('AI_DECISION_ENTRY_MIN_GAIN', 2.0),
        'entry_max_gain': _get('AI_DECISION_ENTRY_MAX_GAIN', 8.0),
        'strong_vol': _get('AI_DECISION_STRONG_VOL', 1.8),
        'low_vol': _get('AI_DECISION_LOW_VOL', 1.3),
        'overheat_gain': _get('AI_DECISION_OVERHEAT_GAIN', 12.0),
        'scanner_profile': str(getattr(settings_module, 'SCANNER_PROFILE', 'balanced')).strip().lower(),
        'monster_price_min': _get('SCANNER_MONSTER_PRICE_MIN', 2.0),
        'monster_price_max': _get('SCANNER_MONSTER_PRICE_MAX', 20.0),
        'monster_mcap_max': _get('SCANNER_MONSTER_MCAP_MAX', 2_000_000_000),
        'monster_relvol_min': _get('SCANNER_MONSTER_RELVOL_MIN', 3.0),
        'monster_day_change_min': _get('SCANNER_MONSTER_DAY_CHANGE_MIN', 5.0),
        'monster_dollar_vol_m_min': _get('SCANNER_MONSTER_DOLLAR_VOL_M_MIN', 10.0),
        'monster_float_tightness_min': _get('SCANNER_MONSTER_FLOAT_TIGHTNESS_MIN', 6.0),
        'monster_float_rotation_min': _get('SCANNER_MONSTER_FLOAT_ROTATION_MIN', 0.03),
        'monster_keep_min_score': _get('SCANNER_MONSTER_KEEP_MIN_SCORE', 34.0),
        'monster_watch_min_score': _get('SCANNER_MONSTER_WATCH_MIN_SCORE', 22.0),
    }


def _build_action(daily_change: pd.Series, rel_volume: pd.Series, cfg: Dict[str, float]) -> pd.Series:
    action = pd.Series('先觀望', index=daily_change.index, dtype=object)

    batch_entry = (
        daily_change.between(cfg['entry_min_gain'], cfg['entry_max_gain'], inclusive='both') &
        (rel_volume >= cfg['strong_vol'])
    )
    pullback_wait = daily_change > cfg['entry_max_gain']

    action.loc[batch_entry] = '可分批進場'
    action.loc[pullback_wait] = '等回踩 1-2% 再評估'
    return action


def _risk_level(risk_score: pd.Series) -> pd.Series:
    level = pd.Series('中', index=risk_score.index, dtype=object)
    level.loc[risk_score >= 4.5] = '高'
    level.loc[risk_score <= 2.2] = '低'
    return level


def apply_decision_risk_layer(
    dataset: pd.DataFrame,
    top_k_signals: int | None = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object]]:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame(), pd.DataFrame(), {'decision_rows': 0}

    cfg = _resolve_decision_config()
    out = dataset.copy()

    rank_score = _to_float_series(out, 'rank_score_v1')
    price = _to_float_series(out, 'price')
    market_cap = _to_float_series(out, 'market_cap_raw')
    daily_change = _to_float_series(out, 'daily_change_pct')
    rel_volume = _to_float_series(out, 'rel_volume')
    dollar_volume_m = _to_float_series(out, 'xq_dollar_volume_m')
    float_tightness = _to_float_series(out, 'float_tightness_proxy')
    volatility = _to_float_series(out, 'volatility_proxy_pct')
    event_score = _to_float_series(out, 'event_score_v1')

    out['scanner_profile'] = cfg['scanner_profile'] if cfg['scanner_profile'] in {'balanced', 'monster_v1'} else 'balanced'
    out['float_rotation_proxy'] = (
        (dollar_volume_m * 1_000_000.0) / market_cap.replace(0, pd.NA)
    ).fillna(0).clip(lower=0, upper=3).round(4)

    if out['scanner_profile'].iloc[0] == 'monster_v1':
        out['scanner_pass_v1'] = (
            price.between(cfg['monster_price_min'], cfg['monster_price_max'], inclusive='both') &
            (market_cap > 0) &
            (market_cap <= cfg['monster_mcap_max']) &
            (rel_volume >= cfg['monster_relvol_min']) &
            (daily_change >= cfg['monster_day_change_min']) &
            (dollar_volume_m >= cfg['monster_dollar_vol_m_min']) &
            (float_tightness >= cfg['monster_float_tightness_min']) &
            (out['float_rotation_proxy'] >= cfg['monster_float_rotation_min'])
        )
    else:
        out['scanner_pass_v1'] = True

    overheat_penalty = ((daily_change > cfg['overheat_gain']) & (rel_volume < cfg['low_vol'])).astype(float) * 2.0
    low_volume_penalty = (rel_volume < cfg['low_vol']).astype(float) * 1.6
    volatility_penalty = volatility.clip(lower=0, upper=60) * 0.05
    event_buffer = (event_score >= 20).astype(float) * 0.8

    out['risk_score_v1'] = (
        1.5 +
        overheat_penalty +
        low_volume_penalty +
        volatility_penalty -
        event_buffer
    ).clip(lower=0.5, upper=8.0).round(2)

    out['risk_level'] = _risk_level(out['risk_score_v1'])
    out['decision_action'] = _build_action(daily_change=daily_change, rel_volume=rel_volume, cfg=cfg)

    out['decision_tag_v1'] = 'replace_candidate'
    profile = out['scanner_profile'].iloc[0]
    keep_min_score = cfg['monster_keep_min_score'] if profile == 'monster_v1' else cfg['keep_min']
    watch_min_score = cfg['monster_watch_min_score'] if profile == 'monster_v1' else cfg['watch_min']

    keep_mask = (
        (rank_score >= keep_min_score) &
        (rel_volume >= cfg['strong_vol']) &
        (out['risk_score_v1'] <= cfg['max_keep_risk']) &
        out['scanner_pass_v1'].astype(bool)
    )
    watch_mask = (
        (rank_score >= watch_min_score) &
        out['scanner_pass_v1'].astype(bool) &
        (out['decision_tag_v1'] != 'keep')
    )

    out.loc[keep_mask, 'decision_tag_v1'] = 'keep'
    out.loc[watch_mask & (~keep_mask), 'decision_tag_v1'] = 'watch'

    out['invalidation_rule'] = '跌破前一日低點或量能掉到 1.0x 以下'
    out.loc[out['decision_action'] == '可分批進場', 'invalidation_rule'] = '跌破 VWAP 或收盤弱於開盤，次日不延續即撤退'
    out.loc[out['decision_action'] == '等回踩 1-2% 再評估', 'invalidation_rule'] = '回踩後量縮且守不住 VWAP，視為失效'

    out = out.sort_values(
        ['decision_tag_v1', 'rank_score_v1', 'event_score_v1', 'multi_radar_score', 'ticker'],
        ascending=[True, False, False, False, True],
    ).reset_index(drop=True)

    decision_signals = out[out['decision_tag_v1'].isin(['keep', 'watch'])].copy()
    decision_signals = decision_signals.sort_values(
        ['decision_tag_v1', 'rank_score_v1', 'risk_score_v1', 'ticker'],
        ascending=[True, False, True, True],
    ).head(int(top_k_signals or cfg['top_k']))

    signal_cols = [
        'ticker', 'decision_tag_v1', 'decision_action', 'risk_level', 'risk_score_v1',
        'invalidation_rule', 'rank_score_v1', 'rank_engine_tier', 'rank_engine_rank',
        'scanner_profile', 'scanner_pass_v1', 'float_rotation_proxy',
        'event_score_v1', 'feature_alpha_score_v1', 'multi_radar_score',
        'daily_change_pct', 'rel_volume', 'monster_score',
        'is_in_ai_focus', 'is_in_fusion', 'is_in_monster_radar', 'is_in_xq',
    ]
    signal_cols = [c for c in signal_cols if c in decision_signals.columns]
    decision_signals = decision_signals[signal_cols].reset_index(drop=True)

    keep_count = int((decision_signals.get('decision_tag_v1') == 'keep').sum()) if len(decision_signals) > 0 else 0
    watch_count = int((decision_signals.get('decision_tag_v1') == 'watch').sum()) if len(decision_signals) > 0 else 0

    return out, decision_signals, {
        'decision_rows': int(len(decision_signals)),
        'keep_count': keep_count,
        'watch_count': watch_count,
        'scanner_profile': out['scanner_profile'].iloc[0] if len(out) > 0 else 'balanced',
        'scanner_pass_count': int(out['scanner_pass_v1'].astype(bool).sum()) if len(out) > 0 else 0,
    }
