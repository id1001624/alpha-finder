from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pandas as pd


def _safe_float(value) -> float:
    parsed = pd.to_numeric(value, errors='coerce')
    if pd.isna(parsed):
        return 0.0
    return float(parsed)


def _collect_source_scores(feature_df: pd.DataFrame, radar_df: pd.DataFrame, event_df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    score_map: Dict[str, Dict[str, float]] = {}

    def _ensure(ticker: str) -> Dict[str, float]:
        if ticker not in score_map:
            score_map[ticker] = {
                'feature': 0.0,
                'radar': 0.0,
                'event': 0.0,
            }
        return score_map[ticker]

    if feature_df is not None and len(feature_df) > 0:
        for _, row in feature_df.iterrows():
            ticker = str(row.get('ticker', '')).strip().upper()
            if not ticker:
                continue
            _ensure(ticker)['feature'] = _safe_float(row.get('feature_alpha_score_v1'))

    if radar_df is not None and len(radar_df) > 0:
        for _, row in radar_df.iterrows():
            ticker = str(row.get('ticker', '')).strip().upper()
            if not ticker:
                continue
            _ensure(ticker)['radar'] = _safe_float(row.get('multi_radar_score'))

    if event_df is not None and len(event_df) > 0:
        for _, row in event_df.iterrows():
            ticker = str(row.get('ticker', '')).strip().upper()
            if not ticker:
                continue
            _ensure(ticker)['event'] = _safe_float(row.get('event_score'))

    return score_map


def build_research_bridge(
    dataset: pd.DataFrame,
    feature_signals: pd.DataFrame,
    radar_signals: pd.DataFrame,
    event_signals: pd.DataFrame,
    output_dir: Path,
    scan_date: str,
    top_n: int = 20,
) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)

    score_map = _collect_source_scores(feature_signals, radar_signals, event_signals)
    rows: List[Dict[str, object]] = []

    for _, row in dataset.iterrows():
        ticker = str(row.get('ticker', '')).strip().upper()
        if not ticker or ticker not in score_map:
            continue

        src = score_map[ticker]
        research_priority_score = (
            src['feature'] * 0.42 +
            src['radar'] * 0.33 +
            src['event'] * 0.25
        )

        source_flags = []
        if src['feature'] > 0:
            source_flags.append('feature')
        if src['radar'] > 0:
            source_flags.append('radar')
        if src['event'] > 0:
            source_flags.append('event')

        rows.append(
            {
                'ticker': ticker,
                'research_priority_score': round(research_priority_score, 2),
                'rank_score_v1': round(_safe_float(row.get('rank_score_v1')), 2),
                'rank_engine_tier': row.get('rank_engine_tier', ''),
                'rank_engine_rank': int(_safe_float(row.get('rank_engine_rank'))),
                'rank_regime': row.get('rank_regime', ''),
                'decision_tag_v1': row.get('decision_tag_v1', ''),
                'decision_action': row.get('decision_action', ''),
                'risk_level': row.get('risk_level', ''),
                'risk_score_v1': round(_safe_float(row.get('risk_score_v1')), 2),
                'invalidation_rule': row.get('invalidation_rule', ''),
                'scanner_profile': row.get('scanner_profile', ''),
                'scanner_pass_v1': bool(row.get('scanner_pass_v1', False)),
                'feature_score': round(src['feature'], 2),
                'radar_score': round(src['radar'], 2),
                'event_score': round(src['event'], 2),
                'source_flags': '|'.join(source_flags),
                'composite_alpha_score_v1': round(_safe_float(row.get('composite_alpha_score_v1')), 2),
                'feature_priority_tier': row.get('feature_priority_tier', ''),
                'radar_priority_tier': row.get('radar_priority_tier', ''),
                'event_tag': row.get('event_type', ''),
                'daily_change_pct': round(_safe_float(row.get('daily_change_pct')), 2),
                'rel_volume': round(_safe_float(row.get('rel_volume')), 2),
                'monster_score': round(_safe_float(row.get('monster_score')), 2),
                'prob_next_day': row.get('prob_next_day', ''),
                'continuation_grade': row.get('continuation_grade', ''),
                'ai_query_hint': row.get('ai_query_hint_focus', row.get('ai_query_hint', '')),
            }
        )

    candidates = pd.DataFrame(rows)
    if len(candidates) > 0:
        candidates = candidates.sort_values(
            ['research_priority_score', 'rank_score_v1', 'event_score', 'feature_score', 'radar_score', 'ticker'],
            ascending=[False, False, False, False, False, True],
        ).head(max(top_n, 1)).reset_index(drop=True)
        candidates['rank'] = range(1, len(candidates) + 1)
        candidates = candidates[['rank'] + [c for c in candidates.columns if c != 'rank']]

    candidates_path = output_dir / 'ai_research_candidates.csv'
    candidates.to_csv(candidates_path, index=False, encoding='utf-8-sig')

    top_tickers = candidates['ticker'].head(10).tolist() if len(candidates) > 0 else []
    prompt_lines = [
        f"# AI Research Brief ({scan_date})",
        "",
        "你現在要做的是『研究判斷』，不是自動下單。",
        "請依照候選優先序，完成：",
        "1. 催化確認（新聞/公告/財報時點）",
        "2. 隔日/後日延續機率判斷（量化）",
        "3. 短線風險與失效條件",
        "",
        f"Top candidates: {', '.join(top_tickers) if top_tickers else 'N/A'}",
        "",
        "輸出格式要求：",
        "- 先給 Top 1（今日最佳）",
        "- 再給 Top 5 備選",
        "- web / api 模式都必須輸出同一份 ai_decision 契約",
        "- 若 api_catalyst_analysis 有資料，優先引用；若沒有，催化欄位仍要保留但可留空",
        "- 最後輸出 ai_decision_YYYY-MM-DD.csv",
    ]
    prompt_path = output_dir / 'ai_research_prompt.md'
    prompt_path.write_text('\n'.join(prompt_lines), encoding='utf-8')

    meta = {
        'scan_date': scan_date,
        'candidate_rows': int(len(candidates)),
        'top_n': int(top_n),
        'files': ['ai_research_candidates.csv', 'ai_research_prompt.md'],
    }
    with open(output_dir / 'ai_research_manifest.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return {
        'candidate_rows': int(len(candidates)),
        'top_tickers': top_tickers,
    }
