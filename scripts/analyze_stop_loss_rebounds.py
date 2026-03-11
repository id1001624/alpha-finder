from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_logging import install_builtin_print_logging


install_builtin_print_logging()


def _fix_ssl_cert_path() -> None:
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

from turso_state import load_recent_execution_log, load_recent_trade_ledger


BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
EXECUTION_LOG_CSV = BACKTEST_DIR / "execution_trade_log.csv"
TRADE_LEDGER_CSV = BACKTEST_DIR / "position_trade_log.csv"
ANALYSIS_DIR = BACKTEST_DIR / "analysis"
LATEST_MD = ANALYSIS_DIR / "stop_loss_rebound_latest.md"
LATEST_CSV = ANALYSIS_DIR / "stop_loss_rebound_latest.csv"
LATEST_MISSED_CSV = ANALYSIS_DIR / "stop_loss_missed_rebound_latest.csv"


def _read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def load_execution_history(limit: int) -> pd.DataFrame:
    local = _read_csv_if_exists(EXECUTION_LOG_CSV)
    if len(local) > 0:
        return local
    return load_recent_execution_log(limit=max(1, int(limit)))


def load_trade_ledger(limit: int) -> pd.DataFrame:
    local = _read_csv_if_exists(TRADE_LEDGER_CSV)
    if len(local) > 0:
        return local
    return load_recent_trade_ledger(limit=max(1, int(limit)))


