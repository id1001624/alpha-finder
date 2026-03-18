"""
補發未即時推播的 Swing 進場/加碼訊號。

當日若出现 swing_entry 或 swing_add 但未即時推播，
系統必須在收盤後固定時間強制補發一則摘要通知，
並記錄是否補發成功。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import install_builtin_print_logging
from ai_trading.canonical_event_log import (
    CANONICAL_ACTION_EVENT_LOG_FILE,
    dispatch_outcome_payload,
    load_canonical_action_events,
    update_canonical_dispatch_outcomes,
)
from ai_trading.swing_core_engine import _send_discord

install_builtin_print_logging()


def send_pending_swing_entries_adds_alert() -> dict:
    """
    檢查當日未即時推播的 swing_entry/swing_add，
    並在固定時間補發摘要通知。
    
    Returns:
        {
            "ok": bool,
            "pending_count": int,
            "resend_count": int,
            "resend_success": bool,
            "message": str,
        }
    """
    if not CANONICAL_ACTION_EVENT_LOG_FILE.exists():
        return {
            "ok": False,
            "pending_count": 0,
            "resend_count": 0,
            "resend_success": False,
            "message": "No canonical action/event log found",
        }

    df = load_canonical_action_events(limit=50000)
    if len(df) == 0:
        return {
            "ok": False,
            "pending_count": 0,
            "resend_count": 0,
            "resend_success": False,
            "message": "Canonical action/event log is empty",
        }

    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")

    # Find pending swing recap actions from today.
    pending_df = df[
        (df["trade_date"].astype(str) == today_str)
        & (df["engine"].astype(str).str.lower() == "swing")
        & (df["dispatch_mode"].astype(str).str.lower() == "recap")
        & (df["dispatch_status"].astype(str).str.lower() == "pending_recap")
        & (df["action_type"].astype(str).str.lower().isin({"entry", "add"}))
    ].copy()

    pending_count = len(pending_df)

    if pending_count == 0:
        return {
            "ok": True,
            "pending_count": 0,
            "resend_count": 0,
            "resend_success": False,
            "message": "No pending swing entries/adds found for today",
        }

    entry_rows = pending_df[pending_df["action_type"].astype(str).str.lower() == "entry"]
    add_rows = pending_df[pending_df["action_type"].astype(str).str.lower() == "add"]

    # Build alert message
    now_ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"[Alpha Finder] 🔔 Swing 進場補發 {now_ts_str}", "", "未即時推播的訊號已補發：", ""]

    if len(entry_rows) > 0:
        lines.append("【Swing 進場】")
        for _, row in entry_rows.iterrows():
            ticker = str(row.get("ticker", ""))
            reason = str(row.get("reason_text", ""))
            lines.append(f"  - {ticker} | {reason}")
        lines.append("")

    if len(add_rows) > 0:
        lines.append("【Swing 加碼】")
        for _, row in add_rows.iterrows():
            ticker = str(row.get("ticker", ""))
            reason = str(row.get("reason_text", ""))
            lines.append(f"  - {ticker} | {reason}")
        lines.append("")

    lines.append("提醒: 收盤後補發。成交請用 Discord /buy profile=swing 回報。")
    alert_message = "\n".join(lines)

    discord_ok, discord_detail = _send_discord(alert_message)

    source_event_ids = [
        str(row.get("source_event_id", "")).strip()
        for _, row in pending_df.iterrows()
        if str(row.get("source_event_id", "")).strip()
    ]
    update_canonical_dispatch_outcomes(
        dispatch_outcome_payload(
            source_event_ids,
            "sent_recap" if bool(discord_ok) else "failed_recap",
            f"resend_pending_swing_entries_adds_alert@{now_ts_str}: {discord_detail if discord_detail else 'OK'}",
        )
    )

    return {
        "ok": True,
        "pending_count": pending_count,
        "resend_count": len(source_event_ids),
        "resend_success": discord_ok,
        "message": alert_message if discord_ok else f"Resend attempt failed: {discord_detail}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Send pending swing entry/add alerts")
    parser.parse_args()

    result = send_pending_swing_entries_adds_alert()

    if result.get("ok"):
        print(f"[OK] pending={result.get('pending_count')} resend={result.get('resend_count')} discord_ok={result.get('resend_success')}")
        if result.get("message"):
            print(result["message"])
    else:
        print(f"[SKIP] {result.get('message')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
