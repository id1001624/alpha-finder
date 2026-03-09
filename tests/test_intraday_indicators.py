import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.intraday_indicators import add_intraday_indicators


def test_add_intraday_indicators_produces_expected_columns():
    close = np.linspace(100, 120, 80)
    df = pd.DataFrame(
        {
            "High": close + 1.5,
            "Low": close - 1.5,
            "Close": close,
            "Volume": np.linspace(100000, 180000, 80),
        }
    )

    enriched = add_intraday_indicators(df)

    for col in ["sqz_on", "sqz_release", "sqzmom_hist", "sqzmom_color", "dynamic_avwap", "long_trigger"]:
        assert col in enriched.columns
    assert enriched["dynamic_avwap"].notna().sum() > 0
    assert enriched["sqzmom_hist"].notna().sum() > 0


def test_intraday_provider_helpers_support_finnhub_payload(monkeypatch):
    from ai_trading import intraday_execution_engine as engine

    class DummyResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    monkeypatch.setattr(engine, "FINNHUB_API_KEY", "demo")

    def fake_get(url, params, timeout):
        assert url.endswith("/stock/candle")
        assert params["resolution"] == "5"
        return DummyResponse(
            {
                "s": "ok",
                "t": [1700000000, 1700000300],
                "o": [100.0, 101.0],
                "h": [101.0, 102.0],
                "l": [99.5, 100.5],
                "c": [100.5, 101.5],
                "v": [120000, 125000],
            }
        )

    monkeypatch.setattr(engine.requests, "get", fake_get)

    assert engine._finnhub_resolution_for_interval("5m") == "5"
    assert engine._parse_period_to_seconds("5d") == 5 * 24 * 3600

    bars = engine._fetch_intraday_bars_from_finnhub("AAPL", "5d", "5m")

    assert len(bars) == 2
    assert list(bars.columns) == ["Datetime", "Open", "High", "Low", "Close", "Volume"]