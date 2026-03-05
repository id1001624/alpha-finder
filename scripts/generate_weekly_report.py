"""
每週制度化評估報告（零人工）

功能：
- 讀取 repo_outputs/backtest/xq_pick_log.csv 的一週訊號
- 讀取 repo_outputs/daily_refresh/*/*/ai_focus_list.csv 的每日焦點名單
- 讀取 repo_outputs/backtest/ai_decision_log.csv 的 AI 決策訊號
- 用 Yahoo Finance 回算持有報酬（預設 hold_days=1）
- 產生單一 Markdown 週報 + 交易明細 CSV
- 同步更新 latest 檔案，供 AI 直接讀取

用法：
python scripts/generate_weekly_report.py
python scripts/generate_weekly_report.py --lookback-days 7 --hold-days 1
python scripts/generate_weekly_report.py --start-date 2026-03-01 --end-date 2026-03-07
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List


def _fix_ssl_cert_path():
    try:
        import certifi

        original = certifi.where()
        try:
            original.encode("ascii")
            return
        except UnicodeEncodeError:
            pass

        safe_dir = os.path.join(os.path.expanduser("~"), ".alpha_finder_certs")
        os.makedirs(safe_dir, exist_ok=True)
        safe_cert = os.path.join(safe_dir, "cacert.pem")

        if not os.path.exists(safe_cert) or os.path.getmtime(original) > os.path.getmtime(safe_cert):
            shutil.copy2(original, safe_cert)

        os.environ["CURL_CA_BUNDLE"] = safe_cert
        os.environ["SSL_CERT_FILE"] = safe_cert
        os.environ["REQUESTS_CA_BUNDLE"] = safe_cert
        os.environ["SSL_NO_VERIFY"] = "0"
    except (ModuleNotFoundError, FileNotFoundError, PermissionError, UnicodeEncodeError):
        pass


_fix_ssl_cert_path()

import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
DEFAULT_PICK_LOG_FILE = BACKTEST_DIR / "xq_pick_log.csv"
DEFAULT_AI_DECISION_LOG_FILE = BACKTEST_DIR / "ai_decision_log.csv"
DEFAULT_DAILY_REFRESH_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh"
DEFAULT_OUTPUT_DIR = BACKTEST_DIR / "weekly_reports"
DEFAULT_LATEST_MD = "weekly_report_latest.md"
DEFAULT_LATEST_TRADES = "weekly_trades_latest.csv"
DEFAULT_LATEST_FUSION_TRADES = "weekly_fusion_trades_latest.csv"
DEFAULT_LATEST_AI_TRADES = "weekly_ai_trades_latest.csv"

import config as _config
from power_awake import keep_system_awake

WEEKLY_REPORT_LOOKBACK_DAYS = int(getattr(_config, "WEEKLY_REPORT_LOOKBACK_DAYS", 7))
WEEKLY_REPORT_HOLD_DAYS = int(getattr(_config, "WEEKLY_REPORT_HOLD_DAYS", 1))
WEEKLY_REPORT_MAX_RANK = int(getattr(_config, "WEEKLY_REPORT_MAX_RANK", 10))
WEEKLY_REPORT_MAX_SYMBOLS = int(getattr(_config, "WEEKLY_REPORT_MAX_SYMBOLS", 80))
WEEKLY_REPORT_OUTPUT_DIR = str(getattr(_config, "WEEKLY_REPORT_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
WEEKLY_REPORT_DAILY_REFRESH_DIR = str(getattr(_config, "LOCAL_OUTPUT_DIR", DEFAULT_DAILY_REFRESH_DIR))


def parse_date_yyyy_mm_dd(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def normalize_ticker(raw: str) -> str:
    ticker = str(raw or "").strip().upper()
    if ticker.endswith(".US"):
        ticker = ticker[:-3]
    return ticker


def resolve_window(start_date: str, end_date: str, lookback_days: int) -> tuple[date, date]:
    if end_date:
        end_d = parse_date_yyyy_mm_dd(end_date)
    else:
        end_d = date.today()

    if start_date:
        start_d = parse_date_yyyy_mm_dd(start_date)
    else:
        start_d = end_d - timedelta(days=max(1, lookback_days) - 1)

    if start_d > end_d:
        raise ValueError("start-date 不可晚於 end-date")

    return start_d, end_d


def load_weekly_entries(
    pick_log_file: Path,
    start_d: date,
    end_d: date,
    max_rank: int,
    max_symbols: int,
) -> pd.DataFrame:
    if not pick_log_file.exists():
        return pd.DataFrame()

    df = pd.read_csv(pick_log_file)
    if "scan_date" not in df.columns or "ticker" not in df.columns:
        return pd.DataFrame()

    out = df.copy()
    out["scan_date"] = pd.to_datetime(out["scan_date"], errors="coerce").dt.date
    out["ticker_raw"] = out["ticker"].astype(str).str.strip().str.upper()
    out["ticker"] = out["ticker_raw"].apply(normalize_ticker)

    if "rank" in out.columns:
        out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    else:
        out["rank"] = pd.NA

    out = out[
        out["scan_date"].notna()
        & (out["scan_date"] >= start_d)
        & (out["scan_date"] <= end_d)
        & (out["ticker"] != "")
    ].copy()

    if len(out) == 0:
        return pd.DataFrame()

    if max_rank > 0:
        out = out[(out["rank"].isna()) | (out["rank"] <= max_rank)]

    if len(out) == 0:
        return pd.DataFrame()

    out = out.sort_values(by=["scan_date", "ticker_raw", "rank"], ascending=[True, True, True], na_position="last")
    out = out.drop_duplicates(subset=["scan_date", "ticker_raw"], keep="first")

    if max_symbols > 0:
        universe = out["ticker"].drop_duplicates().head(max_symbols).tolist()
        out = out[out["ticker"].isin(universe)].copy()

    keep_cols = ["scan_date", "source_file", "rank", "ticker_raw", "ticker", "short_trade_score"]
    for col in keep_cols:
        if col not in out.columns:
            out[col] = pd.NA

    return out[keep_cols].reset_index(drop=True)


def load_ai_decision_entries(
    ai_log_file: Path,
    start_d: date,
    end_d: date,
    max_rank: int,
    max_symbols: int,
) -> pd.DataFrame:
    if not ai_log_file.exists():
        return pd.DataFrame()

    df = pd.read_csv(ai_log_file)
    if "decision_date" not in df.columns or "ticker" not in df.columns:
        return pd.DataFrame()

    out = df.copy()
    out["decision_date"] = pd.to_datetime(out["decision_date"], errors="coerce").dt.date
    out["ticker_raw"] = out["ticker"].astype(str).str.strip().str.upper()
    out["ticker"] = out["ticker_raw"].apply(normalize_ticker)

    if "rank" in out.columns:
        out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    else:
        out["rank"] = pd.NA

    out = out[
        out["decision_date"].notna()
        & (out["decision_date"] >= start_d)
        & (out["decision_date"] <= end_d)
        & (out["ticker"] != "")
    ].copy()

    if len(out) == 0:
        return pd.DataFrame()

    if max_rank > 0:
        out = out[(out["rank"].isna()) | (out["rank"] <= max_rank)]

    if len(out) == 0:
        return pd.DataFrame()

    out = out.sort_values(by=["decision_date", "ticker_raw", "rank"], ascending=[True, True, True], na_position="last")
    out = out.drop_duplicates(subset=["decision_date", "ticker_raw"], keep="first")

    if max_symbols > 0:
        universe = out["ticker"].drop_duplicates().head(max_symbols).tolist()
        out = out[out["ticker"].isin(universe)].copy()

    keep_cols = ["decision_date", "source_ref", "rank", "ticker_raw", "ticker", "short_score_final"]
    for col in keep_cols:
        if col not in out.columns:
            out[col] = pd.NA

    return out[keep_cols].reset_index(drop=True)


def _parse_date_dir_name(raw: str) -> date | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def load_ai_focus_entries(
    daily_refresh_dir: Path,
    start_d: date,
    end_d: date,
    max_rank: int,
    max_symbols: int,
) -> pd.DataFrame:
    if not daily_refresh_dir.exists() or not daily_refresh_dir.is_dir():
        return pd.DataFrame()

    entries: List[pd.DataFrame] = []
    for date_dir in sorted([path for path in daily_refresh_dir.iterdir() if path.is_dir()]):
        scan_d = _parse_date_dir_name(date_dir.name)
        if scan_d is None:
            continue
        if scan_d < start_d or scan_d > end_d:
            continue

        run_dirs = sorted([path for path in date_dir.iterdir() if path.is_dir()])
        if len(run_dirs) == 0:
            continue

        latest_run_dir = run_dirs[-1]
        focus_file = latest_run_dir / "ai_focus_list.csv"
        if not focus_file.exists():
            continue

        try:
            df = pd.read_csv(focus_file)
        except (pd.errors.EmptyDataError, FileNotFoundError, PermissionError, OSError):
            continue

        if "ticker" not in df.columns:
            continue

        out = df.copy()
        out["scan_date"] = scan_d
        out["ticker_raw"] = out["ticker"].astype(str).str.strip().str.upper()
        out["ticker"] = out["ticker_raw"].apply(normalize_ticker)
        out = out[out["ticker"] != ""].copy()
        if len(out) == 0:
            continue

        out["priority_score"] = pd.to_numeric(out.get("priority_score"), errors="coerce")

        if "rank" in out.columns:
            out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
        else:
            out = out.sort_values(by=["priority_score", "ticker_raw"], ascending=[False, True], na_position="last").reset_index(drop=True)
            out["rank"] = out.index + 1

        out["source_file"] = f"{scan_d}_ai_focus_list.csv"
        out["short_trade_score"] = out["priority_score"]

        keep_cols = ["scan_date", "source_file", "rank", "ticker_raw", "ticker", "short_trade_score"]
        entries.append(out[keep_cols])

    if len(entries) == 0:
        return pd.DataFrame()

    merged = pd.concat(entries, ignore_index=True)
    merged["rank"] = pd.to_numeric(merged["rank"], errors="coerce")
    if max_rank > 0:
        merged = merged[(merged["rank"].isna()) | (merged["rank"] <= max_rank)]

    if len(merged) == 0:
        return pd.DataFrame()

    merged["short_trade_score"] = pd.to_numeric(merged["short_trade_score"], errors="coerce")
    merged["_score"] = merged["short_trade_score"].fillna(-1e9)
    merged = merged.sort_values(
        by=["scan_date", "_score", "rank", "ticker_raw"],
        ascending=[True, False, True, True],
        na_position="last",
    )
    merged = merged.drop_duplicates(subset=["scan_date", "ticker_raw"], keep="first")
    merged = merged.drop(columns=["_score"])

    if max_symbols > 0:
        universe = merged["ticker"].drop_duplicates().head(max_symbols).tolist()
        merged = merged[merged["ticker"].isin(universe)].copy()

    return merged.reset_index(drop=True)


def build_local_fusion_entries(
    xq_entries: pd.DataFrame,
    ai_focus_entries: pd.DataFrame,
    max_rank: int,
    max_symbols: int,
) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    if xq_entries is not None and len(xq_entries) > 0:
        frames.append(xq_entries.copy())
    if ai_focus_entries is not None and len(ai_focus_entries) > 0:
        frames.append(ai_focus_entries.copy())

    if len(frames) == 0:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["short_trade_score"] = pd.to_numeric(out["short_trade_score"], errors="coerce")
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["_score"] = out["short_trade_score"].fillna(-1e9)

    out = out.sort_values(
        by=["scan_date", "_score", "rank", "ticker_raw"],
        ascending=[True, False, True, True],
        na_position="last",
    )
    out = out.drop_duplicates(subset=["scan_date", "ticker_raw"], keep="first")
    out["rank"] = out.groupby("scan_date").cumcount() + 1

    if max_rank > 0:
        out = out[out["rank"] <= max_rank]

    if len(out) == 0:
        return pd.DataFrame()

    if max_symbols > 0:
        universe = out["ticker"].drop_duplicates().head(max_symbols).tolist()
        out = out[out["ticker"].isin(universe)].copy()

    keep_cols = ["scan_date", "source_file", "rank", "ticker_raw", "ticker", "short_trade_score"]
    return out[keep_cols].reset_index(drop=True)


def fetch_price_cache(entries: pd.DataFrame, start_d: date, end_d: date, hold_days: int) -> Dict[str, pd.DataFrame]:
    if entries is None or len(entries) == 0:
        return {}

    fetch_start = (start_d - timedelta(days=5)).strftime("%Y-%m-%d")
    fetch_end = (end_d + timedelta(days=max(hold_days + 7, 10))).strftime("%Y-%m-%d")

    cache: Dict[str, pd.DataFrame] = {}
    for ticker in entries["ticker"].drop_duplicates().tolist():
        try:
            hist = yf.Ticker(ticker).history(start=fetch_start, end=fetch_end, interval="1d", auto_adjust=False)
        except (TypeError, KeyError, AttributeError, ValueError):
            continue

        if hist is None or hist.empty or "Close" not in hist.columns:
            continue

        hist = hist.dropna(subset=["Close"]).copy()
        if len(hist) == 0:
            continue

        cache[ticker] = hist

    return cache


def build_trades(
    entries: pd.DataFrame,
    price_cache: Dict[str, pd.DataFrame],
    hold_days: int,
    signal_date_col: str,
    source_col: str,
    score_col: str,
    default_source: str,
) -> pd.DataFrame:
    trades: List[dict] = []

    for _, row in entries.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        signal_date = row.get(signal_date_col)
        hist = price_cache.get(ticker)

        if not ticker or pd.isna(signal_date) or hist is None or hist.empty:
            continue

        hist_index = pd.DatetimeIndex(hist.index)
        if hist_index.tz is not None:
            hist_index = hist_index.tz_convert(None)

        signal_ts = pd.Timestamp(signal_date).normalize()
        trading_days = pd.to_datetime([pd.Timestamp(item).strftime("%Y-%m-%d") for item in hist_index])
        entry_idx = trading_days.searchsorted(signal_ts)
        exit_idx = entry_idx + hold_days

        if entry_idx >= len(hist) or exit_idx >= len(hist):
            continue

        entry_price = float(hist.iloc[entry_idx]["Close"])
        exit_price = float(hist.iloc[exit_idx]["Close"])
        if entry_price <= 0:
            continue

        ret_pct = (exit_price / entry_price - 1) * 100
        trades.append(
            {
                "signal_date": pd.Timestamp(signal_date).strftime("%Y-%m-%d"),
                "entry_date": hist.index[entry_idx].strftime("%Y-%m-%d"),
                "exit_date": hist.index[exit_idx].strftime("%Y-%m-%d"),
                "ticker": ticker,
                "ticker_raw": row.get("ticker_raw", ticker),
                "rank": int(row["rank"]) if pd.notna(row.get("rank")) else 0,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "ret_pct": ret_pct,
                "ret_tag": "WIN" if ret_pct > 0 else ("FLAT" if abs(ret_pct) < 1e-12 else "LOSS"),
                "source_file": row.get(source_col, default_source),
                "short_trade_score": pd.to_numeric(row.get(score_col), errors="coerce"),
                "entry_ts": hist_index[entry_idx],
                "exit_ts": hist_index[exit_idx],
            }
        )

    if not trades:
        return pd.DataFrame()

    out = pd.DataFrame(trades).sort_values(["signal_date", "rank", "ticker"]).reset_index(drop=True)
    out["entry_date"] = pd.to_datetime(out["entry_ts"]).dt.strftime("%Y-%m-%d")
    out["exit_date"] = pd.to_datetime(out["exit_ts"]).dt.strftime("%Y-%m-%d")
    return out.drop(columns=["entry_ts", "exit_ts"])


def calc_max_drawdown_pct(trades_df: pd.DataFrame) -> float:
    if trades_df is None or len(trades_df) == 0:
        return 0.0

    series = trades_df.sort_values(["exit_date", "signal_date"])["ret_pct"].astype(float) / 100.0
    equity = (1 + series).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1
    return float(drawdown.min() * 100)


def summarize_trades(trades_df: pd.DataFrame) -> dict:
    if trades_df is None or len(trades_df) == 0:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "avg_ret": 0.0,
            "median_ret": 0.0,
            "sum_ret": 0.0,
            "max_drawdown": 0.0,
        }

    ret_series = trades_df["ret_pct"].astype(float)
    return {
        "trades": int(len(ret_series)),
        "win_rate": float(ret_series.gt(0).mean() * 100),
        "avg_ret": float(ret_series.mean()),
        "median_ret": float(ret_series.median()),
        "sum_ret": float(ret_series.sum()),
        "max_drawdown": float(calc_max_drawdown_pct(trades_df)),
    }


def summarize_by_rank_bucket(trades_df: pd.DataFrame) -> pd.DataFrame:
    if trades_df is None or len(trades_df) == 0:
        return pd.DataFrame(columns=["bucket", "trades", "win_rate", "avg_ret", "median_ret", "sum_ret"])

    buckets = [
        ("rank 1-3", trades_df[(trades_df["rank"] >= 1) & (trades_df["rank"] <= 3)]),
        ("rank 4-10", trades_df[(trades_df["rank"] >= 4) & (trades_df["rank"] <= 10)]),
    ]

    rows = []
    for label, subset in buckets:
        s = summarize_trades(subset)
        rows.append(
            {
                "bucket": label,
                "trades": s["trades"],
                "win_rate": s["win_rate"],
                "avg_ret": s["avg_ret"],
                "median_ret": s["median_ret"],
                "sum_ret": s["sum_ret"],
            }
        )

    return pd.DataFrame(rows)


def build_strategy_comparison(
    local_entries: pd.DataFrame,
    local_trades: pd.DataFrame,
    fusion_entries: pd.DataFrame,
    fusion_trades: pd.DataFrame,
    ai_entries: pd.DataFrame,
    ai_trades: pd.DataFrame,
) -> pd.DataFrame:
    local = summarize_trades(local_trades)
    fusion = summarize_trades(fusion_trades)
    ai = summarize_trades(ai_trades)

    delta_fusion_win_rate = ai["win_rate"] - fusion["win_rate"]
    delta_fusion_avg_ret = ai["avg_ret"] - fusion["avg_ret"]
    delta_fusion_sum_ret = ai["sum_ret"] - fusion["sum_ret"]
    delta_fusion_drawdown_improve = fusion["max_drawdown"] - ai["max_drawdown"]

    rows = [
        {
            "strategy": "Local(XQ Pick Log)",
            "signals": len(local_entries),
            "trades": local["trades"],
            "win_rate": local["win_rate"],
            "avg_ret": local["avg_ret"],
            "sum_ret": local["sum_ret"],
            "max_drawdown": local["max_drawdown"],
            "ai_minus_fusion_win_rate": 0.0,
            "ai_minus_fusion_avg_ret": 0.0,
            "ai_minus_fusion_sum_ret": 0.0,
            "ai_fusion_drawdown_improve": 0.0,
        },
        {
            "strategy": "Local-Fusion(XQ + ai_focus)",
            "signals": len(fusion_entries),
            "trades": fusion["trades"],
            "win_rate": fusion["win_rate"],
            "avg_ret": fusion["avg_ret"],
            "sum_ret": fusion["sum_ret"],
            "max_drawdown": fusion["max_drawdown"],
            "ai_minus_fusion_win_rate": 0.0,
            "ai_minus_fusion_avg_ret": 0.0,
            "ai_minus_fusion_sum_ret": 0.0,
            "ai_fusion_drawdown_improve": 0.0,
        },
        {
            "strategy": "AI(Decision Log)",
            "signals": len(ai_entries),
            "trades": ai["trades"],
            "win_rate": ai["win_rate"],
            "avg_ret": ai["avg_ret"],
            "sum_ret": ai["sum_ret"],
            "max_drawdown": ai["max_drawdown"],
            "ai_minus_fusion_win_rate": delta_fusion_win_rate,
            "ai_minus_fusion_avg_ret": delta_fusion_avg_ret,
            "ai_minus_fusion_sum_ret": delta_fusion_sum_ret,
            "ai_fusion_drawdown_improve": delta_fusion_drawdown_improve,
        },
    ]
    return pd.DataFrame(rows)


def _fmt_num(value: object, digits: int = 2) -> str:
    num = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(num):
        return ""
    return f"{float(num):.{digits}f}"


def _to_markdown_table(df: pd.DataFrame, columns: List[str], percent_cols: List[str] | None = None, int_cols: List[str] | None = None) -> str:
    if df is None or len(df) == 0:
        return "（無資料）"

    percent_cols = set(percent_cols or [])
    int_cols = set(int_cols or [])

    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, sep]

    for _, row in df.iterrows():
        cells = []
        for col in columns:
            value = row.get(col, "")
            if col in percent_cols:
                text = f"{_fmt_num(value)}%"
            elif col in int_cols:
                text = str(int(float(value))) if pd.notna(value) and str(value) != "" else "0"
            elif isinstance(value, float):
                text = _fmt_num(value)
            else:
                text = str(value)
            cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def build_report_markdown(
    start_d: date,
    end_d: date,
    hold_days: int,
    max_rank: int,
    local_entries_df: pd.DataFrame,
    local_trades_df: pd.DataFrame,
    fusion_entries_df: pd.DataFrame,
    fusion_trades_df: pd.DataFrame,
    ai_entries_df: pd.DataFrame,
    ai_trades_df: pd.DataFrame,
) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    local_overall = summarize_trades(local_trades_df)
    fusion_overall = summarize_trades(fusion_trades_df)
    ai_overall = summarize_trades(ai_trades_df)

    ai_delta_fusion_win_rate = ai_overall["win_rate"] - fusion_overall["win_rate"]
    ai_delta_fusion_avg_ret = ai_overall["avg_ret"] - fusion_overall["avg_ret"]
    ai_delta_fusion_sum_ret = ai_overall["sum_ret"] - fusion_overall["sum_ret"]
    ai_delta_fusion_drawdown_improve = fusion_overall["max_drawdown"] - ai_overall["max_drawdown"]

    ai_delta_local_win_rate = ai_overall["win_rate"] - local_overall["win_rate"]
    ai_delta_local_avg_ret = ai_overall["avg_ret"] - local_overall["avg_ret"]
    ai_delta_local_sum_ret = ai_overall["sum_ret"] - local_overall["sum_ret"]
    ai_delta_local_drawdown_improve = local_overall["max_drawdown"] - ai_overall["max_drawdown"]

    strategy_compare_df = build_strategy_comparison(
        local_entries_df,
        local_trades_df,
        fusion_entries_df,
        fusion_trades_df,
        ai_entries_df,
        ai_trades_df,
    )
    rank_df = summarize_by_rank_bucket(fusion_trades_df)

    top_winners = (
        fusion_trades_df.sort_values("ret_pct", ascending=False)
        .head(10)
        .loc[:, ["signal_date", "ticker", "rank", "ret_pct", "entry_date", "exit_date"]]
        if fusion_trades_df is not None and len(fusion_trades_df) > 0
        else pd.DataFrame()
    )
    top_losers = (
        fusion_trades_df.sort_values("ret_pct", ascending=True)
        .head(10)
        .loc[:, ["signal_date", "ticker", "rank", "ret_pct", "entry_date", "exit_date"]]
        if fusion_trades_df is not None and len(fusion_trades_df) > 0
        else pd.DataFrame()
    )

    local_journal_df = (
        local_trades_df.loc[:, ["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"]]
        if local_trades_df is not None and len(local_trades_df) > 0
        else pd.DataFrame()
    )

    fusion_journal_df = (
        fusion_trades_df.loc[:, ["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"]]
        if fusion_trades_df is not None and len(fusion_trades_df) > 0
        else pd.DataFrame()
    )

    ai_journal_df = (
        ai_trades_df.loc[:, ["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"]]
        if ai_trades_df is not None and len(ai_trades_df) > 0
        else pd.DataFrame()
    )

    lines = [
        "# Alpha Finder 週度制度化評估報告",
        "",
        f"- 產生時間：{generated_at}",
        f"- 週期：{start_d} ~ {end_d}",
        f"- 回測持有天數：{hold_days}",
        f"- rank 限制：<= {max_rank}",
        "",
        "## 三軌總覽（Local / Local-Fusion / AI）",
        "",
        f"- Local 訊號筆數（xq_pick_log）：{len(local_entries_df)}",
        f"- Local 可回算交易筆數：{local_overall['trades']}",
        f"- Local 勝率：{local_overall['win_rate']:.2f}%",
        f"- Local 平均報酬：{local_overall['avg_ret']:.2f}%",
        f"- Local 中位數報酬：{local_overall['median_ret']:.2f}%",
        f"- Local 報酬加總：{local_overall['sum_ret']:.2f}%",
        f"- Local 最大回撤：{local_overall['max_drawdown']:.2f}%",
        "",
        f"- Local-Fusion 訊號筆數（xq_pick_log + ai_focus_list）：{len(fusion_entries_df)}",
        f"- Local-Fusion 可回算交易筆數：{fusion_overall['trades']}",
        f"- Local-Fusion 勝率：{fusion_overall['win_rate']:.2f}%",
        f"- Local-Fusion 平均報酬：{fusion_overall['avg_ret']:.2f}%",
        f"- Local-Fusion 中位數報酬：{fusion_overall['median_ret']:.2f}%",
        f"- Local-Fusion 報酬加總：{fusion_overall['sum_ret']:.2f}%",
        f"- Local-Fusion 最大回撤：{fusion_overall['max_drawdown']:.2f}%",
        "",
        f"- AI 訊號筆數（ai_decision_log）：{len(ai_entries_df)}",
        f"- AI 可回算交易筆數：{ai_overall['trades']}",
        f"- AI 勝率：{ai_overall['win_rate']:.2f}%",
        f"- AI 平均報酬：{ai_overall['avg_ret']:.2f}%",
        f"- AI 中位數報酬：{ai_overall['median_ret']:.2f}%",
        f"- AI 報酬加總：{ai_overall['sum_ret']:.2f}%",
        f"- AI 最大回撤：{ai_overall['max_drawdown']:.2f}%",
        f"- AI 相對 Local-Fusion 差值（主比較）：勝率 {ai_delta_fusion_win_rate:+.2f}%｜平均報酬 {ai_delta_fusion_avg_ret:+.2f}%｜報酬加總 {ai_delta_fusion_sum_ret:+.2f}%｜回撤改善 {ai_delta_fusion_drawdown_improve:+.2f}%",
        f"- AI 相對 Local 差值（參考）：勝率 {ai_delta_local_win_rate:+.2f}%｜平均報酬 {ai_delta_local_avg_ret:+.2f}%｜報酬加總 {ai_delta_local_sum_ret:+.2f}%｜回撤改善 {ai_delta_local_drawdown_improve:+.2f}%",
        "",
        "## 策略比較表",
        "",
        _to_markdown_table(
            strategy_compare_df,
            columns=[
                "strategy",
                "signals",
                "trades",
                "win_rate",
                "avg_ret",
                "sum_ret",
                "max_drawdown",
                "ai_minus_fusion_win_rate",
                "ai_minus_fusion_avg_ret",
                "ai_minus_fusion_sum_ret",
                "ai_fusion_drawdown_improve",
            ],
            percent_cols=[
                "win_rate",
                "avg_ret",
                "sum_ret",
                "max_drawdown",
                "ai_minus_fusion_win_rate",
                "ai_minus_fusion_avg_ret",
                "ai_minus_fusion_sum_ret",
                "ai_fusion_drawdown_improve",
            ],
            int_cols=["signals", "trades"],
        ),
        "",
        "## Rank 區間比較",
        "",
        _to_markdown_table(
            rank_df,
            columns=["bucket", "trades", "win_rate", "avg_ret", "median_ret", "sum_ret"],
            percent_cols=["win_rate", "avg_ret", "median_ret", "sum_ret"],
            int_cols=["trades"],
        ),
        "",
        "## 本週強弱勢（Top 10）",
        "",
        "### Top Winners",
        _to_markdown_table(
            top_winners,
            columns=["signal_date", "ticker", "rank", "ret_pct", "entry_date", "exit_date"],
            percent_cols=["ret_pct"],
            int_cols=["rank"],
        ),
        "",
        "### Top Losers",
        _to_markdown_table(
            top_losers,
            columns=["signal_date", "ticker", "rank", "ret_pct", "entry_date", "exit_date"],
            percent_cols=["ret_pct"],
            int_cols=["rank"],
        ),
        "",
        "## Local 交易日誌（xq_pick_log）",
        "",
        _to_markdown_table(
            local_journal_df,
            columns=["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"],
            percent_cols=["ret_pct"],
            int_cols=["rank"],
        ),
        "",
        "## Local-Fusion 交易日誌（xq_pick_log + ai_focus）",
        "",
        _to_markdown_table(
            fusion_journal_df,
            columns=["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"],
            percent_cols=["ret_pct"],
            int_cols=["rank"],
        ),
        "",
        "## AI 交易日誌（ai_decision_log）",
        "",
        _to_markdown_table(
            ai_journal_df,
            columns=["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"],
            percent_cols=["ret_pct"],
            int_cols=["rank"],
        ),
        "",
        "---",
        "註：本報告為統計回顧用途，不構成投資建議。",
    ]

    return "\n".join(lines).strip() + "\n"


def write_outputs(
    output_dir: Path,
    end_d: date,
    markdown: str,
    local_trades_df: pd.DataFrame,
    fusion_trades_df: pd.DataFrame,
    ai_trades_df: pd.DataFrame,
) -> tuple[Path, Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / f"{end_d}_weekly_report.md"
    latest_report_path = output_dir / DEFAULT_LATEST_MD
    report_path.write_text(markdown, encoding="utf-8")
    shutil.copy2(report_path, latest_report_path)

    trade_path = output_dir / f"{end_d}_weekly_trades.csv"
    latest_trade_path = output_dir / DEFAULT_LATEST_TRADES
    if local_trades_df is None or len(local_trades_df) == 0:
        pd.DataFrame(
            columns=["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"]
        ).to_csv(trade_path, index=False, encoding="utf-8-sig")
    else:
        local_trades_df.to_csv(trade_path, index=False, encoding="utf-8-sig")
    shutil.copy2(trade_path, latest_trade_path)

    fusion_trade_path = output_dir / f"{end_d}_weekly_fusion_trades.csv"
    latest_fusion_trade_path = output_dir / DEFAULT_LATEST_FUSION_TRADES
    if fusion_trades_df is None or len(fusion_trades_df) == 0:
        pd.DataFrame(
            columns=["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"]
        ).to_csv(fusion_trade_path, index=False, encoding="utf-8-sig")
    else:
        fusion_trades_df.to_csv(fusion_trade_path, index=False, encoding="utf-8-sig")
    shutil.copy2(fusion_trade_path, latest_fusion_trade_path)

    ai_trade_path = output_dir / f"{end_d}_weekly_ai_trades.csv"
    latest_ai_trade_path = output_dir / DEFAULT_LATEST_AI_TRADES
    if ai_trades_df is None or len(ai_trades_df) == 0:
        pd.DataFrame(
            columns=["signal_date", "ticker", "rank", "entry_date", "exit_date", "ret_pct", "ret_tag", "source_file"]
        ).to_csv(ai_trade_path, index=False, encoding="utf-8-sig")
    else:
        ai_trades_df.to_csv(ai_trade_path, index=False, encoding="utf-8-sig")
    shutil.copy2(ai_trade_path, latest_ai_trade_path)

    return report_path, trade_path, fusion_trade_path, ai_trade_path


def main() -> None:
    parser = argparse.ArgumentParser(description="產生 Alpha Finder 每週制度化評估報告")
    parser.add_argument("--start-date", default="", help="開始日期 YYYY-MM-DD（可省略）")
    parser.add_argument("--end-date", default="", help="結束日期 YYYY-MM-DD（可省略，預設今天）")
    parser.add_argument("--lookback-days", type=int, default=WEEKLY_REPORT_LOOKBACK_DAYS, help="回看天數（未指定 start-date 時生效）")
    parser.add_argument("--hold-days", type=int, default=WEEKLY_REPORT_HOLD_DAYS, help="持有天數")
    parser.add_argument("--max-rank", type=int, default=WEEKLY_REPORT_MAX_RANK, help="僅計算 rank <= N")
    parser.add_argument("--max-symbols", type=int, default=WEEKLY_REPORT_MAX_SYMBOLS, help="最多統計幾檔 ticker")
    parser.add_argument("--pick-log-file", default=str(DEFAULT_PICK_LOG_FILE), help="xq_pick_log.csv 路徑")
    parser.add_argument("--daily-refresh-dir", default=WEEKLY_REPORT_DAILY_REFRESH_DIR, help="daily_refresh 根目錄（含每日 ai_focus_list）")
    parser.add_argument("--ai-decision-log-file", default=str(DEFAULT_AI_DECISION_LOG_FILE), help="ai_decision_log.csv 路徑")
    parser.add_argument("--output-dir", default=WEEKLY_REPORT_OUTPUT_DIR, help="輸出目錄")
    args = parser.parse_args()

    try:
        start_d, end_d = resolve_window(args.start_date.strip(), args.end_date.strip(), args.lookback_days)
    except ValueError as exc:
        print(f"參數錯誤：{exc}")
        return

    pick_log_file = Path(args.pick_log_file)
    daily_refresh_dir = Path(args.daily_refresh_dir)
    if not daily_refresh_dir.is_absolute():
        daily_refresh_dir = PROJECT_ROOT / daily_refresh_dir
    ai_decision_log_file = Path(args.ai_decision_log_file)
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    local_entries_df = load_weekly_entries(
        pick_log_file=pick_log_file,
        start_d=start_d,
        end_d=end_d,
        max_rank=args.max_rank,
        max_symbols=args.max_symbols,
    )

    ai_focus_entries_df = load_ai_focus_entries(
        daily_refresh_dir=daily_refresh_dir,
        start_d=start_d,
        end_d=end_d,
        max_rank=args.max_rank,
        max_symbols=args.max_symbols,
    )

    fusion_entries_df = build_local_fusion_entries(
        xq_entries=local_entries_df,
        ai_focus_entries=ai_focus_entries_df,
        max_rank=args.max_rank,
        max_symbols=args.max_symbols,
    )

    ai_entries_df = load_ai_decision_entries(
        ai_log_file=ai_decision_log_file,
        start_d=start_d,
        end_d=end_d,
        max_rank=args.max_rank,
        max_symbols=args.max_symbols,
    )

    if len(local_entries_df) == 0 and len(fusion_entries_df) == 0 and len(ai_entries_df) == 0:
        markdown = build_report_markdown(
            start_d=start_d,
            end_d=end_d,
            hold_days=args.hold_days,
            max_rank=args.max_rank,
            local_entries_df=local_entries_df,
            local_trades_df=pd.DataFrame(),
            fusion_entries_df=fusion_entries_df,
            fusion_trades_df=pd.DataFrame(),
            ai_entries_df=ai_entries_df,
            ai_trades_df=pd.DataFrame(),
        )
        report_path, trade_path, fusion_trade_path, ai_trade_path = write_outputs(
            output_dir,
            end_d,
            markdown,
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        )
        print("本週找不到可回算的 Local / Local-Fusion / AI 訊號，已輸出空報告。")
        print(f"週報：{report_path}")
        print(f"Local 交易明細：{trade_path}")
        print(f"Local-Fusion 交易明細：{fusion_trade_path}")
        print(f"AI 交易明細：{ai_trade_path}")
        return

    combined_entries_for_price = pd.concat(
        [local_entries_df.loc[:, ["ticker"]] if len(local_entries_df) > 0 else pd.DataFrame(columns=["ticker"]),
         fusion_entries_df.loc[:, ["ticker"]] if len(fusion_entries_df) > 0 else pd.DataFrame(columns=["ticker"]),
         ai_entries_df.loc[:, ["ticker"]] if len(ai_entries_df) > 0 else pd.DataFrame(columns=["ticker"])],
        ignore_index=True,
    ).drop_duplicates(subset=["ticker"], keep="first")
    price_cache = fetch_price_cache(combined_entries_for_price, start_d, end_d, hold_days=args.hold_days)

    local_trades_df = build_trades(
        local_entries_df,
        price_cache,
        hold_days=args.hold_days,
        signal_date_col="scan_date",
        source_col="source_file",
        score_col="short_trade_score",
        default_source="xq_pick_log",
    )
    fusion_trades_df = build_trades(
        fusion_entries_df,
        price_cache,
        hold_days=args.hold_days,
        signal_date_col="scan_date",
        source_col="source_file",
        score_col="short_trade_score",
        default_source="local_fusion",
    )
    ai_trades_df = build_trades(
        ai_entries_df,
        price_cache,
        hold_days=args.hold_days,
        signal_date_col="decision_date",
        source_col="source_ref",
        score_col="short_score_final",
        default_source="ai_decision_log",
    )

    markdown = build_report_markdown(
        start_d=start_d,
        end_d=end_d,
        hold_days=args.hold_days,
        max_rank=args.max_rank,
        local_entries_df=local_entries_df,
        local_trades_df=local_trades_df,
        fusion_entries_df=fusion_entries_df,
        fusion_trades_df=fusion_trades_df,
        ai_entries_df=ai_entries_df,
        ai_trades_df=ai_trades_df,
    )
    report_path, trade_path, fusion_trade_path, ai_trade_path = write_outputs(
        output_dir,
        end_d,
        markdown,
        local_trades_df,
        fusion_trades_df,
        ai_trades_df,
    )

    print("每週制度化評估報告已產生：")
    print(f"週報：{report_path}")
    print(f"Local 交易明細：{trade_path}")
    print(f"Local-Fusion 交易明細：{fusion_trade_path}")
    print(f"AI 交易明細：{ai_trade_path}")
    print(f"Local 訊號筆數：{len(local_entries_df)}，可回算交易筆數：{len(local_trades_df)}")
    print(f"Local-Fusion 訊號筆數：{len(fusion_entries_df)}，可回算交易筆數：{len(fusion_trades_df)}")
    print(f"AI 訊號筆數：{len(ai_entries_df)}，可回算交易筆數：{len(ai_trades_df)}")


if __name__ == "__main__":
    with keep_system_awake():
        main()
