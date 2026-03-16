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
from datetime import datetime, timedelta
from pathlib import Path

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
import config as app_config

from app_logging import install_builtin_print_logging

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
            'ai_ready_bundle.xlsx',
        ],
        'bundle_sheet_count': len(written_sheets),
        'bundle_sheets': written_sheets,
        'notes': '統一 B 單一路徑：ai_ready_bundle.xlsx 已內含 ai_ready + ai_trading 核心訊號。',
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
    impact_col = 'final_impact' if 'final_impact' in overnight_df.columns else 'impact'
    if len(overnight_df) > 0 and {'ticker', impact_col}.issubset(set(overnight_df.columns)):
        impact_map = {
            str(k).strip().upper(): str(v).strip().lower()
            for k, v in zip(overnight_df['ticker'].tolist(), overnight_df[impact_col].tolist())
        }

    artifacts.dataset['overnight_catalyst'] = artifacts.dataset.get('ticker', pd.Series('', index=artifacts.dataset.index)).astype(str).str.upper().map(impact_map).fillna('')

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

    artifacts.stats['rows'] = len(artifacts.dataset)
    artifacts.stats['ranking_rows'] = len(artifacts.ranking_signals)
    artifacts.stats['decision_rows'] = len(artifacts.decision_signals)
    artifacts.stats['decision_keep_count'] = decision_meta.get('keep_count', 0)
    artifacts.stats['decision_watch_count'] = decision_meta.get('watch_count', 0)
    artifacts.stats['scanner_profile'] = decision_meta.get('scanner_profile', artifacts.stats.get('scanner_profile', 'balanced'))
    artifacts.stats['scanner_pass_count'] = decision_meta.get('scanner_pass_count', artifacts.stats.get('scanner_pass_count', 0))
    artifacts.stats['rank_regime'] = ranking_meta.get('regime', artifacts.stats.get('rank_regime', 'neutral'))
    artifacts.stats['rank_breadth'] = ranking_meta.get('breadth', artifacts.stats.get('rank_breadth', 0.0))
    artifacts.stats['overnight_rows'] = len(overnight_df)

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
        ],
        'stats': artifacts.stats,
        'bridge': bridge_meta,
        'research_mode': research_mode,
        'api_catalyst': api_meta,
        'api_decision': api_decision_meta,
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
    print('[AI_TRADING] overnight rows =', artifacts.stats.get('overnight_rows', 0))
    print('[AI_TRADING] scanner profile =', artifacts.stats.get('scanner_profile', 'balanced'), '| pass count =', artifacts.stats.get('scanner_pass_count', 0))
    print('[AI_TRADING] regime =', artifacts.stats.get('rank_regime', 'neutral'), '| breadth =', artifacts.stats.get('rank_breadth', 0.0))
    print('[AI_TRADING] research mode =', research_mode)
    print('[AI_TRADING] api detector enabled =', api_detector_enabled)
    print('[AI_TRADING] api catalyst rows =', api_meta.get('rows', 0), '| enabled =', api_meta.get('enabled', False), '| reason =', api_meta.get('reason', 'n/a'))
    print('[AI_TRADING] api ai_decision rows =', api_decision_meta.get('rows', 0), '| enabled =', api_decision_meta.get('enabled', False), '| inbox =', api_decision_meta.get('inbox_path', 'n/a'))
    print('[AI_TRADING] bridge rows =', bridge_meta.get('candidate_rows', 0))
    print('[AI_TRADING] unified B bundle =', bundle_meta.get('bundle_updated', False), '| sheets =', bundle_meta.get('bundle_sheet_count', 0), '| reason =', bundle_meta.get('reason', 'n/a'))
    print('[AI_TRADING] output =', run_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
