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


def test_bedtime_strategy_lines_include_swing_recommendation_line():
    build_bedtime_strategy_lines = getattr(recap, "_build_bedtime_strategy_lines")
    df = pd.DataFrame([
        {"ticker": "AAPL", "rank": 1, "decision_tag": "keep", "risk_level": "中", "confidence": 55, "api_final_score": 82}
    ])
    context = {
        "positions_df": pd.DataFrame(),
        "swing_strategy_recommendation": {
            "signal": "buy",
            "confidence": 0.62,
            "symbols": ["AAPL", "MSFT"],
        },
    }
    lines = build_bedtime_strategy_lines(df, context)
    assert any("Swing Core" in line for line in lines)


def test_morning_message_contains_required_sections_and_prior_link():
    build_morning_message = getattr(recap, "_build_morning_message")
    df = pd.DataFrame([{"ticker": "ZVRA", "rank": 1, "decision_tag": "keep", "risk_level": "中"}])
    context = {
        "prior_bedtime_lines": ["ZVRA: 昨晚待驗證"],
        "ai_summary": {
            "summary": "今天先等開盤確認。",
            "focus": ["ZVRA: 隔夜維持強勢"],
            "risk_flags": ["ZVRA: 跌破 AVWAP 先不追"],
            "opening_plan": ["ZVRA: 開盤守住 AVWAP 才考慮進"],
        },
        "execution_summaries_full": [],
        "positions_df": pd.DataFrame(),
    }

    message = build_morning_message(df, {}, "2026-03-17", context)
    assert "承接昨晚計畫:" in message
    assert "現在應該做什麼:" in message
    assert "失效條件:" in message
    assert "結論:" in message


def test_opening_message_contains_required_sections_and_prior_link():
    build_opening_message = getattr(recap, "_build_opening_message")
    df = pd.DataFrame([{"ticker": "ZVRA", "rank": 1, "decision_tag": "keep", "risk_level": "中"}])
    context = {
        "reference_plan_lines": ["ZVRA: 早晨計畫待驗證"],
        "validation_rows": [{"ticker": "ZVRA", "validation_label": "確認續強", "next_step": "守住 AVWAP 才能續抱"}],
        "ai_summary": {
            "summary": "先驗證，不追第一根。",
            "focus": ["ZVRA: 開盤續強"],
            "risk_flags": ["ZVRA: 量縮失敗就退"],
            "opening_plan": ["ZVRA: 只在拉回守住 AVWAP 時進"],
        },
    }

    message = build_opening_message(df, {}, "2026-03-17", context)
    assert "承接早晨計畫:" in message
    assert "開盤驗證結果:" in message
    assert "現在應該做什麼:" in message
    assert "失效條件:" in message
    assert "結論:" in message


def test_bedtime_message_carries_prior_morning_plan():
    build_bedtime_message = getattr(recap, "_build_bedtime_message")
    df = pd.DataFrame([{"ticker": "ZVRA", "rank": 1, "decision_tag": "keep", "risk_level": "中"}])
    context = {
        "prior_morning_lines": ["ZVRA: 早晨計畫"],
        "ai_summary": {
            "summary": "今晚不追價，等明早。",
            "focus": ["ZVRA: 今天維持強勢"],
            "risk_flags": ["ZVRA: 若轉弱先降風險"],
            "opening_plan": ["ZVRA: 明早只在守住 AVWAP 時續抱"],
        },
        "execution_summaries_full": [],
        "positions_df": pd.DataFrame(),
    }

    message = build_bedtime_message(df, {}, "2026-03-17", context)
    assert "承接早晨計畫:" in message
    assert "現在應該做什麼:" in message
    assert "失效條件:" in message


def test_recap_mapped_ticker_focus_and_risk_lines():
    execution_summaries = [
        {
            "ticker": "MULL",
            "underlying_ticker": "MU",
            "has_position": True,
            "latest_action": "add",
            "close": 120.0,
            "vwap": 118.0,
            "sqzmom_hist": 0.35,
            "sqzmom_color": "green",
            "status_label": "續強加碼",
            "guidance": "先觀察",
        }
    ]

    focus_lines = getattr(recap, "_execution_focus_lines")(execution_summaries, limit=3)
    risk_lines = getattr(recap, "_execution_risk_lines")(execution_summaries, limit=3)

    assert any("MULL（標的：MU）" in line and "VWAP 守住" in line for line in focus_lines)
    assert any("MU 跌破 VWAP 則 MULL 出場" in line for line in risk_lines)
