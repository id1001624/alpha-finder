"""
Generate Discord execution alerts from TradingView webhook signals for the latest AI decisions.

Examples:
  python scripts/push_tradingview_execution_alerts.py --dry-run
  python scripts/push_tradingview_execution_alerts.py --top-n 5
  python scripts/push_tradingview_execution_alerts.py --force
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DISCORD_WEBHOOK_URL, SIGNAL_MAX_AGE_MINUTES, SIGNAL_REQUIRE_SAME_DAY, SIGNAL_STORE_PATH
from signal_store import get_latest_signals

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
AI_DECISION_LATEST = BACKTEST_DIR / "ai_decision_latest.csv"
ALERT_DIR = BACKTEST_DIR / "alerts"
STATE_FILE = ALERT_DIR / "tv_execution_state.json"
ALERT_LOG = ALERT_DIR / "tv_execution_alert_log.csv"
LATEST_MSG = ALERT_DIR / "latest_tv_execution_alert.txt"
EXECUTION_LOG = BACKTEST_DIR / "execution_trade_log.csv"
EXECUTION_LATEST = BACKTEST_DIR / "execution_trade_latest.csv"
EXECUTION_DAILY_DIR = BACKTEST_DIR / "daily_execution_trades"

EXECUTION_LOG_FIELDS = [
    "recorded_at",
    "execution_date",
    "execution_time",
    "decision_date",
    "ticker",
    "rank",
    "action",
    "position_effect",
    "decision_tag",
    "risk_level",
    "tech_status",
    "theme",
    "reason_summary",
    "signal_source",
    "exchange",
    "timeframe",
    "tv_event",
    "signal_ts",
    "close",
    "vwap",
    "sqzmom_color",
    "sqzmom_value",
    "signal_signature",
]


def _load_decision_df() -> pd.DataFrame:
    if not AI_DECISION_LATEST.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(AI_DECISION_LATEST, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(AI_DECISION_LATEST)

    if "ticker" not in df.columns:
        return pd.DataFrame()

    out = df.copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    if "rank" in out.columns:
        out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    else:
        out["rank"] = range(1, len(out) + 1)
    out = out[out["ticker"] != ""].copy()
    out = out.dropna(subset=["rank"]).copy()
    out["rank"] = out["rank"].astype(int)
    out = out.sort_values(["rank", "ticker"], ascending=[True, True])
    return out


def _load_signal_map() -> Dict[str, object]:
    try:
        return get_latest_signals(
            SIGNAL_STORE_PATH,
            asof=datetime.now(timezone.utc),
            max_age_minutes=SIGNAL_MAX_AGE_MINUTES,
            require_same_day=SIGNAL_REQUIRE_SAME_DAY,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return {}


def _as_float(value: object) -> Optional[float]:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return float(parsed)


def _classify_action(decision_tag: str, event_name: str, close: Optional[float], vwap: Optional[float], sqz_color: str, sqz_value: Optional[float]) -> str:
    event_norm = str(event_name or "").strip().lower()
    color = str(sqz_color or "").strip().lower()
    sqz_val = sqz_value if sqz_value is not None else 0.0

    if event_norm in {"stop", "stop_loss", "exit", "sell"}:
        return "stop_loss"
    if event_norm in {"take_profit", "tp", "takeprofit"}:
        return "take_profit"
    if event_norm in {"add", "pyramid", "scale_in"}:
        return "add"
    if event_norm in {"entry", "buy", "breakout"}:
        return "entry"

    if close is not None and vwap is not None:
        if close < vwap and color in {"red", "maroon"}:
            return "stop_loss"
        if close > vwap and color in {"red", "maroon"} and sqz_val < 0:
            return "take_profit"
        if decision_tag == "keep" and close >= (vwap * 1.01) and color in {"green", "lime"} and sqz_val >= 0.15:
            return "add"
        if close >= vwap and color in {"green", "lime"} and sqz_val > 0:
            return "entry"

    return ""


def _load_state() -> Dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_state(state: Dict[str, str]) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_signature(symbol: str, action: str, event_obj: object) -> str:
    return "|".join(
        [
            symbol,
            action,
            str(getattr(event_obj, "event", "")),
            str(getattr(event_obj, "ts", "")),
            str(getattr(event_obj, "close", "")),
            str(getattr(event_obj, "vwap", "")),
            str(getattr(event_obj, "sqzmom_color", "")),
            str(getattr(event_obj, "sqzmom_value", "")),
        ]
    )


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "NA"
    return f"{value:.2f}"


def _sanitize_webhook_url(value: str) -> str:
    cleaned = str(value or "").strip().strip('"').strip("'").strip()
    cleaned = cleaned.strip("[]")
    cleaned = cleaned.strip("<>")
    return cleaned


def _safe_read_csv(csv_path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(csv_path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(csv_path)


def _http_post_json(url: str, payload: dict) -> tuple[bool, str]:
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AlphaFinder/1.0"},
            timeout=15,
        )
        if response.ok:
            return True, f"{response.status_code} {response.text[:200]}"
        return False, f"HTTP {response.status_code}: {response.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


def _append_alert_log(rows: List[dict]) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    exists = ALERT_LOG.exists()
    with ALERT_LOG.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "alert_ts",
                "ticker",
                "rank",
                "action",
                "decision_tag",
                "risk_level",
                "tv_event",
                "close",
                "vwap",
                "sqzmom_color",
                "sqzmom_value",
                "signal_ts",
            ],
        )
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _position_effect(action: str) -> str:
    if action == "entry":
        return "open"
    if action == "add":
        return "increase"
    if action == "take_profit":
        return "reduce"
    if action == "stop_loss":
        return "close"
    return "update"


def _normalize_date_time(ts_value: str, fallback_ts: str) -> tuple[str, str]:
    parsed = pd.to_datetime(ts_value, errors="coerce", utc=True)
    if pd.isna(parsed):
        fallback = pd.to_datetime(fallback_ts, errors="coerce")
        if pd.isna(fallback):
            return "", ""
        return fallback.strftime("%Y-%m-%d"), fallback.strftime("%H:%M:%S")
    local_dt = parsed.tz_convert(None)
    return local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M:%S")


def _dedupe_and_sort_execution_df(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) == 0:
        return df
    out = df.copy()
    out = out.drop_duplicates(subset=["ticker", "action", "signal_ts", "timeframe"], keep="last")
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out = out.sort_values(
        ["execution_date", "execution_time", "rank", "ticker", "signal_ts"],
        ascending=[False, False, True, True, False],
        na_position="last",
    )
    out["rank"] = out["rank"].fillna(0).astype(int)
    return out


def _write_execution_outputs(rows: List[dict]) -> None:
    if not rows:
        return

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    EXECUTION_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(rows, columns=EXECUTION_LOG_FIELDS)

    if EXECUTION_LOG.exists():
        existing_df = _safe_read_csv(EXECUTION_LOG)
        merged_df = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        merged_df = new_df
    merged_df = _dedupe_and_sort_execution_df(merged_df)
    merged_df.to_csv(EXECUTION_LOG, index=False, encoding="utf-8-sig")

    latest_df = _dedupe_and_sort_execution_df(new_df)
    latest_df.to_csv(EXECUTION_LATEST, index=False, encoding="utf-8-sig")

    for execution_date, daily_df in latest_df.groupby("execution_date", dropna=False):
        if not execution_date:
            continue
        daily_path = EXECUTION_DAILY_DIR / f"{execution_date}_execution_trade.csv"
        if daily_path.exists():
            existing_daily = _safe_read_csv(daily_path)
            daily_out = pd.concat([existing_daily, daily_df], ignore_index=True)
        else:
            daily_out = daily_df
        daily_out = _dedupe_and_sort_execution_df(daily_out)
        daily_out.to_csv(daily_path, index=False, encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description="Push TradingView execution alerts to Discord")
    parser.add_argument("--top-n", type=int, default=5, help="Only evaluate top N AI decisions")
    parser.add_argument("--force", action="store_true", help="Send even if the same signal was already sent")
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending Discord")
    args = parser.parse_args()

    decision_df = _load_decision_df()
    if len(decision_df) == 0:
        print("No ai_decision_latest.csv rows available.")
        return 1

    signal_map = _load_signal_map()
    if not signal_map:
        print("No fresh TradingView webhook signals found.")
        return 0

    state = _load_state()
    candidates = decision_df.head(max(1, int(args.top_n))).copy()

    alert_lines = []
    state_updates = {}
    log_rows: List[dict] = []
    execution_rows: List[dict] = []
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for _, row in candidates.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker not in signal_map:
            continue

        event_obj = signal_map[ticker]
        decision_tag = str(row.get("decision_tag", "")).strip().lower()
        action = _classify_action(
            decision_tag=decision_tag,
            event_name=str(getattr(event_obj, "event", "")),
            close=_as_float(getattr(event_obj, "close", None)),
            vwap=_as_float(getattr(event_obj, "vwap", None)),
            sqz_color=str(getattr(event_obj, "sqzmom_color", "")),
            sqz_value=_as_float(getattr(event_obj, "sqzmom_value", None)),
        )
        if not action:
            continue

        signature = _build_signature(ticker, action, event_obj)
        if not args.force and state.get(ticker) == signature:
            continue

        close_val = _as_float(getattr(event_obj, "close", None))
        vwap_val = _as_float(getattr(event_obj, "vwap", None))
        sqz_val = _as_float(getattr(event_obj, "sqzmom_value", None))
        line = (
            f"- {action.upper()} {ticker} | rank={row.get('rank', 'NA')} | tag={decision_tag or 'NA'}"
            f" | close={_fmt_price(close_val)} | vwap={_fmt_price(vwap_val)}"
            f" | sqz={getattr(event_obj, 'sqzmom_color', 'NA')}/{_fmt_price(sqz_val)}"
            f" | reason={str(row.get('reason_summary', '')).strip()[:90]}"
        )
        alert_lines.append(line)
        state_updates[ticker] = signature
        log_rows.append(
            {
                "alert_ts": now_ts,
                "ticker": ticker,
                "rank": int(row.get("rank", 0)),
                "action": action,
                "decision_tag": decision_tag,
                "risk_level": str(row.get("risk_level", "")),
                "tv_event": str(getattr(event_obj, "event", "")),
                "close": "" if close_val is None else close_val,
                "vwap": "" if vwap_val is None else vwap_val,
                "sqzmom_color": str(getattr(event_obj, "sqzmom_color", "")),
                "sqzmom_value": "" if sqz_val is None else sqz_val,
                "signal_ts": str(getattr(event_obj, "ts", "")),
            }
        )
        execution_date, execution_time = _normalize_date_time(str(getattr(event_obj, "ts", "")), now_ts)
        execution_rows.append(
            {
                "recorded_at": now_ts,
                "execution_date": execution_date,
                "execution_time": execution_time,
                "decision_date": str(row.get("decision_date", "")),
                "ticker": ticker,
                "rank": int(row.get("rank", 0)),
                "action": action,
                "position_effect": _position_effect(action),
                "decision_tag": decision_tag,
                "risk_level": str(row.get("risk_level", "")),
                "tech_status": str(row.get("tech_status", "")),
                "theme": str(row.get("theme", "")),
                "reason_summary": str(row.get("reason_summary", "")).strip(),
                "signal_source": str(getattr(event_obj, "source", "tradingview")),
                "exchange": str(getattr(event_obj, "exchange", "") or ""),
                "timeframe": str(getattr(event_obj, "timeframe", "") or ""),
                "tv_event": str(getattr(event_obj, "event", "")),
                "signal_ts": str(getattr(event_obj, "ts", "")),
                "close": "" if close_val is None else close_val,
                "vwap": "" if vwap_val is None else vwap_val,
                "sqzmom_color": str(getattr(event_obj, "sqzmom_color", "")),
                "sqzmom_value": "" if sqz_val is None else sqz_val,
                "signal_signature": signature,
            }
        )

    if not alert_lines:
        print("No new actionable TradingView alerts.")
        return 0

    message = "\n".join([
        f"[Alpha Finder] TV Execution Alerts {now_ts}",
        "",
        *alert_lines,
        "",
        "提醒: 這是訊號通知，不是自動下單。請自行確認部位與風險。",
    ])

    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_MSG.write_text(message, encoding="utf-8")
    print("\n=== TradingView Alert Preview ===")
    print(message)

    if not args.dry_run:
        _write_execution_outputs(execution_rows)
        _append_alert_log(log_rows)
        state.update(state_updates)
        _save_state(state)

        webhook_url = _sanitize_webhook_url(DISCORD_WEBHOOK_URL)
        if not webhook_url:
            print("DISCORD_WEBHOOK_URL is missing. Execution log updated without Discord notification.")
            return 0
        ok, detail = _http_post_json(webhook_url, {"content": message[:1900]})
        print(f"[DISCORD] ok={ok} detail={detail}")
        if not ok:
            print("Execution log already updated. Re-run with --force if you want to retry the Discord alert.")
            return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())