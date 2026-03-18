from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ai_trading import intraday_execution_engine as engine


def _write_materialized_contract(path: Path) -> None:
    df = pd.DataFrame(
        [
            {
                "as_of_date": "2026-03-18",
                "ticker": "JTAI",
                "final_priority": 2,
                "decision_status": "keep",
                "decision_reason": "entry=pullback_watch",
                "primary_event_type": "neutral",
                "trigger_score": 88.0,
                "tomorrow_continuation_prob": 62.0,
                "risk_level": "low",
                "preferred_entry_type": "pullback_entry",
                "execution_window": "next_open_session",
                "invalidation_rule": "跌破 VWAP 則全賣退場",
                "execution_action": "BUY_SCALE_IN",
                "position_plan": "回踩分批買入",
                "exit_action": "SELL_ALL_EXIT",
                "user_visibility": "user_visible",
            }
        ]
    )
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _build_enriched_df(phase: str, market_open_utc: datetime) -> pd.DataFrame:
    rows = []
    start_ts = pd.Timestamp(market_open_utc) - pd.Timedelta(minutes=30)
    for i in range(12):
        rows.append(
            {
                "Datetime": start_ts + pd.Timedelta(minutes=i),
                "Open": 95.0,
                "Close": 95.0 + min(i, 7) * 1.0,
                "Volume": 900.0,
                "dynamic_avwap": 100.0,
                "sqzmom_hist": 0.02,
                "sqzmom_color": "green",
                "sqz_release": i == 3,
                "sqz_on": True,
            }
        )

    latest_ts = pd.Timestamp(market_open_utc) + pd.Timedelta(minutes=10)
    prev_ts = latest_ts - pd.Timedelta(minutes=1)

    if phase == "wait":
        prev = {
            "Datetime": prev_ts,
            "Open": 95.0,
            "Close": 100.8,
            "Volume": 1000.0,
            "dynamic_avwap": 100.0,
            "sqzmom_hist": 0.02,
            "sqzmom_color": "green",
            "sqz_release": False,
            "sqz_on": True,
        }
        latest = {
            "Datetime": latest_ts,
            "Open": 95.0,
            "Close": 101.8,
            "Volume": 1400.0,
            "dynamic_avwap": 100.0,
            "sqzmom_hist": -0.05,
            "sqzmom_color": "red",
            "sqz_release": False,
            "sqz_on": True,
        }
    elif phase == "hit":
        prev = {
            "Datetime": prev_ts,
            "Open": 95.0,
            "Close": 100.2,
            "Volume": 1000.0,
            "dynamic_avwap": 100.0,
            "sqzmom_hist": 0.03,
            "sqzmom_color": "green",
            "sqz_release": False,
            "sqz_on": True,
        }
        latest = {
            "Datetime": latest_ts,
            "Open": 95.0,
            "Close": 99.8,
            "Volume": 800.0,
            "dynamic_avwap": 100.0,
            "sqzmom_hist": 0.06,
            "sqzmom_color": "lime",
            "sqz_release": False,
            "sqz_on": True,
        }
    else:  # fail
        prev = {
            "Datetime": prev_ts,
            "Open": 95.0,
            "Close": 95.0,
            "Volume": 1200.0,
            "dynamic_avwap": 99.5,
            "sqzmom_hist": -0.20,
            "sqzmom_color": "red",
            "sqz_release": False,
            "sqz_on": True,
        }
        latest = {
            "Datetime": latest_ts,
            "Open": 95.0,
            "Close": 94.0,
            "Volume": 1600.0,
            "dynamic_avwap": 99.5,
            "sqzmom_hist": -0.40,
            "sqzmom_color": "red",
            "sqz_release": False,
            "sqz_on": True,
        }

    rows[-2] = prev
    rows[-1] = latest
    return pd.DataFrame(rows)


def _empty_positions_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ticker",
            "horizon_tag",
            "strategy_profile",
            "quantity",
            "avg_cost",
            "opened_at",
            "add_count",
            "theme",
        ]
    )


def _empty_trade_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ticker",
            "side",
            "position_effect",
            "strategy_profile",
            "horizon_tag",
            "regime_tag",
            "realized_pnl_delta",
            "recorded_at",
            "recorded_at_ts",
        ]
    )


def test_materialized_contract_is_intraday_seed_and_rank2_allowed(tmp_path, monkeypatch):
    contract = tmp_path / "ai_decision_contract_v2_materialized.csv"
    _write_materialized_contract(contract)

    monkeypatch.setattr(engine, "AI_TRADING_LATEST_DIR", tmp_path)
    monkeypatch.setattr(engine, "AI_READY_LATEST_DIR", tmp_path / "ai_ready")
    monkeypatch.setattr(engine, "DAILY_REFRESH_LATEST_DIR", tmp_path / "daily_refresh")

    load_decision_df = getattr(engine, "_load_decision_df")
    df = load_decision_df()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["ticker"] == "JTAI"
    assert int(row["rank"]) == 2
    assert str(row["execution_action"]).upper() == "BUY_SCALE_IN"
    assert str(row["user_visibility"]).lower() == "user_visible"
    assert str(row["preferred_entry_type"]).lower() == "pullback_entry"
    assert engine.decision_allows_entry(row) is True


