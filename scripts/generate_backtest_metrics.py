"""
scripts/generate_backtest_metrics.py

從 position_trade_log.csv 計算勝率、平均 R、持倉時間等指標，
輸出 Markdown 看板到 repo_outputs/backtest/metrics_dashboard_latest.md

用法：
  python scripts/generate_backtest_metrics.py
  python scripts/generate_backtest_metrics.py --top-trades 10
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
TRADE_LOG_CSV = BACKTEST_DIR / "position_trade_log.csv"
EXECUTION_LOG_CSV = BACKTEST_DIR / "execution_trade_log.csv"
OUTPUT_MD = BACKTEST_DIR / "metrics_dashboard_latest.md"


def _safe_float(value: object, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(parsed) else float(parsed)


def _load_trade_log() -> pd.DataFrame:
    if not TRADE_LOG_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(TRADE_LOG_CSV, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(TRADE_LOG_CSV)
    if df.empty:
        return pd.DataFrame()

    # Normalise key fields
    for col in ("ticker", "side", "position_effect", "horizon_tag", "strategy_profile"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lower()
    df["ticker"] = df["ticker"].str.upper()
    df["realized_pnl_delta"] = pd.to_numeric(df.get("realized_pnl_delta", 0), errors="coerce").fillna(0.0)
    df["realized_pct"] = pd.to_numeric(df.get("realized_pct"), errors="coerce")
    df["holding_days"] = pd.to_numeric(df.get("holding_days"), errors="coerce")
    df["holding_minutes"] = pd.to_numeric(df.get("holding_minutes"), errors="coerce")
    df["recorded_at"] = pd.to_datetime(df.get("recorded_at"), errors="coerce")
    return df


def _load_execution_log() -> pd.DataFrame:
    if not EXECUTION_LOG_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(EXECUTION_LOG_CSV, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(EXECUTION_LOG_CSV)
    if df.empty:
        return pd.DataFrame()
    for col in ("ticker", "action", "horizon_tag", "strategy_profile", "signal_type", "regime_tag"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lower()
    df["ticker"] = df["ticker"].str.upper()
    return df


def _compute_stats(df: pd.DataFrame, label: str) -> list[str]:
    """Compute stats from closed trades (position_effect == 'close')."""
    closed = df[df["position_effect"] == "close"].copy()
    if closed.empty:
        return [f"**{label}**: 尚無已結算交易"]

    total = len(closed)
    wins = int((closed["realized_pnl_delta"] > 0).sum())
    losses = int((closed["realized_pnl_delta"] <= 0).sum())
    win_rate = wins / total * 100 if total > 0 else 0.0
    total_pnl = closed["realized_pnl_delta"].sum()
    avg_pnl = closed["realized_pnl_delta"].mean()
    best_pnl = closed["realized_pnl_delta"].max()
    worst_pnl = closed["realized_pnl_delta"].min()

    avg_hold_days = closed["holding_days"].dropna().mean()
    avg_hold_min = closed["holding_minutes"].dropna().mean()

    best_row = closed.loc[closed["realized_pnl_delta"].idxmax()] if total > 0 else None
    worst_row = closed.loc[closed["realized_pnl_delta"].idxmin()] if total > 0 else None

    lines = [
        f"**{label}**",
        f"- 結算筆數：{total}（勝 {wins} / 敗 {losses}）",
        f"- 勝率：**{win_rate:.1f}%**",
        f"- 累計 PnL：{total_pnl:+.2f}",
        f"- 平均每筆 PnL：{avg_pnl:+.2f}",
        f"- 最佳：{best_pnl:+.2f}" + (f"（{best_row['ticker']}）" if best_row is not None else ""),
        f"- 最差：{worst_pnl:+.2f}" + (f"（{worst_row['ticker']}）" if worst_row is not None else ""),
    ]
    if pd.notna(avg_hold_days):
        lines.append(f"- 平均持倉天數：{avg_hold_days:.1f} 天")
    elif pd.notna(avg_hold_min):
        lines.append(f"- 平均持倉分鐘：{avg_hold_min:.0f} 分鐘")

    # Realized pct distribution
    pct_series = closed["realized_pct"].dropna()
    if len(pct_series) > 0:
        lines.append(
            f"- 漲跌幅分布：avg={pct_series.mean():.1f}%  "
            f"median={pct_series.median():.1f}%  "
            f"max={pct_series.max():.1f}%  "
            f"min={pct_series.min():.1f}%"
        )

    return lines


def _recent_trades_table(df: pd.DataFrame, n: int = 10) -> list[str]:
    closed = df[df["position_effect"] == "close"].copy()
    if closed.empty:
        return ["*尚無已結算交易*"]
    closed = closed.sort_values("recorded_at", ascending=False).head(n)
    rows = ["| Ticker | Side | PnL | PnL% | Hold | Strategy | 時間 |",
            "|--------|------|-----|------|------|----------|------|"]
    for _, row in closed.iterrows():
        pnl = _safe_float(row.get("realized_pnl_delta"))
        pct = _safe_float(row.get("realized_pct"), float("nan"))
        pct_str = f"{pct:.1f}%" if not pd.isna(pct) else "-"
        hd = row.get("holding_days")
        hm = row.get("holding_minutes")
        if pd.notna(hd) and float(hd) >= 0.5:
            hold_str = f"{float(hd):.1f}d"
        elif pd.notna(hm):
            hold_str = f"{float(hm):.0f}m"
        else:
            hold_str = "-"
        strat = str(row.get("strategy_profile", "")).split("_")[-1] if row.get("strategy_profile") else "-"
        ts = str(row.get("recorded_at", ""))[:16] if pd.notna(row.get("recorded_at")) else "-"
        rows.append(
            f"| {row['ticker']} | {str(row.get('side', '')).upper()} "
            f"| {pnl:+.2f} | {pct_str} | {hold_str} | {strat} | {ts} |"
        )
    return rows


def _signal_type_breakdown(exec_df: pd.DataFrame) -> list[str]:
    if exec_df.empty or "signal_type" not in exec_df.columns:
        return []
    counts = exec_df["signal_type"].value_counts()
    if counts.empty:
        return []
    lines = ["**訊號類型分布（execution log）**"]
    for sig_type, cnt in counts.items():
        lines.append(f"- {sig_type}: {cnt} 次")
    return lines


def _regime_breakdown(exec_df: pd.DataFrame) -> list[str]:
    if exec_df.empty or "regime_tag" not in exec_df.columns:
        return []
    entry_df = exec_df[exec_df["action"].isin({"entry", "swing_entry", "add", "swing_add"})]
    if entry_df.empty:
        return []
    counts = entry_df["regime_tag"].value_counts()
    lines = ["**進場當時 Regime 分布**"]
    for regime, cnt in counts.items():
        lines.append(f"- {regime}: {cnt} 次進場")
    return lines


def generate_metrics(top_trades: int = 10) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trade_df = _load_trade_log()
    exec_df = _load_execution_log()

    sections: list[str] = [
        "# Alpha Finder — Backtest Metrics Dashboard",
        f"> Generated: {now}",
        "",
    ]

    if trade_df.empty:
        sections.append("*position_trade_log.csv 尚無資料，先用 Discord /buy /sell 回報成交後才會有數字。*")
    else:
        # Overall
        sections.append("## 整體統計")
        sections.extend(_compute_stats(trade_df, "所有策略合計"))
        sections.append("")

        # By strategy
        strat_col = trade_df["strategy_profile"] if "strategy_profile" in trade_df.columns else pd.Series(dtype=str)
        for strat in sorted(strat_col.dropna().unique()):
            subset = trade_df[trade_df["strategy_profile"] == strat] if "strategy_profile" in trade_df.columns else trade_df
            label_map = {"monster_swing": "Monster Swing（盤中）", "swing_trend": "Swing Core（多日）"}
            label = label_map.get(strat, strat)
            sub_lines = _compute_stats(subset, label)
            sections.extend(sub_lines)
            sections.append("")

        # Recent trades
        sections.append(f"## 最近 {top_trades} 筆已結算交易")
        sections.extend(_recent_trades_table(trade_df, top_trades))
        sections.append("")

    # Execution signal breakdown
    if not exec_df.empty:
        sections.append("## Engine 訊號分析")
        sig_lines = _signal_type_breakdown(exec_df)
        if sig_lines:
            sections.extend(sig_lines)
            sections.append("")
        regime_lines = _regime_breakdown(exec_df)
        if regime_lines:
            sections.extend(regime_lines)
            sections.append("")

    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate backtest metrics dashboard")
    parser.add_argument("--top-trades", type=int, default=10, help="顯示最近幾筆結算交易")
    parser.add_argument("--print-only", action="store_true", help="只印到 stdout，不寫出 .md 檔")
    args = parser.parse_args()

    content = generate_metrics(top_trades=args.top_trades)

    if args.print_only:
        print(content)
    else:
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_MD.write_text(content, encoding="utf-8")
        print(f"[OK] Dashboard written to {OUTPUT_MD}")
        print(content)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
