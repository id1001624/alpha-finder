from __future__ import annotations

from config import LEVERAGED_ETF_MAP


def resolve_underlying_ticker(ticker: str) -> str:
    display = str(ticker or "").strip().upper()
    if not display:
        return ""
    return str(LEVERAGED_ETF_MAP.get(display, display)).strip().upper() or display


def format_ticker_with_underlying(display_ticker: str, underlying_ticker: str) -> str:
    display = str(display_ticker or "").strip().upper()
    underlying = str(underlying_ticker or "").strip().upper()
    if not display:
        return ""
    if not underlying or underlying == display:
        return display
    return f"{display}（標的：{underlying}）"
