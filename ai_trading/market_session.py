from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import (
    INTRADAY_ACTIVE_LAG_MINUTES,
    INTRADAY_ACTIVE_LEAD_MINUTES,
    INTRADAY_ACTIVE_TIMEZONE,
    INTRADAY_MARKET_CLOSE_TIME,
    INTRADAY_MARKET_OPEN_TIME,
    INTRADAY_MARKET_TIMEZONE,
)


def get_zoneinfo(name: str, fallback: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or fallback).strip() or fallback)
    except ZoneInfoNotFoundError:
        return ZoneInfo(fallback)


def parse_hhmm(value: str, fallback: str) -> dt_time:
    raw = str(value or fallback).strip()
    try:
        hour_str, minute_str = raw.split(":", 1)
        return dt_time(hour=int(hour_str), minute=int(minute_str))
    except (ValueError, TypeError):
        hour_str, minute_str = fallback.split(":", 1)
        return dt_time(hour=int(hour_str), minute=int(minute_str))


def _normalize_utc(now_utc: datetime | None = None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def get_intraday_active_window(now_utc: datetime | None = None) -> Dict[str, object]:
    current_utc = _normalize_utc(now_utc)
    active_tz = get_zoneinfo(INTRADAY_ACTIVE_TIMEZONE, "Asia/Taipei")
    market_tz = get_zoneinfo(INTRADAY_MARKET_TIMEZONE, "America/New_York")
    market_now = current_utc.astimezone(market_tz)

    market_open = parse_hhmm(INTRADAY_MARKET_OPEN_TIME, "09:30")
    market_close = parse_hhmm(INTRADAY_MARKET_CLOSE_TIME, "16:00")
    market_date = market_now.date()
    start_market = datetime.combine(market_date, market_open, tzinfo=market_tz) - timedelta(minutes=max(0, int(INTRADAY_ACTIVE_LEAD_MINUTES)))
    end_market = datetime.combine(market_date, market_close, tzinfo=market_tz) + timedelta(minutes=max(0, int(INTRADAY_ACTIVE_LAG_MINUTES)))

    is_market_day = market_now.weekday() < 5
    is_active = is_market_day and start_market <= market_now <= end_market

    return {
        "now_utc": current_utc,
        "now_local": current_utc.astimezone(active_tz),
        "active_timezone": str(INTRADAY_ACTIVE_TIMEZONE or "Asia/Taipei"),
        "market_timezone": str(INTRADAY_MARKET_TIMEZONE or "America/New_York"),
        "market_date": market_date.isoformat(),
        "market_open_local": datetime.combine(market_date, market_open, tzinfo=market_tz),
        "market_close_local": datetime.combine(market_date, market_close, tzinfo=market_tz),
        "market_open_utc": datetime.combine(market_date, market_open, tzinfo=market_tz).astimezone(timezone.utc),
        "market_close_utc": datetime.combine(market_date, market_close, tzinfo=market_tz).astimezone(timezone.utc),
        "active_start_local": start_market.astimezone(active_tz),
        "active_end_local": end_market.astimezone(active_tz),
        "active_start_utc": start_market.astimezone(timezone.utc),
        "active_end_utc": end_market.astimezone(timezone.utc),
        "is_market_day": is_market_day,
        "is_active": is_active,
    }


def is_in_intraday_active_window(now_utc: datetime | None = None) -> bool:
    return bool(get_intraday_active_window(now_utc).get("is_active", False))