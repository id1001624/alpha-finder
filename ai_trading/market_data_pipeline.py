from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import pandas as pd

from .contracts import DataPaths, normalize_ticker, parse_human_market_cap
from .decision_risk import apply_decision_risk_layer
from .event_detector import detect_events
from .feature_engine import apply_feature_engineering
from .radars import apply_multi_radars
from .ranking_engine import apply_ranking_engine


@dataclass
class PipelineArtifacts:
    dataset: pd.DataFrame
    feature_signals: pd.DataFrame
    radar_signals: pd.DataFrame
    event_signals: pd.DataFrame
    ranking_signals: pd.DataFrame
    decision_signals: pd.DataFrame
    stats: Dict[str, object]


class MarketDataPipeline:
    def __init__(self, data_paths: DataPaths):
        self.data_paths = data_paths

    @staticmethod
    def _safe_read_csv(path_str: str) -> pd.DataFrame:
        if not path_str:
            return pd.DataFrame()
        path = Path(path_str)
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(path, encoding='utf-8-sig')
        except UnicodeDecodeError:
            return pd.read_csv(path)

    @staticmethod
    def _normalize_ticker_column(df: pd.DataFrame, ticker_col: str) -> pd.DataFrame:
        if df is None or len(df) == 0 or ticker_col not in df.columns:
            return pd.DataFrame(columns=['ticker'])

        out = df.copy()
        out['ticker'] = out[ticker_col].apply(normalize_ticker)
        out = out[out['ticker'] != ''].copy()
        out = out.drop_duplicates(subset=['ticker'], keep='first')
        return out

    def _load_raw_market(self) -> pd.DataFrame:
        raw = self._safe_read_csv(self.data_paths.raw_market_csv)
        raw = self._normalize_ticker_column(raw, 'Ticker')
        if len(raw) == 0:
            return pd.DataFrame(columns=['ticker'])

        if 'Market_Cap_Raw' not in raw.columns and 'Market_Cap' in raw.columns:
            raw['Market_Cap_Raw'] = raw['Market_Cap'].apply(parse_human_market_cap)

        rename_map = {
            'Price': 'price',
            'Daily_Change': 'daily_change_pct',
            'Rel_Volume': 'rel_volume',
            'Market_Cap': 'market_cap_text',
            'Market_Cap_Raw': 'market_cap_raw',
            'Upside_Pct': 'upside_pct',
            'Num_Analysts': 'num_analysts',
            'Earnings_Status': 'earnings_status',
            'Days_To_Earnings': 'days_to_earnings',
            'core_score_v81': 'core_score_v81',
            'core_score_source': 'core_score_source',
            'TV_SQZ_On': 'tv_sqz_on',
            'TV_SQZMOM_Color': 'tv_sqzmom_color',
            'TV_VWAP': 'tv_vwap',
            'TV_Signal_Age_Min': 'tv_signal_age_min',
            'Sector': 'sector',
            'Industry': 'industry',
        }
        cols = ['ticker'] + [c for c in rename_map.keys() if c in raw.columns]
        out = raw[cols].copy()
        out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})
        return out

    def _load_monster(self) -> pd.DataFrame:
        monster = self._safe_read_csv(self.data_paths.monster_radar_csv)
        monster = self._normalize_ticker_column(monster, '股票代碼')
        if len(monster) == 0:
            return pd.DataFrame(columns=['ticker'])

        rename_map = {
            '妖股分數': 'monster_score',
            '潛力等級': 'monster_tier',
            '型態階段': 'monster_stage',
            '明日偏向': 'monster_next_day_bias',
            '理由摘要': 'monster_reason',
            '評級': 'sector_rating',
            '產業': 'priority_sector',
        }
        cols = ['ticker'] + [c for c in rename_map.keys() if c in monster.columns]
        out = monster[cols].copy()
        out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})
        return out

    def _load_xq(self) -> pd.DataFrame:
        xq = self._safe_read_csv(self.data_paths.xq_updated_csv)
        xq = self._normalize_ticker_column(xq, 'symbol')
        if len(xq) == 0:
            return pd.DataFrame(columns=['ticker'])

        rename_map = {
            'chg_1d_pct': 'xq_chg_1d_pct',
            'chg_3d_pct': 'xq_chg_3d_pct',
            'chg_5d_pct': 'xq_chg_5d_pct',
            'vol_strength': 'xq_vol_strength',
            'dollar_volume_m': 'xq_dollar_volume_m',
            'short_trade_score': 'xq_short_trade_score',
            'swing_score': 'xq_swing_score',
            'momentum_mix': 'xq_momentum_mix',
            'continuation_grade': 'continuation_grade',
            'prob_next_day': 'prob_next_day',
            'prob_day2': 'prob_day2',
            'decision_tag_hint': 'decision_tag_hint',
            'ai_query_hint': 'ai_query_hint',
        }
        cols = ['ticker'] + [c for c in rename_map.keys() if c in xq.columns]
        out = xq[cols].copy()
        out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})
        return out

    def _load_ai_focus(self) -> pd.DataFrame:
        focus = self._safe_read_csv(self.data_paths.ai_focus_csv)
        focus = self._normalize_ticker_column(focus, 'ticker')
        if len(focus) == 0:
            return pd.DataFrame(columns=['ticker'])

        cols = ['ticker'] + [c for c in ['source', 'priority_score', 'ai_query_hint'] if c in focus.columns]
        return focus[cols].copy()

    def _load_fusion_tickers(self) -> List[str]:
        fusion = self._safe_read_csv(self.data_paths.fusion_csv or '')
        if len(fusion) == 0:
            return []

        ticker_col = '股票代碼' if '股票代碼' in fusion.columns else ('Ticker' if 'Ticker' in fusion.columns else None)
        if ticker_col is None:
            return []

        tickers = [normalize_ticker(v) for v in fusion[ticker_col].tolist()]
        return sorted({t for t in tickers if t})

    @staticmethod
    def _compute_composite_score(df: pd.DataFrame) -> pd.Series:
        daily = pd.to_numeric(df.get('daily_change_pct'), errors='coerce').fillna(0)
        rel_volume = pd.to_numeric(df.get('rel_volume'), errors='coerce').fillna(0)
        core_score = pd.to_numeric(df.get('core_score_v81'), errors='coerce').fillna(0)
        monster_score = pd.to_numeric(df.get('monster_score'), errors='coerce').fillna(0)
        xq_short_score = pd.to_numeric(df.get('xq_short_trade_score'), errors='coerce').fillna(0)

        signal_boost = pd.Series(0.0, index=df.index)
        sqz_on = df.get('tv_sqz_on')
        if sqz_on is not None:
            signal_boost += sqz_on.fillna(False).astype(bool).astype(float) * 2.0

        tv_age_raw = df.get('tv_signal_age_min')
        if tv_age_raw is not None:
            tv_age = pd.to_numeric(tv_age_raw, errors='coerce')
            signal_boost += (tv_age <= 240).fillna(False).astype(float) * 2.0

        focus_boost = df.get('is_in_ai_focus', pd.Series(False, index=df.index)).astype(bool).astype(float) * 1.5

        return (
            daily.clip(lower=-5, upper=20) * 0.8 +
            rel_volume.clip(lower=0, upper=8) * 2.4 +
            core_score.clip(lower=0, upper=60) * 0.35 +
            monster_score.clip(lower=0, upper=100) * 0.45 +
            xq_short_score.clip(lower=-20, upper=80) * 0.4 +
            signal_boost +
            focus_boost
        ).round(2)

    def build(self, as_of_date: str) -> PipelineArtifacts:
        raw_df = self._load_raw_market()
        monster_df = self._load_monster()
        xq_df = self._load_xq()
        focus_df = self._load_ai_focus()
        fusion_tickers = set(self._load_fusion_tickers())

        ticker_union = set(raw_df.get('ticker', pd.Series(dtype=str)).tolist())
        ticker_union.update(monster_df.get('ticker', pd.Series(dtype=str)).tolist())
        ticker_union.update(xq_df.get('ticker', pd.Series(dtype=str)).tolist())
        ticker_union.update(focus_df.get('ticker', pd.Series(dtype=str)).tolist())
        ticker_union = {t for t in ticker_union if t}

        if not ticker_union:
            empty = pd.DataFrame()
            return PipelineArtifacts(
                dataset=empty,
                feature_signals=empty,
                radar_signals=empty,
                event_signals=empty,
                ranking_signals=empty,
                decision_signals=empty,
                stats={
                    'rows': 0,
                    'raw_market_rows': len(raw_df),
                    'monster_rows': len(monster_df),
                    'xq_rows': len(xq_df),
                    'ai_focus_rows': len(focus_df),
                },
            )

        dataset = pd.DataFrame({'ticker': sorted(ticker_union)})
        dataset = dataset.merge(raw_df, on='ticker', how='left')
        dataset = dataset.merge(monster_df, on='ticker', how='left')
        dataset = dataset.merge(xq_df, on='ticker', how='left')

        if len(focus_df) > 0:
            focus_keep_cols = ['ticker'] + [c for c in ['source', 'priority_score', 'ai_query_hint'] if c in focus_df.columns]
            dataset = dataset.merge(focus_df[focus_keep_cols], on='ticker', how='left', suffixes=('', '_focus'))

        dataset['is_in_monster_radar'] = dataset['ticker'].isin(set(monster_df.get('ticker', pd.Series(dtype=str)).tolist()))
        dataset['is_in_ai_focus'] = dataset['ticker'].isin(set(focus_df.get('ticker', pd.Series(dtype=str)).tolist()))
        dataset['is_in_xq'] = dataset['ticker'].isin(set(xq_df.get('ticker', pd.Series(dtype=str)).tolist()))
        dataset['is_in_fusion'] = dataset['ticker'].isin(fusion_tickers)

        dataset['as_of_date'] = as_of_date
        dataset['base_alpha_score_v1'] = self._compute_composite_score(dataset)

        dataset, feature_signals = apply_feature_engineering(dataset, top_k_signals=120)
        dataset, radar_signals = apply_multi_radars(dataset, top_k_signals=80)
        dataset['composite_alpha_score_v1'] = (
            dataset['base_alpha_score_v1'] +
            dataset.get('feature_alpha_score_v1', 0).fillna(0) * 0.32 +
            dataset.get('multi_radar_score', 0).fillna(0) * 0.31
        ).round(2)

        event_signals = detect_events(dataset, top_k=40)
        dataset, ranking_signals, ranking_meta = apply_ranking_engine(dataset, event_signals=event_signals)
        dataset, decision_signals, decision_meta = apply_decision_risk_layer(dataset)

        dataset = dataset.sort_values(
            ['rank_score_v1', 'event_score_v1', 'multi_radar_score', 'feature_alpha_score_v1', 'ticker'],
            ascending=[False, False, False, False, True],
        ).reset_index(drop=True)
        dataset['dataset_rank'] = range(1, len(dataset) + 1)

        if len(event_signals) > 0:
            event_signals['as_of_date'] = as_of_date
        if len(feature_signals) > 0:
            feature_signals['as_of_date'] = as_of_date
        if len(radar_signals) > 0:
            radar_signals['as_of_date'] = as_of_date
        if len(ranking_signals) > 0:
            ranking_signals['as_of_date'] = as_of_date
        if len(decision_signals) > 0:
            decision_signals['as_of_date'] = as_of_date

        stats = {
            'rows': len(dataset),
            'raw_market_rows': len(raw_df),
            'monster_rows': len(monster_df),
            'xq_rows': len(xq_df),
            'ai_focus_rows': len(focus_df),
            'feature_rows': len(feature_signals),
            'radar_rows': len(radar_signals),
            'event_rows': len(event_signals),
            'ranking_rows': len(ranking_signals),
            'decision_rows': len(decision_signals),
            'decision_keep_count': decision_meta.get('keep_count', 0),
            'decision_watch_count': decision_meta.get('watch_count', 0),
            'scanner_profile': decision_meta.get('scanner_profile', 'balanced'),
            'scanner_pass_count': decision_meta.get('scanner_pass_count', 0),
            'rank_regime': ranking_meta.get('regime', 'neutral'),
            'rank_breadth': ranking_meta.get('breadth', 0.0),
        }
        return PipelineArtifacts(
            dataset=dataset,
            feature_signals=feature_signals,
            radar_signals=radar_signals,
            event_signals=event_signals,
            ranking_signals=ranking_signals,
            decision_signals=decision_signals,
            stats=stats,
        )
