from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.watchlist_brief import build_saved_watchlist_followup_message, load_all_saved_watchlist_tickers
from app_logging import get_logger
from config import DISCORD_WEBHOOK_URL

logger = get_logger(__name__)

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
ALERT_DIR = BACKTEST_DIR / "alerts"
ALERT_MARKER_DIR = ALERT_DIR / "markers"
WATCHLIST_FOLLOWUP_MESSAGE_TXT = ALERT_DIR / "latest_watchlist_followup_message.txt"
MODE_NAME = "watchlist_followup"


def _sanitize_webhook_url(url: str) -> str:
    return str(url or "").strip()


def _post_json(url: str, payload: dict, timeout: int = 15) -> tuple[bool, str]:
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AlphaFinder/1.0"},
            timeout=timeout,
        )
        if response.ok:
            return True, f"{response.status_code} {response.text[:200]}"
        return False, f"HTTP {response.status_code}: {response.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


def _send_discord(message: str, webhook_url: str) -> tuple[bool, str]:
    if not webhook_url:
        return False, "discord webhook url missing"

    chunks: List[str] = []
    text = message
    while len(text) > 1900:
        split_at = text.rfind("\n", 0, 1900)
        if split_at <= 0:
            split_at = 1900
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)

    for chunk in chunks:
        ok, detail = _post_json(webhook_url, {"content": chunk})
        if not ok:
            return False, detail
    return True, f"sent {len(chunks)} discord message(s)"


def _marker_file_for(run_date: str, channel: str) -> Path:
    safe_date = re.sub(r"[^0-9A-Za-z_-]+", "-", str(run_date or "unknown")).strip("-") or "unknown"
    safe_channel = re.sub(r"[^0-9A-Za-z_-]+", "-", str(channel or "discord")).strip("-") or "discord"
    return ALERT_MARKER_DIR / f"{safe_date}_{MODE_NAME}_{safe_channel}.json"


def _already_sent(run_date: str, channel: str, source_id: object) -> bool:
    marker_file = _marker_file_for(run_date, channel)
    if not marker_file.exists():
        return False
    try:
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return str(marker.get("source_id", "")).strip() == str(source_id)


def _write_sent_marker(run_date: str, channel: str, source_id: object) -> None:
    ALERT_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": run_date,
        "mode": MODE_NAME,
        "channel": channel,
        "source_id": str(source_id),
        "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _marker_file_for(run_date, channel).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Push independent saved watchlist follow-up recap to Discord")
    parser.add_argument("--tickers", default="", help="Optional override tickers, e.g. AAOI NVDA")
    parser.add_argument("--dry-run", action="store_true", help="Print message only, do not send")
    parser.add_argument("--force", action="store_true", help="Send even if a same-day marker already exists")
    args = parser.parse_args()

    if args.tickers.strip():
        tickers = [token.strip().upper() for token in re.split(r"[\s,]+", args.tickers.strip()) if token.strip()]
    else:
        tickers = load_all_saved_watchlist_tickers()

    if not tickers:
        logger.info("[SKIP] no saved watchlist tickers found")
        return 0

    source_id = ",".join(tickers)
    run_date = datetime.now().strftime("%Y-%m-%d")
    message = build_saved_watchlist_followup_message(saved_tickers=tickers)

    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FOLLOWUP_MESSAGE_TXT.write_text(message, encoding="utf-8")
    logger.info("=== Watchlist Follow-up Preview ===\n%s", message)

    if args.dry_run:
        return 0

    if not args.force and _already_sent(run_date, "discord", source_id):
        logger.warning("[SKIP] discord %s already sent for %s -> %s", MODE_NAME, run_date, source_id)
        return 0

    discord_url = _sanitize_webhook_url(os.getenv("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL))
    ok, detail = _send_discord(message, discord_url)
    logger.info("[DISCORD] ok=%s detail=%s", ok, detail)
    if not ok:
        return 1

    _write_sent_marker(run_date, "discord", source_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())