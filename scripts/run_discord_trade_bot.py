from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import get_logger

from ai_trading.position_state import append_trade_ledger, apply_trade_fill, load_positions, save_positions, get_position
from ai_trading.strategy_context import (
    HORIZON_INTRADAY_MONSTER,
    HORIZON_SWING_CORE,
    STRATEGY_MONSTER_SWING,
    STRATEGY_SWING_TREND,
)
from ai_trading.watchlist_brief import (
    add_saved_watchlist_tickers,
    build_watchlist_brief_message,
    format_saved_watchlist_message,
    load_saved_watchlist,
    remove_saved_watchlist_tickers,
)
from config import (
    DISCORD_BOT_ALLOWED_CHANNEL_IDS,
    DISCORD_BOT_ENABLED,
    DISCORD_BOT_PREFIX,
    DISCORD_BOT_SYNC_GUILD_ID,
    DISCORD_BOT_TOKEN,
)
from turso_state import load_recent_execution_log, load_recent_trade_ledger

logger = get_logger(__name__)


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
        f"{DISCORD_BOT_PREFIX}watchsaved\n\n"
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

    @bot.event
    async def on_ready():
        logger.info("Discord trade bot ready: %s", bot.user)

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
        row = positions_df[positions_df["ticker"] == ticker.strip().upper()]
        if len(row) == 0:
            await ctx.send(f"{ticker.upper()} 目前沒有開倉部位。")
            return
        record = row.iloc[0]
        await ctx.send(
            f"{record['ticker']} | qty={float(record['quantity']):g} | avg={float(record['avg_cost']):.2f} | realized={float(record['realized_pnl']):.2f}"
        )

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

    @bot.hybrid_command(name="watchlist", description="整合 ai_decision、持倉與你的關注股，輸出乾淨的盤前排序")
    @app_commands.describe(tickers="可選，額外加入的股票代號，例如 AAPL NVDA TSLA")
    async def watchlist(ctx, *, tickers: str = ""):
        if not await _guard_channel(ctx):
            return
        if getattr(ctx, "interaction", None) is not None:
            await ctx.defer()
        saved_tickers = load_saved_watchlist(_ctx_user_id(ctx))
        try:
            message = build_watchlist_brief_message(raw_tickers=tickers, saved_tickers=saved_tickers)
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

    async def _record_trade(ctx, side: str, ticker: str, quantity: float, price: float, note: str = "", profile: str = "monster"):
        if not await _guard_channel(ctx):
            return
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
            # If selling and we couldn't find an open position for the given profile,
            # try a fallback: locate any existing position for the ticker (ticker-only match)
            # and reuse its horizon/profile so the sell can apply to the real open position.
            if str(side).strip().lower() == "sell" and "no open position for" in str(exc).lower():
                existing_any = get_position(positions_df, ticker)
                if existing_any is not None:
                    fallback_strategy = existing_any.get("strategy_profile", "")
                    fallback_horizon = existing_any.get("horizon_tag", "")
                    try:
                        updated_df, ledger_row = apply_trade_fill(
                            positions_df=positions_df,
                            ticker=ticker,
                            side=side,
                            quantity=float(quantity),
                            price=float(price),
                            horizon_tag=fallback_horizon,
                            strategy_profile=fallback_strategy,
                            signal_type=f"manual_{side}",
                            source="discord_bot",
                            note=note,
                        )
                    except ValueError as exc2:
                        await ctx.send(f"指令失敗: {exc2}")
                        return
                else:
                    await ctx.send(f"指令失敗: {exc}")
                    return
            else:
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