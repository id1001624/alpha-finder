"""
Push decision alerts from ai_decision CSV to Discord (and optional LINE Messaging API),
then persist alert logs for later review.

Examples:
  python scripts/push_alerts_from_ai_decision.py --auto-latest --dry-run
  python scripts/push_alerts_from_ai_decision.py --auto-latest --top-n 5
  python scripts/push_alerts_from_ai_decision.py --csv-file repo_outputs/backtest/inbox/ai_decision_2026-03-05.csv

Env vars:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  LINE_CHANNEL_ACCESS_TOKEN=...
  LINE_TO_USER_ID=...
"""

from __future__ import annotations

import argparse
import csv
import os
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

from cloud_state import CLOUD_AI_DECISION_LATEST
from config import DISCORD_WEBHOOK_URL, SIGNAL_MAX_AGE_MINUTES, SIGNAL_REQUIRE_SAME_DAY, SIGNAL_STORE_PATH
from signal_store import get_latest_signals

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
INBOX_DIR = BACKTEST_DIR / "inbox"
AI_READY_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready" / "latest"
DAILY_REFRESH_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh" / "latest"
ALERT_DIR = BACKTEST_DIR / "alerts"
ALERT_LOG_CSV = ALERT_DIR / "alert_log.csv"
ALERT_MESSAGE_TXT = ALERT_DIR / "latest_alert_message.txt"
ALERT_MARKER_DIR = ALERT_DIR / "markers"
AI_DECISION_LOG_CSV = BACKTEST_DIR / "ai_decision_log.csv"

REQUIRED_COLS = [
    "decision_date",
    "rank",
    "ticker",
    "short_score_final",
    "risk_level",
    "tech_status",
    "decision_tag",
    "reason_summary",
    "catalyst_summary",
    "catalyst_type",
    "catalyst_sentiment",
    "source_ref",
]


def _find_latest_decision_csv() -> Optional[Path]:
    found: List[tuple[float, Path]] = []
    static_candidates = [CLOUD_AI_DECISION_LATEST, BACKTEST_DIR / "ai_decision_latest.csv"]
    for file in static_candidates:
        if not file.exists():
            continue
        try:
            found.append((file.stat().st_mtime, file))
        except OSError:
            continue
    for folder in [INBOX_DIR, AI_READY_LATEST_DIR, DAILY_REFRESH_LATEST_DIR]:
        if not folder.exists():
            continue
        for file in folder.glob("ai_decision_*.csv"):
            try:
                found.append((file.stat().st_mtime, file))
            except OSError:
                continue
    if not found:
        return None
    found.sort(key=lambda x: x[0], reverse=True)
    return found[0][1]


def _load_decision_df(csv_path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path)

    for col in REQUIRED_COLS:
        if col not in df.columns:
            df[col] = ""

    out = df.copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["short_score_final"] = pd.to_numeric(out["short_score_final"], errors="coerce")
    out = out[out["ticker"] != ""].copy()
    out = out.dropna(subset=["rank"]).copy()
    out["rank"] = out["rank"].astype(int)
    out = out.sort_values(["rank", "ticker"], ascending=[True, True])
    return out


