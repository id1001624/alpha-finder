from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading import intraday_execution_engine as engine
from config import DISCORD_WEBHOOK_URL


def _sanitize_webhook_url(value: str) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _build_enriched_df(phase_name: str, market_open_utc: datetime) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
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

    if phase_name == "wait":
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
    elif phase_name == "buy":
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
    else:
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


def _post_with_wait(webhook_url: str, content: str) -> tuple[bool, dict[str, Any], str]:
    wait_url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
    response = requests.post(
        wait_url,
        json={"content": content[:1900]},
        headers={"Content-Type": "application/json", "User-Agent": "AlphaFinder/1.0"},
        timeout=20,
    )
    if not response.ok:
        return False, {}, f"HTTP {response.status_code}: {response.text[:200]}"
    payload = response.json() if response.headers.get("Content-Type", "").startswith("application/json") else {}
    return True, payload if isinstance(payload, dict) else {}, "ok"


def _message_link(guild_id: str, channel_id: str, message_id: str) -> str:
    if not guild_id or not channel_id or not message_id:
        return ""
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run production acceptance for intraday wait/buy/exit Discord notifications")
    parser.add_argument("--output", type=str, default="repo_outputs/backtest/alerts/intraday_prod_acceptance_latest.json")
    parser.add_argument("--contract", type=str, default="repo_outputs/ai_trading/latest/ai_decision_contract_v2_materialized.csv")
    args = parser.parse_args()

    contract_path = Path(args.contract)
    if not contract_path.exists():
        print(json.dumps({"ok": False, "reason": "missing_contract", "path": str(contract_path)}, ensure_ascii=False))
        return 1

    webhook_url = _sanitize_webhook_url(DISCORD_WEBHOOK_URL)
    if not webhook_url:
        print(json.dumps({"ok": False, "reason": "missing_discord_webhook_url"}, ensure_ascii=False))
        return 1

    raw = pd.read_csv(contract_path, encoding="utf-8-sig")
    jtai_raw = raw[raw["ticker"].astype(str).str.upper() == "JTAI"].head(1)
    if len(jtai_raw) == 0:
        print(json.dumps({"ok": False, "reason": "missing_jtai_contract_row"}, ensure_ascii=False))
        return 1

    engine.AI_TRADING_LATEST_DIR = contract_path.parent
    engine.AI_READY_LATEST_DIR = Path("repo_outputs/ai_ready/latest")
    engine.DAILY_REFRESH_LATEST_DIR = Path("repo_outputs/daily_refresh/latest")

    load_decision_df = getattr(engine, "_load_decision_df")
    intraday_seed = load_decision_df()
    seed_jtai = intraday_seed[intraday_seed["ticker"].astype(str).str.upper() == "JTAI"].head(1)
    if len(seed_jtai) == 0:
        print(json.dumps({"ok": False, "reason": "jtai_not_in_intraday_seed"}, ensure_ascii=False))
        return 1

    webhook_meta: dict[str, Any] = {}
    try:
        meta_resp = requests.get(webhook_url, timeout=15)
        if meta_resp.ok:
            payload = meta_resp.json() if meta_resp.headers.get("Content-Type", "").startswith("application/json") else {}
            if isinstance(payload, dict):
                webhook_meta = payload
    except requests.RequestException:
        webhook_meta = {}

    market_open_utc = datetime.now(timezone.utc).replace(hour=13, minute=30, second=0, microsecond=0)
    phase = {"value": "wait"}

    class DummyTradeMemory:
        def find_similar_trades(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        def sync_completed_trades(self, _df: pd.DataFrame) -> None:
            return None

    def load_positions() -> pd.DataFrame:
        if phase["value"] == "exit":
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
        return pd.DataFrame(columns=["ticker", "horizon_tag", "strategy_profile", "quantity", "avg_cost", "opened_at", "add_count", "theme"])

    def get_position_by_profile(positions_df: pd.DataFrame, ticker: str, **_kwargs: Any):
        if len(positions_df) == 0:
            return None
        scoped = positions_df[positions_df["ticker"].astype(str).str.upper() == str(ticker).strip().upper()]
        if len(scoped) == 0:
            return None
        return scoped.iloc[0]

    original_funcs: dict[str, Any] = {
        "get_trade_memory": engine.get_trade_memory,
        "get_intraday_active_window": engine.get_intraday_active_window,
        "detect_regime_tag": engine.detect_regime_tag,
        "sync_runtime_df": engine.sync_runtime_df,
        "_append_action_log": getattr(engine, "_append_action_log"),
        "_write_execution_outputs": getattr(engine, "_write_execution_outputs"),
        "_load_recent_trade_df": getattr(engine, "_load_recent_trade_df"),
        "load_positions": engine.load_positions,
        "get_position_by_profile": engine.get_position_by_profile,
        "_fetch_intraday_bars": getattr(engine, "_fetch_intraday_bars"),
        "add_intraday_indicators": engine.add_intraday_indicators,
        "_load_state": getattr(engine, "_load_state"),
        "_save_state": getattr(engine, "_save_state"),
        "_send_discord": getattr(engine, "_send_discord"),
    }

    state_store: dict[str, str] = {}
    sent_records: list[dict[str, Any]] = []

    def get_trade_memory() -> DummyTradeMemory:
        return DummyTradeMemory()

    def get_intraday_active_window(_now: Any = None) -> dict[str, Any]:
        return {
            "market_open_utc": market_open_utc,
            "active_end_utc": market_open_utc + pd.Timedelta(hours=3),
        }

    def detect_regime_tag() -> str:
        return "neutral"

    def sync_runtime_df(*_args: Any, **_kwargs: Any) -> None:
        return None

    def append_action_log(_rows: Any) -> None:
        return None

    def write_execution_outputs(_rows: Any) -> None:
        return None

    def load_recent_trade_df(limit: int = 240) -> pd.DataFrame:
        _ = limit
        return pd.DataFrame(columns=["ticker", "side", "position_effect", "strategy_profile", "horizon_tag", "regime_tag", "realized_pnl_delta", "recorded_at", "recorded_at_ts"])

    def fetch_intraday_bars(symbol: str, *_args: Any, **_kwargs: Any) -> pd.DataFrame:
        if str(symbol).upper() == "JTAI":
            return pd.DataFrame({"Open": [1.0] * 60, "Close": [1.0] * 60})
        return pd.DataFrame({"Open": [1.0] * 10, "Close": [1.0] * 10})

    def add_intraday_indicators(_bars: pd.DataFrame) -> pd.DataFrame:
        return _build_enriched_df(phase["value"], market_open_utc)

    def load_state() -> dict[str, str]:
        return dict(state_store)

    def save_state(state: dict[str, str]) -> None:
        state_store.clear()
        state_store.update(state)

    def send_discord(message: str) -> tuple[bool, str]:
        ok, payload, detail = _post_with_wait(webhook_url, str(message or ""))
        if ok:
            sent_records.append(
                {
                    "message_id": str(payload.get("id", "")),
                    "channel_id": str(payload.get("channel_id", "")),
                    "timestamp": str(payload.get("timestamp", "")),
                    "content": str(payload.get("content", "")),
                }
            )
        return ok, detail

    try:
        engine.get_trade_memory = get_trade_memory
        engine.get_intraday_active_window = get_intraday_active_window
        engine.detect_regime_tag = detect_regime_tag
        engine.sync_runtime_df = sync_runtime_df
        setattr(engine, "_append_action_log", append_action_log)
        setattr(engine, "_write_execution_outputs", write_execution_outputs)
        setattr(engine, "_load_recent_trade_df", load_recent_trade_df)
        engine.load_positions = load_positions
        engine.get_position_by_profile = get_position_by_profile
        setattr(engine, "_fetch_intraday_bars", fetch_intraday_bars)
        engine.add_intraday_indicators = add_intraday_indicators
        setattr(engine, "_load_state", load_state)
        setattr(engine, "_save_state", save_state)
        setattr(engine, "_send_discord", send_discord)

        phase_runs: list[dict[str, Any]] = []

        phase["value"] = "wait"
        out_wait = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
        phase_runs.append({"phase": "wait", "wait_count": int(out_wait.get("wait_count", 0)), "action_count": int(out_wait.get("action_count", 0)), "sent_count": len(sent_records)})

        phase["value"] = "buy"
        out_buy = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
        phase_runs.append({"phase": "buy", "wait_count": int(out_buy.get("wait_count", 0)), "action_count": int(out_buy.get("action_count", 0)), "sent_count": len(sent_records)})

        before_repeat = len(sent_records)
        out_buy_repeat = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
        after_repeat = len(sent_records)
        phase_runs.append({"phase": "buy_repeat_same_minute", "wait_count": int(out_buy_repeat.get("wait_count", 0)), "action_count": int(out_buy_repeat.get("action_count", 0)), "sent_count_before": before_repeat, "sent_count_after": after_repeat})

        phase["value"] = "exit"
        out_exit = engine.run_intraday_execution_engine(top_n=5, dry_run=False)
        phase_runs.append({"phase": "exit", "wait_count": int(out_exit.get("wait_count", 0)), "action_count": int(out_exit.get("action_count", 0)), "sent_count": len(sent_records)})

    finally:
        for name, func in original_funcs.items():
            setattr(engine, name, func)

    wait_msg = next((x for x in sent_records if "Pullback 待命" in x.get("content", "")), {})
    buy_msg = next((x for x in sent_records if "即時進場指令" in x.get("content", "")), {})
    exit_msg = next((x for x in sent_records if "即時風控指令" in x.get("content", "")), {})

    guild_id = str(webhook_meta.get("guild_id", ""))
    channel_id = str(wait_msg.get("channel_id") or buy_msg.get("channel_id") or exit_msg.get("channel_id") or webhook_meta.get("channel_id", ""))

    result = {
        "ok": True,
        "non_dry_run": True,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contract_row": {
            "ticker": str(jtai_raw.iloc[0].get("ticker", "")),
            "decision_status": str(jtai_raw.iloc[0].get("decision_status", "")),
            "final_priority": int(pd.to_numeric(jtai_raw.iloc[0].get("final_priority"), errors="coerce") or 0),
            "preferred_entry_type": str(jtai_raw.iloc[0].get("preferred_entry_type", "")),
            "execution_window": str(jtai_raw.iloc[0].get("execution_window", "")),
            "execution_action": str(jtai_raw.iloc[0].get("execution_action", "")),
            "user_visibility": str(jtai_raw.iloc[0].get("user_visibility", "")),
            "source_contract": str(contract_path),
        },
        "intraday_seed_row": {
            "ticker": str(seed_jtai.iloc[0].get("ticker", "")),
            "rank": int(pd.to_numeric(seed_jtai.iloc[0].get("rank"), errors="coerce") or 0),
            "decision_tag": str(seed_jtai.iloc[0].get("decision_tag", "")),
            "execution_action": str(seed_jtai.iloc[0].get("execution_action", "")),
            "user_visibility": str(seed_jtai.iloc[0].get("user_visibility", "")),
            "preferred_entry_type": str(seed_jtai.iloc[0].get("preferred_entry_type", "")),
            "source_contract": str(seed_jtai.iloc[0].get("source_contract", "")),
        },
        "phase_runs": phase_runs,
        "wait_message": {
            **wait_msg,
            "jump_link": _message_link(guild_id, channel_id, str(wait_msg.get("message_id", ""))),
        },
        "buy_message": {
            **buy_msg,
            "jump_link": _message_link(guild_id, channel_id, str(buy_msg.get("message_id", ""))),
        },
        "exit_message": {
            **exit_msg,
            "jump_link": _message_link(guild_id, channel_id, str(exit_msg.get("message_id", ""))),
        },
        "dedupe_same_minute": {
            "before_repeat": phase_runs[2].get("sent_count_before", 0) if len(phase_runs) > 2 else 0,
            "after_repeat": phase_runs[2].get("sent_count_after", 0) if len(phase_runs) > 2 else 0,
            "no_duplicate_sent": bool(len(phase_runs) > 2 and phase_runs[2].get("sent_count_before") == phase_runs[2].get("sent_count_after")),
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
