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
- repo_outputs/ai_trading/YYYY-MM-DD/HHMMSS/pipeline_manifest.json
- repo_outputs/ai_trading/latest/*（同步）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.contracts import DataPaths
from ai_trading.catalyst_api import run_catalyst_detector_api
from ai_trading.market_data_pipeline import MarketDataPipeline
from ai_trading.research_bridge import build_research_bridge
import config as app_config

DAILY_REFRESH_LATEST = PROJECT_ROOT / 'repo_outputs' / 'daily_refresh' / 'latest'
AI_READY_LATEST = PROJECT_ROOT / 'repo_outputs' / 'ai_ready' / 'latest'
AI_TRADING_OUTPUT_DIR = PROJECT_ROOT / 'repo_outputs' / 'ai_trading'


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
    artifacts.dataset.to_csv(dataset_file, index=False, encoding='utf-8-sig')
    artifacts.feature_signals.to_csv(feature_file, index=False, encoding='utf-8-sig')
    artifacts.radar_signals.to_csv(radar_file, index=False, encoding='utf-8-sig')
    artifacts.event_signals.to_csv(events_file, index=False, encoding='utf-8-sig')
    artifacts.ranking_signals.to_csv(ranking_file, index=False, encoding='utf-8-sig')
    artifacts.decision_signals.to_csv(decision_file, index=False, encoding='utf-8-sig')

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

    api_meta = {'enabled': False, 'rows': 0, 'reason': 'mode_web'}
    if research_mode == 'api' and bool(getattr(app_config, 'CATALYST_DETECTOR_ENABLED', False)):
        candidates_path = run_dir / 'ai_research_candidates.csv'
        candidates_df = pd.read_csv(candidates_path, encoding='utf-8-sig') if candidates_path.exists() else pd.DataFrame()
        api_meta = run_catalyst_detector_api(
            candidates_df=candidates_df,
            output_dir=run_dir,
            scan_date=scan_date,
            tavily_api_key=str(getattr(app_config, 'TAVILY_API_KEY', '')),
            gemini_api_key=str(getattr(app_config, 'GEMINI_API_KEY', '')),
            gemini_model=str(getattr(app_config, 'GEMINI_MODEL', 'gemini-2.0-flash')),
            top_k=int(getattr(app_config, 'CATALYST_TOP_K', 12)),
            tavily_max_results=int(getattr(app_config, 'CATALYST_TAVILY_MAX_RESULTS', 4)),
            timeout_sec=float(getattr(app_config, 'CATALYST_HTTP_TIMEOUT_SEC', 15.0)),
        )

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
            'ai_research_candidates.csv',
            'ai_research_prompt.md',
            'ai_research_manifest.json',
        ],
        'stats': artifacts.stats,
        'bridge': bridge_meta,
        'research_mode': research_mode,
        'api_catalyst': api_meta,
        'notes': 'AI Trading research-only dataset（不含自動下單）。',
    }

    if bool(api_meta.get('enabled', False)):
        manifest['outputs'].extend([
            'api_catalyst_analysis_daily.csv',
            'api_catalyst_brief.md',
            'api_catalyst_manifest.json',
        ])

    with open(run_dir / 'pipeline_manifest.json', 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    _sync_latest(run_dir, AI_TRADING_OUTPUT_DIR / 'latest')

    print('[AI_TRADING] dataset rows =', artifacts.stats.get('rows', 0))
    print('[AI_TRADING] feature rows =', artifacts.stats.get('feature_rows', 0))
    print('[AI_TRADING] radar rows =', artifacts.stats.get('radar_rows', 0))
    print('[AI_TRADING] event rows =', artifacts.stats.get('event_rows', 0))
    print('[AI_TRADING] ranking rows =', artifacts.stats.get('ranking_rows', 0))
    print('[AI_TRADING] decision rows =', artifacts.stats.get('decision_rows', 0), '| keep =', artifacts.stats.get('decision_keep_count', 0), '| watch =', artifacts.stats.get('decision_watch_count', 0))
    print('[AI_TRADING] scanner profile =', artifacts.stats.get('scanner_profile', 'balanced'), '| pass count =', artifacts.stats.get('scanner_pass_count', 0))
    print('[AI_TRADING] regime =', artifacts.stats.get('rank_regime', 'neutral'), '| breadth =', artifacts.stats.get('rank_breadth', 0.0))
    print('[AI_TRADING] research mode =', research_mode)
    print('[AI_TRADING] api catalyst rows =', api_meta.get('rows', 0), '| enabled =', api_meta.get('enabled', False), '| reason =', api_meta.get('reason', 'n/a'))
    print('[AI_TRADING] bridge rows =', bridge_meta.get('candidate_rows', 0))
    print('[AI_TRADING] output =', run_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