def _load_tv_map() -> Dict[str, object]:
    try:
        return get_latest_signals(
            SIGNAL_STORE_PATH,
            asof=datetime.now(timezone.utc),
            max_age_minutes=SIGNAL_MAX_AGE_MINUTES,
            require_same_day=SIGNAL_REQUIRE_SAME_DAY,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return {}


def _fmt_tv_line(ticker: str, tv_map: Dict[str, object]) -> str:
    event = tv_map.get(ticker)
    if not event:
        return "TV:NA"

    vwap = "NA" if event.vwap is None else f"{float(event.vwap):.2f}"
    sqz = "NA" if event.sqzmom_color in (None, "") else str(event.sqzmom_color)
    sqzv = "NA" if event.sqzmom_value is None else f"{float(event.sqzmom_value):.2f}"
    return f"TV:vwap={vwap},sqz={sqz}/{sqzv}"


def _clip_text(value: object, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _sanitize_webhook_url(value: str) -> str:
    cleaned = str(value or "").strip().strip('"').strip("'").strip()
    cleaned = cleaned.strip("[]")
    cleaned = cleaned.strip("<>")
    return cleaned


def _marker_file_for(decision_date: str, mode: str, channel: str) -> Path:
    safe_date = str(decision_date or "unknown").strip().replace("/", "-")
    safe_mode = str(mode or "full").strip().lower()
    safe_channel = str(channel or "unknown").strip().lower()
    return ALERT_MARKER_DIR / f"{safe_date}_{safe_mode}_{safe_channel}.json"


def _already_sent(decision_date: str, mode: str, channel: str, source_csv: Path) -> bool:
    if mode not in {"bedtime", "morning"}:
        return False
    marker_file = _marker_file_for(decision_date, mode, channel)
    if not marker_file.exists():
        return False
    try:
        marker = pd.read_json(marker_file, typ="series")
    except (ValueError, OSError):
        return False
    return str(marker.get("source_csv", "")).strip() == str(source_csv)


def _write_sent_marker(decision_date: str, mode: str, channel: str, source_csv: Path) -> None:
    if mode not in {"bedtime", "morning"}:
        return
    ALERT_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker_file = _marker_file_for(decision_date, mode, channel)
    payload = {
        "decision_date": decision_date,
        "mode": mode,
        "channel": channel,
        "source_csv": str(source_csv),
        "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    marker_file.write_text(pd.Series(payload).to_json(force_ascii=False, indent=2), encoding="utf-8")


def _load_previous_top1(current_date: str) -> str:
    if not AI_DECISION_LOG_CSV.exists():
        return ""
    try:
        history = pd.read_csv(AI_DECISION_LOG_CSV, encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return ""

    if "decision_date" not in history.columns or "ticker" not in history.columns or "rank" not in history.columns:
        return ""

    history = history.copy()
    history["decision_date"] = history["decision_date"].astype(str).str.strip()
    history["ticker"] = history["ticker"].astype(str).str.strip().str.upper()
    history["rank"] = pd.to_numeric(history["rank"], errors="coerce")
    history = history[(history["decision_date"] != current_date) & (history["rank"] == 1)].copy()
    if len(history) == 0:
        return ""
    history = history.sort_values(["decision_date"], ascending=[False])
    return str(history.iloc[0].get("ticker", "")).strip().upper()


def _build_bedtime_message(df: pd.DataFrame, tv_map: Dict[str, object], title_date: str) -> str:
    selected = df.head(3).copy()
    top1 = selected.iloc[0] if len(selected) > 0 else None
    prev_top1 = _load_previous_top1(title_date)
    changed = "NA"
    if top1 is not None:
        changed = "是" if prev_top1 and str(top1.get("ticker", "")).upper() != prev_top1 else "否"

    lines = [
        f"[Alpha Finder] 睡前摘要 {title_date}",
        "",
    ]

    if top1 is not None:
        lines.extend(
            [
                f"Top 1: {top1.get('ticker', 'NA')} | tag={top1.get('decision_tag', 'NA')} | risk={top1.get('risk_level', 'NA')} | Top1是否改變={changed}",
                f"盤中確認: {_fmt_tv_line(str(top1.get('ticker', '')), tv_map)}",
                f"主催化: {_clip_text(top1.get('catalyst_summary') or top1.get('catalyst_type') or '待確認', 140)}",
                f"決策摘要: {_clip_text(top1.get('reason_summary') or '待確認', 140)}",
                "",
                "Top 3:",
            ]
        )
        for _, row in selected.iterrows():
            lines.append(
                f"- {row.get('rank', 'NA')}. {row.get('ticker', 'NA')} | {row.get('decision_tag', 'NA')} | {_clip_text(row.get('reason_summary'), 90)}"
            )
    else:
        lines.append("No candidates available.")

    lines.append("")
    lines.append("用途: 睡前只看這版，隔天先盯 Top 1 與 Top 3。")
    return "\n".join(lines)


def _build_morning_message(df: pd.DataFrame, tv_map: Dict[str, object], title_date: str) -> str:
    selected = df.head(3).copy()
    top1 = selected.iloc[0] if len(selected) > 0 else None
    lines = [
        f"[Alpha Finder] Overnight Recap {title_date}",
        "",
    ]
    if top1 is not None:
        lines.append(
            f"今日先看: {top1.get('ticker', 'NA')} | tag={top1.get('decision_tag', 'NA')} | {_fmt_tv_line(str(top1.get('ticker', '')), tv_map)}"
        )
        lines.append(f"原因: {_clip_text(top1.get('catalyst_summary') or top1.get('reason_summary'), 160)}")
        lines.append("")
        lines.append("候選觀察:")
        for _, row in selected.iterrows():
            lines.append(
                f"- {row.get('rank', 'NA')}. {row.get('ticker', 'NA')} | risk={row.get('risk_level', 'NA')} | {_clip_text(row.get('reason_summary'), 90)}"
            )
    else:
        lines.append("No candidates available.")
    lines.append("")
    lines.append("用途: 早上只回答三件事: Top1 是誰、理由有沒有變、先打開哪檔。")
    return "\n".join(lines)


def _build_message(df: pd.DataFrame, tv_map: Dict[str, object], top_n: int, tags: set[str], title_date: str) -> str:
    selected = df[df["decision_tag"].isin(tags)].copy()
    selected = selected.head(top_n)

    lines = [
        f"[Alpha Finder] AI Decision Alert {title_date}",
        f"Candidates: {len(selected)}",
        "",
    ]

    if len(selected) == 0:
        lines.append("No candidates matched current filters.")
    else:
        for _, row in selected.iterrows():
            ticker = str(row.get("ticker", ""))
            rank = int(row.get("rank", 0))
            score = row.get("short_score_final")
            score_str = "NA" if pd.isna(score) else f"{float(score):.1f}"
            tag = str(row.get("decision_tag", ""))
            risk = str(row.get("risk_level", "")) or "NA"
            tech = str(row.get("tech_status", "")) or "NA"
            tv_text = _fmt_tv_line(ticker, tv_map)
            lines.append(f"{rank}. {ticker} | tag={tag} | score={score_str} | risk={risk} | tech={tech} | {tv_text}")

    lines.append("")
    lines.append("Action: review in TradingView and follow your stop rules.")
    return "\n".join(lines)


def _render_message(df: pd.DataFrame, tv_map: Dict[str, object], top_n: int, tags: set[str], title_date: str, mode: str) -> str:
    selected = df[df["decision_tag"].isin(tags)].copy().sort_values(["rank", "ticker"], ascending=[True, True])
    if mode == "bedtime":
        return _build_bedtime_message(selected, tv_map, title_date)
    if mode == "morning":
        return _build_morning_message(selected, tv_map, title_date)
    return _build_message(selected, tv_map, top_n=top_n, tags=tags, title_date=title_date)


def _post_json(url: str, payload: dict, headers: Optional[dict] = None, timeout: int = 15) -> tuple[bool, str]:
    req_headers = {"Content-Type": "application/json", "User-Agent": "AlphaFinder/1.0"}
    if headers:
        req_headers.update(headers)
    try:
        response = requests.post(url, json=payload, headers=req_headers, timeout=timeout)
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


def _send_line(message: str, channel_access_token: str, to_user_id: str) -> tuple[bool, str]:
    if not channel_access_token or not to_user_id:
        return False, "line token or to-user-id missing"

    payload = {
        "to": to_user_id,
        "messages": [{"type": "text", "text": message[:5000]}],
    }
    headers = {"Authorization": f"Bearer {channel_access_token}"}
    return _post_json("https://api.line.me/v2/bot/message/push", payload, headers=headers)


def _append_alert_log(rows: List[dict]) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = ALERT_LOG_CSV.exists()

    with ALERT_LOG_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "alert_ts",
                "decision_date",
                "channel",
                "ticker",
                "rank",
                "decision_tag",
                "short_score_final",
                "risk_level",
                "tech_status",
                "source_csv",
            ],
        )
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Push alerts from ai_decision CSV to Discord/LINE")
    parser.add_argument("--csv-file", default="", help="Path to ai_decision_YYYY-MM-DD.csv")
    parser.add_argument("--auto-latest", action="store_true", help="Find latest ai_decision_*.csv automatically")
    parser.add_argument("--top-n", type=int, default=5, help="Top N rows to send")
    parser.add_argument("--tags", default="keep,watch", help="Comma separated tags to include, e.g. keep or keep,watch")
    parser.add_argument("--channel", default="discord", choices=["discord", "line", "both"], help="Notification channel")
    parser.add_argument("--mode", default="full", choices=["full", "bedtime", "morning"], help="Discord/LINE message style")
    parser.add_argument("--dry-run", action="store_true", help="Print message only, do not send")
    args = parser.parse_args()

    csv_path = Path(args.csv_file).resolve() if args.csv_file.strip() else None
    if args.auto_latest or csv_path is None:
        latest = _find_latest_decision_csv()
        if latest is None:
            print("No ai_decision_*.csv found in inbox / ai_ready/latest / daily_refresh/latest")
            return 1
        csv_path = latest

    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return 2

    df = _load_decision_df(csv_path)
    tags = {x.strip().lower() for x in str(args.tags).split(",") if x.strip()}
    if not tags:
        tags = {"keep", "watch"}

    decision_date = "unknown"
    if "decision_date" in df.columns and df["decision_date"].notna().any():
        decision_date = str(df["decision_date"].dropna().iloc[0])

    tv_map = _load_tv_map()
    message = _render_message(
        df=df,
        tv_map=tv_map,
        top_n=max(1, int(args.top_n)),
        tags=tags,
        title_date=decision_date,
        mode=str(args.mode),
    )

    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_MESSAGE_TXT.write_text(message, encoding="utf-8")

    print("\n=== Alert Preview ===")
    print(message)

    sent_channels: List[str] = []
    send_failed = False
    if not args.dry_run:
        if args.channel in {"discord", "both"}:
            if _already_sent(decision_date, args.mode, "discord", csv_path):
                print(f"[SKIP] discord {args.mode} already sent for {decision_date} -> {csv_path}")
            else:
                discord_url = _sanitize_webhook_url(os.getenv("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL))
                ok, detail = _send_discord(message, discord_url)
                print(f"[DISCORD] ok={ok} detail={detail}")
                if ok:
                    sent_channels.append("discord")
                    _write_sent_marker(decision_date, args.mode, "discord", csv_path)
                else:
                    send_failed = True

        if args.channel in {"line", "both"}:
            if _already_sent(decision_date, args.mode, "line", csv_path):
                print(f"[SKIP] line {args.mode} already sent for {decision_date} -> {csv_path}")
            else:
                line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
                line_to = os.getenv("LINE_TO_USER_ID", "").strip()
                ok, detail = _send_line(message, line_token, line_to)
                print(f"[LINE] ok={ok} detail={detail}")
                if ok:
                    sent_channels.append("line")
                    _write_sent_marker(decision_date, args.mode, "line", csv_path)
                else:
                    send_failed = True
    else:
        sent_channels.append("dry_run")

    log_df = df[df["decision_tag"].isin(tags)].head(max(1, int(args.top_n))).copy()
    log_rows: List[dict] = []
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for _, row in log_df.iterrows():
        for ch in sent_channels:
            log_rows.append(
                {
                    "alert_ts": ts_now,
                    "decision_date": decision_date,
                    "channel": ch,
                    "ticker": str(row.get("ticker", "")),
                    "rank": int(row.get("rank", 0)),
                    "decision_tag": str(row.get("decision_tag", "")),
                    "short_score_final": "" if pd.isna(row.get("short_score_final")) else float(row.get("short_score_final")),
                    "risk_level": str(row.get("risk_level", "")),
                    "tech_status": str(row.get("tech_status", "")),
                    "source_csv": str(csv_path),
                }
            )

    if log_rows and not args.dry_run:
        _append_alert_log(log_rows)
        print(f"[ALERT_LOG] appended {len(log_rows)} rows -> {ALERT_LOG_CSV}")

    print(f"[ALERT_MSG] {ALERT_MESSAGE_TXT}")
    return 4 if send_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
