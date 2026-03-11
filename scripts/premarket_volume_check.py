"""
盤前量比對工具（終端輸出，不產生 CSV）

用途：
- 輸入少量候選標的（例如 AI 評分後前 3-8 檔）
- 比對昨量、5日均量、盤前量，快速判斷是否有短炒放量跡象

範例：
    python scripts/premarket_volume_check.py --symbols NVAX,FA,CBZ
    python scripts/premarket_volume_check.py --symbols NVAX,FA --manual-premarket NVAX=120000,FA=80000
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

RUNTIME_DATA_ERRORS = (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import install_builtin_print_logging

NY_TZ = ZoneInfo("America/New_York")

install_builtin_print_logging()


def parse_symbols(symbols_raw: str) -> List[str]:
    return [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]


def parse_manual_premarket(manual_raw: Optional[str]) -> Dict[str, int]:
    if not manual_raw:
        return {}

    result: Dict[str, int] = {}
    for item in manual_raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        ticker, value = item.split("=", 1)
        ticker = ticker.strip().upper()
        value = value.strip().replace("_", "")
        try:
            result[ticker] = int(float(value))
        except ValueError:
            continue
    return result


def get_daily_volume_baseline(ticker: str) -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """
    回傳：昨量、近 5 日均量、昨收
    """
    try:
        hist = yf.Ticker(ticker).history(period="15d", interval="1d", auto_adjust=False)
        if hist.empty or "Volume" not in hist.columns:
            return None, None, None

        hist = hist.dropna(subset=["Volume", "Close"])
        if len(hist) < 6:
            return None, None, None

        yday_volume = int(hist["Volume"].iloc[-1])
        avg5_volume = int(hist["Volume"].tail(5).mean())
        last_close = float(hist["Close"].iloc[-1])
        return yday_volume, avg5_volume, last_close
    except RUNTIME_DATA_ERRORS:
        return None, None, None


def get_yf_premarket_volume(ticker: str) -> Optional[int]:
    """
    估算今天美東 09:30 前盤前成交量。
    若 yfinance 沒給足夠 1m prepost 資料，回傳 None。
    """
    try:
        now_ny = datetime.now(NY_TZ)
        today_ny = now_ny.date()

        intraday = yf.Ticker(ticker).history(period="2d", interval="1m", prepost=True, auto_adjust=False)
        if intraday.empty or "Volume" not in intraday.columns:
            return None

        index_ny = intraday.index.tz_convert(NY_TZ) if intraday.index.tz is not None else intraday.index.tz_localize(NY_TZ)
        intraday = intraday.copy()
        intraday.index = index_ny

        mask_today = intraday.index.date == today_ny
        if not mask_today.any():
            return None

        today_df = intraday.loc[mask_today]
        premarket_df = today_df[today_df.index.time < datetime.strptime("09:30", "%H:%M").time()]
        if premarket_df.empty:
            return None

        return int(premarket_df["Volume"].fillna(0).sum())
    except RUNTIME_DATA_ERRORS:
        return None


def classify_premarket(pre_vs_avg5: Optional[float], pre_vs_yday: Optional[float]) -> str:
    if pre_vs_avg5 is None or pre_vs_yday is None:
        return "資料不足"
    if pre_vs_avg5 >= 0.35 and pre_vs_yday >= 0.30:
        return "強放量"
    if pre_vs_avg5 >= 0.20 and pre_vs_yday >= 0.15:
        return "偏強"
    if pre_vs_avg5 >= 0.10:
        return "中性"
    return "偏弱"


def fmt_num(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:.1f}%"


def run(symbols: List[str], manual_premarket: Dict[str, int]) -> None:
    rows = []

    print("\n=== 盤前量比對（短炒前置）===")
    print("說明：盤前量可用 yfinance 估算；若你用 TradingView Essential，建議手動值覆蓋更準。\n")

    for ticker in symbols:
        yday_volume, avg5_volume, last_close = get_daily_volume_baseline(ticker)
        yf_premarket = get_yf_premarket_volume(ticker)
        final_premarket = manual_premarket.get(ticker, yf_premarket)

        pre_vs_avg5 = None
        pre_vs_yday = None
        if final_premarket is not None and avg5_volume and avg5_volume > 0:
            pre_vs_avg5 = final_premarket / avg5_volume
        if final_premarket is not None and yday_volume and yday_volume > 0:
            pre_vs_yday = final_premarket / yday_volume

        signal = classify_premarket(pre_vs_avg5, pre_vs_yday)
        source = "TV手動" if ticker in manual_premarket else ("YF估算" if yf_premarket is not None else "無")

        rows.append(
            {
                "Ticker": ticker,
                "Last_Close": last_close,
                "Yday_Vol": yday_volume,
                "Avg5_Vol": avg5_volume,
                "Premarket_Vol": final_premarket,
                "Pre/Yday": pre_vs_yday,
                "Pre/Avg5": pre_vs_avg5,
                "Signal": signal,
                "Source": source,
            }
        )

    if not rows:
        print("無可用標的")
        return

    df = pd.DataFrame(rows)
    if "Pre/Avg5" in df.columns:
        df = df.sort_values(by="Pre/Avg5", ascending=False, na_position="last")

    display_cols = ["Ticker", "Last_Close", "Yday_Vol", "Avg5_Vol", "Premarket_Vol", "Pre/Yday", "Pre/Avg5", "Signal", "Source"]

    print("\t".join(display_cols))
    for _, row in df.iterrows():
        print(
            "\t".join(
                [
                    str(row.get("Ticker", "-")),
                    fmt_num(row.get("Last_Close")),
                    fmt_num(row.get("Yday_Vol")),
                    fmt_num(row.get("Avg5_Vol")),
                    fmt_num(row.get("Premarket_Vol")),
                    fmt_pct(row.get("Pre/Yday")),
                    fmt_pct(row.get("Pre/Avg5")),
                    str(row.get("Signal", "-")),
                    str(row.get("Source", "-")),
                ]
            )
        )

    print("\n建議：先看 Signal=強放量/偏強，再搭配你 XQ 分數與盤中 VWAP 做進場。")


def main():
    parser = argparse.ArgumentParser(description="盤前量比對工具（終端輸出）")
    parser.add_argument("--symbols", type=str, required=True, help="逗號分隔股票代碼，例如 NVAX,FA,CBZ")
    parser.add_argument(
        "--manual-premarket",
        type=str,
        default="",
        help="可選，手動覆蓋盤前量，例如 NVAX=120000,FA=80000（建議貼 TradingView 值）",
    )

    args = parser.parse_args()
    symbols = parse_symbols(args.symbols)
    manual = parse_manual_premarket(args.manual_premarket)

    if not symbols:
        print("請提供至少一檔股票代碼")
        return

    run(symbols, manual)


if __name__ == "__main__":
    main()
