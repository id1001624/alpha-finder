from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9\.\-]{0,9}$")


@dataclass(frozen=True)
class DataPaths:
    raw_market_csv: str
    monster_radar_csv: str
    xq_updated_csv: str
    ai_focus_csv: str
    fusion_csv: Optional[str] = None


def normalize_ticker(value) -> str:
    text = str(value).strip().upper()
    text = text.replace('.US', '')
    if not text or text in {'NAN', 'NONE', 'NULL'}:
        return ''
    if not TICKER_PATTERN.match(text):
        return ''
    return text


def parse_human_market_cap(value) -> float:
    if value is None:
        return 0.0

    text = str(value).strip().upper().replace(',', '')
    if not text or text in {'NAN', 'NONE', '-'}:
        return 0.0

    factor = 1.0
    if text.endswith('T'):
        factor = 1_000_000_000_000.0
        text = text[:-1]
    elif text.endswith('B'):
        factor = 1_000_000_000.0
        text = text[:-1]
    elif text.endswith('M'):
        factor = 1_000_000.0
        text = text[:-1]
    elif text.endswith('K'):
        factor = 1_000.0
        text = text[:-1]

    try:
        return float(text) * factor
    except (TypeError, ValueError):
        return 0.0


def parse_probability_mid(value) -> float:
    text = str(value).strip().replace('%', '')
    if not text or text in {'NAN', 'NONE'}:
        return 0.0

    if '-' in text:
        left, right = text.split('-', 1)
        try:
            return (float(left) + float(right)) / 2.0
        except ValueError:
            return 0.0

    try:
        return float(text)
    except ValueError:
        return 0.0
