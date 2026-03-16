from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEMORY_PATH = PROJECT_ROOT / "repo_outputs" / "backtest" / "trade_memory.json"
VECTOR_DIM = 64


class TradeMemory:
    def __init__(self, memory_path: Path | None = None):
        self.memory_path = memory_path or DEFAULT_MEMORY_PATH
        self._records: List[dict] = []
        self._record_keys: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.memory_path.exists():
            self._records = []
            self._record_keys = set()
            return
        try:
            payload = json.loads(self.memory_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            self._records = []
            self._record_keys = set()
            return
        records = payload.get("records", []) if isinstance(payload, dict) else []
        if not isinstance(records, list):
            records = []
        self._records = [row for row in records if isinstance(row, dict)]
        self._record_keys = {
            str(row.get("memory_key", "")).strip()
            for row in self._records
            if str(row.get("memory_key", "")).strip()
        }

    def _save(self) -> None:
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "record_count": len(self._records),
            "records": self._records[-3000:],
        }
        self.memory_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        parsed = pd.to_numeric(value, errors="coerce")
        if pd.isna(parsed):
            return default
        return float(parsed)

    @staticmethod
    def _clip_text(value: object, limit: int = 120) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    @staticmethod
    def _is_completed_trade(row: pd.Series | dict) -> bool:
        action = str((row.get("action") if isinstance(row, dict) else row.get("action", "")) or "").strip().lower()
        position_effect = str((row.get("position_effect") if isinstance(row, dict) else row.get("position_effect", "")) or "").strip().lower()
        if action in {"stop_loss", "take_profit"}:
            return True
        return position_effect in {"close", "reduce"}

    @staticmethod
    def _outcome_label(row: pd.Series | dict) -> str:
        action = str((row.get("action") if isinstance(row, dict) else row.get("action", "")) or "").strip().lower()
        realized_pct = pd.to_numeric((row.get("realized_pct") if isinstance(row, dict) else row.get("realized_pct", "")), errors="coerce")
        if pd.notna(realized_pct):
            if float(realized_pct) > 0.0:
                return "win"
            if float(realized_pct) < 0.0:
                return "loss"
            return "flat"
        if action == "take_profit":
            return "win"
        if action == "stop_loss":
            return "loss"
        return "unknown"

    @staticmethod
    def _row_key(row: pd.Series | dict) -> str:
        ticker = str((row.get("ticker") if isinstance(row, dict) else row.get("ticker", "")) or "").strip().upper()
        action = str((row.get("action") if isinstance(row, dict) else row.get("action", "")) or "").strip().lower()
        signal_ts = str((row.get("signal_ts") if isinstance(row, dict) else row.get("signal_ts", "")) or "").strip()
        timeframe = str((row.get("timeframe") if isinstance(row, dict) else row.get("timeframe", "")) or "").strip()
        return f"{ticker}|{action}|{signal_ts}|{timeframe}"

    @staticmethod
    def _compose_row_text(row: pd.Series | dict) -> str:
        fields = [
            str((row.get("ticker") if isinstance(row, dict) else row.get("ticker", "")) or "").strip().upper(),
            str((row.get("action") if isinstance(row, dict) else row.get("action", "")) or "").strip().lower(),
            str((row.get("signal_type") if isinstance(row, dict) else row.get("signal_type", "")) or "").strip().lower(),
            str((row.get("regime_tag") if isinstance(row, dict) else row.get("regime_tag", "")) or "").strip().lower(),
            str((row.get("sqzmom_color") if isinstance(row, dict) else row.get("sqzmom_color", "")) or "").strip().lower(),
            str((row.get("reason_summary") if isinstance(row, dict) else row.get("reason_summary", "")) or "").strip().lower(),
        ]
        return " | ".join(part for part in fields if part)

    @staticmethod
    def _hash_token(token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    @classmethod
    def _embed_text(cls, text: str) -> List[float]:
        vec = [0.0] * VECTOR_DIM
        if not text:
            return vec
        tokens = [token for token in text.lower().replace("|", " ").split() if token]
        if not tokens:
            return vec
        for token in tokens:
            idx = cls._hash_token(token) % VECTOR_DIM
            vec[idx] += 1.0
        norm = math.sqrt(sum(value * value for value in vec))
        if norm <= 0:
            return vec
        return [value / norm for value in vec]

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        if not vec_a or not vec_b:
            return 0.0
        if len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return float(dot / (norm_a * norm_b))

    def sync_completed_trades(self, execution_df: pd.DataFrame) -> int:
        if len(execution_df) == 0:
            return 0
        added = 0
        for _, row in execution_df.iterrows():
            if not self._is_completed_trade(row):
                continue
            key = self._row_key(row)
            if not key or key in self._record_keys:
                continue
            content_text = self._compose_row_text(row)
            embedding = self._embed_text(content_text)
            record = {
                "memory_key": key,
                "ticker": str(row.get("ticker", "")).strip().upper(),
                "action": str(row.get("action", "")).strip().lower(),
                "signal_type": str(row.get("signal_type", "")).strip().lower(),
                "reason_summary": self._clip_text(row.get("reason_summary") or "", 180),
                "outcome": self._outcome_label(row),
                "recorded_at": str(row.get("recorded_at") or row.get("signal_ts") or "").strip(),
                "embedding": embedding,
            }
            self._records.append(record)
            self._record_keys.add(key)
            added += 1
        if added > 0:
            self._save()
        return added

    def find_similar_trades(self, ticker: str, context: Dict[str, Any], top_k: int = 3, min_similarity: float = 0.28) -> List[dict]:
        if not self._records:
            return []

        ticker_text = str(ticker or "").strip().upper()
        context_text = " | ".join(
            [
                ticker_text,
                str(context.get("signal_type", "")).strip().lower(),
                str(context.get("decision_tag", "")).strip().lower(),
                str(context.get("regime_tag", "")).strip().lower(),
                str(context.get("sqzmom_color", "")).strip().lower(),
                self._clip_text(context.get("reason_summary") or "", 120).lower(),
            ]
        )
        query_vec = self._embed_text(context_text)

        scored: List[tuple[float, dict]] = []
        same_ticker_records = [row for row in self._records if str(row.get("ticker", "")).strip().upper() == ticker_text]
        candidates = same_ticker_records if same_ticker_records else self._records

        for row in candidates:
            embedding = row.get("embedding", [])
            if not isinstance(embedding, list):
                continue
            similarity = self._cosine_similarity(query_vec, [float(v) for v in embedding])
            if similarity < float(min_similarity):
                continue
            scored.append((similarity, row))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: List[dict] = []
        for similarity, row in scored[: max(1, int(top_k))]:
            out.append(
                {
                    "ticker": str(row.get("ticker", "")).strip().upper(),
                    "action": str(row.get("action", "")).strip().lower(),
                    "signal_type": str(row.get("signal_type", "")).strip().lower(),
                    "outcome": str(row.get("outcome", "unknown")).strip().lower(),
                    "similarity": round(float(similarity), 4),
                    "reason_summary": self._clip_text(row.get("reason_summary") or "", 100),
                    "recorded_at": str(row.get("recorded_at", "")).strip(),
                }
            )
        return out


@lru_cache(maxsize=1)
def _get_default_trade_memory() -> TradeMemory:
    return TradeMemory(memory_path=None)


def get_trade_memory(memory_path: Path | None = None) -> TradeMemory:
    if memory_path is None:
        return _get_default_trade_memory()
    return TradeMemory(memory_path=memory_path)
