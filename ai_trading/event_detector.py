from __future__ import annotations

from typing import Dict, List

import pandas as pd
import config as settings_module

from .contracts import parse_probability_mid


def _to_float(value) -> float:
    parsed = pd.to_numeric(value, errors='coerce')
    if pd.isna(parsed):
        return 0.0
    return float(parsed)


def _resolve_event_config() -> dict[str, float]:
    def _get(name: str, default: float) -> float:
        return float(getattr(settings_module, name, default))

    def _get_int(name: str, default: int) -> int:
        return int(getattr(settings_module, name, default))

    return {
        'vol_breakout_daily_min': _get('EVENT_VOL_BREAKOUT_DAILY_MIN', 8.0),
        'vol_breakout_rel_vol_min': _get('EVENT_VOL_BREAKOUT_REL_VOL_MIN', 2.5),
        'vol_breakout_daily_weight': _get('EVENT_VOL_BREAKOUT_DAILY_WEIGHT', 1.8),
        'vol_breakout_rel_vol_weight': _get('EVENT_VOL_BREAKOUT_REL_VOL_WEIGHT', 8.0),
        'vol_breakout_monster_weight': _get('EVENT_VOL_BREAKOUT_MONSTER_WEIGHT', 0.3),
        'earnings_sniper_max_days': _get_int('EVENT_EARNINGS_SNIPER_MAX_DAYS', 3),
        'earnings_sniper_day_weight': _get('EVENT_EARNINGS_SNIPER_DAY_WEIGHT', 7.0),
        'earnings_sniper_rel_vol_weight': _get('EVENT_EARNINGS_SNIPER_REL_VOL_WEIGHT', 4.0),
        'earnings_sniper_core_weight': _get('EVENT_EARNINGS_SNIPER_CORE_WEIGHT', 0.5),
        'post_earnings_min_days': _get_int('EVENT_POST_EARNINGS_MIN_DAYS', -2),
        'post_earnings_max_days': _get_int('EVENT_POST_EARNINGS_MAX_DAYS', 0),
        'post_earnings_daily_min': _get('EVENT_POST_EARNINGS_DAILY_MIN', 2.0),
        'post_earnings_daily_weight': _get('EVENT_POST_EARNINGS_DAILY_WEIGHT', 1.2),
        'post_earnings_rel_vol_weight': _get('EVENT_POST_EARNINGS_REL_VOL_WEIGHT', 5.0),
        'post_earnings_core_weight': _get('EVENT_POST_EARNINGS_CORE_WEIGHT', 0.25),
        'monster_min_score': _get('EVENT_MONSTER_MIN_SCORE', 34.0),
        'monster_min_prob': _get('EVENT_MONSTER_MIN_PROB', 55.0),
        'monster_prob_weight': _get('EVENT_MONSTER_PROB_WEIGHT', 0.4),
        'monster_rel_vol_weight': _get('EVENT_MONSTER_REL_VOL_WEIGHT', 2.0),
        'focus_monster_min_score': _get('EVENT_FOCUS_MONSTER_MIN_SCORE', 25.0),
        'focus_monster_weight': _get('EVENT_FOCUS_MONSTER_WEIGHT', 0.8),
        'focus_rel_vol_weight': _get('EVENT_FOCUS_REL_VOL_WEIGHT', 3.0),
        'focus_core_weight': _get('EVENT_FOCUS_CORE_WEIGHT', 0.2),
    }


