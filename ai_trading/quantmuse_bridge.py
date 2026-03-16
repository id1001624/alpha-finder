from __future__ import annotations

import importlib
import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import List

import pandas as pd


LOGGER = logging.getLogger(__name__)


def _safe_float(value: object, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return default
    return float(parsed)


def _clip_text(value: object, limit: int = 140) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _ensure_quantmuse_path() -> None:
    custom_path = str(os.getenv("QUANTMUSE_PATH", "")).strip()
    if not custom_path:
        return
    try:
        resolved = str(Path(custom_path).resolve())
    except OSError:
        return
    if resolved and resolved not in sys.path:
        sys.path.insert(0, resolved)


def _is_quantmuse_enabled() -> bool:
    raw = str(os.getenv("QUANTMUSE_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


@lru_cache(maxsize=1)
def _load_quantmuse_components() -> dict:
    if not _is_quantmuse_enabled():
        return {
            "available": False,
            "sentiment_analyzer_cls": None,
            "nlp_processor_cls": None,
            "llm_integration_cls": None,
            "langchain_agent_cls": None,
            "reason": "disabled_by_env",
        }

    _ensure_quantmuse_path()
    components = {
        "available": False,
        "sentiment_analyzer_cls": None,
        "nlp_processor_cls": None,
        "llm_integration_cls": None,
        "langchain_agent_cls": None,
        "reason": "import_failed",
        "module_name": "",
    }
    last_error = ""
    module_candidates = ["data_service.ai", "quantmuse.data_service.ai"]
    for module_name in module_candidates:
        try:
            ai_module = importlib.import_module(module_name)
            components["module_name"] = module_name
            components["sentiment_analyzer_cls"] = getattr(ai_module, "SentimentAnalyzer", None)
            components["nlp_processor_cls"] = getattr(ai_module, "NLPProcessor", None)
            components["llm_integration_cls"] = getattr(ai_module, "LLMIntegration", None)
            components["langchain_agent_cls"] = getattr(ai_module, "LangChainAgent", None)
            components["available"] = bool(
                components["sentiment_analyzer_cls"]
                and components["nlp_processor_cls"]
            )
            if components["available"]:
                components["reason"] = "ok"
                break
            components["reason"] = "missing_required_classes"
            break
        except (ImportError, ModuleNotFoundError, AttributeError, RuntimeError, TypeError, ValueError, OSError) as exc:  # pragma: no cover - optional dependency
            LOGGER.debug("QuantMuse module %s unavailable: %s", module_name, exc)
            last_error = exc.__class__.__name__

    if not components.get("module_name") and last_error:
        components["reason"] = f"import_error:{last_error}"
    return components


def get_quantmuse_capabilities() -> dict:
    components = _load_quantmuse_components()
    return {
        "enabled": _is_quantmuse_enabled(),
        "available": bool(components.get("available", False)),
        "has_langchain": bool(components.get("langchain_agent_cls") and components.get("llm_integration_cls")),
        "reason": str(components.get("reason", "unknown")).strip() or "unknown",
        "module_name": str(components.get("module_name", "")).strip(),
        "quantmuse_path": str(os.getenv("QUANTMUSE_PATH", "")).strip(),
        "llm_provider": str(os.getenv("QUANTMUSE_LLM_PROVIDER", "local")).strip().lower() or "local",
    }


def _heuristic_sentiment(text: str) -> tuple[float, float]:
    positive_words = {
        "beat", "beats", "strong", "surge", "growth", "upgrade", "record", "expand", "win", "bullish", "outperform",
    }
    negative_words = {
        "miss", "misses", "weak", "drop", "fall", "downgrade", "lawsuit", "delay", "cut", "bearish", "underperform",
    }
    words = re.findall(r"[a-zA-Z]+", str(text or "").lower())
    if not words:
        return 0.0, 0.0
    pos = sum(1 for w in words if w in positive_words)
    neg = sum(1 for w in words if w in negative_words)
    total = pos + neg
    if total == 0:
        return 0.0, 0.2
    score = (pos - neg) / float(total)
    confidence = min(0.9, 0.35 + total * 0.08)
    return float(max(-1.0, min(1.0, score))), float(confidence)


def analyze_overnight_sentiment(ticker: str, snippets: List[dict]) -> dict:
    texts = []
    for row in snippets[:8]:
        title = str(row.get("title", "")).strip()
        content = str(row.get("content", "")).strip()
        merged = f"{title} {content}".strip()
        if merged:
            texts.append(merged)

    if not texts:
        return {
            "sentiment_score": 0.0,
            "sentiment_confidence": 0.0,
            "sentiment_label": "neutral",
            "sentiment_source": "none",
            "keywords": [],
        }

    components = _load_quantmuse_components()
    if bool(components.get("available", False)):
        try:
            nlp_cls = components.get("nlp_processor_cls")
            sa_cls = components.get("sentiment_analyzer_cls")
            nlp = nlp_cls(use_spacy=False, use_transformers=False) if nlp_cls else None
            analyzer = sa_cls(openai_api_key=None, use_openai=False) if sa_cls else None

            weighted_sum = 0.0
            total_weight = 0.0
            keywords: List[str] = []
            for text in texts:
                score = 0.0
                confidence = 0.5
                if analyzer is not None:
                    result = analyzer.analyze_text_sentiment(text, symbol=ticker)
                    score = _safe_float(getattr(result, "sentiment_score", 0.0), 0.0)
                    confidence = max(0.05, _safe_float(getattr(result, "confidence", 0.5), 0.5))
                    result_keywords = getattr(result, "keywords", [])
                    if isinstance(result_keywords, list):
                        keywords.extend(str(item).strip().lower() for item in result_keywords[:5] if str(item).strip())
                if nlp is not None:
                    processed = nlp.preprocess_text(text)
                    keywords.extend(str(item).strip().lower() for item in getattr(processed, "keywords", [])[:5] if str(item).strip())
                weighted_sum += score * confidence
                total_weight += confidence

            sentiment_score = weighted_sum / total_weight if total_weight > 0 else 0.0
            sentiment_confidence = min(1.0, total_weight / max(len(texts), 1))
            sentiment_label = "positive" if sentiment_score > 0.08 else "negative" if sentiment_score < -0.08 else "neutral"
            clean_keywords = []
            for kw in keywords:
                if kw and kw not in clean_keywords:
                    clean_keywords.append(kw)
            return {
                "sentiment_score": float(max(-1.0, min(1.0, sentiment_score))),
                "sentiment_confidence": float(max(0.0, min(1.0, sentiment_confidence))),
                "sentiment_label": sentiment_label,
                "sentiment_source": "quantmuse",
                "keywords": clean_keywords[:10],
            }
        except (AttributeError, RuntimeError, TypeError, ValueError, OSError, KeyError, ImportError) as exc:  # pragma: no cover - optional dependency
            LOGGER.debug("QuantMuse sentiment failed for %s: %s", ticker, exc)

    merged_text = " ".join(texts)
    score, confidence = _heuristic_sentiment(merged_text)
    sentiment_label = "positive" if score > 0.08 else "negative" if score < -0.08 else "neutral"
    return {
        "sentiment_score": score,
        "sentiment_confidence": confidence,
        "sentiment_label": sentiment_label,
        "sentiment_source": "heuristic",
        "keywords": [],
    }


def blend_overnight_impact(impact: str, sentiment_score: float, sentiment_confidence: float) -> str:
    base = str(impact or "").strip().lower()
    allowed = {"hard_positive", "soft_positive", "neutral", "soft_negative", "hard_negative"}
    if base not in allowed:
        base = "neutral"

    score = float(sentiment_score)
    conf = float(sentiment_confidence)
    if conf < 0.35:
        return base

    if base == "neutral":
        if score >= 0.45:
            return "soft_positive"
        if score <= -0.45:
            return "soft_negative"
        return base

    if base in {"soft_positive", "hard_positive"} and score <= -0.35:
        return "neutral" if base == "soft_positive" else "neutral"
    if base in {"soft_negative", "hard_negative"} and score >= 0.35:
        return "neutral" if base == "soft_negative" else "neutral"
    return base


def _rule_based_swing_strategy(decision_df: pd.DataFrame, positions_df: pd.DataFrame, execution_summaries: List[dict], max_symbols: int) -> dict:
    swing_df = decision_df.copy()
    for col in ["ticker", "decision_tag", "risk_level", "horizon_tag", "strategy_profile", "rank"]:
        if col not in swing_df.columns:
            swing_df[col] = ""

    swing_df["ticker"] = swing_df["ticker"].astype(str).str.strip().str.upper()
    swing_df["decision_tag"] = swing_df["decision_tag"].astype(str).str.strip().str.lower()
    swing_df["horizon_tag"] = swing_df["horizon_tag"].astype(str).str.strip().str.lower()
    swing_df["strategy_profile"] = swing_df["strategy_profile"].astype(str).str.strip().str.lower()
    swing_df["rank"] = pd.to_numeric(swing_df["rank"], errors="coerce").fillna(9999).astype(int)

    swing_df = swing_df[
        (swing_df["horizon_tag"] == "swing_core")
        | (swing_df["strategy_profile"] == "swing_trend")
    ].copy()
    if len(swing_df) == 0:
        swing_df = decision_df.copy()
        if "ticker" in swing_df.columns:
            swing_df["ticker"] = swing_df["ticker"].astype(str).str.strip().str.upper()
        if "rank" in swing_df.columns:
            swing_df["rank"] = pd.to_numeric(swing_df["rank"], errors="coerce").fillna(9999).astype(int)

    candidates = swing_df.sort_values(["rank", "ticker"], ascending=[True, True]).head(max(1, int(max_symbols)))
    symbols = [str(v).strip().upper() for v in candidates.get("ticker", pd.Series(dtype=str)).tolist() if str(v).strip()]

    risk_count = 0
    for item in execution_summaries[:10]:
        action = str(item.get("latest_action", "")).strip().lower()
        if action in {"stop_loss", "take_profit"}:
            risk_count += 1

    signal = "hold"
    confidence = 0.55
    if symbols and risk_count == 0:
        signal = "buy"
        confidence = 0.62
    if risk_count >= 2:
        signal = "reduce"
        confidence = 0.68

    open_positions = int(len(positions_df))
    reasoning = "先控風險再看續強。" if risk_count > 0 else "優先追蹤 Swing Core 延續強勢標的。"
    if open_positions > 0 and risk_count == 0:
        reasoning = f"你目前有 {open_positions} 檔持倉，優先追蹤 Swing Core 延續強勢標的。"
    return {
        "strategy_name": "Bedtime Swing Core Plan",
        "signal": signal,
        "confidence": confidence,
        "reasoning": reasoning,
        "risk_level": "medium" if risk_count else "low",
        "time_horizon": "overnight_to_3d",
        "symbols": symbols[: max(1, int(max_symbols))],
        "source": "rule_based",
    }


def generate_bedtime_swing_recommendation(
    decision_df: pd.DataFrame,
    positions_df: pd.DataFrame,
    execution_summaries: List[dict],
    max_symbols: int = 3,
) -> dict:
    components = _load_quantmuse_components()
    if bool(components.get("langchain_agent_cls") and components.get("llm_integration_cls")):
        try:
            llm_cls = components.get("llm_integration_cls")
            agent_cls = components.get("langchain_agent_cls")
            provider = str(os.getenv("QUANTMUSE_LLM_PROVIDER", "local")).strip().lower() or "local"
            openai_key = str(os.getenv("OPENAI_API_KEY", "")).strip() or None
            model_default = "microsoft/DialoGPT-medium" if provider == "local" else "gpt-3.5-turbo"
            model_name = str(os.getenv("QUANTMUSE_LLM_MODEL", model_default)).strip() or model_default
            llm = llm_cls(provider=provider, api_key=openai_key, model=model_name)
            agent = agent_cls(llm)

            top_df = decision_df.copy()
            if "ticker" in top_df.columns:
                top_df["ticker"] = top_df["ticker"].astype(str).str.strip().str.upper()
            if "rank" in top_df.columns:
                top_df["rank"] = pd.to_numeric(top_df["rank"], errors="coerce")
            top_df = top_df.sort_values(["rank", "ticker"], ascending=[True, True], na_position="last").head(max(3, int(max_symbols)))
            symbols = [str(v).strip().upper() for v in top_df.get("ticker", pd.Series(dtype=str)).tolist() if str(v).strip()]
            if not symbols:
                return _rule_based_swing_strategy(decision_df, positions_df, execution_summaries, max_symbols)

            market_data = pd.DataFrame({
                "close": pd.to_numeric(top_df.get("close", 0.0), errors="coerce").fillna(0.0).tolist(),
                "daily_change": pd.to_numeric(top_df.get("daily_change", 0.0), errors="coerce").fillna(0.0).tolist(),
            })
            sentiment_data = pd.DataFrame({
                "symbol": symbols,
                "sentiment_score": pd.to_numeric(top_df.get("short_score_final", 0.0), errors="coerce").fillna(0.0).tolist()[: len(symbols)],
                "confidence": pd.to_numeric(top_df.get("confidence", 0.0), errors="coerce").fillna(0.0).tolist()[: len(symbols)],
                "source": ["alpha_finder"] * len(symbols),
            })
            portfolio_data = {
                "num_positions": int(len(positions_df)),
                "risk_level": "medium" if len(execution_summaries) > 0 else "low",
                "cash": 0,
                "total_value": 0,
            }

            strategy = agent.generate_strategy_recommendation(market_data, sentiment_data, portfolio_data, symbols)
            return {
                "strategy_name": _clip_text(getattr(strategy, "strategy_name", "Bedtime Swing Core Plan"), 80),
                "signal": str(getattr(strategy, "signal", "hold")).strip().lower() or "hold",
                "confidence": float(max(0.0, min(1.0, _safe_float(getattr(strategy, "confidence", 0.55), 0.55)))),
                "reasoning": _clip_text(getattr(strategy, "reasoning", ""), 160),
                "risk_level": str(getattr(strategy, "risk_level", "medium")).strip().lower() or "medium",
                "time_horizon": str(getattr(strategy, "time_horizon", "overnight_to_3d")).strip() or "overnight_to_3d",
                "symbols": [str(v).strip().upper() for v in getattr(strategy, "symbols", symbols) if str(v).strip()][: max(1, int(max_symbols))],
                "source": "quantmuse_langchain",
            }
        except (AttributeError, RuntimeError, TypeError, ValueError, OSError, KeyError, ImportError) as exc:  # pragma: no cover - optional dependency
            LOGGER.debug("QuantMuse LangChain recommendation failed: %s", exc)

    return _rule_based_swing_strategy(decision_df, positions_df, execution_summaries, max_symbols)
