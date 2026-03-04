"""
將網頁 AI 產生的 ai_decision CSV（必要）歸檔到回測資料夾。

用途：
- 追加到回測主檔：repo_outputs/backtest/ai_decision_log.csv
- 建立每日快照：repo_outputs/backtest/daily_ai_decisions/YYYY-MM-DD_ai_decision.csv
- 更新最新副本：repo_outputs/backtest/ai_decision_latest.csv

範例：
python scripts/record_ai_decision.py --csv-file "repo_outputs/backtest/inbox/ai_decision_2026-03-04.csv"
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
DAILY_AI_DIR = BACKTEST_DIR / "daily_ai_decisions"
MASTER_LOG_FILE = BACKTEST_DIR / "ai_decision_log.csv"
LATEST_CSV_FILE = BACKTEST_DIR / "ai_decision_latest.csv"

REQUIRED_COLUMNS = [
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


def normalize_decision_df(df: pd.DataFrame, fallback_date: str) -> pd.DataFrame:
    out = df.copy()

    for col in REQUIRED_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    out = out[REQUIRED_COLUMNS].copy()
    out["decision_date"] = out["decision_date"].replace("", pd.NA).fillna(fallback_date)
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["tech_status"] = out["tech_status"].astype(str).str.strip()
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")

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


def copy_daily_and_latest(csv_file: Path, decision_date: str) -> None:
    daily_csv = DAILY_AI_DIR / f"{decision_date}_ai_decision.csv"

    shutil.copy2(csv_file, daily_csv)
    shutil.copy2(csv_file, LATEST_CSV_FILE)


def main() -> None:
    parser = argparse.ArgumentParser(description="歸檔 AI 決策輸出（CSV 必要）")
    parser.add_argument("--csv-file", required=True, help="ai_decision_YYYY-MM-DD.csv 路徑")
    parser.add_argument("--date", default="", help="可選，強制指定 decision_date（YYYY-MM-DD）")

    args = parser.parse_args()

    csv_file = Path(args.csv_file)
    if not csv_file.exists():
        print(f"找不到 CSV: {csv_file}")
        return

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_AI_DIR.mkdir(parents=True, exist_ok=True)

    raw_df = pd.read_csv(csv_file)
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
    copy_daily_and_latest(csv_file, decision_date)

    print("\n=== AI 決策已記錄 ===")
    print(f"主檔: {MASTER_LOG_FILE}")
    print(f"每日 CSV: {DAILY_AI_DIR / (decision_date + '_ai_decision.csv')}")
    print(f"最新 CSV: {LATEST_CSV_FILE}")


if __name__ == "__main__":
    main()