def test_pullback_wait_buy_exit_notifications_with_dedupe(tmp_path, monkeypatch):
    contract = tmp_path / "ai_decision_contract_v2_materialized.csv"
    _write_materialized_contract(contract)

    monkeypatch.setattr(engine, "AI_TRADING_LATEST_DIR", tmp_path)
    monkeypatch.setattr(engine, "AI_READY_LATEST_DIR", tmp_path / "ai_ready")
    monkeypatch.setattr(engine, "DAILY_REFRESH_LATEST_DIR", tmp_path / "daily_refresh")

    snapshot_file = tmp_path / "intraday_signal_latest.csv"
    monkeypatch.setattr(engine, "SNAPSHOT_FILE", snapshot_file)
    monkeypatch.setattr(engine, "INTRADAY_DIR", tmp_path)
    monkeypatch.setattr(engine, "ALERT_DIR", tmp_path)
    monkeypatch.setattr(engine, "BACKTEST_DIR", tmp_path)

    market_open_utc = datetime(2026, 3, 18, 13, 30, tzinfo=timezone.utc)
    phase = {"value": "wait"}

    class _DummyTradeMemory:
        def find_similar_trades(self, **_: object) -> list[dict]:
            return []

        def sync_completed_trades(self, _: pd.DataFrame) -> None:
            return None

    def _fake_trade_memory() -> _DummyTradeMemory:
        return _DummyTradeMemory()

    monkeypatch.setattr(engine, "get_trade_memory", _fake_trade_memory)
    monkeypatch.setattr(
        engine,
        "get_intraday_active_window",
        lambda _now: {
            "market_open_utc": market_open_utc,
            "active_end_utc": market_open_utc + pd.Timedelta(hours=3),
        },
    )
    monkeypatch.setattr(engine, "detect_regime_tag", lambda: "neutral")
    monkeypatch.setattr(engine, "sync_runtime_df", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_append_action_log", lambda rows: None)
    monkeypatch.setattr(engine, "_write_execution_outputs", lambda rows: None)
    monkeypatch.setattr(engine, "_load_recent_trade_df", lambda limit=240: _empty_trade_df())

    def _fake_positions() -> pd.DataFrame:
        if phase["value"] == "fail":
            return pd.DataFrame(
                [
                    {
                        "ticker": "JTAI",
                        "horizon_tag": engine.HORIZON_INTRADAY_MONSTER,
                        "strategy_profile": engine.STRATEGY_MONSTER_SWING,
                        "quantity": 100.0,
                        "avg_cost": 100.0,
                        "opened_at": "2026-03-18 13:40:00+00:00",
                        "add_count": 0,
                        "theme": "",
                    }
                ]
            )
        return _empty_positions_df()

    monkeypatch.setattr(engine, "load_positions", _fake_positions)

    def _fake_get_position_by_profile(positions_df: pd.DataFrame, ticker: str, **_: object):
        if len(positions_df) == 0:
            return None
        scoped = positions_df[positions_df["ticker"].astype(str).str.upper() == str(ticker).strip().upper()]
        if len(scoped) == 0:
            return None
        return scoped.iloc[0]

    monkeypatch.setattr(engine, "get_position_by_profile", _fake_get_position_by_profile)

    dummy_bars = pd.DataFrame({"Open": [1.0] * 60, "Close": [1.0] * 60})
    monkeypatch.setattr(engine, "_fetch_intraday_bars", lambda *args, **kwargs: dummy_bars)
    monkeypatch.setattr(engine, "add_intraday_indicators", lambda bars: _build_enriched_df(phase["value"], market_open_utc))

    state_store: dict[str, str] = {}

    def _load_state() -> dict[str, str]:
        return dict(state_store)

    def _save_state(state: dict[str, str]) -> None:
        state_store.clear()
        state_store.update(state)

    monkeypatch.setattr(engine, "_load_state", _load_state)
    monkeypatch.setattr(engine, "_save_state", _save_state)

    sent_messages: list[str] = []

    def _fake_send_discord(message: str):
        sent_messages.append(message)
        return True, "ok"

    monkeypatch.setattr(engine, "_send_discord", _fake_send_discord)

    phase["value"] = "wait"
    out_wait = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
    assert out_wait["wait_count"] == 1
    assert out_wait["action_count"] == 0
    assert len(sent_messages) == 1
    assert "待命觀察" in sent_messages[-1]
    assert "暫不買入" in sent_messages[-1]
    assert "現在買入" not in sent_messages[-1]

    phase["value"] = "hit"
    out_hit = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
    assert out_hit["action_count"] == 1
    assert len(sent_messages) == 2
    assert "現在買入" in sent_messages[-1]
    assert "建議比例=30%" in sent_messages[-1]

    out_hit_dedupe = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
    assert out_hit_dedupe["action_count"] == 0
    assert len(sent_messages) == 2

    phase["value"] = "fail"
    out_fail = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
    assert out_fail["action_count"] == 1
    assert len(sent_messages) == 3
    assert "全賣退場" in sent_messages[-1]

    snapshot_df = pd.read_csv(snapshot_file, encoding="utf-8-sig")
    assert "source_contract" in snapshot_df.columns
    assert "preferred_entry_type" in snapshot_df.columns
    jtai_row = snapshot_df[snapshot_df["ticker"].astype(str).str.upper() == "JTAI"].iloc[0]
    assert str(jtai_row["preferred_entry_type"]).lower() == "pullback_entry"
    assert str(jtai_row["source_contract"]).endswith("ai_decision_contract_v2_materialized.csv")
