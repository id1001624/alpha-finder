import pandas as pd

from ai_trading import intraday_execution_engine as engine
from scripts import push_alerts_from_ai_decision as recap


def test_decision_allows_entry_requires_keep_rank_and_quality():
    decision_allows_entry = getattr(engine, "decision_allows_entry")
    meta = pd.Series(
        {
            "ticker": "ZVRA",
            "rank": 1,
            "decision_tag": "keep",
            "risk_level": "中",
            "confidence": 40,
            "api_final_score": 90,
        }
    )
    assert decision_allows_entry(meta) is True

    weak = meta.copy()
    weak["rank"] = 3
    assert decision_allows_entry(weak) is False


def test_noise_exit_grace_blocks_immediate_reduce():
    is_in_noise_exit_grace = getattr(engine, "is_in_noise_exit_grace")
    signal_ts = pd.Timestamp("2026-03-11T14:40:00Z")
    fill_ts = pd.Timestamp("2026-03-11T14:25:00Z")
    assert is_in_noise_exit_grace(signal_ts, fill_ts) is True


def test_morning_strategy_lines_promote_single_top_candidate():
    build_morning_strategy_lines = getattr(recap, "build_morning_strategy_lines")
    df = pd.DataFrame(
        [
            {"ticker": "ZVRA", "rank": 1, "decision_tag": "keep", "risk_level": "中", "confidence": 40, "api_final_score": 90},
            {"ticker": "VYGR", "rank": 2, "decision_tag": "keep", "risk_level": "中", "confidence": 40, "api_final_score": 92},
        ]
    )
    lines = build_morning_strategy_lines(df, {"positions_df": pd.DataFrame(), "execution_summaries_full": []})
    assert any("今日新倉只看 ZVRA" in line for line in lines)


def test_bedtime_unique_candidate_line_uses_single_top_name():
    bedtime_line = getattr(recap, "_bedtime_unique_candidate_plan_line")
    df = pd.DataFrame(
        [
            {"ticker": "ZVRA", "rank": 1, "decision_tag": "keep", "risk_level": "中", "confidence": 40, "api_final_score": 90},
            {"ticker": "VYGR", "rank": 2, "decision_tag": "keep", "risk_level": "中", "confidence": 40, "api_final_score": 92},
        ]
    )
    line = bedtime_line(df, pd.DataFrame())
    assert "明天唯一候選新倉是 ZVRA" in line