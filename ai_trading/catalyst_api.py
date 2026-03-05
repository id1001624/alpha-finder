from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests


def _safe_float(value, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors='coerce')
    if pd.isna(parsed):
        return default
    return float(parsed)


def _normalize_sentiment(value: str) -> str:
    text = str(value or '').strip().lower()
    if text in {'positive', 'bullish', 'pos'}:
        return 'positive'
    if text in {'negative', 'bearish', 'neg'}:
        return 'negative'
    return 'neutral'


def _extract_json_block(text: str) -> Dict[str, object]:
    raw = str(text or '').strip()
    if not raw:
        return {}

    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        return {}

    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _tavily_search(query: str, api_key: str, max_results: int, timeout_sec: float) -> List[Dict[str, str]]:
    if not api_key:
        return []

    url = 'https://api.tavily.com/search'
    payload = {
        'api_key': api_key,
        'query': query,
        'max_results': int(max_results),
        'search_depth': 'basic',
        'include_answer': False,
        'include_raw_content': False,
    }
    try:
        response = requests.post(url, json=payload, timeout=timeout_sec)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    results = data.get('results', []) if isinstance(data, dict) else []
    out: List[Dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                'title': str(item.get('title', '')).strip(),
                'url': str(item.get('url', '')).strip(),
                'content': str(item.get('content', '')).strip(),
            }
        )
    return out


def _gemini_catalyst_analyze(
    ticker: str,
    snippets: List[Dict[str, str]],
    api_key: str,
    model: str,
    timeout_sec: float,
) -> Dict[str, object]:
    if not api_key:
        return {}

    snippets_text = []
    for idx, item in enumerate(snippets[:6], 1):
        snippets_text.append(
            f"[{idx}] title={item.get('title', '')}\nurl={item.get('url', '')}\ncontent={item.get('content', '')[:420]}"
        )

    prompt = (
        'You are a catalyst detector for short-term momentum stocks. '\
        'Return strict JSON only with fields: '\
        'ticker, catalyst_type, sentiment, hype_score, explosion_probability, confidence, reason. '\
        'Sentiment must be one of positive/neutral/negative. '\
        'hype_score, explosion_probability, confidence are integers from 0 to 100. '\
        f'Ticker: {ticker}\n\n'
        'News snippets:\n'
        + ('\n\n'.join(snippets_text) if snippets_text else 'No snippets found.')
    )

    endpoint = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.15,
            'responseMimeType': 'application/json',
        },
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=timeout_sec)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return {}

    try:
        text = data['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError, TypeError):
        return {}

    parsed = _extract_json_block(text)
    if not parsed:
        return {}

    return {
        'ticker': ticker,
        'catalyst_type': str(parsed.get('catalyst_type', '')).strip(),
        'sentiment': _normalize_sentiment(str(parsed.get('sentiment', 'neutral'))),
        'hype_score': int(round(_safe_float(parsed.get('hype_score'), 0))),
        'explosion_probability': int(round(_safe_float(parsed.get('explosion_probability'), 0))),
        'confidence': int(round(_safe_float(parsed.get('confidence'), 0))),
        'reason': str(parsed.get('reason', '')).strip(),
    }


def run_catalyst_detector_api(
    candidates_df: pd.DataFrame,
    output_dir: Path,
    scan_date: str,
    tavily_api_key: str,
    gemini_api_key: str,
    gemini_model: str,
    top_k: int = 12,
    tavily_max_results: int = 4,
    timeout_sec: float = 15.0,
) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)

    if candidates_df is None or len(candidates_df) == 0:
        empty = pd.DataFrame()
        empty.to_csv(output_dir / 'api_catalyst_analysis_daily.csv', index=False, encoding='utf-8-sig')
        return {'enabled': True, 'rows': 0, 'reason': 'empty_candidates'}

    if not tavily_api_key or not gemini_api_key:
        return {
            'enabled': False,
            'rows': 0,
            'reason': 'missing_api_key',
            'missing': {
                'tavily': not bool(tavily_api_key),
                'gemini': not bool(gemini_api_key),
            },
        }

    rows: List[Dict[str, object]] = []
    top_df = candidates_df.head(max(int(top_k), 1)).copy()

    for _, row in top_df.iterrows():
        ticker = str(row.get('ticker', '')).strip().upper()
        if not ticker:
            continue

        query = f'{ticker} stock news catalyst earnings partnership guidance FDA contract'
        snippets = _tavily_search(query=query, api_key=tavily_api_key, max_results=tavily_max_results, timeout_sec=timeout_sec)

        analysis = _gemini_catalyst_analyze(
            ticker=ticker,
            snippets=snippets,
            api_key=gemini_api_key,
            model=gemini_model,
            timeout_sec=timeout_sec,
        )

        if not analysis:
            analysis = {
                'ticker': ticker,
                'catalyst_type': '',
                'sentiment': 'neutral',
                'hype_score': 0,
                'explosion_probability': 0,
                'confidence': 0,
                'reason': 'analysis_unavailable',
            }

        rank_score = _safe_float(row.get('rank_score_v1'))
        analysis['rank_score_v1'] = round(rank_score, 2)
        analysis['research_priority_score'] = round(_safe_float(row.get('research_priority_score')), 2)
        analysis['decision_tag_v1'] = str(row.get('decision_tag_v1', '')).strip()
        analysis['source_flags'] = str(row.get('source_flags', '')).strip()
        analysis['news_hits'] = int(len(snippets))

        sentiment_boost = 8 if analysis['sentiment'] == 'positive' else (-6 if analysis['sentiment'] == 'negative' else 0)
        analysis['api_final_score'] = round(
            rank_score * 0.58 +
            _safe_float(analysis.get('explosion_probability')) * 0.27 +
            _safe_float(analysis.get('hype_score')) * 0.15 +
            sentiment_boost,
            2,
        )
        rows.append(analysis)

    out = pd.DataFrame(rows)
    if len(out) > 0:
        out = out.sort_values(['api_final_score', 'confidence', 'explosion_probability', 'ticker'], ascending=[False, False, False, True]).reset_index(drop=True)
        out['api_rank'] = range(1, len(out) + 1)
        out = out[['api_rank'] + [c for c in out.columns if c != 'api_rank']]

    out_path = output_dir / 'api_catalyst_analysis_daily.csv'
    out.to_csv(out_path, index=False, encoding='utf-8-sig')

    top_tickers = out['ticker'].head(8).tolist() if len(out) > 0 and 'ticker' in out.columns else []
    brief_lines = [
        f'# API Catalyst Brief ({scan_date})',
        '',
        f'Model: {gemini_model}',
        f'Rows: {len(out)}',
        f'Top tickers: {", ".join(top_tickers) if top_tickers else "N/A"}',
        '',
        'This file is generated by Tavily + Gemini Flash API mode.',
    ]
    (output_dir / 'api_catalyst_brief.md').write_text('\n'.join(brief_lines), encoding='utf-8')

    meta = {
        'enabled': True,
        'scan_date': scan_date,
        'rows': int(len(out)),
        'top_tickers': top_tickers,
        'files': ['api_catalyst_analysis_daily.csv', 'api_catalyst_brief.md'],
    }
    with open(output_dir / 'api_catalyst_manifest.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return meta
