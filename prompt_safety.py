from __future__ import annotations

import re
from typing import Any


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def sanitize_prompt_text(value: Any, *, max_length: int = 500) -> str:
    text = str(value or "")
    text = text.replace("\ufeff", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS_RE.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if max_length > 0 and len(text) > max_length:
        return text[: max_length - 3].rstrip() + "..."
    return text


def sanitize_ticker(value: Any, *, max_length: int = 16) -> str:
    text = re.sub(r"[^A-Z0-9._-]", "", str(value or "").upper())
    return text[:max_length]


def sanitize_prompt_payload(value: Any):
    if isinstance(value, dict):
        return {str(key): sanitize_prompt_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_prompt_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_prompt_text(value, max_length=600)