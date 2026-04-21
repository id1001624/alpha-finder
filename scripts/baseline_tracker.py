"""
Track Top1 vs XQ baseline performance snapshot.

Output:
- repo_outputs/backtest/baseline_tracker.csv (append/update by date)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
TRACKER_FILE = BACKTEST_DIR / "baseline_tracker.csv"
AI_DECISION_LATEST = BACKTEST_DIR / "ai_decision_latest.csv"
XQ_LATEST = PROJECT_ROOT / "repo_outputs" / "daily_refresh" / "latest" / "xq_short_term_updated.csv"


def _read_csv_fallback(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)


def _norm_ticker(text: object) -> str:
    return str(text or "").strip().upper().replace(".US", "")


def main() -> int:
    decision_df = _read_csv_fallback(AI_DECISION_LATEST)
    xq_df = _read_csv_fallback(XQ_LATEST)

    if len(decision_df) == 0 or len(xq_df) == 0:
        print("[BASELINE] missing ai_decision_latest or xq_short_term_updated")
        return 1

    for col in ["ticker", "symbol"]:
        if col in xq_df.columns:
            xq_df["ticker"] = xq_df[col].apply(_norm_ticker)
            break
    if "ticker" not in xq_df.columns:
        print("[BASELINE] xq csv missing ticker/symbol column")
        return 2

    decision_df["ticker"] = decision_df.get("ticker", "").apply(_norm_ticker)
    decision_df["rank"] = pd.to_numeric(decision_df.get("rank"), errors="coerce")
    top_row = decision_df.sort_values("rank", ascending=True).head(1)
    if len(top_row) == 0:
        print("[BASELINE] no top1 in ai_decision_latest")
        return 3

    top1_ticker = str(top_row.iloc[0].get("ticker", "")).strip().upper()
    if not top1_ticker:
        print("[BASELINE] invalid top1 ticker")
        return 4

    score_col = "short_trade_score" if "short_trade_score" in xq_df.columns else None
    if score_col:
        xq_base = xq_df.copy()
        xq_base[score_col] = pd.to_numeric(xq_base[score_col], errors="coerce")
        xq_base = xq_base.sort_values(score_col, ascending=False).head(5)
    else:
        xq_base = xq_df.head(5).copy()

    def _avg(col: str) -> float:
        if col not in xq_base.columns:
            return 0.0
        return float(pd.to_numeric(xq_base[col], errors="coerce").fillna(0.0).mean())

    def _top(col: str) -> float:
        if col not in xq_df.columns:
            return 0.0
        row = xq_df[xq_df["ticker"] == top1_ticker]
        if len(row) == 0:
            return 0.0
        return float(pd.to_numeric(row.iloc[0].get(col), errors="coerce") or 0.0)

    top1_1d = _top("chg_1d_pct")
    top1_3d = _top("chg_3d_pct")
    top1_5d = _top("chg_5d_pct")

    base_1d = _avg("chg_1d_pct")
    base_3d = _avg("chg_3d_pct")
    base_5d = _avg("chg_5d_pct")

    decision_date = str(top_row.iloc[0].get("decision_date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
    out_row = pd.DataFrame(
        [
            {
                "date": decision_date,
                "top1_ticker": top1_ticker,
                "top1_return_1d": round(top1_1d, 4),
                "top1_return_3d": round(top1_3d, 4),
                "top1_return_5d": round(top1_5d, 4),
                "xq_baseline_avg_1d": round(base_1d, 4),
                "xq_baseline_avg_3d": round(base_3d, 4),
                "xq_baseline_avg_5d": round(base_5d, 4),
                "beat_baseline": bool(top1_1d > base_1d),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        ]
    )

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    if TRACKER_FILE.exists():
        old = _read_csv_fallback(TRACKER_FILE)
        merged = pd.concat([old, out_row], ignore_index=True)
        merged = merged.drop_duplicates(subset=["date"], keep="last")
    else:
        merged = out_row

    merged = merged.sort_values("date", ascending=True).reset_index(drop=True)
    merged.to_csv(TRACKER_FILE, index=False, encoding="utf-8-sig")

    print(f"[BASELINE] updated: {TRACKER_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
