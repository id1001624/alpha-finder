import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.position_state import apply_trade_fill


def test_apply_trade_fill_buy_add_sell_flow():
    positions_df = None

    positions_df, buy_row = apply_trade_fill(positions_df, "AAPL", "buy", 100, 10.0, source="test")
    assert buy_row["position_effect"] == "open"
    assert float(positions_df.iloc[0]["quantity"]) == 100
    assert float(positions_df.iloc[0]["avg_cost"]) == 10.0

    positions_df, add_row = apply_trade_fill(positions_df, "AAPL", "add", 100, 14.0, source="test")
    assert add_row["position_effect"] == "increase"
    assert float(positions_df.iloc[0]["quantity"]) == 200
    assert round(float(positions_df.iloc[0]["avg_cost"]), 2) == 12.0

    positions_df, sell_row = apply_trade_fill(positions_df, "AAPL", "sell", 50, 16.0, source="test")
    assert sell_row["position_effect"] == "reduce"
    assert float(positions_df.iloc[0]["quantity"]) == 150
    assert round(float(sell_row["realized_pnl_delta"]), 2) == 200.0