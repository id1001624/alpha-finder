from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests


API_CATALYST_COLUMNS = [
    'api_rank',
    'ticker',
    'catalyst_type',
    'sentiment',
    'hype_score',
    'explosion_probability',
    'confidence',
    'reason',
    'rank_score_v1',
    'research_priority_score',
    'decision_tag_v1',
    'source_flags',
    'news_hits',
    'api_final_score',
]

AI_DECISION_COLUMNS = [
    'decision_date',
    'rank',
    'ticker',
    'short_score_final',
    'swing_score',
    'core_score',
    'risk_level',
    'tech_status',
    'theme',
    'decision_tag',
    'reason_summary',
    'source_ref',
    'research_mode',
    'catalyst_type',
    'catalyst_sentiment',
    'explosion_probability',
    'hype_score',
    'confidence',
    'api_final_score',
    'catalyst_source',
    'catalyst_summary',
]


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


def write_api_catalyst_artifacts(
    output_dir: Path,
    scan_date: str,
    gemini_model: str,
    out_df: pd.DataFrame | None = None,
    *,
    enabled: bool,
    reason: str,
    top_tickers: List[str] | None = None,
    missing: Dict[str, bool] | None = None,
) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)

    out = out_df.copy() if out_df is not None else pd.DataFrame()
    for col in API_CATALYST_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[API_CATALYST_COLUMNS].copy()

    out_path = output_dir / 'api_catalyst_analysis_daily.csv'
    out.to_csv(out_path, index=False, encoding='utf-8-sig')

    resolved_top_tickers = list(top_tickers or [])
    if not resolved_top_tickers and len(out) > 0 and 'ticker' in out.columns:
        resolved_top_tickers = [str(v).strip().upper() for v in out['ticker'].dropna().head(8).tolist()]
    status_label = 'enabled' if enabled else 'placeholder'

    brief_lines = [
        f'# API Catalyst Brief ({scan_date})',
        '',
        f'Model: {gemini_model}',
        f'Status: {status_label}',
        f'Reason: {reason}',
        f'Rows: {len(out)}',
        f'Top tickers: {", ".join(resolved_top_tickers) if resolved_top_tickers else "N/A"}',
        '',
        'This file keeps web/api research artifacts schema-stable for downstream review and backtest logging.',
    ]
    (output_dir / 'api_catalyst_brief.md').write_text('\n'.join(brief_lines), encoding='utf-8')

    meta = {
        'enabled': enabled,
        'scan_date': scan_date,
        'rows': int(len(out)),
        'reason': reason,
        'top_tickers': resolved_top_tickers,
        'files': [
            'api_catalyst_analysis_daily.csv',
            'api_catalyst_brief.md',
            'api_catalyst_manifest.json',
        ],
    }
    if missing is not None:
        meta['missing'] = missing
    with open(output_dir / 'api_catalyst_manifest.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def _derive_tech_status(row: pd.Series) -> str:
    vwap = pd.to_numeric(row.get('tv_vwap'), errors='coerce')
    sqz_color = str(row.get('tv_sqzmom_color', '')).strip()
    if pd.isna(vwap) and not sqz_color:
        return '需技術驗證'
    return 'TV已更新'


def _fallback_reason_summary(row: pd.Series) -> str:
    next_prob = str(row.get('prob_next_day', '')).strip() or 'N/A'
    day2_prob = str(row.get('prob_day2', '')).strip() or 'N/A'
    action = str(row.get('decision_action', '')).strip() or '先觀望'
    invalidation = str(row.get('invalidation_rule', '')).strip() or '跌破前一日低點或量能掉到 1.0x 以下'
    catalyst_summary = str(row.get('reason', '')).strip() or '未見明確催化，改以數據排序為主'
    return f'明日{next_prob}、後日{day2_prob}；建議{action}；失效條件：{invalidation}；催化：{catalyst_summary[:120]}'


def _normalize_api_decision_rows(rows: List[Dict[str, object]], merged_df: pd.DataFrame, scan_date: str) -> pd.DataFrame:
    source_map = {str(row.get('ticker', '')).strip().upper(): row for _, row in merged_df.iterrows()}
    normalized_rows: List[Dict[str, object]] = []

    for idx, raw in enumerate(rows, 1):
        ticker = str(raw.get('ticker', '')).strip().upper()
        if not ticker or ticker not in source_map:
            continue
        src = source_map[ticker]
        normalized_rows.append(
            {
                'decision_date': scan_date,
                'rank': idx,
                'ticker': ticker,
                'short_score_final': round(_safe_float(raw.get('short_score_final', src.get('xq_short_trade_score', src.get('rank_score_v1', 0)))), 2),
                'swing_score': round(_safe_float(raw.get('swing_score', src.get('xq_swing_score', 0))), 2),
                'core_score': round(_safe_float(raw.get('core_score', src.get('core_score_v81', 0))), 2),
                'risk_level': str(raw.get('risk_level', src.get('risk_level', '中'))).strip() or '中',
                'tech_status': str(raw.get('tech_status', '')).strip() or _derive_tech_status(src),
                'theme': str(raw.get('theme', src.get('priority_sector', src.get('sector', '')))).strip(),
                'decision_tag': str(raw.get('decision_tag', src.get('decision_tag_v1', 'watch'))).strip().lower() or 'watch',
                'reason_summary': str(raw.get('reason_summary', '')).strip() or _fallback_reason_summary(src),
                'source_ref': 'market_dataset_daily.csv;api_catalyst_analysis_daily.csv;xq_short_term_updated.csv',
                'research_mode': 'api',
                'catalyst_type': str(raw.get('catalyst_type', src.get('catalyst_type', ''))).strip(),
                'catalyst_sentiment': _normalize_sentiment(str(raw.get('catalyst_sentiment', src.get('sentiment', 'neutral')))),
                'explosion_probability': int(round(_safe_float(raw.get('explosion_probability', src.get('explosion_probability', 0))))),
                'hype_score': int(round(_safe_float(raw.get('hype_score', src.get('hype_score', 0))))),
                'confidence': int(round(_safe_float(raw.get('confidence', src.get('confidence', 0))))),
                'api_final_score': round(_safe_float(raw.get('api_final_score', src.get('api_final_score', 0))), 2),
                'catalyst_source': 'api_catalyst_analysis_daily.csv',
                'catalyst_summary': str(raw.get('catalyst_summary', src.get('reason', ''))).strip(),
            }
        )

    out = pd.DataFrame(normalized_rows)
    if len(out) == 0:
        fallback_rows: List[Dict[str, object]] = []
        top_df = merged_df.sort_values(['api_final_score', 'rank_score_v1', 'ticker'], ascending=[False, False, True]).head(5)
        for idx, (_, src) in enumerate(top_df.iterrows(), 1):
            fallback_rows.append(
                {
                    'decision_date': scan_date,
                    'rank': idx,
                    'ticker': str(src.get('ticker', '')).strip().upper(),
                    'short_score_final': round(_safe_float(src.get('xq_short_trade_score', src.get('rank_score_v1', 0))), 2),
                    'swing_score': round(_safe_float(src.get('xq_swing_score', 0)), 2),
                    'core_score': round(_safe_float(src.get('core_score_v81', 0)), 2),
                    'risk_level': str(src.get('risk_level', '中')).strip() or '中',
                    'tech_status': _derive_tech_status(src),
                    'theme': str(src.get('priority_sector', src.get('sector', ''))).strip(),
                    'decision_tag': str(src.get('decision_tag_v1', 'watch')).strip().lower() or 'watch',
                    'reason_summary': _fallback_reason_summary(src),
                    'source_ref': 'market_dataset_daily.csv;api_catalyst_analysis_daily.csv;xq_short_term_updated.csv',
                    'research_mode': 'api',
                    'catalyst_type': str(src.get('catalyst_type', '')).strip(),
                    'catalyst_sentiment': _normalize_sentiment(str(src.get('sentiment', 'neutral'))),
                    'explosion_probability': int(round(_safe_float(src.get('explosion_probability', 0)))),
                    'hype_score': int(round(_safe_float(src.get('hype_score', 0)))),
                    'confidence': int(round(_safe_float(src.get('confidence', 0)))),
                    'api_final_score': round(_safe_float(src.get('api_final_score', 0)), 2),
                    'catalyst_source': 'api_catalyst_analysis_daily.csv',
                    'catalyst_summary': str(src.get('reason', '')).strip(),
                }
            )
        out = pd.DataFrame(fallback_rows)

    for col in AI_DECISION_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[AI_DECISION_COLUMNS].copy()
    return out


def generate_api_ai_decision(
    merged_df: pd.DataFrame,
    output_dir: Path,
    inbox_dir: Path,
    scan_date: str,
    api_key: str,
    model: str,
    timeout_sec: float,
    top_k: int = 5,
) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    filename = f'ai_decision_{scan_date}.csv'

    if merged_df is None or len(merged_df) == 0:
        return {
            'enabled': False,
            'rows': 0,
            'reason': 'empty_candidates',
            'file': filename,
        }

    if not api_key:
        return {
            'enabled': False,
            'rows': 0,
            'reason': 'missing_gemini_api_key',
            'file': filename,
        }

    required_prompt_cols = [
        'ticker', 'rank_score_v1', 'xq_short_trade_score', 'xq_swing_score', 'core_score_v81',
        'prob_next_day', 'prob_day2', 'decision_action', 'decision_tag_v1', 'risk_level',
        'invalidation_rule', 'priority_sector', 'sector', 'tv_vwap', 'tv_sqzmom_color',
        'catalyst_type', 'sentiment', 'explosion_probability', 'hype_score', 'confidence',
        'api_final_score', 'reason',
    ]
    for col in required_prompt_cols:
        if col not in merged_df.columns:
            merged_df[col] = pd.NA
    top_df = merged_df.sort_values(['api_final_score', 'rank_score_v1', 'ticker'], ascending=[False, False, True]).head(max(int(top_k), 1)).copy()
    prompt_rows = top_df[[
        'ticker', 'rank_score_v1', 'xq_short_trade_score', 'xq_swing_score', 'core_score_v81',
        'prob_next_day', 'prob_day2', 'decision_action', 'decision_tag_v1', 'risk_level',
        'invalidation_rule', 'priority_sector', 'sector', 'tv_vwap', 'tv_sqzmom_color',
        'catalyst_type', 'sentiment', 'explosion_probability', 'hype_score', 'confidence',
        'api_final_score', 'reason',
    ]].fillna('').to_dict(orient='records')

    prompt = (
        '你現在扮演網頁 AI 的 API 備援模式。請根據提供的候選資料，直接輸出最終 ai_decision CSV 對應的 JSON。'
        '只可使用提供資料，不可自創 ticker。回傳嚴格 JSON object，格式為 {"rows":[...]}。'
        '每個 row 只需要提供以下欄位：ticker, short_score_final, swing_score, core_score, risk_level, tech_status, theme, decision_tag, reason_summary, catalyst_type, catalyst_sentiment, explosion_probability, hype_score, confidence, api_final_score, catalyst_summary。'
        '規則：decision_tag 只能是 keep/watch/replace_candidate；reason_summary 必須包含明日/後日判斷 + 建議動作 + 失效條件；若沒有 TV 資料，tech_status 必須為 需技術驗證。'
        f'分析日期：{scan_date}。候選資料：{json.dumps(prompt_rows, ensure_ascii=False)}'
    )

    rows: List[Dict[str, object]] = []
    endpoint = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.1,
            'responseMimeType': 'application/json',
        },
    }
    try:
        response = requests.post(endpoint, json=payload, timeout=timeout_sec)
        response.raise_for_status()
        data = response.json()
        text = data['candidates'][0]['content']['parts'][0]['text']
        parsed = _extract_json_block(text)
        maybe_rows = parsed.get('rows', []) if isinstance(parsed, dict) else []
        if isinstance(maybe_rows, list):
            rows = [item for item in maybe_rows if isinstance(item, dict)]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        rows = []

    if not rows:
        return {
            'enabled': False,
            'rows': 0,
            'reason': 'no_gemini_decision_rows',
            'file': filename,
        }

    out = _normalize_api_decision_rows(rows=rows, merged_df=top_df, scan_date=scan_date)
    if len(out) == 0:
        return {
            'enabled': False,
            'rows': 0,
            'reason': 'invalid_gemini_decision_rows',
            'file': filename,
        }

    out_path = output_dir / filename
    inbox_path = inbox_dir / filename
    out.to_csv(out_path, index=False, encoding='utf-8-sig')
    out.to_csv(inbox_path, index=False, encoding='utf-8-sig')

    return {
        'enabled': True,
        'rows': int(len(out)),
        'reason': 'ok',
        'file': filename,
        'output_path': str(out_path),
        'inbox_path': str(inbox_path),
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
        return write_api_catalyst_artifacts(
            output_dir=output_dir,
            scan_date=scan_date,
            gemini_model=gemini_model,
            out_df=pd.DataFrame(),
            enabled=True,
            reason='empty_candidates',
        )

    if not tavily_api_key or not gemini_api_key:
        return write_api_catalyst_artifacts(
            output_dir=output_dir,
            scan_date=scan_date,
            gemini_model=gemini_model,
            out_df=pd.DataFrame(),
            enabled=False,
            reason='missing_api_key',
            missing={
                'tavily': not bool(tavily_api_key),
                'gemini': not bool(gemini_api_key),
            },
        )

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

    top_tickers = out['ticker'].head(8).tolist() if len(out) > 0 and 'ticker' in out.columns else []
    return write_api_catalyst_artifacts(
        output_dir=output_dir,
        scan_date=scan_date,
        gemini_model=gemini_model,
        out_df=out,
        enabled=True,
        reason='ok',
        top_tickers=top_tickers,
    )
