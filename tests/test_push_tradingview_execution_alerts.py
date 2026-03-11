import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from signal_store import build_signal_event, init_signal_store, upsert_signal_event
from scripts import push_tradingview_execution_alerts as execution_alerts


def test_execution_alerts_write_trade_log_and_dedupe(tmp_path, monkeypatch):
    backtest_dir = tmp_path / "repo_outputs" / "backtest"
    alert_dir = backtest_dir / "alerts"
    daily_dir = backtest_dir / "daily_execution_trades"
    signal_db = tmp_path / "signals.db"

    backtest_dir.mkdir(parents=True, exist_ok=True)
    alert_dir.mkdir(parents=True, exist_ok=True)
    init_signal_store(str(signal_db))

    # Use dynamic timestamps so the signal is always "fresh" regardless of when CI runs
    now_utc = datetime.now(timezone.utc)
    signal_ts = (now_utc - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    received_at = (now_utc - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_str = now_utc.strftime("%Y-%m-%d")

    pd.DataFrame(
        [
            {
                "decision_date": today_str,
                "rank": 1,
                "ticker": "AAPL",
                "decision_tag": "keep",
                "risk_level": "medium",
                "tech_status": "trend_ok",
                "theme": "ai",
                "reason_summary": "Breakout with confirmation",
            }
        ]
    ).to_csv(backtest_dir / "ai_decision_latest.csv", index=False, encoding="utf-8-sig")

    event = build_signal_event(
        {
            "source": "tradingview",
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "timeframe": "5",
            "ts": signal_ts,
            "close": 188.25,
            "vwap": 187.9,
            "sqzmom_value": 0.32,
            "sqzmom_color": "green",
            "event": "entry",
        },
        signature="sig-1",
        received_at=received_at,
    )
    upsert_signal_event(str(signal_db), event)

    monkeypatch.setattr(execution_alerts, "BACKTEST_DIR", backtest_dir)
    monkeypatch.setattr(execution_alerts, "AI_DECISION_LATEST", backtest_dir / "ai_decision_latest.csv")
    monkeypatch.setattr(execution_alerts, "ALERT_DIR", alert_dir)
    monkeypatch.setattr(execution_alerts, "STATE_FILE", alert_dir / "tv_execution_state.json")
    monkeypatch.setattr(execution_alerts, "ALERT_LOG", alert_dir / "tv_execution_alert_log.csv")
    monkeypatch.setattr(execution_alerts, "LATEST_MSG", alert_dir / "latest_tv_execution_alert.txt")
    monkeypatch.setattr(execution_alerts, "EXECUTION_LOG", backtest_dir / "execution_trade_log.csv")
    monkeypatch.setattr(execution_alerts, "EXECUTION_LATEST", backtest_dir / "execution_trade_latest.csv")
    monkeypatch.setattr(execution_alerts, "EXECUTION_DAILY_DIR", daily_dir)
    monkeypatch.setattr(execution_alerts, "SIGNAL_STORE_PATH", str(signal_db))
    monkeypatch.setattr(execution_alerts, "DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(execution_alerts, "_http_post_json", lambda url, payload: (True, "ok"))
    monkeypatch.setattr(sys, "argv", ["push_tradingview_execution_alerts.py", "--top-n", "5"])

    assert execution_alerts.main() == 0

    execution_df = pd.read_csv(backtest_dir / "execution_trade_log.csv", encoding="utf-8-sig")
    assert len(execution_df) == 1
    assert execution_df.loc[0, "ticker"] == "AAPL"
    assert execution_df.loc[0, "action"] == "entry"
    assert execution_df.loc[0, "position_effect"] == "open"
    assert str(execution_df.loc[0, "timeframe"]) == "5"

    daily_csv = sorted(daily_dir.glob("*_execution_trade.csv"))
    assert len(daily_csv) == 1
    daily_df = pd.read_csv(daily_csv[0], encoding="utf-8-sig")
    assert len(daily_df) == 1

    monkeypatch.setattr(sys, "argv", ["push_tradingview_execution_alerts.py", "--top-n", "5"])
    assert execution_alerts.main() == 0

    execution_df_again = pd.read_csv(backtest_dir / "execution_trade_log.csv", encoding="utf-8-sig")
    assert len(execution_df_again) == 1