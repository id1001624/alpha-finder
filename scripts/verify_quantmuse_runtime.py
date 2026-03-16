from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.quantmuse_bridge import (  # noqa: E402
    analyze_overnight_sentiment,
    generate_bedtime_swing_recommendation,
    get_quantmuse_capabilities,
)


def main() -> int:
    capabilities = get_quantmuse_capabilities()

    snippets = [
        {
            "title": "Example earnings beat",
            "content": "Company beat revenue and raised guidance after hours.",
            "url": "https://example.com/news",
        }
    ]
    sentiment = analyze_overnight_sentiment("AAPL", snippets)

    decision_df = pd.DataFrame(
        [
            {"ticker": "AAPL", "rank": 1, "horizon_tag": "swing_core", "strategy_profile": "swing_trend", "decision_tag": "keep"},
            {"ticker": "MSFT", "rank": 2, "horizon_tag": "swing_core", "strategy_profile": "swing_trend", "decision_tag": "watch"},
        ]
    )
    positions_df = pd.DataFrame(columns=["ticker", "quantity", "avg_cost"])
    recommendation = generate_bedtime_swing_recommendation(
        decision_df=decision_df,
        positions_df=positions_df,
        execution_summaries=[],
        max_symbols=3,
    )

    report = {
        "quantmuse_capabilities": capabilities,
        "sentiment_probe": {
            "source": sentiment.get("sentiment_source", ""),
            "label": sentiment.get("sentiment_label", ""),
            "score": sentiment.get("sentiment_score", 0.0),
            "confidence": sentiment.get("sentiment_confidence", 0.0),
        },
        "swing_probe": {
            "source": recommendation.get("source", ""),
            "signal": recommendation.get("signal", ""),
            "confidence": recommendation.get("confidence", 0.0),
            "symbols": recommendation.get("symbols", []),
        },
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))

    # Non-zero exit means native path is not really available.
    # This keeps CI/manual checks explicit instead of silently passing with fallback.
    if not bool(capabilities.get("available")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
