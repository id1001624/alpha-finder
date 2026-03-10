"""
將 AI 決策 CSV 歸檔到回測資料夾。

用途：
- 追加到回測主檔：repo_outputs/backtest/ai_decision_log.csv
- 建立每日快照：repo_outputs/backtest/daily_ai_decisions/YYYY-MM-DD_ai_decision.csv
- 更新最新副本：repo_outputs/backtest/ai_decision_latest.csv

範例：
python scripts/record_ai_decision.py --csv-file "repo_outputs/backtest/inbox/ai_decision_2026-03-04.csv"
python scripts/record_ai_decision.py --auto-latest
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

from cloud_state import sync_ai_decision_latest
from turso_state import sync_ai_decision_latest as sync_ai_decision_latest_to_turso

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

REQUIRED_COLUMNS = BASE_COLUMNS + CATALYST_COLUMNS

VALID_DECISION_TAGS = {"keep", "watch", "replace_candidate"}


def _find_latest_decision_csv() -> Path | None:
    candidates = []
    search_dirs = [INBOX_DIR, AI_READY_LATEST_DIR, DAILY_REFRESH_LATEST_DIR]
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
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["tech_status"] = out["tech_status"].astype(str).str.strip()
    out["research_mode"] = out["research_mode"].astype(str).str.strip().str.lower()
    blank_mode_mask = out["research_mode"] == ""
    out.loc[blank_mode_mask, "research_mode"] = "web"
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["short_score_final"] = pd.to_numeric(out["short_score_final"], errors="coerce")
    out["swing_score"] = pd.to_numeric(out["swing_score"], errors="coerce")
    out["core_score"] = pd.to_numeric(out["core_score"], errors="coerce")
    out["explosion_probability"] = pd.to_numeric(out["explosion_probability"], errors="coerce")
    out["hype_score"] = pd.to_numeric(out["hype_score"], errors="coerce")
    out["confidence"] = pd.to_numeric(out["confidence"], errors="coerce")
    out["api_final_score"] = pd.to_numeric(out["api_final_score"], errors="coerce")

    invalid_tag_mask = ~out["decision_tag"].isin(VALID_DECISION_TAGS)
    if invalid_tag_mask.any():
        out.loc[invalid_tag_mask, "decision_tag"] = out[invalid_tag_mask].apply(_infer_decision_tag, axis=1)

    out = out[out["ticker"] != ""]
    out = out.dropna(subset=["rank"]).copy()
    out["rank"] = out["rank"].astype(int)

    out["recorded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


def append_to_master_log(df: pd.DataFrame) -> None:
    if MASTER_LOG_FILE.exists():
        existing = pd.read_csv(MASTER_LOG_FILE)
        merged = pd.concat([existing, df], ignore_index=True)
        merged = merged.drop_duplicates(subset=["decision_date", "ticker"], keep="last")
        merged = merged.sort_values(["decision_date", "rank"], ascending=[False, True])
        merged.to_csv(MASTER_LOG_FILE, index=False, encoding="utf-8-sig")
    else:
        df.sort_values(["decision_date", "rank"], ascending=[False, True]).to_csv(
            MASTER_LOG_FILE, index=False, encoding="utf-8-sig"
        )


def copy_daily_and_latest(df: pd.DataFrame, decision_date: str) -> None:
    daily_csv = DAILY_AI_DIR / f"{decision_date}_ai_decision.csv"
    export_df = df[REQUIRED_COLUMNS].copy()
    export_df.to_csv(daily_csv, index=False, encoding="utf-8-sig")
    export_df.to_csv(LATEST_CSV_FILE, index=False, encoding="utf-8-sig")
    sync_ai_decision_latest(LATEST_CSV_FILE)


def main() -> None:
    parser = argparse.ArgumentParser(description="歸檔 AI 決策輸出（支援自動找最新 CSV）")
    parser.add_argument("--csv-file", default="", help="ai_decision_YYYY-MM-DD.csv 路徑")
    parser.add_argument("--auto-latest", action="store_true", help="自動搜尋最新 ai_decision_*.csv")
    parser.add_argument("--date", default="", help="可選，強制指定 decision_date（YYYY-MM-DD）")

    args = parser.parse_args()

    csv_file = Path(args.csv_file) if args.csv_file.strip() else None
    if csv_file is None or args.auto_latest:
        found = _find_latest_decision_csv()
        if found is None:
            print("找不到可歸檔的 ai_decision_*.csv（已搜尋 inbox / ai_ready/latest / daily_refresh/latest）")
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

    append_to_master_log(norm_df)
    copy_daily_and_latest(norm_df, decision_date)

    print("\n=== AI 決策已記錄 ===")
    print(f"來源 CSV: {csv_file}")
    print(f"主檔: {MASTER_LOG_FILE}")
    print(f"每日 CSV: {DAILY_AI_DIR / (decision_date + '_ai_decision.csv')}")
    print(f"最新 CSV: {LATEST_CSV_FILE}")
    print(f"cloud_state: {sync_ai_decision_latest(LATEST_CSV_FILE) or '未同步'}")
    print(f"turso_state: {sync_ai_decision_latest_to_turso(LATEST_CSV_FILE) or '未同步'}")


if __name__ == "__main__":
    main()
