from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.position_state import append_trade_ledger, apply_trade_fill, load_positions, save_positions
from config import (
    DISCORD_BOT_ALLOWED_CHANNEL_IDS,
    DISCORD_BOT_ENABLED,
    DISCORD_BOT_PREFIX,
    DISCORD_BOT_SYNC_GUILD_ID,
    DISCORD_BOT_TOKEN,
)
from turso_state import load_recent_execution_log, load_recent_trade_ledger


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
    for _, row in positions.sort_values(["ticker"]).iterrows():
        lines.append(
            f"- {row['ticker']} | qty={float(row['quantity']):g} | avg={float(row['avg_cost']):.2f} | add_count={int(row['add_count'])}"
        )
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "可用指令:\n"
        "/buy ticker quantity price [note]\n"
        "/add ticker quantity price [note]\n"
        "/sell ticker quantity price [note]\n"
        "/positions\n"
        "/position ticker\n"
        "/trades [ticker] [limit]\n"
        "/executions [ticker] [limit]\n\n"
        "也保留文字指令相容:\n"
        f"{DISCORD_BOT_PREFIX}buy AAPL 100 188.2\n"
        f"{DISCORD_BOT_PREFIX}add AAPL 50 190.1\n"
        f"{DISCORD_BOT_PREFIX}sell AAPL 80 196.5\n"
        f"{DISCORD_BOT_PREFIX}positions\n"
        f"{DISCORD_BOT_PREFIX}position AAPL\n"
        f"{DISCORD_BOT_PREFIX}trades AAPL 5\n"
        f"{DISCORD_BOT_PREFIX}executions AAPL 5"
    )


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
        print("DISCORD_BOT_ENABLED is false.")
        return 2
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN is missing.")
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
                print(f"Discord trade bot synced {len(synced)} global command(s).")
                return

            try:
                guild = discord.Object(id=int(sync_guild_raw))
            except ValueError:
                synced = await self.tree.sync()
                print(f"DISCORD_BOT_SYNC_GUILD_ID invalid, fallback to global sync ({len(synced)} command(s)).")
                return

            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Discord trade bot synced {len(synced)} guild command(s) to {sync_guild_raw}.")

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
        print(f"Discord trade bot ready: {bot.user}")

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

    async def _record_trade(ctx, side: str, ticker: str, quantity: float, price: float, note: str = ""):
        if not await _guard_channel(ctx):
            return
        positions_df = load_positions()
        try:
            updated_df, ledger_row = apply_trade_fill(
                positions_df=positions_df,
                ticker=ticker,
                side=side,
                quantity=float(quantity),
                price=float(price),
                source="discord_bot",
                note=note,
            )
        except ValueError as exc:
            await ctx.send(f"指令失敗: {exc}")
            return

        save_positions(updated_df)
        append_trade_ledger(ledger_row)
        await ctx.send(
            f"已記錄 {side.upper()} {ticker.upper()} | qty={float(quantity):g} | price={float(price):.2f} | after_qty={float(ledger_row['after_qty']):g}"
        )

    @bot.hybrid_command(name="buy", description="記錄新的買進成交")
    @app_commands.describe(ticker="股票代號，例如 AAPL", quantity="成交股數", price="成交價格", note="備註，可留空")
    async def buy(ctx, ticker: str, quantity: float, price: float, *, note: str = ""):
        await _record_trade(ctx, "buy", ticker, quantity, price, note)

    @bot.hybrid_command(name="add", description="記錄加碼成交")
    @app_commands.describe(ticker="股票代號，例如 AAPL", quantity="成交股數", price="成交價格", note="備註，可留空")
    async def add(ctx, ticker: str, quantity: float, price: float, *, note: str = ""):
        await _record_trade(ctx, "add", ticker, quantity, price, note)

    @bot.hybrid_command(name="sell", description="記錄賣出成交")
    @app_commands.describe(ticker="股票代號，例如 AAPL", quantity="成交股數", price="成交價格", note="備註，可留空")
    async def sell(ctx, ticker: str, quantity: float, price: float, *, note: str = ""):
        await _record_trade(ctx, "sell", ticker, quantity, price, note)

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