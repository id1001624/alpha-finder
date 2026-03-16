import pandas as pd

from ai_trading import quantmuse_bridge as qm


def test_blend_overnight_impact_uses_sentiment_confidence_gate():
    assert qm.blend_overnight_impact("neutral", sentiment_score=0.9, sentiment_confidence=0.1) == "neutral"
    assert qm.blend_overnight_impact("neutral", sentiment_score=0.5, sentiment_confidence=0.8) == "soft_positive"
    assert qm.blend_overnight_impact("neutral", sentiment_score=-0.6, sentiment_confidence=0.8) == "soft_negative"


def test_generate_bedtime_swing_recommendation_rule_based_buy(monkeypatch):
    monkeypatch.setattr(
        qm,
        "_load_quantmuse_components",
        lambda: {
            "available": False,
            "sentiment_analyzer_cls": None,
            "nlp_processor_cls": None,
            "llm_integration_cls": None,
            "langchain_agent_cls": None,
            "reason": "test",
        },
    )

    decision_df = pd.DataFrame(
        [
            {"ticker": "AAPL", "rank": 1, "horizon_tag": "swing_core", "strategy_profile": "swing_trend", "decision_tag": "keep"},
            {"ticker": "MSFT", "rank": 2, "horizon_tag": "swing_core", "strategy_profile": "swing_trend", "decision_tag": "watch"},
        ]
    )
    reco = qm.generate_bedtime_swing_recommendation(
        decision_df=decision_df,
        positions_df=pd.DataFrame(),
        execution_summaries=[],
        max_symbols=2,
    )

    assert reco["source"] == "rule_based"
    assert reco["signal"] == "buy"
    assert reco["symbols"][:2] == ["AAPL", "MSFT"]


def test_generate_bedtime_swing_recommendation_rule_based_reduce_on_risk(monkeypatch):
    monkeypatch.setattr(
        qm,
        "_load_quantmuse_components",
        lambda: {
            "available": False,
            "sentiment_analyzer_cls": None,
            "nlp_processor_cls": None,
            "llm_integration_cls": None,
            "langchain_agent_cls": None,
            "reason": "test",
        },
    )

    decision_df = pd.DataFrame([{"ticker": "AAPL", "rank": 1, "decision_tag": "keep"}])
    execution_summaries = [
        {"latest_action": "stop_loss"},
        {"latest_action": "take_profit"},
    ]
    reco = qm.generate_bedtime_swing_recommendation(
        decision_df=decision_df,
        positions_df=pd.DataFrame([{"ticker": "AAPL", "quantity": 10}]),
        execution_summaries=execution_summaries,
        max_symbols=1,
    )

    assert reco["source"] == "rule_based"
    assert reco["signal"] == "reduce"
