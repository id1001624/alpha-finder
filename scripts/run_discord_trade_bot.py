from __future__ import annotations

import asyncio
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import get_logger

from ai_trading.position_state import append_trade_ledger, apply_trade_fill, load_positions, save_positions
from ai_trading.quantmuse_bridge import get_quantmuse_capabilities
from ai_trading.strategy_context import (
    HORIZON_INTRADAY_MONSTER,
    HORIZON_SWING_CORE,
    STRATEGY_MONSTER_SWING,
    STRATEGY_SWING_TREND,
)
from ai_trading.watchlist_brief import (
    add_saved_watchlist_tickers,
    build_watchlist_brief_message,
    build_saved_watchlist_followup_message,
    format_saved_watchlist_message,
    load_saved_watchlist,
    remove_saved_watchlist_tickers,
)
from scripts.push_alerts_from_ai_decision import build_recap_message_preview
from config import (
    DISCORD_BOT_ALLOWED_CHANNEL_IDS,
    DISCORD_BOT_ENABLED,
    DISCORD_BOT_PREFIX,
    DISCORD_BOT_SYNC_GUILD_ID,
    DISCORD_BOT_TOKEN,
)
from turso_state import load_recent_execution_log, load_recent_trade_ledger

logger = get_logger(__name__)
ALERT_DIR = PROJECT_ROOT / "repo_outputs" / "backtest" / "alerts"
RECAP_STATUS_LATEST_JSON = ALERT_DIR / "recap_status_latest.json"
BLOCKING_API_TIMEOUT_SEC = 120


def _parse_allowed_channel_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _format_positions() -> str:
    positions = load_positions()
    if len(positions) == 0:
        return "目前沒有開倉部位。"
    lines = ["目前開倉部位:"]
    for _, row in positions.sort_values(["ticker", "horizon_tag", "strategy_profile"]).iterrows():
        lines.append(
            f"- {row['ticker']} | {row.get('horizon_tag', HORIZON_INTRADAY_MONSTER)} | {row.get('strategy_profile', STRATEGY_MONSTER_SWING)} | qty={float(row['quantity']):g} | avg={float(row['avg_cost']):.2f} | add_count={int(row['add_count'])}"
        )
    return "\n".join(lines)


