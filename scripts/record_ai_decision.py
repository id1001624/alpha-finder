"""
將 AI 決策 CSV 歸檔到回測資料夾。

用途：
- 追加到回測主檔：repo_outputs/backtest/ai_decision_log.csv
- 建立每日快照：repo_outputs/backtest/daily_ai_decisions/YYYY-MM-DD_ai_decision.csv
- 更新最新副本：repo_outputs/backtest/ai_decision_latest.csv

範例：
python scripts/record_ai_decision.py --csv-file "repo_outputs/backtest/inbox/ai_decision_2026-03-04.csv"
python scripts/record_ai_decision.py --auto-latest
python scripts/record_ai_decision.py --auto-latest --replace-date
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import install_builtin_print_logging
from ai_trading.strategy_context import (
    REGIME_NEUTRAL,
    detect_regime_tag,
    ensure_decision_strategy_columns,
)
from ai_trading.trade_command_contract import (
    ACTION_BUY_AGGRESSIVE,
    ACTION_BUY_SCALE_IN,
    ACTION_NO_TRADE,
    ACTION_SELL_ALL_EXIT,
    ACTION_SELL_SCALE_OUT,
    USER_BOT_ONLY,
    USER_VISIBLE,
    enrich_trade_command_fields,
)
from ai_trading.ticker_mapping import resolve_underlying_ticker

from turso_state import sync_ai_decision_latest as sync_ai_decision_latest_to_turso

install_builtin_print_logging()

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
DAILY_AI_DIR = BACKTEST_DIR / "daily_ai_decisions"
MASTER_LOG_FILE = BACKTEST_DIR / "ai_decision_log.csv"
LATEST_CSV_FILE = BACKTEST_DIR / "ai_decision_latest.csv"
INBOX_DIR = BACKTEST_DIR / "inbox"
AI_READY_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready" / "latest"
DAILY_REFRESH_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh" / "latest"
AI_TRADING_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_trading" / "latest"

BASE_COLUMNS = [
    "decision_date",
    "rank",
    "ticker",
    "underlying_ticker",
    "short_score_final",
    "swing_score",
    "core_score",
    "risk_level",
    "tech_status",
    "theme",
    "decision_tag",
    "reason_summary",
    "source_ref",
]

CATALYST_COLUMNS = [
    "research_mode",
    "catalyst_type",
    "catalyst_sentiment",
    "explosion_probability",
    "hype_score",
    "confidence",
    "api_final_score",
    "catalyst_source",
    "catalyst_summary",
]

STRATEGY_COLUMNS = [
    "horizon_tag",
    "strategy_profile",
    "signal_type",
    "regime_tag",
]

RAW_PRESERVE_COLUMNS = [
    "decision_tag_v1",
    "rank_score_v2_adjusted",
    "protocol_gate_reason",
]

FINAL_DECISION_COLUMNS = [
    "local_rank",
    "local_decision_tag",
    "final_rank",
    "final_priority",
    "trade_eligibility",
    "candidate_origin",
    "web_override_flag",
    "web_override_reason",
    "web_delta_score",
    "execution_action",
    "position_plan",
    "exit_action",
    "user_visibility",
]

REQUIRED_COLUMNS = list(
    dict.fromkeys(
        BASE_COLUMNS
        + CATALYST_COLUMNS
        + STRATEGY_COLUMNS
        + RAW_PRESERVE_COLUMNS
        + FINAL_DECISION_COLUMNS
    )
)

ALLOWED_EXECUTION_ACTIONS = {
    ACTION_BUY_SCALE_IN,
    ACTION_BUY_AGGRESSIVE,
    ACTION_SELL_SCALE_OUT,
    ACTION_SELL_ALL_EXIT,
    ACTION_NO_TRADE,
}

BUY_EXECUTION_ACTIONS = {
    ACTION_BUY_SCALE_IN,
    ACTION_BUY_AGGRESSIVE,
}

ALLOWED_LOCAL_DECISION_TAGS = {
    "keep",
    "watch",
    "replacecandidate",
    "replace",
    "unknown",
}

ALLOWED_CANDIDATE_ORIGINS = {
    "local",
    "web_challenger",
}

ALLOWED_TRADE_ELIGIBILITY = {
    "tradable",
    "downgraded",
    "blocked",
    "watch_only",
}

AI_DECISION_CONTRACT_V2_COLUMNS = [
    "as_of_date",
    "ticker",
    "company_name",
    "decision_mode",
    "final_priority",
    "decision_status",
    "decision_score",
    "decision_reason",
    "primary_event_type",
    "trigger_source",
    "trigger_score",
    "continuation_rank",
    "tomorrow_continuation_prob",
    "confidence_tier",
    "entry_plan",
    "execution_window",
    "avoid_chase_flag",
    "preferred_entry_type",
    "vwap_status",
    "sqzmom_status",
    "volume_status",
    "invalidation_rule",
    "risk_level",
    "risk_note",
    "dilution_flag",
    "halt_risk_flag",
    "source_sheet_trace",
    "protocol_version",
    "data_version",
    "decision_ts",
    "execution_action",
    "position_plan",
    "exit_action",
    "user_visibility",
]

MATERIALIZED_CONTRACT_FILE = "ai_decision_contract_v2_materialized.csv"

VALID_DECISION_TAGS = {"keep", "watch", "replace_candidate"}


def _find_latest_decision_csv(include_preview_sources: bool = False) -> Path | None:
    candidates = []
    search_dirs = [INBOX_DIR]
    if include_preview_sources:
        search_dirs.extend([AI_READY_LATEST_DIR, DAILY_REFRESH_LATEST_DIR])
    for directory in search_dirs:
        if not directory.exists():
            continue
        for file in directory.glob("ai_decision_*.csv"):
            try:
                candidates.append((file.stat().st_mtime, file))
            except OSError:
                continue

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _infer_decision_tag(row: pd.Series) -> str:
    short_score = pd.to_numeric(row.get("short_score_final"), errors="coerce")
    core_score = pd.to_numeric(row.get("core_score"), errors="coerce")
    tech_status = str(row.get("tech_status", "")).strip()

    if pd.isna(short_score):
        return "watch"

    if short_score < 10:
        return "replace_candidate"

    if short_score >= 20 and tech_status != "需技術驗證":
        return "keep"

    if not pd.isna(core_score) and core_score <= 8 and short_score < 12:
        return "replace_candidate"

    return "watch"


def _read_csv_fallback(csv_path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(csv_path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(csv_path)


def _clean_text(value: object, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in {"", "nan", "none", "null", "na", "n/a"}:
        return default
    return text


def _to_bool(value: object) -> bool:
    text = _clean_text(value, "").lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return False
    return bool(int(parsed))


def _normalize_trade_eligibility(value: object, decision_tag: str) -> str:
    normalized = _clean_text(value, "").lower().replace("-", "_").replace(" ", "_")
    legacy_map = {
        "eligible": "tradable",
        "review_only": "downgraded",
        "no_trade": "blocked",
    }
    normalized = legacy_map.get(normalized, normalized)
    if normalized in ALLOWED_TRADE_ELIGIBILITY:
        return normalized
    if decision_tag == "keep":
        return "tradable"
    return "watch_only"


def _normalize_execution_action(value: object, decision_tag: str, trade_eligibility: str) -> str:
    action = _clean_text(value, "").upper()
    if action == "BOT_ONLY":
        action = ACTION_NO_TRADE
    if action in ALLOWED_EXECUTION_ACTIONS:
        if trade_eligibility != "tradable" and action in BUY_EXECUTION_ACTIONS:
            return ACTION_NO_TRADE
        return action
    if decision_tag != "keep":
        return ACTION_NO_TRADE
    if trade_eligibility != "tradable":
        return ACTION_NO_TRADE
    return ACTION_BUY_SCALE_IN


def _normalize_local_decision_tag(value: object, fallback_value: object = "") -> str:
    text = _clean_text(value, "")
    if not text:
        text = _clean_text(fallback_value, "")
    normalized = text.lower().replace("_", "").replace("-", "").replace(" ", "")
    if normalized in ALLOWED_LOCAL_DECISION_TAGS:
        return normalized
    return "unknown"


def _normalize_candidate_origin(value: object) -> str:
    normalized = _clean_text(value, "").lower().replace("-", "_").replace(" ", "_")
    if normalized in {"web", "webchallenger", "challenger"}:
        normalized = "web_challenger"
    if normalized in ALLOWED_CANDIDATE_ORIGINS:
        return normalized
    return "local"


def _normalize_research_mode(value: object) -> str:
    normalized = _clean_text(value, "").lower().replace("-", "_").replace("+", "_plus_")
    if normalized in {"bundle_only"}:
        return "bundle_only"
    if normalized in {
        "bundle_plus_web",
        "bundleplusweb",
        "web",
        "api",
    }:
        return "bundle_plus_web"
    return "bundle_plus_web"


def _normalize_web_delta_score(value: object) -> int:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return 0
    as_int = int(round(float(parsed)))
    return max(-100, min(100, as_int))


def _infer_confidence_tier(value: object) -> str:
    score = pd.to_numeric(value, errors="coerce")
    if pd.isna(score):
        return "medium"
    val = float(score)
    if val >= 70:
        return "high"
    if val >= 45:
        return "medium"
    return "low"


def _infer_preferred_entry_type(tech_status: str) -> str:
    text = _clean_text(tech_status, "").lower()
    if "pullback" in text or "回踩" in text:
        return "pullback_entry"
    if "breakout" in text or "突破" in text:
        return "breakout_or_reclaim"
    if "avoid" in text or "追" in text:
        return "unknown"
    return "tactical_entry"


def _load_api_catalyst_map() -> pd.DataFrame:
    api_path = AI_TRADING_LATEST_DIR / "api_catalyst_analysis_daily.csv"
    if not api_path.exists():
        return pd.DataFrame()

    try:
        df = _read_csv_fallback(api_path)
    except (FileNotFoundError, PermissionError, OSError, pd.errors.EmptyDataError):
        return pd.DataFrame()

    if len(df) == 0 or "ticker" not in df.columns:
        return pd.DataFrame()

    out = df.copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out = out[out["ticker"] != ""].copy()
    if len(out) == 0:
        return pd.DataFrame()

    rename_map = {
        "sentiment": "catalyst_sentiment",
        "reason": "catalyst_summary",
    }
    out = out.rename(columns=rename_map)
    keep_cols = [
        "ticker",
        "catalyst_type",
        "catalyst_sentiment",
        "explosion_probability",
        "hype_score",
        "confidence",
        "api_final_score",
        "catalyst_summary",
    ]
    for col in keep_cols:
        if col not in out.columns:
            out[col] = ""
    out = out[keep_cols].drop_duplicates(subset=["ticker"], keep="first")
    out["catalyst_source"] = "api_catalyst_analysis_daily.csv"
    out["research_mode"] = "api"
    return out


def _fill_missing_values(base: pd.Series, incoming: pd.Series) -> pd.Series:
    base_obj = base.astype(object)
    incoming_obj = incoming.astype(object)
    base_missing = base_obj.isna() | (base_obj.astype(str).str.strip() == "")
    return base_obj.where(~base_missing, incoming_obj)


def enrich_with_api_catalyst(df: pd.DataFrame) -> pd.DataFrame:
    catalyst_df = _load_api_catalyst_map()
    if len(catalyst_df) == 0:
        return df

    out = df.copy()
    merged = out.merge(catalyst_df, on="ticker", how="left", suffixes=("", "__api"))

    for col in CATALYST_COLUMNS:
        incoming_col = f"{col}__api"
        if incoming_col not in merged.columns:
            continue
        merged[col] = _fill_missing_values(merged[col], merged[incoming_col])
        merged = merged.drop(columns=[incoming_col])

    return merged


def normalize_decision_df(df: pd.DataFrame, fallback_date: str) -> pd.DataFrame:
    out = df.copy()

    for col in REQUIRED_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    out = out[REQUIRED_COLUMNS].copy()
    out = enrich_with_api_catalyst(out)
    out["decision_date"] = out["decision_date"].replace("", pd.NA).fillna(fallback_date)
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["underlying_ticker"] = out["underlying_ticker"].astype(str).str.strip().str.upper()
    out["underlying_ticker"] = out["underlying_ticker"].where(out["underlying_ticker"] != "", out["ticker"].apply(resolve_underlying_ticker))
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["tech_status"] = out["tech_status"].astype(str).str.strip()
    out["research_mode"] = out["research_mode"].apply(_normalize_research_mode)

    out["web_override_flag"] = out["web_override_flag"].apply(_to_bool)
    out["web_override_reason"] = out["web_override_reason"].astype(str).str.strip()
    out["local_decision_tag"] = [
        _normalize_local_decision_tag(
            value=row.get("local_decision_tag", ""),
            fallback_value=row.get("decision_tag_v1", row.get("decision_tag", "")),
        )
        for _, row in out.iterrows()
    ]
    out["candidate_origin"] = out["candidate_origin"].apply(_normalize_candidate_origin)

    rank_num = pd.to_numeric(out["rank"], errors="coerce")
    local_rank_num = pd.to_numeric(out["local_rank"], errors="coerce").combine_first(rank_num)
    final_rank_num = pd.to_numeric(out["final_rank"], errors="coerce")
    final_priority_num = pd.to_numeric(out["final_priority"], errors="coerce")

    final_rank_num = final_rank_num.combine_first(final_priority_num).combine_first(rank_num).combine_first(local_rank_num)
    final_priority_num = final_priority_num.combine_first(final_rank_num)

    out["local_rank"] = local_rank_num
    out["final_rank"] = final_rank_num
    out["final_priority"] = final_priority_num
    out["rank"] = final_rank_num

    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["short_score_final"] = pd.to_numeric(out["short_score_final"], errors="coerce")
    out["swing_score"] = pd.to_numeric(out["swing_score"], errors="coerce")
    out["core_score"] = pd.to_numeric(out["core_score"], errors="coerce")
    out["explosion_probability"] = pd.to_numeric(out["explosion_probability"], errors="coerce")
    out["hype_score"] = pd.to_numeric(out["hype_score"], errors="coerce")
    out["confidence"] = pd.to_numeric(out["confidence"], errors="coerce")
    out["api_final_score"] = pd.to_numeric(out["api_final_score"], errors="coerce")
    out["web_delta_score"] = out["web_delta_score"].apply(_normalize_web_delta_score)

    out["trade_eligibility"] = [
        _normalize_trade_eligibility(value=row.get("trade_eligibility", ""), decision_tag=str(row.get("decision_tag", "")))
        for _, row in out.iterrows()
    ]
    out["execution_action"] = [
        _normalize_execution_action(
            value=row.get("execution_action", ""),
            decision_tag=str(row.get("decision_tag", "")),
            trade_eligibility=str(row.get("trade_eligibility", "")),
        )
        for _, row in out.iterrows()
    ]
    out["position_plan"] = out["position_plan"].astype(str).str.strip()
    out["exit_action"] = out["exit_action"].astype(str).str.strip().str.upper()
    out["user_visibility"] = out["user_visibility"].astype(str).str.strip().str.lower()

    try:
        regime_default = detect_regime_tag()
    except (OSError, ValueError, TypeError, RuntimeError):
        regime_default = REGIME_NEUTRAL
    out = ensure_decision_strategy_columns(out, default_regime=regime_default)

    invalid_tag_mask = ~out["decision_tag"].isin(VALID_DECISION_TAGS)
    if invalid_tag_mask.any():
        out.loc[invalid_tag_mask, "decision_tag"] = out[invalid_tag_mask].apply(_infer_decision_tag, axis=1)

    out.loc[out["decision_tag"] != "keep", "trade_eligibility"] = out.loc[
        out["decision_tag"] != "keep", "trade_eligibility"
    ].replace({"tradable": "watch_only"})

    buy_block_mask = (out["trade_eligibility"] != "tradable") & out["execution_action"].isin(BUY_EXECUTION_ACTIONS)
    out.loc[buy_block_mask, "execution_action"] = ACTION_NO_TRADE

    out = out[out["ticker"] != ""]
    out = out.dropna(subset=["rank"]).copy()
    out["rank"] = out["rank"].astype(int)
    out["local_rank"] = pd.to_numeric(out["local_rank"], errors="coerce").fillna(out["rank"]).astype(int)
    out["final_rank"] = pd.to_numeric(out["final_rank"], errors="coerce").fillna(out["rank"]).astype(int)
    out["final_priority"] = pd.to_numeric(out["final_priority"], errors="coerce").fillna(out["final_rank"]).astype(int)

    out["recorded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


def append_to_master_log(df: pd.DataFrame, replace_date: bool = False) -> None:
    incoming_dates = set(df["decision_date"].astype(str).tolist())

    if MASTER_LOG_FILE.exists():
        existing = pd.read_csv(MASTER_LOG_FILE)
        if replace_date:
            existing_dates = existing["decision_date"].astype(str)
            existing = existing[~existing_dates.isin(incoming_dates)].copy()
        merged = pd.concat([existing, df], ignore_index=True)
        merged = merged.drop_duplicates(subset=["decision_date", "ticker"], keep="last")
        merged = merged.sort_values(["decision_date", "rank"], ascending=[False, True])
        merged.to_csv(MASTER_LOG_FILE, index=False, encoding="utf-8-sig")
    else:
        df.sort_values(["decision_date", "rank"], ascending=[False, True]).to_csv(
            MASTER_LOG_FILE, index=False, encoding="utf-8-sig"
        )


def copy_daily_and_latest(df: pd.DataFrame, decision_date: str) -> str | None:
    daily_csv = DAILY_AI_DIR / f"{decision_date}_ai_decision.csv"
    export_df = df[REQUIRED_COLUMNS].copy()
    export_df.to_csv(daily_csv, index=False, encoding="utf-8-sig")
    export_df.to_csv(LATEST_CSV_FILE, index=False, encoding="utf-8-sig")
    return sync_ai_decision_latest_to_turso(LATEST_CSV_FILE)


def _build_official_materialized_df(df: pd.DataFrame, decision_date: str) -> pd.DataFrame:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for idx, (_, row) in enumerate(df.sort_values(["final_rank", "ticker"], ascending=[True, True]).iterrows(), 1):
        ticker = _clean_text(row.get("ticker"), "").upper()
        if not ticker:
            continue

        decision_tag = _clean_text(row.get("decision_tag"), "watch").lower()
        if decision_tag not in VALID_DECISION_TAGS:
            decision_tag = "watch"
        decision_status = "keep" if decision_tag == "keep" else "watch"

        trade_eligibility = _normalize_trade_eligibility(row.get("trade_eligibility", ""), decision_tag)
        execution_action = _normalize_execution_action(row.get("execution_action", ""), decision_tag, trade_eligibility)

        visibility = _clean_text(row.get("user_visibility", "")).lower()
        if visibility not in {USER_VISIBLE, USER_BOT_ONLY}:
            if decision_status != "keep" or trade_eligibility != "tradable" or execution_action == ACTION_NO_TRADE:
                visibility = USER_BOT_ONLY
            else:
                visibility = USER_VISIBLE

        if visibility == USER_BOT_ONLY:
            execution_action = ACTION_NO_TRADE

        invalidation_rule = _clean_text(row.get("invalidation_rule"), "")
        exit_action = _clean_text(row.get("exit_action"), "").upper()
        if exit_action not in {ACTION_SELL_SCALE_OUT, ACTION_SELL_ALL_EXIT}:
            exit_action = ACTION_SELL_ALL_EXIT if invalidation_rule else ACTION_SELL_SCALE_OUT

        gate_reason = _clean_text(row.get("protocol_gate_reason"), "")
        web_reason = _clean_text(row.get("web_override_reason"), "")
        note_parts = [
            f"trade_eligibility={trade_eligibility}",
        ]
        if gate_reason:
            note_parts.append(f"gate={gate_reason}")
        if invalidation_rule:
            note_parts.append(f"invalidate={invalidation_rule}")
        if _to_bool(row.get("web_override_flag", False)):
            note_parts.append("web_override=true")
        if web_reason:
            note_parts.append(f"web_override_reason={web_reason}")

        local_rank = pd.to_numeric(row.get("local_rank"), errors="coerce")
        final_rank = pd.to_numeric(row.get("final_rank"), errors="coerce")
        final_priority = int(final_rank) if pd.notna(final_rank) else idx
        continuation_rank = int(local_rank) if pd.notna(local_rank) else final_priority

        confidence_val = pd.to_numeric(row.get("confidence"), errors="coerce")
        confidence_tier = _clean_text(row.get("confidence_tier"), "") or _infer_confidence_tier(confidence_val)
        tech_status = _clean_text(row.get("tech_status"), "")

        rows.append(
            {
                "as_of_date": _clean_text(row.get("decision_date"), decision_date),
                "ticker": ticker,
                "company_name": "",
                "decision_mode": "official_final",
                "final_priority": final_priority,
                "decision_status": decision_status,
                "decision_score": float(pd.to_numeric(row.get("short_score_final"), errors="coerce") or 0.0),
                "decision_reason": _clean_text(row.get("reason_summary"), ""),
                "primary_event_type": _clean_text(row.get("catalyst_type"), "local_only"),
                "trigger_source": _clean_text(row.get("catalyst_source"), "web_final"),
                "trigger_score": float(pd.to_numeric(row.get("hype_score"), errors="coerce") or 0.0),
                "continuation_rank": continuation_rank,
                "tomorrow_continuation_prob": float(confidence_val) if pd.notna(confidence_val) else 0.0,
                "confidence_tier": confidence_tier,
                "entry_plan": _clean_text(row.get("decision_action"), ""),
                "execution_window": "next_open_session",
                "avoid_chase_flag": ("avoid" in tech_status.lower()) or ("追" in tech_status),
                "preferred_entry_type": _infer_preferred_entry_type(tech_status),
                "vwap_status": _clean_text(row.get("vwap_status"), ""),
                "sqzmom_status": _clean_text(row.get("sqzmom_status"), ""),
                "volume_status": _clean_text(row.get("volume_status"), ""),
                "invalidation_rule": invalidation_rule,
                "risk_level": _clean_text(row.get("risk_level"), ""),
                "risk_note": "; ".join(note_parts),
                "dilution_flag": False,
                "halt_risk_flag": False,
                "source_sheet_trace": _clean_text(row.get("source_ref"), "ai_decision_final"),
                "protocol_version": "official_final_contract_v1",
                "data_version": decision_date,
                "decision_ts": now_text,
                "execution_action": execution_action,
                "position_plan": _clean_text(row.get("position_plan"), ""),
                "exit_action": exit_action,
                "user_visibility": visibility,
            }
        )

    out = pd.DataFrame(rows, columns=AI_DECISION_CONTRACT_V2_COLUMNS)
    out = enrich_trade_command_fields(out)
    return out.reindex(columns=AI_DECISION_CONTRACT_V2_COLUMNS)


def _write_official_materialized_contract(df: pd.DataFrame, decision_date: str) -> list[Path]:
    materialized_df = _build_official_materialized_df(df=df, decision_date=decision_date)
    target_paths: list[Path] = []

    primary = AI_TRADING_LATEST_DIR / MATERIALIZED_CONTRACT_FILE
    primary.parent.mkdir(parents=True, exist_ok=True)
    materialized_df.to_csv(primary, index=False, encoding="utf-8-sig")
    target_paths.append(primary)

    for legacy_dir in [AI_READY_LATEST_DIR, DAILY_REFRESH_LATEST_DIR]:
        legacy_path = legacy_dir / MATERIALIZED_CONTRACT_FILE
        if not legacy_path.exists():
            continue
        try:
            legacy_path.unlink()
        except OSError:
            continue

    return target_paths


def main() -> None:
    parser = argparse.ArgumentParser(description="歸檔 AI 決策輸出（支援自動找最新 CSV）")
    parser.add_argument("--csv-file", default="", help="ai_decision_YYYY-MM-DD.csv 路徑")
    parser.add_argument("--auto-latest", action="store_true", help="自動搜尋最新 ai_decision_*.csv")
    parser.add_argument(
        "--include-preview-sources",
        action="store_true",
        help="auto-latest 時一併搜尋 ai_ready/latest 與 daily_refresh/latest（預設只搜 inbox 正式檔）",
    )
    parser.add_argument("--date", default="", help="可選，強制指定 decision_date（YYYY-MM-DD）")
    parser.add_argument(
        "--replace-date",
        action="store_true",
        help="匯入前先刪除 ai_decision_log.csv 中相同 decision_date 的舊資料，再寫入新版",
    )

    args = parser.parse_args()

    csv_file = Path(args.csv_file) if args.csv_file.strip() else None
    if csv_file is None or args.auto_latest:
        found = _find_latest_decision_csv(include_preview_sources=bool(args.include_preview_sources))
        if found is None:
            if args.include_preview_sources:
                print("找不到可歸檔的 ai_decision_*.csv（已搜尋 inbox / ai_ready/latest / daily_refresh/latest）")
            else:
                print("找不到可歸檔的 ai_decision_*.csv（預設只搜尋 repo_outputs/backtest/inbox）")
            return
        csv_file = found

    if not csv_file.exists():
        print(f"找不到 CSV: {csv_file}")
        return

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_AI_DIR.mkdir(parents=True, exist_ok=True)

    raw_df = _read_csv_fallback(csv_file)
    decision_date = args.date.strip()
    if not decision_date:
        if "decision_date" in raw_df.columns and raw_df["decision_date"].notna().any():
            decision_date = str(raw_df["decision_date"].dropna().iloc[0])
        else:
            decision_date = datetime.now().strftime("%Y-%m-%d")

    norm_df = normalize_decision_df(raw_df, fallback_date=decision_date)
    if len(norm_df) == 0:
        print("CSV 沒有可用的決策資料（ticker/rank）")
        return

    append_to_master_log(norm_df, replace_date=args.replace_date)
    turso_sync_state = copy_daily_and_latest(norm_df, decision_date)
    materialized_paths = _write_official_materialized_contract(df=norm_df, decision_date=decision_date)

    print("\n=== AI 決策已記錄 ===")
    print(f"來源 CSV: {csv_file}")
    print(f"replace_date: {'true' if args.replace_date else 'false'}")
    print(f"主檔: {MASTER_LOG_FILE}")
    print(f"每日 CSV: {DAILY_AI_DIR / (decision_date + '_ai_decision.csv')}")
    print(f"最新 CSV: {LATEST_CSV_FILE}")
    print("official materialized:")
    for path in materialized_paths:
        print(f"- {path}")
    print(f"turso_state: {turso_sync_state or '未同步'}")


if __name__ == "__main__":
    main()
