from __future__ import annotations

from typing import Dict, Tuple

import pandas as pd
import config as settings_module

from .contracts import parse_probability_mid


def _to_float_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors='coerce').fillna(default)


def _normalize_01(value: pd.Series, min_v: float, max_v: float) -> pd.Series:
    if max_v <= min_v:
        return pd.Series(0.0, index=value.index, dtype=float)
    out = (value.astype(float) - float(min_v)) / float(max_v - min_v)
    return out.clip(lower=0.0, upper=1.0)


def _append_reason(reason: pd.Series, mask: pd.Series, token: str) -> pd.Series:
    token = str(token or '').strip()
    if not token:
        return reason
    if not isinstance(mask, pd.Series):
        mask = pd.Series(bool(mask), index=reason.index)
    else:
        mask = mask.fillna(False).astype(bool)
    current = reason.astype(str)
    reason.loc[mask] = current.loc[mask].apply(lambda s: token if not s else f'{s}|{token}')
    return reason


def _resolve_decision_config() -> Dict[str, float]:
    def _get(name: str, default: float) -> float:
        return float(getattr(settings_module, name, default))

    def _get_bool(name: str, default: bool = False) -> bool:
        return bool(getattr(settings_module, name, default))

    return {
        'top_k': _get('AI_DECISION_TOP_K', 80),
        'keep_min': _get('AI_DECISION_KEEP_MIN_SCORE', 42.0),
        'watch_min': _get('AI_DECISION_WATCH_MIN_SCORE', 30.0),
        'max_keep_risk': _get('AI_DECISION_MAX_KEEP_RISK_SCORE', 3.2),
        'keep_min_live_relax': _get('AI_DECISION_KEEP_MIN_LIVE_RELAX', 8.0),
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
        'followthrough_keep_min': _get('AI_DECISION_FOLLOWTHROUGH_KEEP_MIN', 30.0),
        'followthrough_watch_min': _get('AI_DECISION_FOLLOWTHROUGH_WATCH_MIN', 20.0),
        'keep_live_relax': _get('AI_DECISION_KEEP_LIVE_RELAX', 5.0),
        'keep_promote_verif_min': _get('AI_DECISION_KEEP_PROMOTE_VERIF_MIN', 70.0),
        'exhaustion_avoid_min': _get('AI_DECISION_EXHAUSTION_AVOID_MIN', 70.0),
        'exhaustion_neutral_min': _get('AI_DECISION_EXHAUSTION_NEUTRAL_MIN', 45.0),
        'exhaustion_watch_min': _get('AI_DECISION_EXHAUSTION_WATCH_MIN', 55.0),
        'exhaustion_ignition_max': _get('AI_DECISION_EXHAUSTION_IGNITION_MAX', 35.0),
        'overnight_room_gap_max': _get('AI_DECISION_OVERNIGHT_ROOM_GAP_MAX', 5.0),
        'high_gap_block': _get('AI_DECISION_HIGH_GAP_BLOCK', 16.0),
        'proxy_gap_relax': _get('AI_DECISION_PROXY_GAP_RELAX', 4.0),
        'close_loc_min': _get('AI_DECISION_CLOSE_LOCATION_MIN', 0.55),
        'upper_wick_block_pct': _get('AI_DECISION_UPPER_WICK_BLOCK_PCT', 3.0),
        'catalyst_verif_high': _get('AI_DECISION_CATALYST_VERIF_HIGH', 85.0),
        'catalyst_verif_mid': _get('AI_DECISION_CATALYST_VERIF_MID', 70.0),
        'catalyst_strength_pullback_min': _get('AI_DECISION_CATALYST_STRENGTH_PULLBACK_MIN', 60.0),
        'catalyst_strength_monster_min': _get('AI_DECISION_CATALYST_STRENGTH_MONSTER_MIN', 75.0),
        'premarket_default_gap': _get('AI_DECISION_PREMARKET_DEFAULT_GAP', 0.0),
        'keep_max_count': _get('AI_DECISION_KEEP_MAX_COUNT', 2),
        'watch_max_count': _get('AI_DECISION_WATCH_MAX_COUNT', 6),
        'total_max_count': _get('AI_DECISION_TOTAL_MAX_COUNT', 8),
        'cap_relax_live_verified': _get_bool('AI_DECISION_CAP_RELAX_LIVE_VERIFIED', False),
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


def _normalize_overnight_catalyst(df: pd.DataFrame) -> pd.Series:
    if 'overnight_catalyst' not in df.columns:
        return pd.Series('', index=df.index, dtype=object)
    out = df['overnight_catalyst'].fillna('').astype(str).str.strip().str.lower()
    return out.replace({'none': '', 'nan': '', 'null': ''})


def _to_bool_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    return df[col].fillna(False).astype(bool)


def apply_decision_risk_layer(
    dataset: pd.DataFrame,
    top_k_signals: int | None = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object]]:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame(), pd.DataFrame(), {'decision_rows': 0}

    cfg = _resolve_decision_config()
    out = dataset.copy()
    out['promote_to_keep_reason'] = ''
    out['cap_hit_flag'] = False

    rank_col = 'rank_score_v2_adjusted' if 'rank_score_v2_adjusted' in out.columns else 'rank_score_v1'
    rank_score = _to_float_series(out, rank_col)
    out['pre_gate_rank'] = rank_score.rank(method='first', ascending=False).astype(int)
    price = _to_float_series(out, 'price')
    market_cap = _to_float_series(out, 'market_cap_raw')
    daily_change = _to_float_series(out, 'daily_change_pct')
    rel_volume = _to_float_series(out, 'rel_volume')
    vol_strength = _to_float_series(out, 'xq_vol_strength')
    dollar_volume_m = _to_float_series(out, 'xq_dollar_volume_m')
    float_tightness = _to_float_series(out, 'float_tightness_proxy')
    volatility = _to_float_series(out, 'volatility_proxy_pct')
    event_score = _to_float_series(out, 'event_score_v1')
    vwap = _to_float_series(out, 'tv_vwap')
    xq_last = _to_float_series(out, 'xq_last') if 'xq_last' in out.columns else price.copy()
    xq_high_5d = _to_float_series(out, 'xq_high_5d')
    xq_low_5d = _to_float_series(out, 'xq_low_5d')

    premarket_raw = (
        pd.to_numeric(out['premarket_gap_pct'], errors='coerce')
        if 'premarket_gap_pct' in out.columns
        else pd.Series(float('nan'), index=out.index, dtype=float)
    )
    close_location_raw = (
        pd.to_numeric(out['close_location_value'], errors='coerce')
        if 'close_location_value' in out.columns
        else pd.Series(float('nan'), index=out.index, dtype=float)
    )
    upper_wick_raw = (
        pd.to_numeric(out['upper_wick_pct'], errors='coerce')
        if 'upper_wick_pct' in out.columns
        else pd.Series(float('nan'), index=out.index, dtype=float)
    )
    market_snapshot_live = _to_bool_series(out, 'market_snapshot_live')
    out['market_snapshot_live'] = market_snapshot_live

    premarket_gap = premarket_raw.fillna(cfg['premarket_default_gap'])
    sqzmom_hist = _to_float_series(out, 'tv_sqzmom_hist')
    sqz_on = _to_bool_series(out, 'tv_sqz_on')
    sqzmom_color = out.get('tv_sqzmom_color', pd.Series('', index=out.index)).fillna('').astype(str).str.lower()
    momentum_accel_1d3d = _to_float_series(out, 'momentum_accel_1d3d')
    momentum_accel_3d5d = _to_float_series(out, 'momentum_accel_3d5d')
    overnight_catalyst = _normalize_overnight_catalyst(out)

    out['overnight_catalyst'] = overnight_catalyst

    range_width = (xq_high_5d - xq_low_5d).abs()
    close_location_proxy = ((xq_last - xq_low_5d) / range_width.replace(0, pd.NA)).fillna(0.5).clip(lower=0.0, upper=1.0)
    upper_wick_proxy = ((xq_high_5d - xq_last).clip(lower=0.0) / xq_last.replace(0, pd.NA) * 100.0).fillna(0.0).clip(lower=0.0, upper=100.0)

    close_location = close_location_raw.where(close_location_raw.between(0.0, 1.0), pd.NA).fillna(close_location_proxy)
    upper_wick = upper_wick_raw.where(upper_wick_raw >= 0.0, pd.NA).fillna(upper_wick_proxy)
    premarket_missing_mask = premarket_raw.isna()
    close_location_proxy_mask = close_location_raw.isna()
    upper_wick_proxy_mask = upper_wick_raw.isna()

    out['premarket_gap_pct'] = premarket_gap.round(2)
    out['close_location_value'] = close_location.round(4)
    out['upper_wick_pct'] = upper_wick.round(2)

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

    catalyst_time = pd.to_datetime(
        out.get('overnight_catalyst_time', pd.Series('', index=out.index)),
        errors='coerce',
        utc=True,
    )
    now_ts = pd.Timestamp.now(tz='UTC')
    age_hours = ((now_ts - catalyst_time).dt.total_seconds() / 3600.0).fillna(999.0)

    catalyst_verif = pd.Series(35.0, index=out.index, dtype=float)
    catalyst_verif.loc[overnight_catalyst.isin(['hard_positive', 'hard_negative'])] = 90.0
    catalyst_verif.loc[overnight_catalyst.isin(['soft_positive', 'soft_negative'])] = 70.0
    catalyst_verif.loc[overnight_catalyst.eq('neutral')] = 50.0
    catalyst_verif += _normalize_01(event_score, 0.0, 100.0) * 10.0
    out['catalyst_verifiability_score'] = catalyst_verif.clip(lower=0.0, upper=100.0).round(2)

    catalyst_freshness = pd.Series(35.0, index=out.index, dtype=float)
    catalyst_freshness.loc[age_hours <= 24.0] = 95.0
    catalyst_freshness.loc[(age_hours > 24.0) & (age_hours <= 72.0)] = 75.0
    catalyst_freshness.loc[(age_hours > 72.0) & (age_hours <= 168.0)] = 55.0
    catalyst_freshness.loc[overnight_catalyst.eq('')] = 40.0
    out['catalyst_freshness_score'] = catalyst_freshness.clip(lower=0.0, upper=100.0).round(2)

    catalyst_strength = (
        out['catalyst_verifiability_score'] * 0.45 +
        out['catalyst_freshness_score'] * 0.35 +
        _normalize_01(event_score, 0.0, 100.0) * 100.0 * 0.20
    )
    out['catalyst_strength_score'] = catalyst_strength.clip(lower=0.0, upper=100.0).round(2)

    exhaustion_score = (
        _normalize_01(out['premarket_gap_pct'].clip(lower=0.0), 0.0, 20.0) * 0.30 +
        _normalize_01(daily_change.clip(lower=0.0), 0.0, 20.0) * 0.20 +
        _normalize_01(rel_volume, 0.0, 8.0) * 0.15 +
        _normalize_01(volatility, 0.0, 40.0) * 0.15 +
        (1.0 - out['close_location_value'].clip(lower=0.0, upper=1.0)) * 0.10 +
        _normalize_01(out['upper_wick_pct'], 0.0, 8.0) * 0.10
    ) * 100.0
    out['open_exhaustion_risk_score'] = exhaustion_score.clip(lower=0.0, upper=100.0).round(2)

    followthrough_score = (
        _normalize_01(rank_score, 0.0, 120.0) * 0.22 +
        _normalize_01(_to_float_series(out, 'trend_persistence_score'), 0.0, 20.0) * 0.16 +
        _normalize_01(_to_float_series(out, 'feature_alpha_score_v1'), 0.0, 80.0) * 0.14 +
        _normalize_01(_to_float_series(out, 'multi_radar_score'), 0.0, 100.0) * 0.12 +
        _normalize_01(event_score, 0.0, 100.0) * 0.10 +
        _normalize_01(out['catalyst_strength_score'], 0.0, 100.0) * 0.10 +
        (1.0 - _normalize_01(out['open_exhaustion_risk_score'], 0.0, 100.0)) * 0.08 +
        (1.0 - _normalize_01(out['risk_score_v1'], 0.5, 8.0)) * 0.08
    ) * 100.0
    out['overnight_followthrough_score'] = followthrough_score.clip(lower=0.0, upper=100.0).round(2)

    close_above_vwap = (price >= vwap) & vwap.gt(0)
    sqzmom_positive = (sqzmom_hist > 0) | sqzmom_color.isin(['green', 'lime'])
    sqzmom_uptrend = ((sqzmom_hist > 0) & (momentum_accel_1d3d > 0)) | (
        sqzmom_color.isin(['green', 'lime']) & (momentum_accel_1d3d >= 0) & (momentum_accel_3d5d >= 0)
    )

    out['tomorrow_entry_readiness'] = 'neutral'
    avoid_chase_mask = (daily_change > 15.0) & (~overnight_catalyst.eq('hard_positive'))
    ignition_ready_mask = daily_change.between(3.0, 10.0, inclusive='both') & (vol_strength > 1.5) & close_above_vwap & sqzmom_positive
    pullback_watch_mask = (daily_change < 5.0) & sqz_on & sqzmom_uptrend

    out.loc[avoid_chase_mask, 'tomorrow_entry_readiness'] = 'avoid_chase'
    out.loc[ignition_ready_mask & (~avoid_chase_mask), 'tomorrow_entry_readiness'] = 'ignition_ready'
    out.loc[pullback_watch_mask & (~avoid_chase_mask) & (~ignition_ready_mask), 'tomorrow_entry_readiness'] = 'pullback_watch'

    high_exhaustion = out['open_exhaustion_risk_score'] >= cfg['exhaustion_avoid_min']
    mid_exhaustion = out['open_exhaustion_risk_score'].between(cfg['exhaustion_neutral_min'], cfg['exhaustion_avoid_min'], inclusive='left')
    pullback_ready = (
        (out['open_exhaustion_risk_score'] < cfg['exhaustion_neutral_min']) &
        (out['catalyst_strength_score'] >= cfg['catalyst_strength_pullback_min'])
    )
    ignition_ready_v2 = (
        (out['open_exhaustion_risk_score'] < cfg['exhaustion_ignition_max']) &
        (out['close_location_value'] >= 0.75) &
        (out['premarket_gap_pct'] <= cfg['overnight_room_gap_max']) &
        (out['catalyst_strength_score'] >= cfg['catalyst_strength_pullback_min'])
    )

    out.loc[mid_exhaustion, 'tomorrow_entry_readiness'] = 'neutral'
    out.loc[pullback_ready & (~mid_exhaustion) & (~high_exhaustion), 'tomorrow_entry_readiness'] = 'pullback_watch'
    out.loc[ignition_ready_v2 & (~high_exhaustion), 'tomorrow_entry_readiness'] = 'ignition_ready'
    out.loc[high_exhaustion, 'tomorrow_entry_readiness'] = 'avoid_chase'

    base_prob = out.get('prob_next_day', pd.Series('', index=out.index)).apply(parse_probability_mid)
    setup_type = out.get('setup_type', pd.Series('', index=out.index)).fillna('').astype(str).str.strip().str.lower()
    overnight_missing = overnight_catalyst.eq('')

    adjusted_prob = base_prob.astype(float).copy()
    hard_positive_mask = overnight_catalyst.eq('hard_positive')
    hard_negative_mask = overnight_catalyst.eq('hard_negative')
    spike_no_catalyst_mask = (daily_change > 15.0) & overnight_missing
    compression_setup_mask = (daily_change < 5.0) & setup_type.eq('compression_setup')

    adjusted_prob.loc[spike_no_catalyst_mask] = base_prob.loc[spike_no_catalyst_mask] * 0.7
    adjusted_prob.loc[compression_setup_mask] = base_prob.loc[compression_setup_mask] * 1.2
    adjusted_prob.loc[hard_positive_mask] = (base_prob.loc[hard_positive_mask] * 1.35).clip(upper=90.0)
    adjusted_prob.loc[hard_negative_mask] = base_prob.loc[hard_negative_mask] * 0.4
    followthrough_mapped = (25.0 + out['overnight_followthrough_score'] * 0.60).clip(lower=0.0, upper=99.0)
    adjusted_prob = adjusted_prob.where(adjusted_prob > 0, followthrough_mapped)
    adjusted_prob = (adjusted_prob * 0.4 + followthrough_mapped * 0.6)
    out['tomorrow_continuation_prob_adjusted'] = adjusted_prob.clip(lower=0.0, upper=99.0).round(2)

    out['decision_tag_v1'] = 'replace_candidate'
    profile = out['scanner_profile'].iloc[0]
    keep_min_score = cfg['monster_keep_min_score'] if profile == 'monster_v1' else cfg['keep_min']
    watch_min_score = cfg['monster_watch_min_score'] if profile == 'monster_v1' else cfg['watch_min']
    keep_rank_min = pd.Series(keep_min_score, index=out.index, dtype=float)
    keep_rank_min = keep_rank_min.where(~market_snapshot_live, keep_rank_min - cfg['keep_min_live_relax'])
    keep_rank_min = keep_rank_min.clip(lower=0.0)

    hard_limit_risk = out['risk_level'].astype(str).eq('高')
    hard_limit_exhaustion = out['open_exhaustion_risk_score'] >= cfg['exhaustion_avoid_min']

    live_gap_unverified = (
        market_snapshot_live &
        (out['premarket_gap_pct'] >= cfg['high_gap_block']) &
        (out['catalyst_verifiability_score'] < cfg['catalyst_verif_mid'])
    )
    proxy_gap_unverified = (
        (~market_snapshot_live) &
        (out['premarket_gap_pct'] >= (cfg['high_gap_block'] + cfg['proxy_gap_relax'])) &
        (out['catalyst_verifiability_score'] < cfg['catalyst_verif_mid'])
    )
    wick_close_downgrade = (
        (out['close_location_value'] < cfg['close_loc_min']) &
        (out['upper_wick_pct'] > cfg['upper_wick_block_pct'])
    )
    overheat_downgrade = (daily_change >= cfg['overheat_gain']) & (~overnight_catalyst.isin(['soft_positive', 'hard_positive']))

    hard_limit_mask = hard_limit_risk | hard_limit_exhaustion | live_gap_unverified

    out['protocol_gate_reason'] = ''
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], hard_limit_risk, 'risk_too_high')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], hard_limit_exhaustion, 'exhaustion_hard_block')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], live_gap_unverified, 'premarket_too_hot')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], live_gap_unverified, 'catalyst_unverified')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], proxy_gap_unverified, 'premarket_too_hot_proxy')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], proxy_gap_unverified, 'catalyst_unverified')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], wick_close_downgrade, 'weak_close')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], wick_close_downgrade, 'wick_exhaustion')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], overheat_downgrade, 'overheat_chase_risk')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], premarket_missing_mask, 'premarket_source_missing')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], close_location_proxy_mask, 'close_location_proxy_used')
    out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], upper_wick_proxy_mask, 'upper_wick_proxy_used')

    keep_followthrough_min = pd.Series(cfg['followthrough_keep_min'], index=out.index, dtype=float)
    keep_followthrough_min = keep_followthrough_min.where(~market_snapshot_live, keep_followthrough_min - cfg['keep_live_relax'])
    keep_followthrough_min = keep_followthrough_min.clip(lower=0.0)

    keep_mask = (
        (rank_score >= keep_rank_min) &
        (rel_volume >= cfg['strong_vol']) &
        (out['risk_score_v1'] <= cfg['max_keep_risk']) &
        out['scanner_pass_v1'].astype(bool) &
        (out['overnight_followthrough_score'] >= keep_followthrough_min) &
        (out['open_exhaustion_risk_score'] < cfg['exhaustion_watch_min']) &
        (~proxy_gap_unverified) &
        (~wick_close_downgrade) &
        (~overheat_downgrade) &
        (~hard_limit_mask)
    )
    watch_mask = (
        (rank_score >= watch_min_score) &
        out['scanner_pass_v1'].astype(bool) &
        (out['overnight_followthrough_score'] >= cfg['followthrough_watch_min']) &
        (out['open_exhaustion_risk_score'] < cfg['exhaustion_avoid_min']) &
        (~hard_limit_mask) &
        (out['decision_tag_v1'] != 'keep')
    )

    out.loc[keep_mask, 'decision_tag_v1'] = 'keep'
    out.loc[watch_mask & (~keep_mask), 'decision_tag_v1'] = 'watch'

    watch_downgrade_mask = (
        out['decision_tag_v1'].eq('keep') &
        (
            out['open_exhaustion_risk_score'].between(cfg['exhaustion_watch_min'], cfg['exhaustion_avoid_min'], inclusive='left') |
            proxy_gap_unverified |
            wick_close_downgrade |
            overheat_downgrade
        )
    )
    out.loc[watch_downgrade_mask, 'decision_tag_v1'] = 'watch'

    promote_mask = (
        out['decision_tag_v1'].eq('watch') &
        market_snapshot_live &
        (out['overnight_followthrough_score'] >= keep_followthrough_min) &
        (out['open_exhaustion_risk_score'] < cfg['exhaustion_watch_min']) &
        (out['catalyst_verifiability_score'] >= cfg['keep_promote_verif_min']) &
        (~proxy_gap_unverified) &
        (~wick_close_downgrade) &
        (~hard_limit_mask)
    )
    out.loc[promote_mask, 'decision_tag_v1'] = 'keep'
    out.loc[promote_mask, 'promote_to_keep_reason'] = 'live_followthrough_exhaustion_catalyst'

    keep_cap = max(0, int(cfg['keep_max_count']))
    total_cap = max(1, int(cfg['total_max_count']))
    relax_live_verified_mask = (
        market_snapshot_live &
        (out['catalyst_verifiability_score'] >= cfg['keep_promote_verif_min'])
    )
    if bool(cfg.get('cap_relax_live_verified', False)) and bool(relax_live_verified_mask.any()):
        keep_cap = keep_cap + 1
        total_cap = total_cap + 1

    if keep_cap > 0:
        keep_idx = out.index[out['decision_tag_v1'].eq('keep')].tolist()
        if len(keep_idx) > keep_cap:
            keep_order = out.loc[keep_idx].sort_values(
                ['market_snapshot_live', 'overnight_followthrough_score', rank_col],
                ascending=[False, False, False],
            )
            demote_idx = keep_order.index[keep_cap:]
            out.loc[demote_idx, 'decision_tag_v1'] = 'watch'
            demote_mask = pd.Series(out.index.isin(demote_idx), index=out.index)
            out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], demote_mask, 'keep_cap_downgrade')
            out.loc[demote_mask, 'cap_hit_flag'] = True

    watch_cap = max(0, int(cfg['watch_max_count']))
    eligible = out[out['decision_tag_v1'].isin(['keep', 'watch'])].copy()
    if len(eligible) > 0:
        eligible = eligible.assign(
            _decision_order=eligible['decision_tag_v1'].map({'keep': 0, 'watch': 1}).fillna(2),
            _reason_count=eligible['protocol_gate_reason'].fillna('').astype(str).str.count(r'\|') + eligible['protocol_gate_reason'].fillna('').astype(str).ne('').astype(int),
        ).sort_values(
            ['_decision_order', 'market_snapshot_live', 'overnight_followthrough_score', rank_col, '_reason_count'],
            ascending=[True, False, False, False, True],
        )

        if watch_cap > 0:
            keep_n = int((eligible['decision_tag_v1'] == 'keep').sum())
            watch_idx = eligible.index[eligible['decision_tag_v1'].eq('watch')]
            keep_slots = min(total_cap, keep_n)
            watch_slots = max(0, min(watch_cap, total_cap - keep_slots))
            if len(watch_idx) > watch_slots:
                prune_watch_idx = watch_idx[watch_slots:]
                out.loc[prune_watch_idx, 'decision_tag_v1'] = 'replace_candidate'
                prune_watch_mask = pd.Series(out.index.isin(prune_watch_idx), index=out.index)
                out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], prune_watch_mask, 'watch_cap_pruned')
                out.loc[prune_watch_mask, 'cap_hit_flag'] = True

        eligible = out[out['decision_tag_v1'].isin(['keep', 'watch'])].copy()
        if len(eligible) > total_cap:
            eligible = eligible.assign(
                _decision_order=eligible['decision_tag_v1'].map({'keep': 0, 'watch': 1}).fillna(2),
                _reason_count=eligible['protocol_gate_reason'].fillna('').astype(str).str.count(r'\|') + eligible['protocol_gate_reason'].fillna('').astype(str).ne('').astype(int),
            ).sort_values(
                ['_decision_order', 'market_snapshot_live', 'overnight_followthrough_score', rank_col, '_reason_count'],
                ascending=[True, False, False, False, True],
            )
            prune_total_idx = eligible.index[total_cap:]
            out.loc[prune_total_idx, 'decision_tag_v1'] = 'replace_candidate'
            prune_total_mask = pd.Series(out.index.isin(prune_total_idx), index=out.index)
            out['protocol_gate_reason'] = _append_reason(out['protocol_gate_reason'], prune_total_mask, 'total_cap_pruned')
            out.loc[prune_total_mask, 'cap_hit_flag'] = True

    out.loc[out['open_exhaustion_risk_score'] >= cfg['exhaustion_neutral_min'], 'decision_action'] = '等回踩 1-2% 再評估'
    out.loc[out['open_exhaustion_risk_score'] >= cfg['exhaustion_avoid_min'], 'decision_action'] = '先觀望'
    out.loc[proxy_gap_unverified | wick_close_downgrade | overheat_downgrade, 'decision_action'] = '等回踩 1-2% 再評估'
    out.loc[out['decision_tag_v1'].eq('keep'), 'decision_action'] = '可分批進場'

    out['monster_overlay'] = 'no'
    monster_overlay_mask = (
        (out['catalyst_verifiability_score'] >= cfg['catalyst_verif_high']) &
        (out['catalyst_strength_score'] >= cfg['catalyst_strength_monster_min']) &
        (out['open_exhaustion_risk_score'] < cfg['exhaustion_neutral_min'])
    )
    out.loc[monster_overlay_mask, 'monster_overlay'] = 'yes'

    out['post_gate_decision'] = out['decision_tag_v1']
    out['gate_stage'] = 'replace'
    out.loc[out['decision_tag_v1'].eq('watch'), 'gate_stage'] = 'watch'
    out.loc[out['decision_tag_v1'].eq('keep'), 'gate_stage'] = 'keep'
    out.loc[watch_downgrade_mask | proxy_gap_unverified | wick_close_downgrade | overheat_downgrade, 'gate_stage'] = 'downgrade_watch'
    out.loc[promote_mask, 'gate_stage'] = 'promoted_keep'
    out.loc[hard_limit_mask, 'gate_stage'] = 'hard_block'
    out.loc[out['cap_hit_flag'].astype(bool), 'gate_stage'] = 'cap_pruned'

    selected_order = out[out['decision_tag_v1'].isin(['keep', 'watch'])].sort_values(
        ['decision_tag_v1', rank_col, 'overnight_followthrough_score', 'ticker'],
        ascending=[True, False, False, True],
    )
    selected_rank = pd.Series(range(1, len(selected_order) + 1), index=selected_order.index, dtype=int)
    out['decision_rank_after_gate'] = 0
    out.loc[selected_rank.index, 'decision_rank_after_gate'] = selected_rank

    out['final_elimination_owner'] = 'selected'
    out.loc[out['decision_tag_v1'].eq('keep'), 'final_elimination_owner'] = 'selected_keep'
    out.loc[out['decision_tag_v1'].eq('watch'), 'final_elimination_owner'] = 'selected_watch'
    replace_mask = out['decision_tag_v1'].eq('replace_candidate')
    out.loc[replace_mask, 'final_elimination_owner'] = 'other_gate'
    out.loc[replace_mask & hard_limit_exhaustion, 'final_elimination_owner'] = 'exhaustion_hard_block'
    out.loc[replace_mask & live_gap_unverified & out['final_elimination_owner'].eq('other_gate'), 'final_elimination_owner'] = 'live_gap_block'
    out.loc[replace_mask & hard_limit_risk & out['final_elimination_owner'].eq('other_gate'), 'final_elimination_owner'] = 'risk_block'
    out.loc[replace_mask & out['cap_hit_flag'].astype(bool) & out['final_elimination_owner'].eq('other_gate'), 'final_elimination_owner'] = 'top_n_cap'
    out.loc[
        replace_mask
        & (watch_downgrade_mask | proxy_gap_unverified | wick_close_downgrade | overheat_downgrade)
        & out['final_elimination_owner'].eq('other_gate'),
        'final_elimination_owner',
    ] = 'downgrade_rule'

    out['invalidation_rule'] = '跌破前一日低點或量能掉到 1.0x 以下'
    out.loc[out['decision_action'] == '可分批進場', 'invalidation_rule'] = '跌破 VWAP 或收盤弱於開盤，次日不延續即撤退'
    out.loc[out['decision_action'] == '等回踩 1-2% 再評估', 'invalidation_rule'] = '回踩後量縮且守不住 VWAP，視為失效'
    out.loc[live_gap_unverified | proxy_gap_unverified | wick_close_downgrade, 'invalidation_rule'] = '開高失守盤前高且跌破 VWAP，視為隔夜動能失效'

    out = out.sort_values(
        ['decision_tag_v1', rank_col, 'event_score_v1', 'multi_radar_score', 'ticker'],
        ascending=[True, False, False, False, True],
    ).reset_index(drop=True)

    decision_signals = out[out['decision_tag_v1'].isin(['keep', 'watch'])].copy()
    decision_signals = decision_signals.sort_values(
        ['decision_tag_v1', rank_col, 'risk_score_v1', 'ticker'],
        ascending=[True, False, True, True],
    ).head(int(top_k_signals or cfg['top_k']))

    signal_cols = [
        'ticker', 'decision_tag_v1', 'decision_action', 'risk_level', 'risk_score_v1',
        'invalidation_rule', 'rank_score_v1', 'rank_score_v2_adjusted', 'rank_engine_tier', 'rank_engine_rank',
        'overnight_catalyst', 'setup_type', 'tomorrow_entry_readiness', 'tomorrow_continuation_prob_adjusted',
        'premarket_gap_pct', 'close_location_value', 'upper_wick_pct',
        'catalyst_verifiability_score', 'catalyst_freshness_score', 'catalyst_strength_score',
        'open_exhaustion_risk_score', 'overnight_followthrough_score', 'monster_overlay', 'protocol_gate_reason',
        'pre_gate_rank', 'post_gate_decision', 'gate_stage', 'promote_to_keep_reason', 'market_data_source', 'market_snapshot_live',
        'cap_hit_flag', 'final_elimination_owner', 'decision_rank_after_gate',
        'scanner_profile', 'scanner_pass_v1', 'float_rotation_proxy',
        'event_score_v1', 'feature_alpha_score_v1', 'multi_radar_score',
        'daily_change_pct', 'rel_volume', 'monster_score',
        'is_in_ai_focus', 'is_in_fusion', 'is_in_monster_radar', 'is_in_xq',
    ]
    signal_cols = [c for c in signal_cols if c in decision_signals.columns]
    decision_signals = decision_signals[signal_cols].reset_index(drop=True)

    keep_count = int((decision_signals.get('decision_tag_v1') == 'keep').sum()) if len(decision_signals) > 0 else 0
    watch_count = int((decision_signals.get('decision_tag_v1') == 'watch').sum()) if len(decision_signals) > 0 else 0

    reason_counts: Dict[str, int] = {}
    for text in out['protocol_gate_reason'].fillna('').astype(str).tolist():
        for token in [t.strip() for t in text.split('|') if t.strip()]:
            reason_counts[token] = reason_counts.get(token, 0) + 1

    return out, decision_signals, {
        'decision_rows': int(len(decision_signals)),
        'keep_count': keep_count,
        'watch_count': watch_count,
        'scanner_profile': out['scanner_profile'].iloc[0] if len(out) > 0 else 'balanced',
        'scanner_pass_count': int(out['scanner_pass_v1'].astype(bool).sum()) if len(out) > 0 else 0,
        'funnel_total': int(len(out)),
        'funnel_hard_exhaustion': int(hard_limit_exhaustion.sum()),
        'funnel_hard_live_gap': int(live_gap_unverified.sum()),
        'funnel_hard_risk': int(hard_limit_risk.sum()),
        'funnel_watch_exhaustion': int(out['open_exhaustion_risk_score'].between(cfg['exhaustion_watch_min'], cfg['exhaustion_avoid_min'], inclusive='left').sum()),
        'funnel_watch_proxy_gap': int(proxy_gap_unverified.sum()),
        'funnel_watch_wick_close': int(wick_close_downgrade.sum()),
        'funnel_watch_overheat': int(overheat_downgrade.sum()),
        'funnel_live_rows': int(market_snapshot_live.sum()),
        'funnel_proxy_rows': int((~market_snapshot_live).sum()),
        'gate_reason_counts': reason_counts,
    }
