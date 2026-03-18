"""
建立 AI Trading（無自動下單）每日資料集與事件偵測輸出。

輸入（預設讀 latest）：
- repo_outputs/daily_refresh/latest/raw_market_daily.csv
- repo_outputs/daily_refresh/latest/monster_radar_daily.csv
- repo_outputs/daily_refresh/latest/fusion_top_daily.csv
- repo_outputs/daily_refresh/latest/ai_focus_list.csv
- repo_outputs/ai_ready/latest/xq_short_term_updated.csv

輸出：
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/market_dataset_daily.csv
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/feature_signals_daily.csv
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/ranking_signals_daily.csv
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/decision_signals_daily.csv
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/event_signals_daily.csv
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/api_catalyst_analysis_daily.csv
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/api_catalyst_brief.md
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/pipeline_manifest.json
- repo_outputs/ai_trading/latest/*（同步）
- repo_outputs/ai_ready/latest/ai_ready_bundle.xlsx（統一 B：合併 ai_ready + ai_trading 核心 sheet）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.contracts import DataPaths
from ai_trading.catalyst_api import generate_api_ai_decision, run_catalyst_detector_api
from ai_trading.decision_risk import apply_decision_risk_layer
from ai_trading.market_data_pipeline import MarketDataPipeline
from ai_trading.quantmuse_bridge import analyze_overnight_sentiment, blend_overnight_impact
from ai_trading.ranking_engine import apply_ranking_engine
from ai_trading.research_bridge import build_research_bridge
from ai_trading.utils.yfinance_ssl import ensure_ascii_cert_bundle
import config as app_config

from app_logging import install_builtin_print_logging

_YFINANCE_CA_BUNDLE = ensure_ascii_cert_bundle()

import yfinance as yf

install_builtin_print_logging()

DAILY_REFRESH_LATEST = PROJECT_ROOT / 'repo_outputs' / 'daily_refresh' / 'latest'
AI_READY_LATEST = PROJECT_ROOT / 'repo_outputs' / 'ai_ready' / 'latest'
AI_TRADING_OUTPUT_DIR = PROJECT_ROOT / 'repo_outputs' / 'ai_trading'
BACKTEST_INBOX_DIR = PROJECT_ROOT / 'repo_outputs' / 'backtest' / 'inbox'


def _previous_trading_day_str(base_dt: datetime | None = None) -> str:
    now_dt = base_dt or datetime.now()
    weekday = now_dt.weekday()
    if weekday == 0:
        delta_days = 3
    elif weekday == 6:
        delta_days = 2
    else:
        delta_days = 1
    return (now_dt - timedelta(days=delta_days)).strftime('%Y-%m-%d')


def _sync_latest(src_dir: Path, latest_dir: Path) -> None:
    latest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, latest_dir, dirs_exist_ok=True)


def _read_csv_fallback(csv_path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(csv_path, encoding='utf-8-sig')
    except UnicodeDecodeError:
        return pd.read_csv(csv_path)


def _extract_json_dict(text: str) -> dict:
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


def _tavily_search(query: str, api_key: str, timeout_sec: float, max_results: int = 2) -> list[dict[str, str]]:
    if not api_key:
        return []

    payload = {
        'api_key': api_key,
        'query': query,
        'max_results': max(int(max_results), 1),
        'search_depth': 'basic',
        'include_answer': False,
        'include_raw_content': False,
    }
    try:
        response = requests.post('https://api.tavily.com/search', json=payload, timeout=timeout_sec)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    rows = []
    for item in (data.get('results', []) if isinstance(data, dict) else []):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                'title': str(item.get('title', '')).strip(),
                'url': str(item.get('url', '')).strip(),
                'content': str(item.get('content', '')).strip(),
            }
        )
    return rows


def _gemini_classify_overnight(
    ticker: str,
    snippets: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_sec: float,
) -> tuple[str, str]:
    if not api_key or not snippets:
        return 'unknown', ''

    snippet_payload = []
    for idx, row in enumerate(snippets[:6], 1):
        snippet_payload.append(
            {
                'idx': idx,
                'title': str(row.get('title', ''))[:140],
                'url': str(row.get('url', ''))[:220],
                'content': str(row.get('content', ''))[:360],
            }
        )

    prompt = (
        'Classify overnight catalyst impact for this ticker. Return strict JSON only with keys '\
        'impact and catalyst_type. '\
        'impact must be one of: hard_positive, soft_positive, neutral, soft_negative, hard_negative. '\
        f'Ticker: {ticker}\n'
        f'Snippets JSON:\n{json.dumps(snippet_payload, ensure_ascii=False)}'
    )
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.1,
            'responseMimeType': 'application/json',
        },
    }
    endpoint = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    headers = {
        'Content-Type': 'application/json',
        'x-goog-api-key': api_key,
    }

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_sec)
        response.raise_for_status()
        data = response.json()
        text = data['candidates'][0]['content']['parts'][0]['text']
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return 'unknown', ''

    parsed = _extract_json_dict(text)
    allowed = {'hard_positive', 'soft_positive', 'neutral', 'soft_negative', 'hard_negative'}
    impact = str(parsed.get('impact', 'unknown')).strip().lower()
    if impact not in allowed:
        impact = 'unknown'

    catalyst_type = str(parsed.get('catalyst_type', '')).strip()
    return impact, catalyst_type


def _build_overnight_catalyst_check(
    ranking_signals: pd.DataFrame,
    tavily_api_key: str,
    gemini_api_key: str,
    gemini_model: str,
    timeout_sec: float,
    top_n: int = 20,
) -> pd.DataFrame:
    cols = [
        'ticker',
        'catalyst_time',
        'catalyst_type',
        'impact',
        'sentiment_score',
        'sentiment_confidence',
        'final_impact',
        'source_snippet',
    ]
    if ranking_signals is None or len(ranking_signals) == 0 or 'ticker' not in ranking_signals.columns:
        return pd.DataFrame(columns=cols)

    top_df = ranking_signals.head(max(int(top_n), 1)).copy()
    tickers = [str(v).strip().upper() for v in top_df['ticker'].tolist() if str(v).strip()]
    if not tickers:
        return pd.DataFrame(columns=cols)

    rank_map = {ticker: idx for idx, ticker in enumerate(tickers)}

    def _process_ticker(ticker: str) -> dict[str, object]:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        snippets: list[dict[str, str]] = []
        for query in (f'{ticker} after hours news', f'{ticker} 8-K filing'):
            snippets.extend(_tavily_search(query=query, api_key=tavily_api_key, timeout_sec=timeout_sec, max_results=2))
            time.sleep(0.05)

        source_snippet = ''
        if snippets:
            first = snippets[0]
            source_snippet = str(first.get('content') or first.get('title') or '')[:280]

        impact, catalyst_type = _gemini_classify_overnight(
            ticker=ticker,
            snippets=snippets,
            api_key=gemini_api_key,
            model=gemini_model,
            timeout_sec=timeout_sec,
        )

        sentiment_result = analyze_overnight_sentiment(ticker=ticker, snippets=snippets)
        score_val = pd.to_numeric(sentiment_result.get('sentiment_score', 0.0), errors='coerce')
        confidence_val = pd.to_numeric(sentiment_result.get('sentiment_confidence', 0.0), errors='coerce')
        sentiment_score = float(score_val) if pd.notna(score_val) else 0.0
        sentiment_confidence = float(confidence_val) if pd.notna(confidence_val) else 0.0
        final_impact = blend_overnight_impact(
            impact=impact,
            sentiment_score=sentiment_score,
            sentiment_confidence=sentiment_confidence,
        )

        return {
            'ticker': ticker,
            'catalyst_time': now_str,
            'catalyst_type': catalyst_type,
            'impact': impact if impact else 'unknown',
            'sentiment_score': sentiment_score,
            'sentiment_confidence': sentiment_confidence,
            'final_impact': final_impact,
            'source_snippet': source_snippet,
        }

    rows: list[dict[str, str]] = []
    batch_size = 5
    max_workers = 3
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_ticker, ticker): ticker for ticker in batch}
            for future in as_completed(futures):
                rows.append(future.result())
        time.sleep(0.6)

    out = pd.DataFrame(rows)
    for col in cols:
        if col not in out.columns:
            out[col] = ''
    out = out[cols].copy()
    out['rank_tmp'] = out['ticker'].map(rank_map).fillna(9999)
    out = out.sort_values(['rank_tmp', 'ticker'], ascending=[True, True]).drop(columns=['rank_tmp']).reset_index(drop=True)
    return out


PRE_EVENT_WATCHLIST_COLUMNS = [
    'ticker',
    'event_date',
    'days_to_event',
    'earnings_session',
    'sector',
    'theme_tags',
    'prior_gap_profile',
    'analyst_count',
    'target_price',
    'upside_pct',
    'float_proxy',
    'market_cap',
    'liquidity_proxy',
    'pre_event_score',
    'watch_reason',
    'source_ts',
]

LIVE_EVENT_FEED_COLUMNS = [
    'event_id',
    'ts',
    'ticker',
    'source_name',
    'source_tier',
    'headline',
    'url',
    'event_type_raw',
    'primary_event_type',
    'sentiment_raw',
    'price_at_event',
    'market_cap',
    'session',
    'dedupe_key',
]

EVENT_SCORE_LOG_COLUMNS = [
    'event_id',
    'ticker',
    'source_score',
    'catalyst_score',
    'theme_score',
    'preearn_score',
    'market_structure_score',
    'dilution_risk',
    'noise_penalty',
    'trigger_score',
    'score_reason',
    'high_priority_flag',
    'scoring_version',
    'ts',
]

TRADE_TRIGGER_QUEUE_COLUMNS = [
    'event_id',
    'ticker',
    'trigger_score',
    'decision_mode',
    'trigger_source',
    'primary_event_type',
    'price_at_event',
    'current_price',
    'price_extension_pct',
    'relvol_1m',
    'relvol_5m',
    'vwap_dist',
    'sqzmom_1m',
    'sqzmom_5m',
    'entry_signal_status',
    'execution_window',
    'invalidate_if',
    'queue_rank',
    'ts',
]

AI_DECISION_CONTRACT_V2_COLUMNS = [
    'as_of_date',
    'ticker',
    'company_name',
    'decision_mode',
    'final_priority',
    'decision_status',
    'decision_score',
    'decision_reason',
    'primary_event_type',
    'trigger_source',
    'trigger_score',
    'continuation_rank',
    'tomorrow_continuation_prob',
    'confidence_tier',
    'entry_plan',
    'execution_window',
    'avoid_chase_flag',
    'preferred_entry_type',
    'vwap_status',
    'sqzmom_status',
    'volume_status',
    'invalidation_rule',
    'risk_level',
    'risk_note',
    'dilution_flag',
    'halt_risk_flag',
    'source_sheet_trace',
    'protocol_version',
    'data_version',
    'decision_ts',
    'execution_action',
    'position_plan',
    'exit_action',
    'user_visibility',
]


def _normalize_event_type(raw: object) -> str:
    text = str(raw or '').strip().lower()
    if not text:
        return 'neutral_other'

    mapping = [
        ('earnings', 'earnings_beat'),
        ('guidance raise', 'guidance_raise'),
        ('guidance', 'guidance_init'),
        ('contract', 'major_contract'),
        ('customer', 'major_customer'),
        ('fda', 'fda_update'),
        ('upgrade', 'analyst_upgrade'),
        ('target', 'analyst_target_raise'),
        ('8-k', 'sec_8k_positive'),
        ('10-q', 'sec_10q_positive'),
        ('10-k', 'sec_10k_positive'),
        ('insider', 'insider_buy'),
        ('offering', 'offering'),
        ('atm', 'atm_program'),
        ('warrant', 'warrant'),
        ('convertible', 'convertible'),
        ('reverse split', 'reverse_split'),
        ('shelf', 'shelf_registration'),
        ('rumor', 'rumor_unverified'),
    ]
    for key, value in mapping:
        if key in text:
            return value
    return 'neutral_other'


def _source_tier_from_name(source_name: object) -> str:
    text = str(source_name or '').strip().lower()
    if any(k in text for k in ['sec', '8-k', '10-k', '10-q', 'fda', 'government', 'contract']):
        return 'A'
    if any(k in text for k in ['analyst', 'earnings', 'benzinga']):
        return 'B'
    return 'C'


def _resolve_optional_path(path_str: str) -> Path | None:
    text = str(path_str or '').strip()
    if not text:
        return None
    p = Path(text)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _safe_series(df: pd.DataFrame, col: str, default=0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index)
    return df[col]


def _build_pre_event_watchlist(dataset: pd.DataFrame, scan_date: str, top_n: int) -> pd.DataFrame:
    if dataset is None or len(dataset) == 0:
        return pd.DataFrame(columns=PRE_EVENT_WATCHLIST_COLUMNS)

    df = dataset.copy()
    days_to_event = pd.to_numeric(_safe_series(df, 'days_to_earnings', pd.NA), errors='coerce')
    event_score = pd.to_numeric(_safe_series(df, 'event_score_v1', 0.0), errors='coerce').fillna(0.0)
    rel_volume = pd.to_numeric(_safe_series(df, 'rel_volume', 0.0), errors='coerce').fillna(0.0)
    rank_score = pd.to_numeric(_safe_series(df, 'rank_score_v2_adjusted', 0.0), errors='coerce').fillna(0.0)
    pre_event_score = (
        ((15.0 - days_to_event.clip(lower=1.0, upper=14.0)).fillna(0.0) * 2.0)
        + event_score * 0.35
        + rel_volume.clip(lower=0.0, upper=8.0) * 5.0
        + rank_score * 0.15
    )

    candidate_mask = days_to_event.between(1, 14, inclusive='both')
    out = df[candidate_mask].copy()
    if len(out) == 0:
        out = df.copy()

    out = out.assign(_pre_event_score=pre_event_score.reindex(out.index).fillna(0.0))
    out = out.sort_values('_pre_event_score', ascending=False).head(max(1, int(top_n)))

    out_df = pd.DataFrame(
        {
            'ticker': out.get('ticker', pd.Series('', index=out.index)).astype(str).str.upper(),
            'event_date': '',
            'days_to_event': pd.to_numeric(out.get('days_to_earnings'), errors='coerce'),
            'earnings_session': out.get('earnings_status', pd.Series('', index=out.index)).astype(str).str.lower(),
            'sector': out.get('sector', pd.Series('', index=out.index)).astype(str),
            'theme_tags': (
                out.get('sector', pd.Series('', index=out.index)).astype(str)
                + '|'
                + out.get('industry', pd.Series('', index=out.index)).astype(str)
            ).str.strip('|'),
            'prior_gap_profile': pd.to_numeric(out.get('premarket_gap_pct'), errors='coerce').fillna(0.0).round(2),
            'analyst_count': pd.to_numeric(out.get('num_analysts'), errors='coerce').fillna(0).astype(int),
            'target_price': pd.to_numeric(out.get('price'), errors='coerce').fillna(0.0).round(4),
            'upside_pct': pd.to_numeric(out.get('upside_pct'), errors='coerce').fillna(0.0).round(2),
            'float_proxy': pd.to_numeric(out.get('float_rotation_proxy'), errors='coerce').fillna(0.0).round(4),
            'market_cap': pd.to_numeric(out.get('market_cap_raw'), errors='coerce').fillna(0.0).round(2),
            'liquidity_proxy': pd.to_numeric(out.get('xq_dollar_volume_m'), errors='coerce').fillna(0.0).round(2),
            'pre_event_score': pd.to_numeric(out.get('_pre_event_score'), errors='coerce').fillna(0.0).round(2),
            'watch_reason': 'pre_event_or_high_eventscore',
            'source_ts': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        }
    )
    out_df['event_date'] = out_df['days_to_event'].apply(
        lambda d: (datetime.strptime(scan_date, '%Y-%m-%d') + timedelta(days=int(d))).strftime('%Y-%m-%d') if pd.notna(d) and int(d) >= 0 else ''
    )
    return out_df[PRE_EVENT_WATCHLIST_COLUMNS].reset_index(drop=True)


def _load_external_live_event_feed() -> pd.DataFrame:
    feed_specs = [
        (str(getattr(app_config, 'AI_SNIPER_EVENT_FEED_CSV', '')), 'external_event_feed'),
        (str(getattr(app_config, 'AI_SNIPER_NEWS_FEED_CSV', '')), 'Stock Titan News'),
        (str(getattr(app_config, 'AI_SNIPER_SEC_FEED_CSV', '')), 'Stock Titan SEC'),
        (str(getattr(app_config, 'AI_SNIPER_ANALYST_FEED_CSV', '')), 'Benzinga Pro'),
    ]
    frames = []

    for path_str, source_name in feed_specs:
        path = _resolve_optional_path(path_str)
        if path is None or not path.exists():
            continue

        src = _read_csv_fallback(path)
        if len(src) == 0:
            continue

        out = src.copy()
        rename_map = {
            'timestamp': 'ts',
            'time': 'ts',
            'symbol': 'ticker',
            'type': 'event_type_raw',
            'event_type': 'event_type_raw',
            'source': 'source_name',
            'title': 'headline',
            'link': 'url',
            'price': 'price_at_event',
            'sentiment': 'sentiment_raw',
        }
        out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})

        if 'source_name' not in out.columns:
            out['source_name'] = source_name
        out['source_name'] = out['source_name'].astype(str).replace('', source_name)

        if 'event_id' not in out.columns:
            out['event_id'] = [f"ext_{int(time.time())}_{i}" for i in range(len(out))]
        if 'ts' not in out.columns:
            out['ts'] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        if 'ticker' not in out.columns:
            out['ticker'] = ''
        out['ticker'] = out['ticker'].astype(str).str.upper().str.replace('.US', '', regex=False).str.strip()
        out = out[out['ticker'] != ''].copy()

        if 'event_type_raw' not in out.columns:
            out['event_type_raw'] = 'neutral_other'
        if 'primary_event_type' not in out.columns:
            out['primary_event_type'] = out['event_type_raw'].apply(_normalize_event_type)
        if 'source_tier' not in out.columns:
            out['source_tier'] = out['source_name'].apply(_source_tier_from_name)
        if 'headline' not in out.columns:
            out['headline'] = ''
        if 'url' not in out.columns:
            out['url'] = ''
        if 'sentiment_raw' not in out.columns:
            out['sentiment_raw'] = 'neutral'
        if 'price_at_event' not in out.columns:
            out['price_at_event'] = 0.0
        if 'market_cap' not in out.columns:
            out['market_cap'] = 0.0
        if 'session' not in out.columns:
            out['session'] = 'intraday'
        if 'dedupe_key' not in out.columns:
            out['dedupe_key'] = out['ticker'].astype(str) + '|' + out['ts'].astype(str) + '|' + out['primary_event_type'].astype(str)

        frames.append(out[LIVE_EVENT_FEED_COLUMNS])

    if not frames:
        return pd.DataFrame(columns=LIVE_EVENT_FEED_COLUMNS)

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=['dedupe_key'], keep='last')
    return merged.reset_index(drop=True)


def _build_live_event_feed(
    dataset: pd.DataFrame,
    overnight_df: pd.DataFrame,
    event_signals: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, object]]:
    external = _load_external_live_event_feed()
    if len(external) > 0:
        return external, {'mode': 'external', 'rows': int(len(external))}

    price_map = {}
    mcap_map = {}
    if dataset is not None and len(dataset) > 0:
        price_map = dict(zip(dataset['ticker'].astype(str).str.upper(), pd.to_numeric(dataset.get('price'), errors='coerce').fillna(0.0)))
        mcap_map = dict(zip(dataset['ticker'].astype(str).str.upper(), pd.to_numeric(dataset.get('market_cap_raw'), errors='coerce').fillna(0.0)))

    proxy_rows = []
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    if overnight_df is not None and len(overnight_df) > 0:
        for idx, row in overnight_df.iterrows():
            ticker = str(row.get('ticker', '')).strip().upper()
            if not ticker:
                continue
            event_type_raw = str(row.get('catalyst_type', '')).strip().lower() or str(row.get('final_impact', '')).strip().lower()
            ts = str(row.get('catalyst_time', '')).strip() or now_str
            primary = _normalize_event_type(event_type_raw)
            proxy_rows.append(
                {
                    'event_id': f'proxy_overnight_{ticker}_{idx + 1}',
                    'ts': ts,
                    'ticker': ticker,
                    'source_name': 'overnight_catalyst_proxy',
                    'source_tier': 'C',
                    'headline': str(row.get('source_snippet', '')).strip(),
                    'url': '',
                    'event_type_raw': event_type_raw or 'neutral_other',
                    'primary_event_type': primary,
                    'sentiment_raw': str(row.get('final_impact', row.get('impact', 'neutral'))).strip().lower() or 'neutral',
                    'price_at_event': float(price_map.get(ticker, 0.0)),
                    'market_cap': float(mcap_map.get(ticker, 0.0)),
                    'session': 'premarket',
                    'dedupe_key': f'{ticker}|{ts}|{primary}',
                }
            )

    if not proxy_rows and event_signals is not None and len(event_signals) > 0:
        for idx, row in event_signals.iterrows():
            ticker = str(row.get('ticker', '')).strip().upper()
            if not ticker:
                continue
            event_type_raw = str(row.get('event_type', 'neutral_other')).strip().lower()
            primary = _normalize_event_type(event_type_raw)
            proxy_rows.append(
                {
                    'event_id': f'proxy_event_{ticker}_{idx + 1}',
                    'ts': now_str,
                    'ticker': ticker,
                    'source_name': 'event_signals_proxy',
                    'source_tier': 'C',
                    'headline': str(row.get('event_reason', '')).strip(),
                    'url': '',
                    'event_type_raw': event_type_raw,
                    'primary_event_type': primary,
                    'sentiment_raw': 'neutral',
                    'price_at_event': float(price_map.get(ticker, 0.0)),
                    'market_cap': float(mcap_map.get(ticker, 0.0)),
                    'session': 'intraday',
                    'dedupe_key': f'{ticker}|{now_str}|{primary}',
                }
            )

    if not proxy_rows:
        return pd.DataFrame(columns=LIVE_EVENT_FEED_COLUMNS), {'mode': 'none', 'rows': 0}

    out = pd.DataFrame(proxy_rows)[LIVE_EVENT_FEED_COLUMNS].drop_duplicates(subset=['dedupe_key'], keep='first')
    return out.reset_index(drop=True), {'mode': 'proxy', 'rows': int(len(out))}


def _build_event_score_log(live_event_feed: pd.DataFrame, pre_event_watchlist: pd.DataFrame, dataset: pd.DataFrame) -> pd.DataFrame:
    if live_event_feed is None or len(live_event_feed) == 0:
        return pd.DataFrame(columns=EVENT_SCORE_LOG_COLUMNS)

    pre_event_days = {}
    if pre_event_watchlist is not None and len(pre_event_watchlist) > 0:
        pre_event_days = dict(zip(pre_event_watchlist['ticker'].astype(str).str.upper(), pd.to_numeric(pre_event_watchlist.get('days_to_event'), errors='coerce')))

    rel_vol_map = {}
    market_cap_map = {}
    sector_map = {}
    if dataset is not None and len(dataset) > 0:
        rel_vol_map = dict(zip(dataset['ticker'].astype(str).str.upper(), pd.to_numeric(dataset.get('rel_volume'), errors='coerce').fillna(0.0)))
        market_cap_map = dict(zip(dataset['ticker'].astype(str).str.upper(), pd.to_numeric(dataset.get('market_cap_raw'), errors='coerce').fillna(0.0)))
        sector_map = dict(zip(dataset['ticker'].astype(str).str.upper(), dataset.get('sector', pd.Series('', index=dataset.index)).astype(str)))

    source_score_map = {'A': 4.0, 'B': 2.0, 'C': 1.0}
    catalyst_score_map = {
        'guidance_raise': 4.0,
        'earnings_beat': 4.0,
        'major_contract': 3.0,
        'major_customer': 3.0,
        'fda_update': 3.0,
        'analyst_upgrade': 2.0,
        'analyst_target_raise': 2.0,
        'sec_8k_positive': 3.0,
        'sec_10q_positive': 3.0,
        'sec_10k_positive': 2.0,
        'theme_breakout': 2.0,
        'rumor_unverified': 0.0,
    }
    dilution_map = {
        'offering': 6.0,
        'atm_program': 6.0,
        'warrant': 6.0,
        'reverse_split': 6.0,
        'convertible': 4.0,
        'shelf_registration': 3.0,
        'dilution_other': 4.0,
    }

    dedupe_count = live_event_feed['dedupe_key'].astype(str).value_counts().to_dict()

    rows = []
    for _, row in live_event_feed.iterrows():
        ticker = str(row.get('ticker', '')).strip().upper()
        if not ticker:
            continue

        primary = _normalize_event_type(row.get('primary_event_type', row.get('event_type_raw', '')))
        tier = str(row.get('source_tier', _source_tier_from_name(row.get('source_name')))).strip().upper()
        source_score = source_score_map.get(tier, 1.0)
        catalyst_score = catalyst_score_map.get(primary, 1.0)

        sector_text = str(sector_map.get(ticker, '')).lower()
        theme_score = 2.0 if any(k in sector_text for k in ['technology', 'semiconductor', 'defense', 'energy']) else 1.0
        days_to_event = pd.to_numeric(pre_event_days.get(ticker, pd.NA), errors='coerce')
        preearn_score = 2.0 if pd.notna(days_to_event) and 1 <= float(days_to_event) <= 14 else 0.0
        rel_vol = float(rel_vol_map.get(ticker, 0.0))
        market_cap = float(market_cap_map.get(ticker, 0.0))
        market_structure_score = 0.0
        if market_cap > 0 and market_cap <= 8_000_000_000:
            market_structure_score += 1.0
        if rel_vol >= 1.8:
            market_structure_score += 1.0

        dilution_risk = float(dilution_map.get(primary, 0.0))
        noise_penalty = 0.0
        if primary == 'rumor_unverified':
            noise_penalty += 2.0
        if dedupe_count.get(str(row.get('dedupe_key', '')), 0) > 1:
            noise_penalty += 1.0
        if not str(row.get('headline', '')).strip():
            noise_penalty += 1.0

        trigger_score = source_score + catalyst_score + theme_score + preearn_score + market_structure_score - dilution_risk - noise_penalty

        rows.append(
            {
                'event_id': row.get('event_id', ''),
                'ticker': ticker,
                'source_score': round(source_score, 2),
                'catalyst_score': round(catalyst_score, 2),
                'theme_score': round(theme_score, 2),
                'preearn_score': round(preearn_score, 2),
                'market_structure_score': round(market_structure_score, 2),
                'dilution_risk': round(dilution_risk, 2),
                'noise_penalty': round(noise_penalty, 2),
                'trigger_score': round(trigger_score, 2),
                'score_reason': f"src={source_score};cat={catalyst_score};theme={theme_score};pre={preearn_score};ms={market_structure_score};dil={dilution_risk};noise={noise_penalty}",
                'high_priority_flag': bool(trigger_score >= float(getattr(app_config, 'AI_SNIPER_HIGH_PRIORITY_MIN_SCORE', 9.0))),
                'scoring_version': 'event_sniper_protocol_v1',
                'ts': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            }
        )

    out = pd.DataFrame(rows)
    if len(out) == 0:
        return pd.DataFrame(columns=EVENT_SCORE_LOG_COLUMNS)
    out = out.sort_values(['trigger_score', 'high_priority_flag'], ascending=[False, False]).reset_index(drop=True)
    return out[EVENT_SCORE_LOG_COLUMNS]


def _build_trade_trigger_queue(event_score_log: pd.DataFrame, live_event_feed: pd.DataFrame, dataset: pd.DataFrame, decision_signals: pd.DataFrame) -> pd.DataFrame:
    if event_score_log is None or len(event_score_log) == 0:
        return pd.DataFrame(columns=TRADE_TRIGGER_QUEUE_COLUMNS)

    trigger_min = float(getattr(app_config, 'AI_SNIPER_TRIGGER_MIN_SCORE', 7.0))
    queue_top_n = max(1, int(getattr(app_config, 'AI_SNIPER_QUEUE_TOP_N', 40)))

    merged = event_score_log.merge(
        live_event_feed[['event_id', 'source_name', 'primary_event_type', 'price_at_event', 'ts']],
        on='event_id',
        how='left',
    )
    merged = merged.merge(
        dataset[[c for c in ['ticker', 'price', 'tv_vwap', 'tv_sqzmom_hist', 'rel_volume'] if c in dataset.columns]],
        on='ticker',
        how='left',
    )

    invalidation_map = {}
    if decision_signals is not None and len(decision_signals) > 0:
        invalidation_map = dict(zip(decision_signals['ticker'].astype(str).str.upper(), decision_signals.get('invalidation_rule', pd.Series('', index=decision_signals.index)).astype(str)))

    def _num(value: object, default: float = 0.0) -> float:
        parsed = pd.to_numeric(value, errors='coerce')
        if pd.isna(parsed):
            return float(default)
        return float(parsed)

    rows = []
    for _, row in merged.iterrows():
        trigger_score = _num(row.get('trigger_score'), 0.0)
        if trigger_score < trigger_min:
            continue

        primary = _normalize_event_type(row.get('primary_event_type', ''))
        if primary in {'offering', 'atm_program', 'warrant', 'reverse_split', 'convertible', 'shelf_registration', 'dilution_other'}:
            continue

        ticker = str(row.get('ticker', '')).strip().upper()
        event_price = _num(row.get('price_at_event'), 0.0)
        current_price = _num(row.get('price'), event_price)
        if current_price <= 0:
            continue

        extension = 0.0
        if event_price > 0:
            extension = (current_price - event_price) / event_price * 100.0

        relvol_1m = _num(row.get('rel_volume'), 0.0)
        relvol_5m = relvol_1m
        vwap_val = _num(row.get('tv_vwap'), 0.0)
        vwap_dist = ((current_price - vwap_val) / vwap_val * 100.0) if vwap_val > 0 else 0.0
        sqz = _num(row.get('tv_sqzmom_hist'), 0.0)

        ready = (vwap_dist >= -0.3) and (sqz >= 0.0) and (relvol_1m >= 1.5) and (extension <= 6.0)
        avoid_chase = extension >= 8.0
        if avoid_chase:
            status = 'avoid_chase'
        elif ready:
            status = 'ready'
        else:
            status = 'watch'

        rows.append(
            {
                'event_id': str(row.get('event_id', '')),
                'ticker': ticker,
                'trigger_score': round(trigger_score, 2),
                'decision_mode': 'sniper',
                'trigger_source': str(row.get('source_name', '')).strip(),
                'primary_event_type': primary,
                'price_at_event': round(event_price, 4),
                'current_price': round(current_price, 4),
                'price_extension_pct': round(extension, 2),
                'relvol_1m': round(relvol_1m, 2),
                'relvol_5m': round(relvol_5m, 2),
                'vwap_dist': round(vwap_dist, 2),
                'sqzmom_1m': round(sqz, 4),
                'sqzmom_5m': round(sqz, 4),
                'entry_signal_status': status,
                'execution_window': 'open_0_15m' if status == 'ready' else 'intraday',
                'invalidate_if': invalidation_map.get(ticker, '回落且有效跌破 VWAP 或 SQZMOM 轉負'),
                'queue_rank': 0,
                'ts': str(row.get('ts', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))),
            }
        )

    out = pd.DataFrame(rows)
    if len(out) == 0:
        return pd.DataFrame(columns=TRADE_TRIGGER_QUEUE_COLUMNS)

    status_order = {'ready': 0, 'watch': 1, 'avoid_chase': 2}
    out['_status_order'] = out['entry_signal_status'].map(status_order).fillna(9)
    out = out.sort_values(['_status_order', 'trigger_score', 'price_extension_pct'], ascending=[True, False, True]).head(queue_top_n).copy()
    out['queue_rank'] = range(1, len(out) + 1)
    out = out.drop(columns=['_status_order'])
    return out[TRADE_TRIGGER_QUEUE_COLUMNS].reset_index(drop=True)


def _build_bundle_contract_status(
    scan_date: str,
    decision_signals: pd.DataFrame,
    ranking_signals: pd.DataFrame,
    live_event_feed: pd.DataFrame,
    event_score_log: pd.DataFrame,
    trade_trigger_queue: pd.DataFrame,
    ai_focus_available: bool,
) -> pd.DataFrame:
    sniper_required = bool(getattr(app_config, 'AI_BUNDLE_CONTRACT_REQUIRE_SNIPER', False))
    sniper_enabled = bool(getattr(app_config, 'AI_SNIPER_LANE_ENABLED', True))

    code = 'OK'
    severity = 'info'
    message = 'bundle_contract_ready'
    status = 'ready'

    core_missing = (decision_signals is None or 'ticker' not in decision_signals.columns) or (ranking_signals is None or 'ticker' not in ranking_signals.columns)
    if core_missing:
        code = 'ERROR_LOCAL_CORE_MISSING'
        severity = 'error'
        status = 'error'
        message = 'decision_signals_daily or ranking_signals_daily missing'
    else:
        schema_issues = []
        core_required = {
            'decision_signals_daily': ['ticker', 'decision_tag_v1', 'decision_action', 'risk_level', 'invalidation_rule'],
            'ranking_signals_daily': ['ticker', 'rank_engine_rank', 'rank_score_v2_adjusted'],
        }
        check_map = {
            'decision_signals_daily': decision_signals,
            'ranking_signals_daily': ranking_signals,
            'live_event_feed': live_event_feed,
            'event_score_log': event_score_log,
            'trade_trigger_queue': trade_trigger_queue,
        }
        optional_required = {
            'live_event_feed': LIVE_EVENT_FEED_COLUMNS,
            'event_score_log': EVENT_SCORE_LOG_COLUMNS,
            'trade_trigger_queue': TRADE_TRIGGER_QUEUE_COLUMNS,
        }

        for file_name, req_cols in core_required.items():
            df = check_map[file_name]
            missing_cols = [c for c in req_cols if c not in df.columns]
            if missing_cols:
                schema_issues.append(f"{file_name}:{'|'.join(missing_cols)}")
        for file_name, req_cols in optional_required.items():
            df = check_map[file_name]
            if len(df) == 0:
                continue
            missing_cols = [c for c in req_cols if c not in df.columns]
            if missing_cols:
                schema_issues.append(f"{file_name}:{'|'.join(missing_cols)}")

        if schema_issues:
            code = 'ERROR_SCHEMA_MISMATCH'
            severity = 'error'
            status = 'error'
            message = '; '.join(schema_issues)
        elif sniper_required and sniper_enabled and (len(live_event_feed) == 0 or len(event_score_log) == 0 or len(trade_trigger_queue) == 0):
            code = 'ERROR_SNIPER_PIPELINE_MISSING'
            severity = 'error'
            status = 'error'
            message = 'sniper required but one of live_event_feed/event_score_log/trade_trigger_queue is empty'
        elif len(live_event_feed) > 0:
            conflict = (
                live_event_feed.groupby('dedupe_key')['primary_event_type'].nunique(dropna=True).reset_index(name='n')
                if 'dedupe_key' in live_event_feed.columns and 'primary_event_type' in live_event_feed.columns
                else pd.DataFrame()
            )
            if len(conflict) > 0 and int((conflict['n'] > 1).sum()) > 0:
                code = 'ERROR_CONFLICTING_SOURCE'
                severity = 'error'
                status = 'error'
                message = 'same dedupe_key has conflicting primary_event_type'
        if code == 'OK' and (not ai_focus_available):
            code = 'WARN_AI_FOCUS_LIST_MISSING'
            severity = 'warning'
            status = 'warning'
            message = 'ai_focus_list missing: warning only, not blocking'
        if code == 'OK' and sniper_enabled and len(live_event_feed) == 0:
            code = 'SNIPER_DISABLED_FALLBACK_TO_LOCAL'
            severity = 'warning'
            status = 'warning'
            message = 'sniper lane has no live feed rows, fallback to continuation lane'

    out = pd.DataFrame(
        [
            {
                'scan_date': scan_date,
                'status': status,
                'severity': severity,
                'error_code': code,
                'message': message,
                'sniper_required': sniper_required,
                'sniper_enabled': sniper_enabled,
                'core_ready': bool(code != 'ERROR_LOCAL_CORE_MISSING'),
                'sniper_ready': bool(len(live_event_feed) > 0 and len(event_score_log) > 0 and len(trade_trigger_queue) > 0),
                'ai_focus_available': bool(ai_focus_available),
                'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            }
        ]
    )
    return out


def _build_ai_decision_contract_template() -> pd.DataFrame:
    return pd.DataFrame(columns=AI_DECISION_CONTRACT_V2_COLUMNS)


def _to_float_or_none(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors='coerce')
    if pd.isna(parsed):
        return None
    return float(parsed)


def _coalesce_float(*values: object) -> float | None:
    for value in values:
        parsed = _to_float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _compute_market_shape_metrics(
    open_price: float | None,
    high_price: float | None,
    low_price: float | None,
    last_price: float | None,
    prev_close: float | None,
) -> tuple[float | None, float | None]:
    close_location_value = None
    upper_wick_pct = None

    if high_price is not None and low_price is not None and last_price is not None and high_price > low_price:
        close_location_value = (last_price - low_price) / (high_price - low_price)
        close_location_value = max(0.0, min(1.0, close_location_value))

    if high_price is not None and last_price is not None:
        body_top = max(v for v in [open_price, last_price] if v is not None)
        denominator = prev_close if prev_close not in (None, 0.0) else last_price
        if denominator not in (None, 0.0):
            upper_wick_pct = max(0.0, high_price - body_top) / denominator * 100.0

    return close_location_value, upper_wick_pct


def _is_us_premarket_window(now_utc: datetime | None = None) -> bool:
    ts_utc = now_utc or datetime.now(timezone.utc)
    ny_dt = ts_utc.astimezone(ZoneInfo('America/New_York'))

    if ny_dt.weekday() >= 5:
        return False

    hhmm = ny_dt.hour * 100 + ny_dt.minute
    return 400 <= hhmm < 930


def _fetch_finnhub_market_snapshot(ticker: str, api_key: str, timeout_sec: float) -> dict[str, object]:
    if not api_key:
        return {}

    try:
        response = requests.get(
            'https://finnhub.io/api/v1/quote',
            params={'symbol': ticker, 'token': api_key},
            timeout=max(3.0, float(timeout_sec)),
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
    except (requests.RequestException, ValueError):
        return {}

    if not isinstance(payload, dict):
        return {}

    last_price = _to_float_or_none(payload.get('c'))
    open_price = _to_float_or_none(payload.get('o'))
    high_price = _to_float_or_none(payload.get('h'))
    low_price = _to_float_or_none(payload.get('l'))
    prev_close = _to_float_or_none(payload.get('pc'))

    premarket_gap_pct = None
    if _is_us_premarket_window() and last_price is not None and prev_close not in (None, 0.0):
        premarket_gap_pct = (last_price - prev_close) / prev_close * 100.0

    close_location_value, upper_wick_pct = _compute_market_shape_metrics(
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        last_price=last_price,
        prev_close=prev_close,
    )

    source = 'finnhub_quote'
    if premarket_gap_pct is not None:
        source = 'finnhub_premarket'

    return {
        'ticker': ticker,
        'premarket_gap_pct_live': premarket_gap_pct,
        'close_location_value_live': close_location_value,
        'upper_wick_pct_live': upper_wick_pct,
        'market_open_price': open_price,
        'market_high_price': high_price,
        'market_low_price': low_price,
        'market_last_price': last_price,
        'market_prev_close': prev_close,
        'market_data_source': source,
        'market_snapshot_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
    }


def _fetch_yfinance_market_snapshot(ticker: str) -> dict[str, object]:
    try:
        ticker_obj = yf.Ticker(ticker)
    except (TypeError, ValueError, AttributeError):
        return {}

    fast_info: dict[str, object] = {}
    try:
        raw_fast_info = ticker_obj.fast_info
        if hasattr(raw_fast_info, 'items'):
            fast_info = dict(raw_fast_info)
    except (TypeError, ValueError, AttributeError):
        fast_info = {}

    info: dict[str, object] = {}
    try:
        raw_info = ticker_obj.get_info()
        if isinstance(raw_info, dict):
            info = raw_info
    except (TypeError, ValueError, AttributeError, KeyError):
        try:
            raw_info = ticker_obj.info
            if isinstance(raw_info, dict):
                info = raw_info
        except (TypeError, ValueError, AttributeError, KeyError):
            info = {}

    last_price = _coalesce_float(
        fast_info.get('lastPrice'),
        fast_info.get('last_price'),
        fast_info.get('regularMarketPrice'),
        info.get('regularMarketPrice'),
        info.get('currentPrice'),
    )
    open_price = _coalesce_float(
        fast_info.get('open'),
        fast_info.get('regularMarketOpen'),
        info.get('open'),
        info.get('regularMarketOpen'),
    )
    high_price = _coalesce_float(
        fast_info.get('dayHigh'),
        fast_info.get('day_high'),
        fast_info.get('regularMarketDayHigh'),
        info.get('dayHigh'),
        info.get('regularMarketDayHigh'),
    )
    low_price = _coalesce_float(
        fast_info.get('dayLow'),
        fast_info.get('day_low'),
        fast_info.get('regularMarketDayLow'),
        info.get('dayLow'),
        info.get('regularMarketDayLow'),
    )
    prev_close = _coalesce_float(
        fast_info.get('previousClose'),
        fast_info.get('previous_close'),
        fast_info.get('regularMarketPreviousClose'),
        info.get('previousClose'),
        info.get('regularMarketPreviousClose'),
    )
    premarket_price = _coalesce_float(
        info.get('preMarketPrice'),
        info.get('postMarketPrice'),
    )

    gap_anchor = premarket_price if premarket_price is not None else last_price
    premarket_gap_pct = None
    if gap_anchor is not None and prev_close not in (None, 0.0):
        premarket_gap_pct = (gap_anchor - prev_close) / prev_close * 100.0

    close_location_value, upper_wick_pct = _compute_market_shape_metrics(
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        last_price=last_price,
        prev_close=prev_close,
    )

    source = 'yfinance_quote'
    if premarket_price is not None:
        source = 'yfinance_premarket'

    return {
        'ticker': ticker,
        'premarket_gap_pct_live': premarket_gap_pct,
        'close_location_value_live': close_location_value,
        'upper_wick_pct_live': upper_wick_pct,
        'market_open_price': open_price,
        'market_high_price': high_price,
        'market_low_price': low_price,
        'market_last_price': last_price,
        'market_prev_close': prev_close,
        'market_data_source': source,
        'market_snapshot_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
    }


def _apply_phase2_market_enrichment(dataset: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    if dataset is None or len(dataset) == 0 or 'ticker' not in dataset.columns:
        return dataset, {'enabled': False, 'reason': 'empty_dataset'}

    enabled = bool(getattr(app_config, 'AI_DECISION_PHASE2_MARKET_ENABLED', True))
    if not enabled:
        return dataset, {'enabled': False, 'reason': 'disabled'}

    provider = str(getattr(app_config, 'AI_DECISION_PHASE2_PROVIDER', 'auto')).strip().lower()
    finnhub_key = str(getattr(app_config, 'FINNHUB_API_KEY', '')).strip()
    if provider not in {'auto', 'finnhub', 'yfinance'}:
        provider = 'auto'
    if provider == 'auto':
        provider = 'finnhub' if finnhub_key else 'yfinance'

    max_tickers = max(1, int(getattr(app_config, 'AI_DECISION_PHASE2_MAX_TICKERS', 80)))
    max_workers = max(1, min(16, int(getattr(app_config, 'AI_DECISION_PHASE2_MAX_WORKERS', 8))))
    timeout_sec = max(3.0, float(getattr(app_config, 'AI_DECISION_PHASE2_TIMEOUT_SEC', 8.0)))

    rank_col = 'rank_score_v2_adjusted' if 'rank_score_v2_adjusted' in dataset.columns else (
        'rank_score_v1' if 'rank_score_v1' in dataset.columns else 'base_alpha_score_v1'
    )
    ranked = dataset.copy()
    ranked[rank_col] = pd.to_numeric(ranked.get(rank_col), errors='coerce').fillna(-1e9)
    tickers = (
        ranked.sort_values(rank_col, ascending=False)['ticker']
        .astype(str)
        .str.strip()
        .str.upper()
        .replace('', pd.NA)
        .dropna()
        .drop_duplicates()
        .head(max_tickers)
        .tolist()
    )
    if not tickers:
        return dataset, {'enabled': False, 'reason': 'no_ticker'}

    def _fetch_one(symbol: str) -> dict[str, object]:
        if provider == 'finnhub' and finnhub_key:
            row = _fetch_finnhub_market_snapshot(symbol, api_key=finnhub_key, timeout_sec=timeout_sec)
            if row:
                return row
            return _fetch_yfinance_market_snapshot(symbol)
        if provider == 'yfinance':
            return _fetch_yfinance_market_snapshot(symbol)

        row = _fetch_finnhub_market_snapshot(symbol, api_key=finnhub_key, timeout_sec=timeout_sec) if finnhub_key else {}
        if row:
            return row
        return _fetch_yfinance_market_snapshot(symbol)

    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, ticker): ticker for ticker in tickers}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            if future.exception() is not None:
                continue
            row = future.result()
            if row and isinstance(row, dict):
                rows.append(row)

    if not rows:
        return dataset, {
            'enabled': True,
            'reason': 'no_market_snapshot',
            'provider': provider,
            'requested_tickers': len(tickers),
            'snapshots': 0,
            'premarket_coverage': 0,
            'ohlc_coverage': 0,
        }

    snap_df = pd.DataFrame(rows).drop_duplicates(subset=['ticker'], keep='first')
    merged = dataset.copy().merge(snap_df, on='ticker', how='left')

    premarket_live = pd.to_numeric(merged.get('premarket_gap_pct_live'), errors='coerce')
    close_loc_live = pd.to_numeric(merged.get('close_location_value_live'), errors='coerce')
    upper_wick_live = pd.to_numeric(merged.get('upper_wick_pct_live'), errors='coerce')

    premarket_existing = pd.to_numeric(merged.get('premarket_gap_pct'), errors='coerce') if 'premarket_gap_pct' in merged.columns else pd.Series(float('nan'), index=merged.index, dtype=float)
    close_loc_existing = pd.to_numeric(merged.get('close_location_value'), errors='coerce') if 'close_location_value' in merged.columns else pd.Series(float('nan'), index=merged.index, dtype=float)
    upper_wick_existing = pd.to_numeric(merged.get('upper_wick_pct'), errors='coerce') if 'upper_wick_pct' in merged.columns else pd.Series(float('nan'), index=merged.index, dtype=float)

    merged['premarket_gap_pct'] = premarket_live.combine_first(premarket_existing)
    merged['close_location_value'] = close_loc_live.combine_first(close_loc_existing)
    merged['upper_wick_pct'] = upper_wick_live.combine_first(upper_wick_existing)

    merged['market_snapshot_live'] = premarket_live.notna() | close_loc_live.notna() | upper_wick_live.notna()
    merged['market_data_source'] = merged.get('market_data_source', pd.Series('', index=merged.index)).fillna('')
    merged['market_snapshot_time'] = merged.get('market_snapshot_time', pd.Series('', index=merged.index)).fillna('')

    return merged, {
        'enabled': True,
        'reason': 'ok',
        'provider': provider,
        'requested_tickers': len(tickers),
        'snapshots': int(len(snap_df)),
        'premarket_coverage': int(premarket_live.notna().sum()),
        'ohlc_coverage': int((close_loc_live.notna() & upper_wick_live.notna()).sum()),
    }


def _refresh_unified_ai_ready_bundle(
    scan_date: str,
    ai_ready_latest_dir: Path,
    ai_trading_latest_dir: Path,
    include_api_catalyst: bool,
) -> dict:
    """Rebuild ai_ready bundle as a single B-path input for web AI."""
    ai_ready_latest_dir.mkdir(parents=True, exist_ok=True)

    sheet_map = [
        (ai_ready_latest_dir, 'ai_focus_list.csv', 'ai_focus_list'),
        (ai_ready_latest_dir, 'fusion_top_daily.csv', 'fusion_top_daily'),
        (ai_ready_latest_dir, 'monster_radar_daily.csv', 'monster_radar_daily'),
        (ai_ready_latest_dir, 'raw_market_daily.csv', 'raw_market_daily'),
        (ai_ready_latest_dir, 'theme_heat_daily.csv', 'theme_heat_daily'),
        (ai_ready_latest_dir, 'theme_leaders_daily.csv', 'theme_leaders_daily'),
        (ai_ready_latest_dir, 'xq_short_term_updated.csv', 'xq_short_term_updated'),
        (ai_trading_latest_dir, 'market_dataset_daily.csv', 'market_dataset_daily'),
        (ai_trading_latest_dir, 'feature_signals_daily.csv', 'feature_signals_daily'),
        (ai_trading_latest_dir, 'radar_signals_daily.csv', 'radar_signals_daily'),
        (ai_trading_latest_dir, 'event_signals_daily.csv', 'event_signals_daily'),
        (ai_trading_latest_dir, 'ranking_signals_daily.csv', 'ranking_signals_daily'),
        (ai_trading_latest_dir, 'decision_signals_daily.csv', 'decision_signals_daily'),
        (ai_trading_latest_dir, 'pre_event_watchlist.csv', 'pre_event_watchlist'),
        (ai_trading_latest_dir, 'live_event_feed.csv', 'live_event_feed'),
        (ai_trading_latest_dir, 'event_score_log.csv', 'event_score_log'),
        (ai_trading_latest_dir, 'trade_trigger_queue.csv', 'trade_trigger_queue'),
        (ai_trading_latest_dir, 'bundle_contract_status.csv', 'bundle_contract_status'),
        (ai_trading_latest_dir, 'ai_decision_contract_v2_template.csv', 'ai_decision_contract_v2_template'),
        (ai_trading_latest_dir, 'ai_decision_contract_v2_materialized.csv', 'ai_decision_contract_v2_material'),
        (ai_trading_latest_dir, 'decision_funnel_daily.csv', 'decision_funnel_daily'),
        (ai_trading_latest_dir, 'decision_outcome_audit_daily.csv', 'decision_outcome_audit_daily'),
        (ai_trading_latest_dir, 'attribution_summary_daily.csv', 'attribution_summary_daily'),
        (ai_trading_latest_dir, 'baseline_v1_vs_variants_metrics.csv', 'baseline_v1_vs_variants_metrics'),
        (ai_trading_latest_dir, 'release_readiness_report.csv', 'release_readiness_report'),
        (ai_trading_latest_dir, 'release_threshold_monitor_report.csv', 'release_threshold_monitor_report'),
        (ai_trading_latest_dir, 'output_schema_stability_report.csv', 'output_schema_stability_report'),
        (ai_trading_latest_dir, 'baseline_v1_config.csv', 'baseline_v1_config'),
        (ai_trading_latest_dir, 'ai_decision_latest.csv', 'ai_decision_latest'),
        (ai_trading_latest_dir, 'overnight_catalyst_check.csv', 'overnight_catalyst_check'),
        (ai_trading_latest_dir, 'ai_research_candidates.csv', 'ai_research_candidates'),
    ]

    if include_api_catalyst:
        sheet_map.append((ai_trading_latest_dir, 'api_catalyst_analysis_daily.csv', 'api_catalyst_analysis'))

    bundle_path = ai_ready_latest_dir / 'ai_ready_bundle.xlsx'
    temp_path = ai_ready_latest_dir / 'ai_ready_bundle.tmp.xlsx'

    written_sheets = []
    try:
        with pd.ExcelWriter(
            temp_path,
            engine='xlsxwriter',
            engine_kwargs={'options': {'strings_to_urls': False}},
        ) as writer:
            for src_dir, csv_name, sheet_name in sheet_map:
                csv_path = src_dir / csv_name
                if not csv_path.exists():
                    continue
                df = _read_csv_fallback(csv_path)
                df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
                written_sheets.append(sheet_name)
    except (ModuleNotFoundError, ImportError, OSError, PermissionError, ValueError):
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return {
            'bundle_updated': False,
            'bundle_path': str(bundle_path),
            'bundle_sheet_count': 0,
            'bundle_sheets': [],
            'reason': 'xlsx_build_failed',
        }

    if not written_sheets:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return {
            'bundle_updated': False,
            'bundle_path': str(bundle_path),
            'bundle_sheet_count': 0,
            'bundle_sheets': [],
            'reason': 'no_source_csv',
        }

    try:
        os.replace(temp_path, bundle_path)
    except (PermissionError, OSError):
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return {
            'bundle_updated': False,
            'bundle_path': str(bundle_path),
            'bundle_sheet_count': len(written_sheets),
            'bundle_sheets': written_sheets,
            'reason': 'bundle_file_locked',
        }

    manifest = {
        'scan_date': scan_date,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'mode': 'unified_b',
        'files': [
            'ai_focus_list.csv',
            'fusion_top_daily.csv',
            'monster_radar_daily.csv',
            'raw_market_daily.csv',
            'theme_heat_daily.csv',
            'theme_leaders_daily.csv',
            'xq_short_term_updated.csv',
            'overnight_catalyst_check.csv',
            'pre_event_watchlist.csv',
            'live_event_feed.csv',
            'event_score_log.csv',
            'trade_trigger_queue.csv',
            'bundle_contract_status.csv',
            'ai_decision_contract_v2_template.csv',
            'ai_decision_contract_v2_materialized.csv',
            'decision_funnel_daily.csv',
            'decision_outcome_audit_daily.csv',
            'attribution_summary_daily.csv',
            'baseline_v1_vs_variants_metrics.csv',
            'release_readiness_report.csv',
            'release_threshold_monitor_report.csv',
            'output_schema_stability_report.csv',
            'baseline_v1_config.csv',
            'ai_decision_latest.csv',
            'ai_ready_bundle.xlsx',
        ],
        'bundle_sheet_count': len(written_sheets),
        'bundle_sheets': written_sheets,
        'notes': '統一 B 單一路徑：bundle 內含 continuation + sniper lane 資料；ai_decision_latest 僅為 pipeline preview，官方 final ai_decision 仍由 Web AI 依 bundle+Protocol 產出。',
    }
    with open(ai_ready_latest_dir / 'README_ai_quick_pack.json', 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return {
        'bundle_updated': True,
        'bundle_path': str(bundle_path),
        'bundle_sheet_count': len(written_sheets),
        'bundle_sheets': written_sheets,
        'reason': 'ok',
    }


def refresh_unified_ai_ready_bundle(
    scan_date: str,
    ai_ready_latest_dir: Path,
    ai_trading_latest_dir: Path,
    include_api_catalyst: bool,
) -> dict:
    return _refresh_unified_ai_ready_bundle(
        scan_date=scan_date,
        ai_ready_latest_dir=ai_ready_latest_dir,
        ai_trading_latest_dir=ai_trading_latest_dir,
        include_api_catalyst=include_api_catalyst,
    )


def main() -> int:
    scan_date = _previous_trading_day_str()
    run_stamp = str(os.getenv('AI_BUILD_RUN_STAMP', '')).strip() or datetime.now().strftime('%H%M%S')

    run_dir = AI_TRADING_OUTPUT_DIR / scan_date / run_stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    paths = DataPaths(
        raw_market_csv=str(DAILY_REFRESH_LATEST / 'raw_market_daily.csv'),
        monster_radar_csv=str(DAILY_REFRESH_LATEST / 'monster_radar_daily.csv'),
        xq_updated_csv=str(AI_READY_LATEST / 'xq_short_term_updated.csv'),
        ai_focus_csv=str(DAILY_REFRESH_LATEST / 'ai_focus_list.csv'),
        fusion_csv=str(DAILY_REFRESH_LATEST / 'fusion_top_daily.csv'),
    )

    pipeline = MarketDataPipeline(paths)
    artifacts = pipeline.build(as_of_date=scan_date)

    dataset_file = run_dir / 'market_dataset_daily.csv'
    feature_file = run_dir / 'feature_signals_daily.csv'
    radar_file = run_dir / 'radar_signals_daily.csv'
    events_file = run_dir / 'event_signals_daily.csv'
    ranking_file = run_dir / 'ranking_signals_daily.csv'
    decision_file = run_dir / 'decision_signals_daily.csv'
    overnight_file = run_dir / 'overnight_catalyst_check.csv'
    pre_event_watchlist_file = run_dir / 'pre_event_watchlist.csv'
    live_event_feed_file = run_dir / 'live_event_feed.csv'
    event_score_log_file = run_dir / 'event_score_log.csv'
    trade_trigger_queue_file = run_dir / 'trade_trigger_queue.csv'
    bundle_contract_status_file = run_dir / 'bundle_contract_status.csv'
    ai_decision_contract_template_file = run_dir / 'ai_decision_contract_v2_template.csv'

    overnight_top_n = int(getattr(app_config, 'OVERNIGHT_CATALYST_TOP_N', 20))
    catalyst_timeout = min(float(getattr(app_config, 'CATALYST_HTTP_TIMEOUT_SEC', 15.0)), 8.0)
    overnight_df = _build_overnight_catalyst_check(
        ranking_signals=artifacts.ranking_signals,
        tavily_api_key=str(getattr(app_config, 'TAVILY_API_KEY', '')),
        gemini_api_key=str(getattr(app_config, 'GEMINI_API_KEY', '')),
        gemini_model=str(getattr(app_config, 'GEMINI_MODEL', 'gemini-2.0-flash')),
        timeout_sec=max(catalyst_timeout, 3.0),
        top_n=max(overnight_top_n, 1),
    )
    overnight_df.to_csv(overnight_file, index=False, encoding='utf-8-sig')

    impact_map = {}
    catalyst_time_map = {}
    catalyst_type_map = {}
    impact_col = 'final_impact' if 'final_impact' in overnight_df.columns else 'impact'
    if len(overnight_df) > 0 and {'ticker', impact_col}.issubset(set(overnight_df.columns)):
        impact_map = {
            str(k).strip().upper(): str(v).strip().lower()
            for k, v in zip(overnight_df['ticker'].tolist(), overnight_df[impact_col].tolist())
        }
    if len(overnight_df) > 0 and {'ticker', 'catalyst_time'}.issubset(set(overnight_df.columns)):
        catalyst_time_map = {
            str(k).strip().upper(): str(v).strip()
            for k, v in zip(overnight_df['ticker'].tolist(), overnight_df['catalyst_time'].tolist())
        }
    if len(overnight_df) > 0 and {'ticker', 'catalyst_type'}.issubset(set(overnight_df.columns)):
        catalyst_type_map = {
            str(k).strip().upper(): str(v).strip().lower()
            for k, v in zip(overnight_df['ticker'].tolist(), overnight_df['catalyst_type'].tolist())
        }

    ticker_key = artifacts.dataset.get('ticker', pd.Series('', index=artifacts.dataset.index)).astype(str).str.upper()
    artifacts.dataset['overnight_catalyst'] = ticker_key.map(impact_map).fillna('')
    artifacts.dataset['overnight_catalyst_time'] = ticker_key.map(catalyst_time_map).fillna('')
    artifacts.dataset['overnight_catalyst_type'] = ticker_key.map(catalyst_type_map).fillna('')

    artifacts.dataset, phase2_market_meta = _apply_phase2_market_enrichment(artifacts.dataset)

    artifacts.dataset, artifacts.ranking_signals, ranking_meta = apply_ranking_engine(
        dataset=artifacts.dataset,
        event_signals=artifacts.event_signals,
    )
    artifacts.dataset, artifacts.decision_signals, decision_meta = apply_decision_risk_layer(
        dataset=artifacts.dataset,
    )

    rank_sort_col = 'rank_score_v2_adjusted' if 'rank_score_v2_adjusted' in artifacts.dataset.columns else 'rank_score_v1'
    artifacts.dataset = artifacts.dataset.sort_values(
        [rank_sort_col, 'rank_score_v1', 'event_score_v1', 'multi_radar_score', 'feature_alpha_score_v1', 'ticker'],
        ascending=[False, False, False, False, False, True],
    ).reset_index(drop=True)
    artifacts.dataset['dataset_rank'] = range(1, len(artifacts.dataset) + 1)

    if len(artifacts.ranking_signals) > 0:
        artifacts.ranking_signals['as_of_date'] = scan_date
    if len(artifacts.decision_signals) > 0:
        artifacts.decision_signals['as_of_date'] = scan_date

    artifacts.dataset.to_csv(dataset_file, index=False, encoding='utf-8-sig')
    artifacts.feature_signals.to_csv(feature_file, index=False, encoding='utf-8-sig')
    artifacts.radar_signals.to_csv(radar_file, index=False, encoding='utf-8-sig')
    artifacts.event_signals.to_csv(events_file, index=False, encoding='utf-8-sig')
    artifacts.ranking_signals.to_csv(ranking_file, index=False, encoding='utf-8-sig')
    artifacts.decision_signals.to_csv(decision_file, index=False, encoding='utf-8-sig')

    sniper_enabled = bool(getattr(app_config, 'AI_SNIPER_LANE_ENABLED', True))
    if sniper_enabled:
        pre_event_watchlist_df = _build_pre_event_watchlist(
            dataset=artifacts.dataset,
            scan_date=scan_date,
            top_n=max(1, int(getattr(app_config, 'AI_SNIPER_PRE_EVENT_TOP_N', 120))),
        )
        live_event_feed_df, sniper_feed_meta = _build_live_event_feed(
            dataset=artifacts.dataset,
            overnight_df=overnight_df,
            event_signals=artifacts.event_signals,
        )
        event_score_log_df = _build_event_score_log(
            live_event_feed=live_event_feed_df,
            pre_event_watchlist=pre_event_watchlist_df,
            dataset=artifacts.dataset,
        )
        trade_trigger_queue_df = _build_trade_trigger_queue(
            event_score_log=event_score_log_df,
            live_event_feed=live_event_feed_df,
            dataset=artifacts.dataset,
            decision_signals=artifacts.decision_signals,
        )
    else:
        pre_event_watchlist_df = pd.DataFrame(columns=PRE_EVENT_WATCHLIST_COLUMNS)
        live_event_feed_df = pd.DataFrame(columns=LIVE_EVENT_FEED_COLUMNS)
        event_score_log_df = pd.DataFrame(columns=EVENT_SCORE_LOG_COLUMNS)
        trade_trigger_queue_df = pd.DataFrame(columns=TRADE_TRIGGER_QUEUE_COLUMNS)
        sniper_feed_meta = {'mode': 'disabled', 'rows': 0}

    pre_event_watchlist_df.to_csv(pre_event_watchlist_file, index=False, encoding='utf-8-sig')
    live_event_feed_df.to_csv(live_event_feed_file, index=False, encoding='utf-8-sig')
    event_score_log_df.to_csv(event_score_log_file, index=False, encoding='utf-8-sig')
    trade_trigger_queue_df.to_csv(trade_trigger_queue_file, index=False, encoding='utf-8-sig')

    ai_focus_available = Path(paths.ai_focus_csv).exists()
    bundle_contract_status_df = _build_bundle_contract_status(
        scan_date=scan_date,
        decision_signals=artifacts.decision_signals,
        ranking_signals=artifacts.ranking_signals,
        live_event_feed=live_event_feed_df,
        event_score_log=event_score_log_df,
        trade_trigger_queue=trade_trigger_queue_df,
        ai_focus_available=ai_focus_available,
    )
    bundle_contract_status_df.to_csv(bundle_contract_status_file, index=False, encoding='utf-8-sig')

    ai_decision_template_df = _build_ai_decision_contract_template()
    ai_decision_template_df.to_csv(ai_decision_contract_template_file, index=False, encoding='utf-8-sig')

    artifacts.stats['rows'] = len(artifacts.dataset)
    artifacts.stats['ranking_rows'] = len(artifacts.ranking_signals)
    artifacts.stats['decision_rows'] = len(artifacts.decision_signals)
    artifacts.stats['decision_keep_count'] = decision_meta.get('keep_count', 0)
    artifacts.stats['decision_watch_count'] = decision_meta.get('watch_count', 0)
    artifacts.stats['decision_funnel_total'] = decision_meta.get('funnel_total', len(artifacts.dataset))
    artifacts.stats['decision_funnel_hard_exhaustion'] = decision_meta.get('funnel_hard_exhaustion', 0)
    artifacts.stats['decision_funnel_hard_live_gap'] = decision_meta.get('funnel_hard_live_gap', 0)
    artifacts.stats['decision_funnel_hard_risk'] = decision_meta.get('funnel_hard_risk', 0)
    artifacts.stats['decision_funnel_watch_exhaustion'] = decision_meta.get('funnel_watch_exhaustion', 0)
    artifacts.stats['decision_funnel_watch_proxy_gap'] = decision_meta.get('funnel_watch_proxy_gap', 0)
    artifacts.stats['decision_funnel_watch_wick_close'] = decision_meta.get('funnel_watch_wick_close', 0)
    artifacts.stats['decision_funnel_watch_overheat'] = decision_meta.get('funnel_watch_overheat', 0)
    artifacts.stats['decision_funnel_live_rows'] = decision_meta.get('funnel_live_rows', 0)
    artifacts.stats['decision_funnel_proxy_rows'] = decision_meta.get('funnel_proxy_rows', 0)
    artifacts.stats['decision_gate_reason_counts'] = decision_meta.get('gate_reason_counts', {})
    artifacts.stats['scanner_profile'] = decision_meta.get('scanner_profile', artifacts.stats.get('scanner_profile', 'balanced'))
    artifacts.stats['scanner_pass_count'] = decision_meta.get('scanner_pass_count', artifacts.stats.get('scanner_pass_count', 0))
    artifacts.stats['rank_regime'] = ranking_meta.get('regime', artifacts.stats.get('rank_regime', 'neutral'))
    artifacts.stats['rank_breadth'] = ranking_meta.get('breadth', artifacts.stats.get('rank_breadth', 0.0))
    artifacts.stats['overnight_rows'] = len(overnight_df)
    artifacts.stats['phase2_market_provider'] = phase2_market_meta.get('provider', '')
    artifacts.stats['phase2_market_snapshots'] = phase2_market_meta.get('snapshots', 0)
    artifacts.stats['phase2_market_premarket_coverage'] = phase2_market_meta.get('premarket_coverage', 0)
    artifacts.stats['phase2_market_ohlc_coverage'] = phase2_market_meta.get('ohlc_coverage', 0)
    artifacts.stats['phase2_market_reason'] = phase2_market_meta.get('reason', 'n/a')
    artifacts.stats['sniper_enabled'] = sniper_enabled
    artifacts.stats['sniper_feed_mode'] = sniper_feed_meta.get('mode', 'none')
    artifacts.stats['live_event_rows'] = int(len(live_event_feed_df))
    artifacts.stats['event_score_rows'] = int(len(event_score_log_df))
    artifacts.stats['trade_trigger_rows'] = int(len(trade_trigger_queue_df))
    artifacts.stats['pre_event_rows'] = int(len(pre_event_watchlist_df))
    artifacts.stats['bundle_contract_status'] = str(bundle_contract_status_df.iloc[0].get('error_code', 'UNKNOWN')) if len(bundle_contract_status_df) > 0 else 'UNKNOWN'

    funnel_cols = [
        'ticker',
        'pre_gate_rank',
        'rank_score_v2_adjusted',
        'rankscorev2adjusted',
        'overnight_followthrough_score',
        'open_exhaustion_risk_score',
        'market_data_source',
        'market_snapshot_live',
        'protocol_gate_reason',
        'post_gate_decision',
        'gate_stage',
        'decision_rank_after_gate',
        'cap_hit_flag',
        'final_elimination_owner',
        'promote_to_keep_reason',
        'as_of_date',
    ]
    funnel_df = artifacts.dataset.copy()
    if 'rankscorev2adjusted' not in funnel_df.columns and 'rank_score_v2_adjusted' in funnel_df.columns:
        funnel_df['rankscorev2adjusted'] = funnel_df['rank_score_v2_adjusted']
    for col in funnel_cols:
        if col not in funnel_df.columns:
            funnel_df[col] = ''
    funnel_df = funnel_df[funnel_cols].copy()
    funnel_df = funnel_df.rename(columns={'as_of_date': 'asofdate'})
    funnel_df.to_csv(run_dir / 'decision_funnel_daily.csv', index=False, encoding='utf-8-sig')

    bridge_meta = build_research_bridge(
        dataset=artifacts.dataset,
        feature_signals=artifacts.feature_signals,
        radar_signals=artifacts.radar_signals,
        event_signals=artifacts.event_signals,
        output_dir=run_dir,
        scan_date=scan_date,
        top_n=20,
    )

    research_mode = str(getattr(app_config, 'AI_RESEARCH_MODE', 'web')).strip().lower()
    if research_mode not in {'web', 'api'}:
        research_mode = 'web'
    api_detector_enabled = research_mode == 'api' and bool(getattr(app_config, 'CATALYST_DETECTOR_ENABLED', False))

    gemini_model = str(getattr(app_config, 'GEMINI_MODEL', 'gemini-2.0-flash'))
    api_meta = {'enabled': False, 'rows': 0, 'reason': 'mode_web' if research_mode != 'api' else 'detector_disabled'}
    api_decision_meta = {'enabled': False, 'rows': 0, 'reason': 'mode_web'}
    if api_detector_enabled:
        candidates_path = run_dir / 'ai_research_candidates.csv'
        candidates_df = pd.read_csv(candidates_path, encoding='utf-8-sig') if candidates_path.exists() else pd.DataFrame()
        api_meta = run_catalyst_detector_api(
            candidates_df=candidates_df,
            output_dir=run_dir,
            scan_date=scan_date,
            tavily_api_key=str(getattr(app_config, 'TAVILY_API_KEY', '')),
            gemini_api_key=str(getattr(app_config, 'GEMINI_API_KEY', '')),
            gemini_model=gemini_model,
            top_k=int(getattr(app_config, 'CATALYST_TOP_K', 12)),
            tavily_max_results=int(getattr(app_config, 'CATALYST_TAVILY_MAX_RESULTS', 4)),
            timeout_sec=float(getattr(app_config, 'CATALYST_HTTP_TIMEOUT_SEC', 15.0)),
        )

        api_catalyst_path = run_dir / 'api_catalyst_analysis_daily.csv'
        api_catalyst_df = _read_csv_fallback(api_catalyst_path) if api_catalyst_path.exists() else pd.DataFrame()
        if bool(api_meta.get('enabled', False)) and int(api_meta.get('rows', 0)) > 0 and len(api_catalyst_df) > 0:
            merged_df = artifacts.dataset.merge(candidates_df, on='ticker', how='inner', suffixes=('', '_candidate')) if len(candidates_df) > 0 else pd.DataFrame()
            merged_df = merged_df.merge(api_catalyst_df, on='ticker', how='left', suffixes=('', '_api'))
            api_decision_meta = generate_api_ai_decision(
                merged_df=merged_df,
                output_dir=run_dir,
                inbox_dir=BACKTEST_INBOX_DIR,
                scan_date=scan_date,
                api_key=str(getattr(app_config, 'GEMINI_API_KEY', '')),
                model=gemini_model,
                timeout_sec=float(getattr(app_config, 'CATALYST_HTTP_TIMEOUT_SEC', 15.0)),
                top_k=5,
            )
        else:
            api_decision_meta = {
                'enabled': False,
                'rows': 0,
                'reason': str(api_meta.get('reason', 'api_catalyst_unavailable')),
            }

    manifest = {
        'scan_date': scan_date,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'inputs': {
            'raw_market_daily': paths.raw_market_csv,
            'monster_radar_daily': paths.monster_radar_csv,
            'xq_short_term_updated': paths.xq_updated_csv,
            'ai_focus_list': paths.ai_focus_csv,
            'fusion_top_daily': paths.fusion_csv,
        },
        'outputs': [
            'market_dataset_daily.csv',
            'feature_signals_daily.csv',
            'radar_signals_daily.csv',
            'event_signals_daily.csv',
            'ranking_signals_daily.csv',
            'decision_signals_daily.csv',
            'overnight_catalyst_check.csv',
            'ai_research_candidates.csv',
            'ai_research_prompt.md',
            'ai_research_manifest.json',
            'api_catalyst_analysis_daily.csv',
            'api_catalyst_brief.md',
            'api_catalyst_manifest.json',
            'decision_funnel_daily.csv',
            'pre_event_watchlist.csv',
            'live_event_feed.csv',
            'event_score_log.csv',
            'trade_trigger_queue.csv',
            'bundle_contract_status.csv',
            'ai_decision_contract_v2_template.csv',
            'ai_decision_contract_v2_materialized.csv',
        ],
        'stats': artifacts.stats,
        'bridge': bridge_meta,
        'research_mode': research_mode,
        'api_catalyst': api_meta,
        'api_decision': api_decision_meta,
        'phase2_market': phase2_market_meta,
        'notes': 'AI Trading research-only dataset（不含自動下單）。',
    }

    if bool(api_meta.get('enabled', False)):
        manifest['outputs'].extend([
            'api_catalyst_analysis_daily.csv',
            'api_catalyst_brief.md',
            'api_catalyst_manifest.json',
        ])
    if bool(api_decision_meta.get('enabled', False)):
        manifest['outputs'].append(api_decision_meta.get('file', f'ai_decision_{scan_date}.csv'))

    with open(run_dir / 'pipeline_manifest.json', 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    _sync_latest(run_dir, AI_TRADING_OUTPUT_DIR / 'latest')

    bundle_meta = _refresh_unified_ai_ready_bundle(
        scan_date=scan_date,
        ai_ready_latest_dir=AI_READY_LATEST,
        ai_trading_latest_dir=AI_TRADING_OUTPUT_DIR / 'latest',
        include_api_catalyst=bool(api_meta.get('enabled', False)),
    )

    print('[AI_TRADING] dataset rows =', artifacts.stats.get('rows', 0))
    print('[AI_TRADING] feature rows =', artifacts.stats.get('feature_rows', 0))
    print('[AI_TRADING] radar rows =', artifacts.stats.get('radar_rows', 0))
    print('[AI_TRADING] event rows =', artifacts.stats.get('event_rows', 0))
    print('[AI_TRADING] ranking rows =', artifacts.stats.get('ranking_rows', 0))
    print('[AI_TRADING] decision rows =', artifacts.stats.get('decision_rows', 0), '| keep =', artifacts.stats.get('decision_keep_count', 0), '| watch =', artifacts.stats.get('decision_watch_count', 0))
    print('[AI_TRADING] funnel hard(exhaust/gap/risk) =', artifacts.stats.get('decision_funnel_hard_exhaustion', 0), '/', artifacts.stats.get('decision_funnel_hard_live_gap', 0), '/', artifacts.stats.get('decision_funnel_hard_risk', 0))
    print('[AI_TRADING] funnel watch(exhaust/proxy_gap/wick/overheat) =', artifacts.stats.get('decision_funnel_watch_exhaustion', 0), '/', artifacts.stats.get('decision_funnel_watch_proxy_gap', 0), '/', artifacts.stats.get('decision_funnel_watch_wick_close', 0), '/', artifacts.stats.get('decision_funnel_watch_overheat', 0))
    print('[AI_TRADING] overnight rows =', artifacts.stats.get('overnight_rows', 0))
    print('[AI_TRADING] phase2 market =', phase2_market_meta.get('provider', 'n/a'), '| snapshots =', phase2_market_meta.get('snapshots', 0), '| premarket =', phase2_market_meta.get('premarket_coverage', 0), '| ohlc =', phase2_market_meta.get('ohlc_coverage', 0), '| reason =', phase2_market_meta.get('reason', 'n/a'))
    print('[AI_TRADING] scanner profile =', artifacts.stats.get('scanner_profile', 'balanced'), '| pass count =', artifacts.stats.get('scanner_pass_count', 0))
    print('[AI_TRADING] regime =', artifacts.stats.get('rank_regime', 'neutral'), '| breadth =', artifacts.stats.get('rank_breadth', 0.0))
    print('[AI_TRADING] research mode =', research_mode)
    print('[AI_TRADING] api detector enabled =', api_detector_enabled)
    print('[AI_TRADING] api catalyst rows =', api_meta.get('rows', 0), '| enabled =', api_meta.get('enabled', False), '| reason =', api_meta.get('reason', 'n/a'))
    print('[AI_TRADING] api ai_decision rows =', api_decision_meta.get('rows', 0), '| enabled =', api_decision_meta.get('enabled', False), '| inbox =', api_decision_meta.get('inbox_path', 'n/a'))
    print('[AI_TRADING] bridge rows =', bridge_meta.get('candidate_rows', 0))
    print('[AI_TRADING] sniper lane =', sniper_enabled, '| feed mode =', sniper_feed_meta.get('mode', 'none'), '| live events =', len(live_event_feed_df), '| trigger queue =', len(trade_trigger_queue_df))
    print('[AI_TRADING] bundle contract code =', bundle_contract_status_df.iloc[0].get('error_code', 'UNKNOWN') if len(bundle_contract_status_df) > 0 else 'UNKNOWN')
    print('[AI_TRADING] unified B bundle =', bundle_meta.get('bundle_updated', False), '| sheets =', bundle_meta.get('bundle_sheet_count', 0), '| reason =', bundle_meta.get('reason', 'n/a'))
    print('[AI_TRADING] output =', run_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
