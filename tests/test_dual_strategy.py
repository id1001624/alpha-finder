"""
tests/test_dual_strategy.py

Unit tests for dual-strategy features:
  - Ignition / pullback entry split
  - Regime rejection logic
  - Portfolio guards
  - ensure_decision_strategy_columns horizon assignment
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from ai_trading.intraday_execution_engine import (
    _is_ignition_entry,
    _is_pullback_entry,
    _portfolio_blocks_new_entry,
)
from ai_trading.strategy_context import (
    HORIZON_INTRADAY_MONSTER,
    HORIZON_SWING_CORE,
    REGIME_NEUTRAL,
    REGIME_RISK_OFF,
    ensure_decision_strategy_columns,
    classify_watch_horizon,
    normalize_horizon_tag,
)


# ──────────────────────────────────────────────────────────────────────────────
# _is_ignition_entry
# ──────────────────────────────────────────────────────────────────────────────

class TestIsIgnitionEntry:
    def _call(
        self,
        *,
        close_val: float = 102.0,
        avwap: float = 100.0,
        hist: float = 0.5,
        prev_hist: float = 0.3,
        sqz_release: bool = True,
        color: str = "lime",
        # Allow overriding the config buffer pct
        avwap_buffer_pct: float = 0.5,
    ) -> bool:
        with patch(
            "ai_trading.intraday_execution_engine.INTRADAY_ENTRY_MIN_AVWAP_BUFFER_PCT",
            avwap_buffer_pct,
        ):
            return _is_ignition_entry(close_val, avwap, hist, prev_hist, sqz_release, color)

    def test_all_conditions_met_returns_true(self):
        assert self._call() is True

    def test_no_squeeze_release_returns_false(self):
        assert self._call(sqz_release=False) is False

    def test_hist_zero_returns_false(self):
        assert self._call(hist=0.0) is False

    def test_hist_not_rising_returns_false(self):
        # hist <= prev_hist
        assert self._call(hist=0.3, prev_hist=0.5) is False

    def test_close_below_avwap_buffer_returns_false(self):
        # close at avwap exactly, buffer=0.5% → needs close >= 100.5
        assert self._call(close_val=100.0, avwap=100.0, avwap_buffer_pct=0.5) is False

    def test_red_color_returns_false(self):
        assert self._call(color="red") is False

    def test_maroon_color_returns_false(self):
        assert self._call(color="maroon") is False

    def test_green_color_is_valid(self):
        assert self._call(color="green") is True


# ──────────────────────────────────────────────────────────────────────────────
# _is_pullback_entry
# ──────────────────────────────────────────────────────────────────────────────

def _make_valid_history(*, sqz_release_at_idx: int = 5, n: int = 12, open_price: float = 95.0, max_close: float = 103.0) -> pd.DataFrame:
    """Build a valid_history DataFrame that satisfies the recent_release and impulse move checks."""
    opens = [open_price] * n
    closes = [open_price] * (n - 1) + [max_close]
    sqz_releases = [False] * n
    if 0 <= sqz_release_at_idx < n:
        sqz_releases[sqz_release_at_idx] = True
    return pd.DataFrame({
        "Open": opens,
        "Close": closes,
        "sqz_release": sqz_releases,
    })


class TestIsPullbackEntry:
    """Tests for the _is_pullback_entry function.

    Config defaults used (patched where needed):
      INTRADAY_PULLBACK_AVWAP_MIN_PCT  = -1.5
      INTRADAY_PULLBACK_AVWAP_MAX_PCT  =  0.5
      INTRADAY_PULLBACK_MIN_HIST       = -0.02
      INTRADAY_PULLBACK_MAX_VOL_RATIO  = patched to 3.0
      INTRADAY_IMPULSE_MIN_MOVE_PCT    = 2.0
    """

    _PATCH_TARGETS = {
        "INTRADAY_PULLBACK_AVWAP_MIN_PCT": -1.5,
        "INTRADAY_PULLBACK_AVWAP_MAX_PCT": 0.5,
        "INTRADAY_PULLBACK_MIN_HIST": -0.02,
        "INTRADAY_PULLBACK_MAX_VOL_RATIO": 3.0,
        "INTRADAY_IMPULSE_MIN_MOVE_PCT": 2.0,
    }

    def _call(
        self,
        *,
        close_val: float = 100.2,
        avwap: float = 100.5,
        hist: float = 0.1,
        prev_hist: float = 0.05,
        color: str = "lime",
        prev_vol: float = 1000.0,
        cur_vol: float = 800.0,
        valid_history: pd.DataFrame | None = None,
    ) -> bool:
        latest = pd.Series({"Close": close_val, "Volume": cur_vol})
        previous = pd.Series({"Volume": prev_vol})
        if valid_history is None:
            valid_history = _make_valid_history()
        with patch.multiple(
            "ai_trading.intraday_execution_engine",
            **{k: v for k, v in self._PATCH_TARGETS.items()},
        ):
            return _is_pullback_entry(latest, previous, avwap, hist, prev_hist, color, valid_history)

    def test_price_in_range_returns_true(self):
        # close=100.2, avwap=100.5 → gap = -0.30% (within [-1.5, +0.5])
        assert self._call(close_val=100.2, avwap=100.5) is True

    def test_price_too_far_above_avwap_returns_false(self):
        # gap = +5% → above max 0.5%
        assert self._call(close_val=105.0, avwap=100.0) is False

    def test_price_too_far_below_avwap_returns_false(self):
        # gap = -3% → below min -1.5%
        assert self._call(close_val=97.0, avwap=100.0) is False

    def test_zero_avwap_returns_false(self):
        assert self._call(avwap=0.0) is False

    def test_excessive_volume_returns_false(self):
        # vol_ratio = 5000/1000 = 5.0 > max 3.0
        assert self._call(cur_vol=5000.0, prev_vol=1000.0) is False

    def test_hist_below_min_returns_false(self):
        # hist = -0.1 < min -0.02
        assert self._call(hist=-0.1) is False

    def test_hist_falling_with_red_color_returns_false(self):
        # hist < prev_hist and color=red
        assert self._call(hist=0.05, prev_hist=0.10, color="red") is False


# ──────────────────────────────────────────────────────────────────────────────
# _portfolio_blocks_new_entry — regime filter
# ──────────────────────────────────────────────────────────────────────────────

class TestPortfolioBlocksNewEntry:

    def _call(
        self,
        rank: int = 2,
        regime_tag: str = REGIME_NEUTRAL,
        planned: int = 0,
        today_entries: int = 0,
        theme: str = "",
    ) -> bool:
        meta = pd.Series({"rank": rank, "theme": theme})
        with patch.multiple(
            "ai_trading.intraday_execution_engine",
            PORTFOLIO_DAILY_MAX_STRATEGY_LOSS=-999999.0,
            PORTFOLIO_DAILY_MAX_TOTAL_LOSS=-999999.0,
            PORTFOLIO_MAX_NEW_ENTRIES_PER_STRATEGY=5,
            PORTFOLIO_MAX_THEME_EXPOSURE=3,
        ):
            return _portfolio_blocks_new_entry(
                meta,
                pd.DataFrame(),
                pd.DataFrame(),
                regime_tag,
                planned,
                today_entries,
            )

    def test_risk_off_rank_above_1_is_blocked(self):
        assert self._call(rank=2, regime_tag=REGIME_RISK_OFF) is True

    def test_risk_off_rank_3_is_blocked(self):
        assert self._call(rank=3, regime_tag=REGIME_RISK_OFF) is True

    def test_risk_off_rank_1_is_allowed(self):
        assert self._call(rank=1, regime_tag=REGIME_RISK_OFF) is False

    def test_neutral_regime_allows_all_ranks(self):
        assert self._call(rank=5, regime_tag=REGIME_NEUTRAL) is False

    def test_max_entry_count_blocks(self):
        # 3 already today + 2 planned = 5 >= max 5 → blocked
        result = self._call(rank=2, regime_tag=REGIME_NEUTRAL, planned=2, today_entries=3)
        with patch.multiple(
            "ai_trading.intraday_execution_engine",
            PORTFOLIO_DAILY_MAX_STRATEGY_LOSS=-999999.0,
            PORTFOLIO_DAILY_MAX_TOTAL_LOSS=-999999.0,
            PORTFOLIO_MAX_NEW_ENTRIES_PER_STRATEGY=5,
            PORTFOLIO_MAX_THEME_EXPOSURE=3,
        ):
            meta = pd.Series({"rank": 2, "theme": ""})
            blocked = _portfolio_blocks_new_entry(
                meta, pd.DataFrame(), pd.DataFrame(),
                REGIME_NEUTRAL, 2, 3,
            )
        assert blocked is True

    def test_under_entry_limit_not_blocked(self):
        # 1 today + 1 planned = 2 < max 5 → not blocked
        with patch.multiple(
            "ai_trading.intraday_execution_engine",
            PORTFOLIO_DAILY_MAX_STRATEGY_LOSS=-999999.0,
            PORTFOLIO_DAILY_MAX_TOTAL_LOSS=-999999.0,
            PORTFOLIO_MAX_NEW_ENTRIES_PER_STRATEGY=5,
            PORTFOLIO_MAX_THEME_EXPOSURE=3,
        ):
            meta = pd.Series({"rank": 2, "theme": ""})
            blocked = _portfolio_blocks_new_entry(
                meta, pd.DataFrame(), pd.DataFrame(),
                REGIME_NEUTRAL, 1, 1,
            )
        assert blocked is False


# ──────────────────────────────────────────────────────────────────────────────
# ensure_decision_strategy_columns — horizon assignment
# ──────────────────────────────────────────────────────────────────────────────

class TestEnsureDecisionStrategyColumns:

    def _df(self, tickers: list[str]) -> pd.DataFrame:
        return pd.DataFrame([
            {"ticker": t, "horizon_tag": "", "strategy_profile": "", "signal_type": "", "regime_tag": ""}
            for t in tickers
        ])

    def test_core_ticker_gets_swing_core(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU,VRT,LITE"):
            result = ensure_decision_strategy_columns(self._df(["MU", "VRT"]))
        assert all(result["horizon_tag"] == HORIZON_SWING_CORE)

    def test_non_core_ticker_gets_intraday_monster(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU,VRT,LITE"):
            result = ensure_decision_strategy_columns(self._df(["NVDA", "TSLA"]))
        assert all(result["horizon_tag"] == HORIZON_INTRADAY_MONSTER)

    def test_mixed_tickers_split_correctly(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU,VRT"):
            result = ensure_decision_strategy_columns(self._df(["MU", "NVDA", "VRT", "TSLA"]))
        horizon = result.set_index("ticker")["horizon_tag"].to_dict()
        assert horizon["MU"] == HORIZON_SWING_CORE
        assert horizon["VRT"] == HORIZON_SWING_CORE
        assert horizon["NVDA"] == HORIZON_INTRADAY_MONSTER
        assert horizon["TSLA"] == HORIZON_INTRADAY_MONSTER

    def test_existing_valid_horizon_is_preserved(self):
        df = pd.DataFrame([
            {"ticker": "NVDA", "horizon_tag": HORIZON_SWING_CORE, "strategy_profile": "", "signal_type": "", "regime_tag": ""}
        ])
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU"):
            result = ensure_decision_strategy_columns(df)
        # Explicit horizon_tag should be preserved (NVDA is not core but has explicit swing tag)
        assert result.iloc[0]["horizon_tag"] == HORIZON_SWING_CORE

    def test_empty_signal_type_filled_with_watch(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", ""):
            result = ensure_decision_strategy_columns(self._df(["NVDA"]))
        assert result.iloc[0]["signal_type"] == "watch"

    def test_regime_tag_defaults_to_neutral(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", ""):
            result = ensure_decision_strategy_columns(self._df(["NVDA"]))
        assert result.iloc[0]["regime_tag"] == "neutral"


# ──────────────────────────────────────────────────────────────────────────────
# classify_watch_horizon
# ──────────────────────────────────────────────────────────────────────────────

class TestClassifyWatchHorizon:

    def test_core_ticker_returns_swing_core(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU,VRT"):
            assert classify_watch_horizon("MU") == HORIZON_SWING_CORE
            assert classify_watch_horizon("VRT") == HORIZON_SWING_CORE

    def test_explosive_move_returns_intraday_monster(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU"):
            result = classify_watch_horizon(
                "NVDA",
                decision={"daily_change_pct": 8.5, "catalyst_type": ""},
            )
        assert result == HORIZON_INTRADAY_MONSTER

    def test_earnings_catalyst_returns_intraday_monster(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU"):
            result = classify_watch_horizon(
                "XYZ",
                decision={"catalyst_type": "earnings_beat", "daily_change_pct": 1.0},
            )
        assert result == HORIZON_INTRADAY_MONSTER

    def test_saved_watchlist_only_returns_swing(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU"):
            result = classify_watch_horizon(
                "AAPL",
                source_flags={"saved_watchlist": True, "ai_decision": False},
            )
        assert result == HORIZON_SWING_CORE

    def test_ordinary_ticker_defaults_to_swing_core(self):
        with patch("ai_trading.strategy_context.CORE_LIST_TICKERS", "MU"):
            result = classify_watch_horizon(
                "IBM",
                decision={"catalyst_type": "", "daily_change_pct": 0.5},
            )
        assert result == HORIZON_SWING_CORE