def detect_events(dataset: pd.DataFrame, top_k: int = 40) -> pd.DataFrame:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame()

    cfg = _resolve_event_config()
    rows: List[Dict[str, object]] = []

    for _, row in dataset.iterrows():
        ticker = str(row.get('ticker', '')).strip().upper()
        if not ticker:
            continue

        daily_change = _to_float(row.get('daily_change_pct'))
        rel_volume = _to_float(row.get('rel_volume'))
        monster_score = _to_float(row.get('monster_score'))
        core_score = _to_float(row.get('core_score_v81'))
        daily_for_score = min(max(daily_change, -5.0), 40.0)
        rel_for_score = min(max(rel_volume, 0.0), 20.0)
        dte = pd.to_numeric(row.get('days_to_earnings'), errors='coerce')
        earnings_status = str(row.get('earnings_status', '')).strip().lower()
        next_day_prob_mid = parse_probability_mid(row.get('prob_next_day'))

        event_type = ''
        reason = ''
        score = 0.0
        risk_note = '一般風險'

        if daily_change >= cfg['vol_breakout_daily_min'] and rel_volume >= cfg['vol_breakout_rel_vol_min']:
            event_type = 'vol_breakout'
            score = daily_for_score * cfg['vol_breakout_daily_weight'] + (rel_for_score - 1) * cfg['vol_breakout_rel_vol_weight'] + monster_score * cfg['vol_breakout_monster_weight']
            reason = f'當日強突破：漲幅{daily_change:.1f}% / 量能{rel_volume:.1f}x'
            risk_note = '追價風險偏高'
        elif earnings_status == 'upcoming' and pd.notna(dte) and 0 <= float(dte) <= cfg['earnings_sniper_max_days']:
            event_type = 'earnings_sniper'
            score = ((cfg['earnings_sniper_max_days'] + 1) - float(dte)) * cfg['earnings_sniper_day_weight'] + rel_for_score * cfg['earnings_sniper_rel_vol_weight'] + core_score * cfg['earnings_sniper_core_weight']
            reason = f'財報狙擊窗口：D-{int(float(dte))} / 核心分{core_score:.1f}'
            risk_note = '事件落地波動大'
        elif earnings_status == 'past' and pd.notna(dte) and cfg['post_earnings_min_days'] <= float(dte) <= cfg['post_earnings_max_days'] and daily_change > cfg['post_earnings_daily_min']:
            event_type = 'post_earnings_follow'
            score = daily_for_score * cfg['post_earnings_daily_weight'] + rel_for_score * cfg['post_earnings_rel_vol_weight'] + core_score * cfg['post_earnings_core_weight']
            reason = f'財報後延續：漲幅{daily_change:.1f}% / 量能{rel_volume:.1f}x'
            risk_note = '續強/轉弱切換快'
        elif monster_score >= cfg['monster_min_score'] and next_day_prob_mid >= cfg['monster_min_prob']:
            event_type = 'monster_continuation'
            score = monster_score + next_day_prob_mid * cfg['monster_prob_weight'] + rel_for_score * cfg['monster_rel_vol_weight']
            reason = f'妖股續航：Monster {monster_score:.1f} / 明日機率中位 {next_day_prob_mid:.1f}%'
            risk_note = '高波動高回撤'
        elif bool(row.get('is_in_ai_focus', False)) and monster_score >= cfg['focus_monster_min_score']:
            event_type = 'focus_reinforcement'
            score = monster_score * cfg['focus_monster_weight'] + rel_for_score * cfg['focus_rel_vol_weight'] + core_score * cfg['focus_core_weight']
            reason = f'AI 關注強化：focus + Monster {monster_score:.1f}'
            risk_note = '需確認隔夜催化'

        if not event_type:
            continue

        rows.append(
            {
                'ticker': ticker,
                'event_type': event_type,
                'event_score': round(score, 2),
                'daily_change_pct': round(daily_change, 2),
                'rel_volume': round(rel_volume, 2),
                'monster_score': round(monster_score, 2),
                'core_score_v81': round(core_score, 2),
                'prob_next_day_mid': round(next_day_prob_mid, 2),
                'event_reason': reason,
                'risk_note': risk_note,
            }
        )

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out = out.sort_values(['event_score', 'monster_score', 'rel_volume'], ascending=[False, False, False])
    return out.head(max(top_k, 1)).reset_index(drop=True)
