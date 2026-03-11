from datetime import date

import pandas as pd

from scripts import analyze_stop_loss_rebounds as report


def test_extract_stop_loss_events_collapses_same_ticker_same_day():
    execution_df = pd.DataFrame(
        [
            {
                "ticker": "AAOI",
                "action": "stop_loss",
                "execution_ts": pd.Timestamp("2026-03-10T14:30:00Z"),
                "execution_day": date(2026, 3, 10),
                "close": 18.0,
                "reason_summary": "first",
                "rank": 1,
                "decision_tag": "keep",
            },
            {
                "ticker": "AAOI",
                "action": "stop_loss",
                "execution_ts": pd.Timestamp("2026-03-10T15:10:00Z"),
                "execution_day": date(2026, 3, 10),
                "close": 17.5,
                "reason_summary": "second",
                "rank": 1,
                "decision_tag": "keep",
            },
            {
                "ticker": "NVDA",
                "action": "stop_loss",
                "execution_ts": pd.Timestamp("2026-03-09T15:10:00Z"),
                "execution_day": date(2026, 3, 9),
                "close": 100.0,
                "reason_summary": "nvda",
                "rank": 2,
                "decision_tag": "watch",
            },
        ]
    )

    stop_df = report.extract_stop_loss_events(execution_df, lookback_days=10, asof=date(2026, 3, 11))

    assert len(stop_df) == 2
    aaoi_row = stop_df[stop_df["ticker"] == "AAOI"].iloc[0]
    assert pd.Timestamp(aaoi_row["stop_ts"]) == pd.Timestamp("2026-03-10T14:30:00Z")
    assert aaoi_row["stop_close"] == 18.0


def test_evaluate_stop_loss_rebounds_marks_missed_rebound_without_reentry():
    stop_df = pd.DataFrame(
        [
            {
                "ticker": "AAOI",
                "stop_ts": pd.Timestamp("2026-03-10T14:30:00Z"),
                "stop_date": date(2026, 3, 10),
                "stop_close": 18.0,
                "stop_rank": 1,
                "stop_decision_tag": "keep",
                "stop_reason": "stop",
            },
            {
                "ticker": "NVDA",
                "stop_ts": pd.Timestamp("2026-03-10T14:30:00Z"),
                "stop_date": date(2026, 3, 10),
                "stop_close": 100.0,
                "stop_rank": 2,
                "stop_decision_tag": "watch",
                "stop_reason": "stop",
            },
        ]
    )
    execution_df = pd.DataFrame(
        [
            {
                "ticker": "NVDA",
                "action": "entry",
                "execution_ts": pd.Timestamp("2026-03-11T15:00:00Z"),
                "execution_day": date(2026, 3, 11),
                "close": 106.0,
            }
        ]
    )
    trade_df = pd.DataFrame(
        [
            {
                "ticker": "AAOI",
                "side": "buy",
                "recorded_ts": pd.Timestamp("2026-03-12T15:00:00Z"),
                "price": 19.2,
            }
        ]
    )
    price_cache = {
        "AAOI": pd.DataFrame(
            [
                {"trade_date": date(2026, 3, 11), "Close": 19.0, "High": 19.8},
                {"trade_date": date(2026, 3, 12), "Close": 20.0, "High": 20.5},
            ]
        ),
        "NVDA": pd.DataFrame(
            [
                {"trade_date": date(2026, 3, 11), "Close": 104.0, "High": 106.0},
                {"trade_date": date(2026, 3, 12), "Close": 107.0, "High": 108.0},
            ]
        ),
    }

    out = report.evaluate_stop_loss_rebounds(
        stop_df,
        execution_df,
        trade_df,
        price_cache,
        forward_days=5,
        rebound_threshold_pct=8.0,
    )

    aaoi = out[out["ticker"] == "AAOI"].iloc[0]
    nvda = out[out["ticker"] == "NVDA"].iloc[0]

    assert bool(aaoi["rebound_hit"]) is True
    assert bool(aaoi["missed_rebound"]) is True
    assert bool(aaoi["manual_buy_hit"]) is True
    assert bool(nvda["reentry_signal_hit"]) is True
    assert bool(nvda["missed_rebound"]) is False


def test_build_missed_rebound_view_returns_daily_focus_columns():
    report_df = pd.DataFrame(
        [
            {
                "ticker": "CRDU",
                "stop_date": "2026-03-10",
                "peak_high_date": "2026-03-11",
                "stop_close": 23.47,
                "max_high_pct": 11.63,
                "reentry_signal_hit": False,
                "manual_buy_hit": False,
                "missed_rebound": True,
            },
            {
                "ticker": "MULL",
                "stop_date": "2026-03-10",
                "peak_high_date": "2026-03-11",
                "stop_close": 164.05,
                "max_high_pct": 5.77,
                "reentry_signal_hit": True,
                "manual_buy_hit": False,
                "missed_rebound": False,
            },
        ]
    )

    missed_view = report.build_missed_rebound_view(report_df)

    assert list(missed_view["ticker"]) == ["CRDU"]
    assert missed_view.iloc[0]["days_to_peak"] == 1
    assert "沒有 engine 再進場" == missed_view.iloc[0]["reentry_status"]