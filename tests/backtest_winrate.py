"""
簡易短炒勝率回測（終端輸出）

策略（預設）：
1) 當日成交量 > 昨日成交量 * vol_increase_min
2) 當日收盤 > 昨日收盤
3) 當日收盤 > SMA5（可關閉）
符合即在當日收盤買入，持有 hold_days 天後收盤賣出。

資料來源：
- 預設讀 repo_outputs/daily_refresh/latest/ai_focus_list.csv 的 ticker
- 也可用 --symbols 直接指定
- 或使用 xq_pick_log 模式直接讀每日 XQ 入選記錄

範例：
python tests/backtest_winrate.py --start 2025-10-01 --end 2026-03-01
python tests/backtest_winrate.py --symbols NVAX,FA,CBZ --start 2025-12-01 --end 2026-03-01 --hold-days 3
python tests/backtest_winrate.py --mode xq-pick-log --start 2026-01-01 --end 2026-03-01
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

DEFAULT_FOCUS_FILE = Path("repo_outputs/daily_refresh/latest/ai_focus_list.csv")
DEFAULT_XQ_PICK_LOG_FILE = Path("repo_outputs/backtest/xq_pick_log.csv")


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    ret_pct: float
    signal_date: str = ""
    rank: int = 0
    source: str = ""


def parse_symbols(raw: str) -> List[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def parse_date_yyyy_mm_dd(date_str: str) -> datetime.date:
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def load_tickers(symbols: Optional[str], focus_file: Path, max_symbols: int) -> List[str]:
    if symbols:
        return parse_symbols(symbols)[:max_symbols]

    if focus_file.exists():
        df = pd.read_csv(focus_file)
        if "ticker" in df.columns:
            tickers = [str(v).strip().upper() for v in df["ticker"].tolist() if str(v).strip()]
            dedup = list(dict.fromkeys(tickers))
            return dedup[:max_symbols]

    return []


def load_xq_pick_log_entries(
    pick_log_file: Path,
    start: str,
    end: str,
    max_rank: int,
    max_symbols: int,
) -> pd.DataFrame:
    if not pick_log_file.exists():
        return pd.DataFrame()

    df = pd.read_csv(pick_log_file)
    required_cols = {"scan_date", "ticker"}
    if not required_cols.issubset(set(df.columns)):
        return pd.DataFrame()

    df = df.copy()
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["scan_date"] = pd.to_datetime(df["scan_date"], errors="coerce").dt.date
    if "rank" in df.columns:
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    else:
        df["rank"] = None
    if "source_file" not in df.columns:
        df["source_file"] = ""

    start_d = parse_date_yyyy_mm_dd(start)
    end_d = parse_date_yyyy_mm_dd(end)

    filtered = df[
        df["scan_date"].notna()
        & (df["scan_date"] >= start_d)
        & (df["scan_date"] <= end_d)
    ].copy()

    if len(filtered) == 0:
        return pd.DataFrame()

    filtered = filtered[filtered["ticker"] != ""]

    if max_rank > 0 and "rank" in filtered.columns:
        filtered = filtered[(filtered["rank"].isna()) | (filtered["rank"] <= max_rank)]

    if len(filtered) == 0:
        return pd.DataFrame()

    # 同一天同 ticker 只留 rank 最佳一筆
    filtered = filtered.sort_values(by=["scan_date", "ticker", "rank"], ascending=[True, True, True], na_position="last")
    filtered = filtered.drop_duplicates(subset=["scan_date", "ticker"], keep="first")

    # 控制總回測 ticker universe 數量，避免 API 負擔過大
    universe = filtered["ticker"].drop_duplicates().head(max_symbols).tolist()
    filtered = filtered[filtered["ticker"].isin(universe)].copy()
    return filtered.reset_index(drop=True)


def fetch_history(ticker: str, start: str, end: str) -> pd.DataFrame:
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
    except Exception:
        return pd.DataFrame()

    if hist is None or hist.empty:
        return pd.DataFrame()

    hist = hist.copy()
    hist = hist[[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in hist.columns]]
    if "Close" not in hist.columns or "Volume" not in hist.columns:
        return pd.DataFrame()

    hist = hist.dropna(subset=["Close", "Volume"])
    hist["sma5"] = hist["Close"].rolling(5).mean()
    hist["close_prev"] = hist["Close"].shift(1)
    hist["vol_prev"] = hist["Volume"].shift(1)
    return hist


def build_price_cache_for_pick_log(entries: pd.DataFrame, start: str, end: str) -> Dict[str, pd.DataFrame]:
    if entries is None or len(entries) == 0:
        return {}

    start_d = parse_date_yyyy_mm_dd(start)
    end_d = parse_date_yyyy_mm_dd(end) + timedelta(days=20)
    cache: Dict[str, pd.DataFrame] = {}
    for ticker in entries["ticker"].drop_duplicates().tolist():
        hist = fetch_history(ticker, start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d"))
        if hist is None or hist.empty:
            continue
        cache[ticker] = hist
    return cache


def find_entry_exit_from_signal_date(hist: pd.DataFrame, signal_date: datetime.date, hold_days: int) -> Optional[Tuple[pd.Timestamp, pd.Timestamp, float, float]]:
    if hist is None or hist.empty:
        return None

    trading_days = hist.index.normalize()
    signal_ts = pd.Timestamp(signal_date)

    # 找 signal_date 當天或之後的第一個交易日
    pos = trading_days.searchsorted(signal_ts)
    if pos >= len(hist):
        return None

    entry_idx = pos
    exit_idx = entry_idx + hold_days
    if exit_idx >= len(hist):
        return None

    entry_row = hist.iloc[entry_idx]
    exit_row = hist.iloc[exit_idx]
    entry_price = float(entry_row["Close"])
    exit_price = float(exit_row["Close"])
    if entry_price <= 0:
        return None

    return hist.index[entry_idx], hist.index[exit_idx], entry_price, exit_price


def run_backtest_from_xq_pick_log(entries: pd.DataFrame, hold_days: int) -> List[Trade]:
    if entries is None or len(entries) == 0:
        return []

    start = str(entries["scan_date"].min())
    end = str(entries["scan_date"].max())
    price_cache = build_price_cache_for_pick_log(entries, start, end)

    trades: List[Trade] = []
    for _, row in entries.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        signal_date = row.get("scan_date")
        if not ticker or pd.isna(signal_date):
            continue

        hist = price_cache.get(ticker)
        result = find_entry_exit_from_signal_date(hist, signal_date, hold_days)
        if result is None:
            continue

        entry_dt, exit_dt, entry_price, exit_price = result
        ret_pct = ((exit_price / entry_price) - 1) * 100
        rank_val = row.get("rank")
        rank_num = int(rank_val) if pd.notna(rank_val) else 0

        trades.append(
            Trade(
                ticker=ticker,
                signal_date=pd.Timestamp(signal_date).strftime("%Y-%m-%d"),
                entry_date=entry_dt.strftime("%Y-%m-%d"),
                exit_date=exit_dt.strftime("%Y-%m-%d"),
                entry_price=entry_price,
                exit_price=exit_price,
                ret_pct=ret_pct,
                rank=rank_num,
                source=str(row.get("source_file", "")),
            )
        )

    return trades


def run_backtest_for_ticker(
    ticker: str,
    hist: pd.DataFrame,
    hold_days: int,
    vol_increase_min: float,
    require_above_sma5: bool,
) -> List[Trade]:
    trades: List[Trade] = []
    if hist.empty or len(hist) < 10:
        return trades

    idx = hist.index
    for i in range(6, len(hist) - hold_days):
        row = hist.iloc[i]

        if pd.isna(row["close_prev"]) or pd.isna(row["vol_prev"]) or row["vol_prev"] <= 0:
            continue
        if require_above_sma5 and (pd.isna(row["sma5"]) or row["Close"] <= row["sma5"]):
            continue

        cond_volume_up = row["Volume"] > row["vol_prev"] * vol_increase_min
        cond_price_up = row["Close"] > row["close_prev"]

        if not (cond_volume_up and cond_price_up):
            continue

        exit_row = hist.iloc[i + hold_days]
        entry_price = float(row["Close"])
        exit_price = float(exit_row["Close"])
        if entry_price <= 0:
            continue

        ret_pct = ((exit_price / entry_price) - 1) * 100
        trades.append(
            Trade(
                ticker=ticker,
                entry_date=idx[i].strftime("%Y-%m-%d"),
                exit_date=idx[i + hold_days].strftime("%Y-%m-%d"),
                entry_price=entry_price,
                exit_price=exit_price,
                ret_pct=ret_pct,
            )
        )

    return trades


def summarize(trades: List[Trade]) -> Dict[str, float]:
    if not trades:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "avg_ret": 0.0,
            "median_ret": 0.0,
            "sum_ret": 0.0,
        }

    returns = [t.ret_pct for t in trades]
    wins = [r for r in returns if r > 0]
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_ret": float(pd.Series(returns).mean()),
        "median_ret": float(pd.Series(returns).median()),
        "sum_ret": float(pd.Series(returns).sum()),
    }


def _filter_by_rank_range(trades: List[Trade], min_rank: int, max_rank: int) -> List[Trade]:
    return [t for t in trades if t.rank >= min_rank and t.rank <= max_rank]


def print_by_rank_report(trades: List[Trade]) -> None:
    rank_1_3 = _filter_by_rank_range(trades, 1, 3)
    rank_4_10 = _filter_by_rank_range(trades, 4, 10)

    s13 = summarize(rank_1_3)
    s410 = summarize(rank_4_10)

    print("\n--- Rank 區間比較（xq-pick-log）---")
    print("區間\t交易筆數\t勝率\t平均報酬\t中位數報酬\t總報酬")
    print(f"rank 1-3\t{s13['trades']}\t{s13['win_rate']:.2f}%\t{s13['avg_ret']:.2f}%\t{s13['median_ret']:.2f}%\t{s13['sum_ret']:.2f}%")
    print(f"rank 4-10\t{s410['trades']}\t{s410['win_rate']:.2f}%\t{s410['avg_ret']:.2f}%\t{s410['median_ret']:.2f}%\t{s410['sum_ret']:.2f}%")

    if s13['trades'] == 0 and s410['trades'] == 0:
        print("說明：目前沒有可比較的 rank 交易資料。")
        return

    if s13['win_rate'] > s410['win_rate']:
        print("結論：rank 1-3 勝率較高。")
    elif s13['win_rate'] < s410['win_rate']:
        print("結論：rank 4-10 勝率較高。")
    else:
        print("結論：兩個 rank 區間勝率相同。")


def print_report(trades: List[Trade], title: str = "短炒回測結果（量增 + 價強）") -> None:
    summary = summarize(trades)

    print(f"\n=== {title} ===")
    print(f"交易筆數: {summary['trades']}")
    print(f"勝率: {summary['win_rate']:.2f}%")
    print(f"平均報酬: {summary['avg_ret']:.2f}%")
    print(f"中位數報酬: {summary['median_ret']:.2f}%")
    print(f"總報酬加總: {summary['sum_ret']:.2f}%")

    if not trades:
        print("\n沒有產生交易訊號，請放寬條件或加大回測區間。")
        return

    df = pd.DataFrame([t.__dict__ for t in trades])
    per_ticker = (
        df.groupby("ticker", as_index=False)
        .agg(
            trades=("ret_pct", "count"),
            win_rate=("ret_pct", lambda s: (s.gt(0).mean() * 100)),
            avg_ret=("ret_pct", "mean"),
            median_ret=("ret_pct", "median"),
        )
        .sort_values(["win_rate", "avg_ret"], ascending=False)
    )

    print("\n--- 各標的表現（前 15）---")
    print(per_ticker.head(15).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    print("\n--- 最近 10 筆交易 ---")
    print(df.tail(10).to_string(index=False, float_format=lambda x: f"{x:.2f}"))


def main() -> None:
    parser = argparse.ArgumentParser(description="簡易短炒勝率回測")
    parser.add_argument("--mode", choices=["signal", "xq-pick-log"], default="signal", help="回測模式：signal=條件觸發、xq-pick-log=直接吃 xq_pick_log")
    parser.add_argument("--start", required=True, help="開始日期，例如 2025-10-01")
    parser.add_argument("--end", required=True, help="結束日期，例如 2026-03-01")
    parser.add_argument("--symbols", default="", help="逗號分隔 ticker，若不填則讀 ai_focus_list.csv")
    parser.add_argument("--focus-file", default=str(DEFAULT_FOCUS_FILE), help="ai_focus_list.csv 路徑")
    parser.add_argument("--xq-pick-log-file", default=str(DEFAULT_XQ_PICK_LOG_FILE), help="xq_pick_log.csv 路徑")
    parser.add_argument("--xq-max-rank", type=int, default=10, help="xq-pick-log 模式只吃前幾名（rank <= N）")
    parser.add_argument("--by-rank-report", action="store_true", help="xq-pick-log 模式額外輸出 rank 1-3 vs rank 4-10 勝率比較")
    parser.add_argument("--max-symbols", type=int, default=30, help="最多回測幾檔")
    parser.add_argument("--hold-days", type=int, default=1, help="持有天數（預設 1）")
    parser.add_argument("--vol-increase-min", type=float, default=1.05, help="量增門檻，1.05=比昨量多 5%%")
    parser.add_argument("--disable-sma5-filter", action="store_true", help="關閉 Close>SMA5 條件")

    args = parser.parse_args()

    if args.mode == "xq-pick-log":
        pick_log_file = Path(args.xq_pick_log_file)
        entries = load_xq_pick_log_entries(
            pick_log_file=pick_log_file,
            start=args.start,
            end=args.end,
            max_rank=args.xq_max_rank,
            max_symbols=args.max_symbols,
        )

        if len(entries) == 0:
            print("找不到可回測的 xq_pick_log 記錄，請先跑 scripts/update_xq_with_history.py 或確認日期區間")
            return

        print(f"模式: xq-pick-log")
        print(f"訊號筆數: {len(entries)}")
        print(f"日期區間: {args.start} ~ {args.end}")
        print(f"持有天數: {args.hold_days}")
        print(f"xq rank 限制: <= {args.xq_max_rank}")

        all_trades = run_backtest_from_xq_pick_log(entries, hold_days=args.hold_days)
        print_report(all_trades, title="短炒回測結果（xq_pick_log 模式）")
        if args.by_rank_report:
            print_by_rank_report(all_trades)
        return

    focus_file = Path(args.focus_file)
    tickers = load_tickers(args.symbols, focus_file, args.max_symbols)

    if not tickers:
        print("找不到可回測標的，請用 --symbols 指定，或先跑 main.py 產生 ai_focus_list.csv")
        return

    print(f"回測標的數: {len(tickers)}")
    print(f"日期區間: {args.start} ~ {args.end}")
    print(f"持有天數: {args.hold_days}")
    print(f"條件: 量增>{args.vol_increase_min:.2f}x 且 當日收盤>昨收")
    if args.disable_sma5_filter:
        print("SMA5 條件: 關閉")
    else:
        print("SMA5 條件: 開啟（收盤需 > SMA5）")

    all_trades: List[Trade] = []
    for ticker in tickers:
        hist = fetch_history(ticker, args.start, args.end)
        trades = run_backtest_for_ticker(
            ticker=ticker,
            hist=hist,
            hold_days=args.hold_days,
            vol_increase_min=args.vol_increase_min,
            require_above_sma5=not args.disable_sma5_filter,
        )
        all_trades.extend(trades)

    print_report(all_trades, title="短炒回測結果（量增 + 價強）")


if __name__ == "__main__":
    main()