def normalize_execution_history(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    for col in [
        "recorded_at",
        "execution_date",
        "execution_time",
        "ticker",
        "action",
        "position_effect",
        "rank",
        "decision_tag",
        "close",
        "reason_summary",
        "signal_ts",
    ]:
        if col not in out.columns:
            out[col] = ""
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["action"] = out["action"].astype(str).str.strip().str.lower()
    out["position_effect"] = out["position_effect"].astype(str).str.strip().str.lower()
    out["decision_tag"] = out["decision_tag"].astype(str).str.strip().str.lower()
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["execution_ts"] = pd.to_datetime(
        out["signal_ts"].where(out["signal_ts"].astype(str).str.strip() != "", None),
        errors="coerce",
        utc=True,
    )
    fallback_ts = pd.to_datetime(
        out["execution_date"].astype(str).str.strip() + " " + out["execution_time"].astype(str).str.strip(),
        errors="coerce",
        utc=True,
    )
    out["execution_ts"] = out["execution_ts"].fillna(fallback_ts)
    out["execution_day"] = out["execution_ts"].dt.tz_convert(None).dt.date
    out = out[out["ticker"] != ""].copy()
    out = out[out["execution_ts"].notna()].copy()
    return out.sort_values(["ticker", "execution_ts"], ascending=[True, True]).reset_index(drop=True)


def normalize_trade_ledger(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    for col in ["recorded_at", "ticker", "side", "quantity", "price", "position_effect", "source", "note"]:
        if col not in out.columns:
            out[col] = ""
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["side"] = out["side"].astype(str).str.strip().str.lower()
    out["quantity"] = pd.to_numeric(out["quantity"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out["recorded_ts"] = pd.to_datetime(out["recorded_at"], errors="coerce", utc=True)
    out = out[out["ticker"] != ""].copy()
    out = out[out["recorded_ts"].notna()].copy()
    return out.sort_values(["ticker", "recorded_ts"], ascending=[True, True]).reset_index(drop=True)


def extract_stop_loss_events(execution_df: pd.DataFrame, lookback_days: int, asof: date | None = None) -> pd.DataFrame:
    if len(execution_df) == 0:
        return pd.DataFrame()
    anchor = asof or date.today()
    min_day = anchor - timedelta(days=max(1, int(lookback_days)) - 1)
    out = execution_df.copy()
    out = out[out["action"] == "stop_loss"].copy()
    out = out[out["execution_day"].notna()].copy()
    out = out[out["execution_day"] >= min_day].copy()
    if len(out) == 0:
        return pd.DataFrame()
    out = out.sort_values(["ticker", "execution_ts"], ascending=[True, True])
    out = out.groupby(["ticker", "execution_day"], as_index=False).first()
    out = out.rename(columns={
        "execution_ts": "stop_ts",
        "execution_day": "stop_date",
        "close": "stop_close",
        "reason_summary": "stop_reason",
        "rank": "stop_rank",
        "decision_tag": "stop_decision_tag",
    })
    return out.reset_index(drop=True)


def fetch_price_cache(events_df: pd.DataFrame, forward_days: int) -> Dict[str, pd.DataFrame]:
    if len(events_df) == 0:
        return {}
    start_day = min(events_df["stop_date"]) - timedelta(days=3)
    end_day = max(events_df["stop_date"]) + timedelta(days=max(7, int(forward_days) + 7))
    fetch_start = start_day.strftime("%Y-%m-%d")
    fetch_end = end_day.strftime("%Y-%m-%d")

    cache: Dict[str, pd.DataFrame] = {}
    for ticker in events_df["ticker"].drop_duplicates().tolist():
        try:
            hist = yf.Ticker(ticker).history(start=fetch_start, end=fetch_end, interval="1d", auto_adjust=False)
        except (TypeError, KeyError, AttributeError, ValueError):
            continue
        if hist is None or hist.empty:
            continue
        out = hist.copy().reset_index()
        dt_col = out.columns[0]
        out = out.rename(columns={dt_col: "Date"})
        out["trade_date"] = pd.to_datetime(out["Date"], errors="coerce").dt.date
        out = out.dropna(subset=["trade_date", "Close"]).copy()
        if "High" not in out.columns:
            out["High"] = out["Close"]
        cache[ticker] = out.sort_values("trade_date").reset_index(drop=True)
    return cache


def evaluate_stop_loss_rebounds(
    stop_df: pd.DataFrame,
    execution_df: pd.DataFrame,
    trade_df: pd.DataFrame,
    price_cache: Dict[str, pd.DataFrame],
    forward_days: int,
    rebound_threshold_pct: float,
) -> pd.DataFrame:
    rows: List[dict] = []
    signal_actions = {"entry", "add"}

    for _, row in stop_df.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        stop_ts = pd.Timestamp(row.get("stop_ts"))
        stop_date = row.get("stop_date")
        stop_close = pd.to_numeric(row.get("stop_close"), errors="coerce")
        if not ticker or pd.isna(stop_close) or float(stop_close) <= 0 or pd.isna(stop_ts):
            continue

        hist = price_cache.get(ticker, pd.DataFrame())
        future_hist = hist[hist["trade_date"] > stop_date].copy() if len(hist) > 0 else pd.DataFrame()
        future_hist = future_hist.head(max(1, int(forward_days))) if len(future_hist) > 0 else future_hist

        max_close = float(future_hist["Close"].max()) if len(future_hist) > 0 else float("nan")
        max_high = float(future_hist["High"].max()) if len(future_hist) > 0 else float("nan")
        peak_close_date = future_hist.loc[future_hist["Close"].idxmax(), "trade_date"] if len(future_hist) > 0 else pd.NaT
        peak_high_date = future_hist.loc[future_hist["High"].idxmax(), "trade_date"] if len(future_hist) > 0 else pd.NaT

        max_close_pct = ((max_close / float(stop_close)) - 1.0) * 100.0 if pd.notna(max_close) else float("nan")
        max_high_pct = ((max_high / float(stop_close)) - 1.0) * 100.0 if pd.notna(max_high) else float("nan")

        future_signals = execution_df[
            (execution_df["ticker"] == ticker)
            & (execution_df["execution_ts"] > stop_ts)
            & (execution_df["action"].isin(signal_actions))
        ].copy()
        future_signals = future_signals[future_signals["execution_day"] <= (stop_date + timedelta(days=max(1, int(forward_days)) + 3))]
        future_signals = future_signals.sort_values("execution_ts", ascending=True)
        first_signal = future_signals.iloc[0] if len(future_signals) > 0 else None

        future_buys = trade_df[
            (trade_df["ticker"] == ticker)
            & (trade_df["recorded_ts"] > stop_ts)
            & (trade_df["side"] == "buy")
        ].copy()
        future_buys = future_buys.sort_values("recorded_ts", ascending=True)
        first_manual_buy = future_buys.iloc[0] if len(future_buys) > 0 else None

        rebound_hit = bool(pd.notna(max_high_pct) and float(max_high_pct) >= float(rebound_threshold_pct))
        reentry_signal_hit = first_signal is not None
        manual_buy_hit = first_manual_buy is not None
        missed_rebound = rebound_hit and not reentry_signal_hit

        rows.append(
            {
                "ticker": ticker,
                "stop_date": stop_date,
                "stop_ts": pd.Timestamp(stop_ts).tz_convert(None).strftime("%Y-%m-%d %H:%M:%S"),
                "stop_close": float(stop_close),
                "stop_rank": int(row.get("stop_rank")) if pd.notna(row.get("stop_rank")) else 9999,
                "stop_decision_tag": str(row.get("stop_decision_tag", "")),
                "stop_reason": str(row.get("stop_reason", "")).strip(),
                "future_trading_days": int(len(future_hist)),
                "max_close": float("nan") if pd.isna(max_close) else float(max_close),
                "max_close_pct": float("nan") if pd.isna(max_close_pct) else float(max_close_pct),
                "max_high": float("nan") if pd.isna(max_high) else float(max_high),
                "max_high_pct": float("nan") if pd.isna(max_high_pct) else float(max_high_pct),
                "peak_close_date": "" if pd.isna(peak_close_date) else str(peak_close_date),
                "peak_high_date": "" if pd.isna(peak_high_date) else str(peak_high_date),
                "rebound_hit": rebound_hit,
                "reentry_signal_hit": reentry_signal_hit,
                "reentry_signal_date": "" if first_signal is None else str(first_signal.get("execution_day", "")),
                "reentry_signal_action": "" if first_signal is None else str(first_signal.get("action", "")),
                "reentry_signal_close": float("nan") if first_signal is None or pd.isna(first_signal.get("close")) else float(first_signal.get("close")),
                "manual_buy_hit": manual_buy_hit,
                "manual_buy_date": "" if first_manual_buy is None else pd.Timestamp(first_manual_buy.get("recorded_ts")).tz_convert(None).strftime("%Y-%m-%d %H:%M:%S"),
                "manual_buy_price": float("nan") if first_manual_buy is None or pd.isna(first_manual_buy.get("price")) else float(first_manual_buy.get("price")),
                "missed_rebound": missed_rebound,
            }
        )

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    return out.sort_values(["missed_rebound", "max_high_pct", "stop_date", "ticker"], ascending=[False, False, False, True]).reset_index(drop=True)


def _fmt_num(value: object, digits: int = 2) -> str:
    num = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(num):
        return ""
    return f"{float(num):.{digits}f}"


def _to_markdown_table(df: pd.DataFrame, columns: List[str], percent_cols: List[str] | None = None) -> str:
    if df is None or len(df) == 0:
        return "（無資料）"
    percent_cols = set(percent_cols or [])
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, sep]
    for _, row in df.iterrows():
        cells: List[str] = []
        for col in columns:
            value = row.get(col, "")
            if col in percent_cols:
                text = f"{_fmt_num(value)}%"
            else:
                text = str(value)
            cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_missed_rebound_view(report_df: pd.DataFrame) -> pd.DataFrame:
    if report_df is None or len(report_df) == 0:
        return pd.DataFrame(
            columns=[
                "ticker",
                "stop_date",
                "peak_high_date",
                "days_to_peak",
                "stop_close",
                "max_high_pct",
                "reentry_status",
                "manual_buy_status",
                "followup_note",
            ]
        )

    out = report_df.copy()
    out = out[out["missed_rebound"]].copy()
    if len(out) == 0:
        return pd.DataFrame(
            columns=[
                "ticker",
                "stop_date",
                "peak_high_date",
                "days_to_peak",
                "stop_close",
                "max_high_pct",
                "reentry_status",
                "manual_buy_status",
                "followup_note",
            ]
        )

    out["stop_date"] = pd.to_datetime(out["stop_date"], errors="coerce")
    out["peak_high_date"] = pd.to_datetime(out["peak_high_date"], errors="coerce")
    out["days_to_peak"] = (out["peak_high_date"] - out["stop_date"]).dt.days
    out["reentry_status"] = out["reentry_signal_hit"].map({True: "已有 engine 再進場", False: "沒有 engine 再進場"})
    out["manual_buy_status"] = out["manual_buy_hit"].map({True: "有手動買回", False: "沒有手動買回"})
    out["followup_note"] = out.apply(
        lambda row: "應納入 watchlist follow-up" if not bool(row.get("manual_buy_hit")) else "手動已補回，仍可回看提醒時機",
        axis=1,
    )
    out["max_high_pct"] = pd.to_numeric(out["max_high_pct"], errors="coerce")
    out = out.sort_values(["max_high_pct", "stop_date", "ticker"], ascending=[False, False, True], na_position="last")
    out["stop_date"] = out["stop_date"].dt.strftime("%Y-%m-%d")
    out["peak_high_date"] = out["peak_high_date"].dt.strftime("%Y-%m-%d").fillna("")
    keep_cols = [
        "ticker",
        "stop_date",
        "peak_high_date",
        "days_to_peak",
        "stop_close",
        "max_high_pct",
        "reentry_status",
        "manual_buy_status",
        "followup_note",
    ]
    return out[keep_cols].reset_index(drop=True)


def build_summary_markdown(report_df: pd.DataFrame, lookback_days: int, forward_days: int, rebound_threshold_pct: float) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_events = int(len(report_df)) if report_df is not None else 0
    rebound_hits = int(report_df["rebound_hit"].sum()) if total_events else 0
    reentry_hits = int(report_df["reentry_signal_hit"].sum()) if total_events else 0
    manual_buy_hits = int(report_df["manual_buy_hit"].sum()) if total_events else 0
    missed_hits = int(report_df["missed_rebound"].sum()) if total_events else 0
    missed_df = build_missed_rebound_view(report_df)
    top_missed = missed_df.head(10) if len(missed_df) > 0 else pd.DataFrame()
    missed_rate = (missed_hits / rebound_hits * 100.0) if rebound_hits > 0 else 0.0

    lines = [
        "# Stop Loss Missed Rebound Daily",
        "",
        f"- 產生時間：{generated_at}",
        f"- stop_loss 回看區間：近 {lookback_days} 天",
        f"- 反彈觀察窗口：停損後 {forward_days} 個交易日",
        f"- 明顯反彈門檻：最高價 >= {rebound_threshold_pct:.1f}%",
        "",
        "## 總覽",
        "",
        f"- stop_loss 事件數：{total_events}",
        f"- 達到反彈門檻：{rebound_hits}",
        f"- 期間內出現 engine 再進場訊號：{reentry_hits}",
        f"- 期間內出現手動買回：{manual_buy_hits}",
        f"- 漏報型反彈：{missed_hits}",
        f"- 反彈事件中的漏報比率：{missed_rate:.2f}%",
        "",
        "## 今日只看真正漏報案例",
        "",
        _to_markdown_table(
            top_missed,
            columns=["ticker", "stop_date", "peak_high_date", "days_to_peak", "max_high_pct", "reentry_status", "manual_buy_status", "followup_note"],
            percent_cols=["max_high_pct"],
        ),
        "",
    ]
    if len(top_missed) == 0:
        lines.extend([
            "目前沒有真正漏報案例。",
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="分析 stop_loss 後 3-5 天內的反彈與漏報情況")
    parser.add_argument("--lookback-days", type=int, default=30, help="回看近幾天的 stop_loss 事件")
    parser.add_argument("--forward-days", type=int, default=5, help="停損後觀察幾個交易日的反彈")
    parser.add_argument("--rebound-threshold-pct", type=float, default=8.0, help="最高價反彈幾 % 以上視為明顯反彈")
    parser.add_argument("--execution-limit", type=int, default=5000, help="當本機 execution log 不存在時，從 Turso 讀多少筆 execution")
    parser.add_argument("--ledger-limit", type=int, default=5000, help="當本機 trade ledger 不存在時，從 Turso 讀多少筆 trade ledger")
    args = parser.parse_args()

    execution_df = normalize_execution_history(load_execution_history(limit=max(1, int(args.execution_limit))))
    trade_df = normalize_trade_ledger(load_trade_ledger(limit=max(1, int(args.ledger_limit))))
    stop_df = extract_stop_loss_events(execution_df, lookback_days=max(1, int(args.lookback_days)))
    if len(stop_df) == 0:
        print("No stop_loss events found in the selected lookback window.")
        return 0

    price_cache = fetch_price_cache(stop_df, forward_days=max(1, int(args.forward_days)))
    report_df = evaluate_stop_loss_rebounds(
        stop_df,
        execution_df,
        trade_df,
        price_cache,
        forward_days=max(1, int(args.forward_days)),
        rebound_threshold_pct=float(args.rebound_threshold_pct),
    )

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    today_tag = datetime.now().strftime("%Y-%m-%d")
    csv_path = ANALYSIS_DIR / f"stop_loss_rebound_report_{today_tag}.csv"
    md_path = ANALYSIS_DIR / f"stop_loss_rebound_report_{today_tag}.md"

    report_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    missed_df = build_missed_rebound_view(report_df)
    markdown = build_summary_markdown(
        report_df,
        lookback_days=max(1, int(args.lookback_days)),
        forward_days=max(1, int(args.forward_days)),
        rebound_threshold_pct=float(args.rebound_threshold_pct),
    )
    md_path.write_text(markdown, encoding="utf-8")
    shutil.copy2(csv_path, LATEST_CSV)
    missed_df.to_csv(LATEST_MISSED_CSV, index=False, encoding="utf-8-sig")
    shutil.copy2(md_path, LATEST_MD)

    print(markdown)
    print("")
    print(f"CSV: {csv_path}")
    print(f"MD : {md_path}")
    print(f"MISSED CSV : {LATEST_MISSED_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())