def _normalize_profile(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip().lower()
    if text in {"swing", "swing_trend", "core", "swing_core"}:
        return STRATEGY_SWING_TREND, HORIZON_SWING_CORE
    return STRATEGY_MONSTER_SWING, HORIZON_INTRADAY_MONSTER


def _strategy_to_profile_label(strategy_profile: str) -> str:
    if str(strategy_profile).strip().lower() == STRATEGY_SWING_TREND:
        return "swing"
    return "monster"


def _help_text() -> str:
    return (
        "可用指令:\n"
        "/buy ticker quantity price [note] [profile]\n"
        "/add ticker quantity price [note] [profile]\n"
        "/sell ticker quantity price [note] [profile]\n"
        "/positions\n"
        "/position ticker\n"
        "/trades [ticker] [limit]\n"
        "/executions [ticker] [limit]\n"
        "/watchlist [tickers]\n"
        "/watchadd tickers\n"
        "/watchremove tickers\n"
        "/watchsaved\n\n"
        "/recap [mode] [debug] [tickers]\n\n"
        "/recapstatus\n\n"
        "格式規則:\n"
        "- 沒有 [] 的參數 = 必填\n"
        "- 有 [] 的參數 = 可不填\n\n"
        "參數說明:\n"
        "- ticker: 股票代號，例如 MU / AAPL\n"
        "- quantity: 成交股數\n"
        "- price: 真實成交價\n"
        "- note: 備註，可留空\n"
        "- profile: monster 或 swing；不填預設 monster\n"
        "- limit: 顯示幾筆，預設 5，最大 20\n"
        "- tickers: 可一次多檔，空白或逗號分隔\n\n"
        "- recap mode: bedtime / morning / opening / watchlist\n"
        "- debug: true/false，顯示 Gemini/Tavily 是否啟用與摘要資料量\n\n"
        "- recapstatus: 顯示最近一次 recap 的命中摘要與原因碼（不觸發 API）\n\n"
        "也保留文字指令相容:\n"
        f"{DISCORD_BOT_PREFIX}buy AAPL 100 188.2 monster\n"
        f"{DISCORD_BOT_PREFIX}add AAPL 50 190.1 swing\n"
        f"{DISCORD_BOT_PREFIX}sell AAPL 80 196.5 monster\n"
        f"{DISCORD_BOT_PREFIX}positions\n"
        f"{DISCORD_BOT_PREFIX}position AAPL\n"
        f"{DISCORD_BOT_PREFIX}trades AAPL 5\n"
        f"{DISCORD_BOT_PREFIX}executions AAPL 5\n"
        f"{DISCORD_BOT_PREFIX}watchlist AAPL NVDA TSLA\n"
        f"{DISCORD_BOT_PREFIX}watchadd AAPL NVDA\n"
        f"{DISCORD_BOT_PREFIX}watchremove AAPL\n"
        f"{DISCORD_BOT_PREFIX}watchsaved\n"
        f"{DISCORD_BOT_PREFIX}recap bedtime\n"
        f"{DISCORD_BOT_PREFIX}recap watchlist AAPL NVDA\n\n"
        f"{DISCORD_BOT_PREFIX}recapstatus\n\n"
        "規則提醒:\n"
        "- watchsaved 不會自動把你的成交改成 swing\n"
        "- 要做 swing 倉，請在 /buy /add /sell 明確填 profile=swing\n"
        "- 同一筆倉位後續請沿用同一個 profile\n\n"
        "手動成交後請立刻用 /buy、/add、/sell 回報，後續 engine 與 recap 才會沿用正確持倉狀態。"
    )


def _split_chunks(text: str, limit: int = 1800) -> list[str]:
    chunks: list[str] = []
    remaining = str(text or "")
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_long_message(ctx, text: str) -> None:
    for chunk in _split_chunks(text):
        await ctx.send(chunk)


def _ctx_user_id(ctx) -> int:
    author = getattr(ctx, "author", None)
    if author is not None and getattr(author, "id", None) is not None:
        return int(author.id)
    interaction = getattr(ctx, "interaction", None)
    user = getattr(interaction, "user", None) if interaction is not None else None
    if user is not None and getattr(user, "id", None) is not None:
        return int(user.id)
    return 0


def _save_recap_status(payload: dict) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **(payload if isinstance(payload, dict) else {}),
    }
    RECAP_STATUS_LATEST_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_recap_status() -> dict:
    if not RECAP_STATUS_LATEST_JSON.exists():
        return {}
    try:
        data = json.loads(RECAP_STATUS_LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _derive_recap_reason_code(mode: str, pipeline_debug: dict, ai_summary_generated: bool) -> str:
    mode_text = str(mode or "").strip().lower()
    if mode_text == "watchlist":
        return "watchlist_manual"
    gemini_enabled = bool((pipeline_debug or {}).get("gemini_enabled"))
    tavily_enabled = bool((pipeline_debug or {}).get("tavily_enabled"))
    if gemini_enabled and ai_summary_generated:
        return "ai_summary_ok"
    if gemini_enabled and not ai_summary_generated:
        return "gemini_fallback"
    if (not gemini_enabled) and tavily_enabled:
        return "gemini_disabled_tavily_only"
    return "rules_only"


def _build_recapstatus_message(status: dict) -> str:
    if not status:
        return "目前還沒有 recap 狀態紀錄。先執行 /recap bedtime。"
    lines = [
        "[Alpha Finder] Recap 狀態",
        f"時間: {status.get('updated_at', 'NA')}",
        f"mode: {status.get('mode', 'NA')}",
        f"ok: {bool(status.get('ok', False))}",
        f"reason_code: {status.get('reason_code', 'NA')}",
        f"decision_date: {status.get('decision_date', 'NA')}",
        f"source_id: {status.get('source_id', '')}",
    ]

    if "gemini_enabled" in status:
        lines.append(f"gemini_enabled: {bool(status.get('gemini_enabled'))}")
    if "tavily_enabled" in status:
        lines.append(f"tavily_enabled: {bool(status.get('tavily_enabled'))}")
    if "ai_summary_generated" in status:
        lines.append(f"ai_summary_generated: {bool(status.get('ai_summary_generated'))}")
    if "tracked_news_count" in status:
        lines.append(f"tracked_news_count: {int(status.get('tracked_news_count', 0) or 0)}")
    if "conflict_news_count" in status:
        lines.append(f"conflict_news_count: {int(status.get('conflict_news_count', 0) or 0)}")
    if "has_prior_bedtime_plan" in status:
        lines.append(f"has_prior_bedtime_plan: {bool(status.get('has_prior_bedtime_plan'))}")
    if "has_prior_morning_plan" in status:
        lines.append(f"has_prior_morning_plan: {bool(status.get('has_prior_morning_plan'))}")
    if "execution_summary_source" in status and str(status.get("execution_summary_source", "")).strip():
        lines.append(f"execution_summary_source: {status.get('execution_summary_source', '')}")

    note = str(status.get("note", "")).strip()
    if note:
        lines.append(f"note: {note}")
    return "\n".join(lines)


def _format_recent_trades(ticker: str = "", limit: int = 5) -> str:
    df = load_recent_trade_ledger(limit=limit, ticker=ticker)
    ticker_label = str(ticker or "").strip().upper()
    title = f"最近成交 {ticker_label}:" if ticker_label else "最近成交:"
    if len(df) == 0:
        return f"{title}\n目前查不到 Turso 成交紀錄。"
    lines = [title]
    for _, row in df.iterrows():
        lines.append(
            f"- {row.get('recorded_at', '')} | {row.get('ticker', '')} | {str(row.get('side', '')).upper()} | qty={float(row.get('quantity', 0.0)):g} | price={float(row.get('price', 0.0)):.2f} | after={float(row.get('after_qty', 0.0)):g}"
        )
    return "\n".join(lines)


def _format_recent_executions(ticker: str = "", limit: int = 5) -> str:
    df = load_recent_execution_log(limit=limit, ticker=ticker)
    ticker_label = str(ticker or "").strip().upper()
    title = f"最近 execution {ticker_label}:" if ticker_label else "最近 execution:"
    if len(df) == 0:
        return f"{title}\n目前查不到 Turso execution 紀錄。"
    lines = [title]
    for _, row in df.iterrows():
        close_value = pd.to_numeric(row.get("close"), errors="coerce")
        close_text = "NA" if pd.isna(close_value) else f"{float(close_value):.2f}"
        lines.append(
            f"- {row.get('execution_date', '')} {row.get('execution_time', '')} | {row.get('ticker', '')} | {str(row.get('action', '')).upper()} | rank={int(pd.to_numeric(row.get('rank'), errors='coerce') or 0)} | close={close_text} | tf={row.get('timeframe', '') or 'NA'}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Discord trade bot for manual fill capture")
    parser.parse_args()

    if not DISCORD_BOT_ENABLED:
        logger.error("DISCORD_BOT_ENABLED is false.")
        return 2
    if not DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN is missing.")
        return 3

    import discord
    from discord import app_commands
    from discord.ext import commands

    intents = discord.Intents.default()
    intents.message_content = True
    allowed_channel_ids = _parse_allowed_channel_ids(DISCORD_BOT_ALLOWED_CHANNEL_IDS)

    class TradeBot(commands.Bot):
        async def setup_hook(self) -> None:
            sync_guild_raw = str(DISCORD_BOT_SYNC_GUILD_ID or "").strip()
            if not sync_guild_raw:
                synced = await self.tree.sync()
                logger.info("Discord trade bot synced %s global command(s).", len(synced))
                return

            try:
                guild = discord.Object(id=int(sync_guild_raw))
            except ValueError:
                synced = await self.tree.sync()
                logger.warning("DISCORD_BOT_SYNC_GUILD_ID invalid, fallback to global sync (%s command(s)).", len(synced))
                return

            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Discord trade bot synced %s guild command(s) to %s.", len(synced), sync_guild_raw)

    bot = TradeBot(command_prefix=DISCORD_BOT_PREFIX, intents=intents, help_command=None)
    startup_quantmuse_caps = get_quantmuse_capabilities()

    async def _guard_channel(ctx) -> bool:
        if not allowed_channel_ids:
            return True
        is_allowed = ctx.channel is not None and ctx.channel.id in allowed_channel_ids
        if is_allowed:
            return True
        message = "這個頻道沒有在允許清單內，請改到指定交易頻道使用。"
        if getattr(ctx, "interaction", None) is not None:
            await ctx.send(message, ephemeral=True)
        else:
            await ctx.send(message)
        return False

    async def _build_watchlist_recap_output(ctx, tickers: str, debug: bool, trigger: str) -> str:
        saved_tickers = await asyncio.to_thread(load_saved_watchlist, _ctx_user_id(ctx))
        message = await asyncio.wait_for(
            asyncio.to_thread(build_watchlist_brief_message, raw_tickers=tickers, saved_tickers=saved_tickers),
            timeout=BLOCKING_API_TIMEOUT_SEC,
        )
        note = "watchlist alias output"
        if debug:
            followup_card = await asyncio.to_thread(build_saved_watchlist_followup_message, saved_tickers=saved_tickers)
            debug_lines = [
                "",
                "[Recap Debug]",
                f"saved_tickers={len(saved_tickers)}",
                "watchlist_mode=ai_decision + positions + saved_watchlist + optional extras",
                "followup_card_enabled=true",
            ]
            message = message + "\n" + "\n".join(debug_lines)
            message = message + "\n\n" + followup_card
            note = "watchlist alias output + debug"

        _save_recap_status(
            {
                "ok": True,
                "mode": "watchlist",
                "reason_code": _derive_recap_reason_code("watchlist", {}, False),
                "decision_date": datetime.now().strftime("%Y-%m-%d"),
                "source_id": f"saved={len(saved_tickers)}|trigger={trigger}",
                "gemini_enabled": None,
                "tavily_enabled": None,
                "ai_summary_generated": None,
                "tracked_news_count": 0,
                "conflict_news_count": 0,
                "note": note,
            }
        )
        return message

    @bot.event
    async def on_ready():
        logger.info("Discord trade bot ready: %s", bot.user)
        caps = startup_quantmuse_caps
        logger.info(
            "QuantMuse capabilities | enabled=%s available=%s has_langchain=%s reason=%s module=%s provider=%s path=%s",
            bool(caps.get("enabled")),
            bool(caps.get("available")),
            bool(caps.get("has_langchain")),
            str(caps.get("reason", "")),
            str(caps.get("module_name", "")),
            str(caps.get("llm_provider", "")),
            str(caps.get("quantmuse_path", "")),
        )

    @bot.hybrid_command(name="tradehelp", description="顯示可用的交易指令")
    async def tradehelp(ctx):
        if not await _guard_channel(ctx):
            return
        await ctx.send(_help_text())

    @bot.hybrid_command(name="positions", description="查看目前所有開倉部位")
    async def positions(ctx):
        if not await _guard_channel(ctx):
            return
        await ctx.send(_format_positions())

    @bot.hybrid_command(name="position", description="查詢單一股票目前持倉")
    @app_commands.describe(ticker="股票代號，例如 AAPL")
    async def position(ctx, ticker: str):
        if not await _guard_channel(ctx):
            return
        positions_df = load_positions()
        rows = positions_df[positions_df["ticker"] == ticker.strip().upper()].copy()
        rows = rows[pd.to_numeric(rows.get("quantity", 0.0), errors="coerce").fillna(0.0) > 0].copy()
        if len(rows) == 0:
            await ctx.send(f"{ticker.upper()} 目前沒有開倉部位。")
            return
        lines = [f"{ticker.upper()} 目前部位:"]
        for _, record in rows.sort_values(["horizon_tag", "strategy_profile", "updated_at"], ascending=[True, True, False]).iterrows():
            lines.append(
                f"- {record['horizon_tag']}/{record['strategy_profile']} (profile={_strategy_to_profile_label(str(record.get('strategy_profile', '')))})"
                f" | qty={float(record['quantity']):g} | avg={float(record['avg_cost']):.2f} | realized={float(record['realized_pnl']):.2f}"
            )
        await ctx.send("\n".join(lines))

    @bot.hybrid_command(name="trades", description="查詢最近成交紀錄（Turso）")
    @app_commands.describe(ticker="可選，股票代號，例如 AAPL", limit="最多幾筆，預設 5")
    async def trades(ctx, ticker: str = "", limit: int = 5):
        if not await _guard_channel(ctx):
            return
        limit_value = max(1, min(int(limit), 20))
        await ctx.send(_format_recent_trades(ticker=ticker, limit=limit_value))

    @bot.hybrid_command(name="executions", description="查詢最近 execution 歷史（Turso）")
    @app_commands.describe(ticker="可選，股票代號，例如 AAPL", limit="最多幾筆，預設 5")
    async def executions(ctx, ticker: str = "", limit: int = 5):
        if not await _guard_channel(ctx):
            return
        limit_value = max(1, min(int(limit), 20))
        await ctx.send(_format_recent_executions(ticker=ticker, limit=limit_value))

    @bot.hybrid_command(name="watchlist", description="同 /recap watchlist：整合 ai_decision、持倉與關注股的結論卡")
    @app_commands.describe(tickers="可選，額外加入的股票代號，例如 AAPL NVDA TSLA")
    async def watchlist(ctx, *, tickers: str = ""):
        if not await _guard_channel(ctx):
            return
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            await ctx.defer()
        try:
            message = await _build_watchlist_recap_output(ctx, tickers=tickers, debug=False, trigger="watchlist")
        except asyncio.TimeoutError:
            await ctx.send("指令逾時：watchlist 外部資料回應太慢，請稍後再試。")
            return
        except ValueError as exc:
            await ctx.send(f"指令失敗: {exc}")
            return
        await _send_long_message(ctx, message)

    @bot.hybrid_command(name="watchadd", description="把股票加入你的保存關注清單")
    @app_commands.describe(tickers="以空白或逗號分隔股票代號，例如 AAPL NVDA")
    async def watchadd(ctx, *, tickers: str):
        if not await _guard_channel(ctx):
            return
        try:
            updated = add_saved_watchlist_tickers(_ctx_user_id(ctx), tickers)
        except ValueError as exc:
            await ctx.send(f"指令失敗: {exc}")
            return
        await ctx.send("已更新關注股:\n" + "\n".join(f"- {ticker}" for ticker in updated))

    @bot.hybrid_command(name="watchremove", description="把股票從你的保存關注清單移除")
    @app_commands.describe(tickers="以空白或逗號分隔股票代號，例如 AAPL NVDA")
    async def watchremove(ctx, *, tickers: str):
        if not await _guard_channel(ctx):
            return
        try:
            updated = remove_saved_watchlist_tickers(_ctx_user_id(ctx), tickers)
        except ValueError as exc:
            await ctx.send(f"指令失敗: {exc}")
            return
        if not updated:
            await ctx.send("你的保存關注股目前已清空。")
            return
        await ctx.send("移除後關注股:\n" + "\n".join(f"- {ticker}" for ticker in updated))

    @bot.hybrid_command(name="watchsaved", description="查看你保存的關注股清單")
    async def watchsaved(ctx):
        if not await _guard_channel(ctx):
            return
        await ctx.send(format_saved_watchlist_message(_ctx_user_id(ctx)))

    @bot.hybrid_command(name="recap", description="手動觸發 recap 結論卡（bedtime/morning/opening/watchlist）")
    @app_commands.describe(
        mode="bedtime/morning/opening/watchlist",
        debug="是否附上 Gemini/Tavily/新聞覆蓋檢查資訊",
        tickers="mode=watchlist 時可選，額外加入股票，例如 AAPL NVDA",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="睡前 bedtime", value="bedtime"),
            app_commands.Choice(name="早晨 morning", value="morning"),
            app_commands.Choice(name="開盤 opening", value="opening"),
            app_commands.Choice(name="追蹤 watchlist", value="watchlist"),
        ]
    )
    async def recap(ctx, mode: str = "bedtime", debug: bool = False, *, tickers: str = ""):
        if not await _guard_channel(ctx):
            return
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            await ctx.defer()

        mode_text = str(mode or "bedtime").strip().lower() or "bedtime"
        if mode_text == "watchlist":
            try:
                message = await _build_watchlist_recap_output(ctx, tickers=tickers, debug=bool(debug), trigger="recap")
            except asyncio.TimeoutError:
                await ctx.send("指令逾時：watchlist 外部資料回應太慢，請稍後再試。")
                return
            except ValueError as exc:
                await ctx.send(f"指令失敗: {exc}")
                return
            await _send_long_message(ctx, message)
            return

        try:
            preview = await asyncio.wait_for(
                asyncio.to_thread(
                    build_recap_message_preview,
                    mode=mode_text,
                    top_n=5,
                    tags={"keep", "watch"},
                    respect_mode_window=False,
                ),
                timeout=BLOCKING_API_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            _save_recap_status(
                {
                    "ok": False,
                    "mode": mode_text,
                    "reason_code": "timeout_blocking_api",
                    "decision_date": "unknown",
                    "source_id": "",
                    "note": "manual recap timeout",
                }
            )
            await ctx.send("指令逾時：recap 外部資料回應太慢，請稍後再試。")
            return
        except ValueError as exc:
            await ctx.send(f"指令失敗: {exc}")
            return

        if not bool(preview.get("ok")):
            _save_recap_status(
                {
                    "ok": False,
                    "mode": mode_text,
                    "reason_code": str(preview.get("skip_reason", "preview_error")).strip() or "preview_error",
                    "decision_date": str(preview.get("decision_date", "unknown")),
                    "source_id": str(preview.get("source_id", "")),
                    "note": "preview not ok",
                }
            )
            await ctx.send(f"無法產生 recap: {preview.get('skip_reason', 'unknown')}")
            return

        message = str(preview.get("message", "")).strip()
        pipe = preview.get("pipeline_debug", {}) if isinstance(preview.get("pipeline_debug"), dict) else {}
        _save_recap_status(
            {
                "ok": True,
                "mode": str(preview.get("mode", mode_text)),
                "reason_code": _derive_recap_reason_code(str(preview.get("mode", mode_text)), pipe, bool(pipe.get("ai_summary_generated"))),
                "decision_date": str(preview.get("decision_date", "unknown")),
                "source_id": str(preview.get("source_id", "")),
                "gemini_enabled": bool(pipe.get("gemini_enabled")),
                "tavily_enabled": bool(pipe.get("tavily_enabled")),
                "ai_summary_generated": bool(pipe.get("ai_summary_generated")),
                "tracked_news_count": int(pipe.get("tracked_news_count", 0) or 0),
                "conflict_news_count": int(pipe.get("conflict_news_count", 0) or 0),
                "has_prior_bedtime_plan": bool(pipe.get("has_prior_bedtime_plan")),
                "has_prior_morning_plan": bool(pipe.get("has_prior_morning_plan")),
                "execution_summary_source": str(pipe.get("execution_summary_source", "")),
                "note": "manual recap",
            }
        )

        if debug:
            debug_lines = [
                "",
                "[Recap Debug]",
                f"mode={preview.get('mode', mode_text)}",
                f"decision_date={preview.get('decision_date', 'unknown')}",
                f"source_id={preview.get('source_id', '')}",
                f"gemini_enabled={bool(pipe.get('gemini_enabled'))}",
                f"tavily_enabled={bool(pipe.get('tavily_enabled'))}",
                f"ai_summary_generated={bool(pipe.get('ai_summary_generated'))}",
                f"tracked_news_count={int(pipe.get('tracked_news_count', 0) or 0)}",
                f"conflict_news_count={int(pipe.get('conflict_news_count', 0) or 0)}",
                f"has_prior_bedtime_plan={bool(pipe.get('has_prior_bedtime_plan'))}",
                f"has_prior_morning_plan={bool(pipe.get('has_prior_morning_plan'))}",
                f"execution_summary_source={str(pipe.get('execution_summary_source', ''))}",
            ]
            message = message + "\n" + "\n".join(debug_lines)

        await _send_long_message(ctx, message)

    @bot.hybrid_command(name="recapstatus", description="查看最近一次 recap 命中摘要與原因碼（不觸發 API）")
    async def recapstatus(ctx):
        if not await _guard_channel(ctx):
            return
        status = _load_recap_status()
        await ctx.send(_build_recapstatus_message(status))

    async def _record_trade(ctx, side: str, ticker: str, quantity: float, price: float, note: str = "", profile: str = "monster"):
        if not await _guard_channel(ctx):
            return
        # Slash command needs an early ACK to avoid the "application did not respond" toast.
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            await ctx.defer()
        positions_df = load_positions()
        strategy_profile, horizon_tag = _normalize_profile(profile)
        try:
            updated_df, ledger_row = apply_trade_fill(
                positions_df=positions_df,
                ticker=ticker,
                side=side,
                quantity=float(quantity),
                price=float(price),
                horizon_tag=horizon_tag,
                strategy_profile=strategy_profile,
                signal_type=f"manual_{side}",
                source="discord_bot",
                note=note,
            )
        except ValueError as exc:
            if side == "sell" and "no open position for" in str(exc):
                ticker_norm = str(ticker or "").strip().upper()
                open_rows = positions_df[
                    (positions_df["ticker"] == ticker_norm)
                    & (pd.to_numeric(positions_df.get("quantity", 0.0), errors="coerce").fillna(0.0) > 0)
                ].copy()
                if len(open_rows) > 0:
                    lines = [
                        f"指令失敗: {exc}",
                        f"你現在送的是 profile={str(profile or 'monster').strip().lower() or 'monster'}，但 {ticker_norm} 可用部位是：",
                    ]
                    for _, row in open_rows.sort_values(["horizon_tag", "strategy_profile"]).iterrows():
                        lines.append(
                            f"- {row.get('horizon_tag', '')}/{row.get('strategy_profile', '')}"
                            f" (profile={_strategy_to_profile_label(str(row.get('strategy_profile', '')))})"
                            f" | qty={float(row.get('quantity', 0.0)):g} | avg={float(row.get('avg_cost', 0.0)):.2f}"
                        )
                    lines.append("請用相同 profile 重送 /sell，或先用 /position ticker 確認。")
                    await ctx.send("\n".join(lines))
                    return
            await ctx.send(f"指令失敗: {exc}")
            return

        save_positions(updated_df)
        append_trade_ledger(ledger_row)
        refreshed = load_positions()
        current = refreshed[
            (refreshed["ticker"] == ticker.upper())
            & (refreshed["horizon_tag"] == horizon_tag)
            & (refreshed["strategy_profile"] == strategy_profile)
        ]
        if len(current) > 0:
            position_row = current.iloc[0]
            avg_cost = float(position_row.get("avg_cost", 0.0))
            add_count = int(pd.to_numeric(position_row.get("add_count", 0), errors="coerce") or 0)
        else:
            avg_cost = float(ledger_row.get("avg_cost_after", 0.0))
            add_count = 0
        await ctx.send(
            f"已記錄 {side.upper()} {ticker.upper()} | {horizon_tag}/{strategy_profile} | qty={float(quantity):g} | price={float(price):.2f} | after_qty={float(ledger_row['after_qty']):g} | avg={avg_cost:.2f} | add_count={add_count}\n"
            "後續 engine / recap 會直接沿用這個持倉狀態。"
        )

    @bot.hybrid_command(name="buy", description="記錄新的買進成交")
    @app_commands.describe(ticker="股票代號，例如 AAPL", quantity="成交股數", price="成交價格", note="備註，可留空", profile="monster 或 swing")
    async def buy(ctx, ticker: str, quantity: float, price: float, *, note: str = "", profile: str = "monster"):
        await _record_trade(ctx, "buy", ticker, quantity, price, note, profile)

    @bot.hybrid_command(name="add", description="記錄加碼成交")
    @app_commands.describe(ticker="股票代號，例如 AAPL", quantity="成交股數", price="成交價格", note="備註，可留空", profile="monster 或 swing")
    async def add(ctx, ticker: str, quantity: float, price: float, *, note: str = "", profile: str = "monster"):
        await _record_trade(ctx, "add", ticker, quantity, price, note, profile)

    @bot.hybrid_command(name="sell", description="記錄賣出成交")
    @app_commands.describe(ticker="股票代號，例如 AAPL", quantity="成交股數", price="成交價格", note="備註，可留空", profile="monster 或 swing")
    async def sell(ctx, ticker: str, quantity: float, price: float, *, note: str = "", profile: str = "monster"):
        await _record_trade(ctx, "sell", ticker, quantity, price, note, profile)

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("參數格式不正確，請用 /tradehelp 或 !tradehelp 查看範例。")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("參數不足，請用 /tradehelp 或 !tradehelp 查看範例。")
            return
        raise error

    bot.run(DISCORD_BOT_TOKEN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())