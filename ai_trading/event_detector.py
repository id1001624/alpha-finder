from __future__ import annotations

from typing import Dict, List

import pandas as pd

from .contracts import parse_probability_mid


def _to_float(value) -> float:
    parsed = pd.to_numeric(value, errors='coerce')
    if pd.isna(parsed):
        return 0.0
    return float(parsed)


def detect_events(dataset: pd.DataFrame, top_k: int = 40) -> pd.DataFrame:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame()

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

        if daily_change >= 8 and rel_volume >= 2.5:
            event_type = 'vol_breakout'
            score = daily_for_score * 1.8 + (rel_for_score - 1) * 8 + monster_score * 0.3
            reason = f'當日強突破：漲幅{daily_change:.1f}% / 量能{rel_volume:.1f}x'
            risk_note = '追價風險偏高'
        elif earnings_status == 'upcoming' and pd.notna(dte) and 0 <= float(dte) <= 3:
            event_type = 'earnings_sniper'
            score = (4 - float(dte)) * 7 + rel_for_score * 4 + core_score * 0.5
            reason = f'財報狙擊窗口：D-{int(float(dte))} / 核心分{core_score:.1f}'
            risk_note = '事件落地波動大'
        elif earnings_status == 'past' and pd.notna(dte) and -2 <= float(dte) <= 0 and daily_change > 2:
            event_type = 'post_earnings_follow'
            score = daily_for_score * 1.2 + rel_for_score * 5 + core_score * 0.25
            reason = f'財報後延續：漲幅{daily_change:.1f}% / 量能{rel_volume:.1f}x'
            risk_note = '續強/轉弱切換快'
        elif monster_score >= 34 and next_day_prob_mid >= 55:
            event_type = 'monster_continuation'
            score = monster_score + next_day_prob_mid * 0.4 + rel_for_score * 2
            reason = f'妖股續航：Monster {monster_score:.1f} / 明日機率中位 {next_day_prob_mid:.1f}%'
            risk_note = '高波動高回撤'
        elif bool(row.get('is_in_ai_focus', False)) and monster_score >= 25:
            event_type = 'focus_reinforcement'
            score = monster_score * 0.8 + rel_for_score * 3 + core_score * 0.2
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
