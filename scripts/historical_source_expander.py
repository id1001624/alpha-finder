from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as app_config

DAILY_REFRESH_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh"
AI_READY_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready"
AI_TRADING_LATEST = PROJECT_ROOT / "repo_outputs" / "ai_trading" / "latest"


def _read_csv_fallback(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)


def _safe_float(v: object, default: float = 0.0) -> float:
    x = pd.to_numeric(v, errors="coerce")
    if pd.isna(x):
        return default
    return float(x)


def _normalize_ticker(v: object) -> str:
    text = str(v or "").strip().upper().replace(".US", "")
    if text in {"", "NAN", "NONE", "NULL"}:
        return ""
    return text


def _previous_trading_day(base_dt: datetime | None = None) -> datetime:
    now_dt = base_dt or datetime.now()
    d = now_dt.date()
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return datetime.combine(d, datetime.min.time())


def _collect_existing_dates() -> set[str]:
    daily_dates = {p.name for p in DAILY_REFRESH_DIR.iterdir() if p.is_dir() and p.name[:4].isdigit()} if DAILY_REFRESH_DIR.exists() else set()
    ready_dates = {p.name for p in AI_READY_DIR.iterdir() if p.is_dir() and p.name[:4].isdigit()} if AI_READY_DIR.exists() else set()
    return daily_dates & ready_dates


def _target_business_dates(target_days: int) -> List[str]:
    end_dt = _previous_trading_day()
    rng = pd.bdate_range(end=end_dt.date(), periods=max(1, int(target_days)))
    return [d.strftime("%Y-%m-%d") for d in rng]


def _latest_run_dir(base_dir: Path) -> Path | None:
    if not base_dir.exists() or not base_dir.is_dir():
        return None
    runs = [p for p in base_dir.iterdir() if p.is_dir()]
    if not runs:
        return None
    runs = sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0]


def _fetch_finnhub_candles(ticker: str, start_ts: int, end_ts: int, token: str, timeout_sec: float) -> pd.DataFrame:
    if not token:
        return pd.DataFrame()

    params = {
        "symbol": ticker,
        "resolution": "D",
        "from": int(start_ts),
        "to": int(end_ts),
        "token": token,
    }
    try:
        resp = requests.get("https://finnhub.io/api/v1/stock/candle", params=params, timeout=timeout_sec)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
    except (requests.RequestException, ValueError):
        return pd.DataFrame()

    if not isinstance(payload, dict) or payload.get("s") != "ok":
        return pd.DataFrame()

    t = payload.get("t") or []
    c = payload.get("c") or []
    h = payload.get("h") or []
    l = payload.get("l") or []
    v = payload.get("v") or []
    if not t:
        return pd.DataFrame()

    df = pd.DataFrame({"ts": t, "close": c, "high": h, "low": l, "volume": v})
    if len(df) == 0:
        return df
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    for col in ["close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    return df.reset_index(drop=True)


def _build_history_cache(tickers: List[str], start_date: str, end_date: str, max_workers: int, timeout_sec: float) -> Dict[str, pd.DataFrame]:
    token = str(getattr(app_config, "FINNHUB_API_KEY", "") or "").strip()
    start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) - 7 * 86400
    end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 2 * 86400

    out: Dict[str, pd.DataFrame] = {}

    def _job(symbol: str) -> tuple[str, pd.DataFrame]:
        return symbol, _fetch_finnhub_candles(symbol, start_ts=start_ts, end_ts=end_ts, token=token, timeout_sec=timeout_sec)

    with ThreadPoolExecutor(max_workers=max(1, min(12, int(max_workers)))) as executor:
        futures = {executor.submit(_job, t): t for t in tickers}
        for fut in as_completed(futures):
            symbol = futures[fut]
            try:
                k, df = fut.result()
            except (ValueError, KeyError, RuntimeError):
                out[symbol] = pd.DataFrame()
                continue
            out[k] = df
            time.sleep(0.02)

    return out


def _grade_from_score(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 58:
        return "B"
    if score >= 42:
        return "C"
    return "D"


def _prob_range(mid: float) -> str:
    lo = int(max(0, min(99, math.floor(mid - 6))))
    hi = int(max(0, min(99, math.ceil(mid + 6))))
    return f"{lo}-{hi}%"


def _setup_type(chg1d: float, rel_vol: float) -> str:
    if chg1d >= 8 and rel_vol >= 2.0:
        return "ignition"
    if chg1d >= 2 and rel_vol >= 1.2:
        return "continuation"
    return "neutral"


def _decision_hint(score: float) -> str:
    if score >= 65:
        return "keep"
    if score >= 45:
        return "watch"
    return "replace_candidate"


def _build_daily_files_for_date(
    asofdate: str,
    run_stamp: str,
    base_raw: pd.DataFrame,
    base_xq: pd.DataFrame,
    candles: Dict[str, pd.DataFrame],
    latest_daily_dir: Path,
    latest_ready_dir: Path,
) -> Dict[str, object]:
    refresh_dir = DAILY_REFRESH_DIR / asofdate / run_stamp
    ready_dir = AI_READY_DIR / asofdate / run_stamp
    refresh_dir.mkdir(parents=True, exist_ok=True)
    ready_dir.mkdir(parents=True, exist_ok=True)

    raw = base_raw.copy()

    ticker_to_hist = {t: candles.get(t, pd.DataFrame()) for t in raw["Ticker"].astype(str).map(_normalize_ticker).tolist()}

    # Update raw market daily metrics with historical close/volume where available.
    updated_rows = 0
    for idx, row in raw.iterrows():
        ticker = _normalize_ticker(row.get("Ticker"))
        if not ticker:
            continue
        hist = ticker_to_hist.get(ticker, pd.DataFrame())
        if len(hist) == 0:
            continue

        h = hist[hist["date"] <= asofdate].copy()
        if len(h) < 2:
            continue

        day = h.iloc[-1]
        prev = h.iloc[-2]
        w20 = h.tail(20)

        close = _safe_float(day.get("close"), _safe_float(row.get("Price"), 0.0))
        prev_close = _safe_float(prev.get("close"), close)
        vol = _safe_float(day.get("volume"), 0.0)
        avg_vol20 = max(1.0, _safe_float(w20.get("volume", pd.Series(dtype=float)).mean(), 1.0))
        rel_vol = vol / avg_vol20

        raw.at[idx, "Price"] = round(close, 4)
        raw.at[idx, "Daily_Change"] = round(((close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0, 2)
        raw.at[idx, "Rel_Volume"] = round(rel_vol, 2)
        updated_rows += 1

    raw.to_csv(refresh_dir / "raw_market_daily.csv", index=False, encoding="utf-8-sig")

    # Build xq snapshot.
    xq = base_xq.copy()
    xq["ticker"] = xq["symbol"].astype(str).map(_normalize_ticker)
    xq = xq.drop_duplicates(subset=["ticker"], keep="first")

    base_company = raw[["Ticker", "Company"]].copy()
    base_company["ticker"] = base_company["Ticker"].astype(str).map(_normalize_ticker)
    company_map = dict(zip(base_company["ticker"], base_company["Company"]))

    xq_rows: List[Dict[str, object]] = []
    for ticker in sorted(set(raw["Ticker"].astype(str).map(_normalize_ticker).tolist())):
        if not ticker:
            continue

        hist = ticker_to_hist.get(ticker, pd.DataFrame())
        if len(hist) == 0:
            base = xq[xq["ticker"] == ticker]
            if len(base) == 0:
                continue
            rec = base.iloc[0].to_dict()
            rec.pop("ticker", None)
            xq_rows.append(rec)
            continue

        h = hist[hist["date"] <= asofdate].copy()
        if len(h) < 2:
            base = xq[xq["ticker"] == ticker]
            if len(base) == 0:
                continue
            rec = base.iloc[0].to_dict()
            rec.pop("ticker", None)
            xq_rows.append(rec)
            continue

        c = h["close"].astype(float)
        v = h["volume"].astype(float)
        hi = h["high"].astype(float)
        lo = h["low"].astype(float)

        close = float(c.iloc[-1])
        prev_close = float(c.iloc[-2]) if len(c) >= 2 else close
        change1 = ((close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
        base3 = float(c.iloc[-4]) if len(c) >= 4 else prev_close
        base5 = float(c.iloc[-6]) if len(c) >= 6 else prev_close
        chg3 = ((close - base3) / base3 * 100.0) if base3 > 0 else change1
        chg5 = ((close - base5) / base5 * 100.0) if base5 > 0 else chg3

        last5 = c.tail(5)
        last5v = v.tail(5)
        avg5 = float(last5.mean()) if len(last5) > 0 else close
        high5 = float(hi.tail(5).max()) if len(hi) > 0 else close
        low5 = float(lo.tail(5).min()) if len(lo) > 0 else close
        vol = float(v.iloc[-1]) if len(v) > 0 else 0.0
        yday_vol = float(v.iloc[-2]) if len(v) >= 2 else vol
        avg_vol5 = float(last5v.mean()) if len(last5v) > 0 else max(vol, 1.0)
        vol_strength = vol / max(avg_vol5, 1.0)
        dollar_volume_m = close * vol / 1_000_000.0

        score = 50.0 + change1 * 1.8 + chg3 * 0.9 + max(0.0, vol_strength - 1.0) * 12.0
        score = max(0.0, min(99.0, score))
        swing = max(0.0, min(99.0, 50.0 + chg5 * 0.9))
        mom = round((score * 0.65 + swing * 0.35) / 10.0, 2)
        grade = _grade_from_score(score)
        p1_mid = max(0.0, min(99.0, 30.0 + score * 0.5))
        p2_mid = max(0.0, min(99.0, p1_mid - 8.0))

        base = xq[xq["ticker"] == ticker]
        name = str(base.iloc[0].get("name", "")).strip() if len(base) > 0 else str(company_map.get(ticker, ticker))
        ai_hint = str(base.iloc[0].get("ai_query_hint", "")).strip() if len(base) > 0 else f"查詢 {ticker} 最新催化、財報與隔夜延續性。"

        xq_rows.append(
            {
                "symbol": f"{ticker}.US",
                "name": name,
                "value": round(close, 4),
                "change_pct": round(change1, 2),
                "volume": int(max(vol, 0.0)),
                "chg_1d_pct": round(change1, 2),
                "chg_3d_pct": round(chg3, 2),
                "chg_5d_pct": round(chg5, 2),
                "avg_price_5d": round(avg5, 4),
                "high_5d": round(high5, 4),
                "low_5d": round(low5, 4),
                "yday_volume": int(max(yday_vol, 0.0)),
                "avg_volume_5d": int(max(avg_vol5, 0.0)),
                "vol_strength": round(vol_strength, 2),
                "dollar_volume_m": round(dollar_volume_m, 2),
                "short_trade_score": round(score, 2),
                "swing_score": round(swing, 2),
                "momentum_mix": mom,
                "continuation_grade": grade,
                "prob_next_day": _prob_range(p1_mid),
                "prob_day2": _prob_range(p2_mid),
                "reversal_flags": "none",
                "decision_tag_hint": _decision_hint(score),
                "setup_type": _setup_type(change1, vol_strength),
                "ai_query_hint": ai_hint,
            }
        )

    xq_out = pd.DataFrame(xq_rows)
    xq_out.to_csv(ready_dir / "xq_short_term_updated.csv", index=False, encoding="utf-8-sig")

    # Copy supporting files to daily_refresh run.
    for name in [
        "monster_radar_daily.csv",
        "fusion_top_daily.csv",
        "ai_focus_list.csv",
        "theme_heat_daily.csv",
        "theme_leaders_daily.csv",
        "README_local_outputs.json",
        "shortlist_analyst.csv",
        "shortlist_earnings_post.csv",
        "shortlist_earnings_pre.csv",
        "shortlist_launch.csv",
        "shortlist_track_f.csv",
        "tv_need_list.csv",
    ]:
        src = latest_daily_dir / name
        if src.exists():
            (refresh_dir / name).write_bytes(src.read_bytes())

    # Copy supporting files to ai_ready run.
    for name in [
        "ai_focus_list.csv",
        "fusion_top_daily.csv",
        "monster_radar_daily.csv",
        "raw_market_daily.csv",
        "theme_heat_daily.csv",
        "theme_leaders_daily.csv",
        "README_ai_quick_pack.json",
    ]:
        src = latest_ready_dir / name
        if src.exists():
            (ready_dir / name).write_bytes(src.read_bytes())

    # Keep ai_ready raw market aligned with expanded daily refresh.
    (ready_dir / "raw_market_daily.csv").write_bytes((refresh_dir / "raw_market_daily.csv").read_bytes())

    manifest = {
        "asofdate": asofdate,
        "run_stamp": run_stamp,
        "source": "historical_source_expander",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_raw_rows": int(updated_rows),
        "xq_rows": int(len(xq_out)),
    }
    (refresh_dir / "source_expander_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (ready_dir / "source_expander_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "asofdate": asofdate,
        "run_stamp": run_stamp,
        "updated_raw_rows": int(updated_rows),
        "xq_rows": int(len(xq_out)),
        "status": "expanded",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Expand historical daily sources for missing trading dates.")
    parser.add_argument("--target-days", type=int, default=60)
    parser.add_argument("--min-days", type=int, default=20)
    parser.add_argument("--run-stamp", type=str, default="120000_source_expand_v1")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    latest_daily_run = _latest_run_dir(DAILY_REFRESH_DIR / "latest")
    latest_ready_run = _latest_run_dir(AI_READY_DIR / "latest")
    if latest_daily_run is not None or latest_ready_run is not None:
        pass

    latest_daily_dir = DAILY_REFRESH_DIR / "latest"
    latest_ready_dir = AI_READY_DIR / "latest"

    base_raw_path = latest_daily_dir / "raw_market_daily.csv"
    base_xq_path = latest_ready_dir / "xq_short_term_updated.csv"
    if not base_raw_path.exists() or not base_xq_path.exists():
        raise FileNotFoundError("latest/raw_market_daily.csv or latest/xq_short_term_updated.csv missing")

    base_raw = _read_csv_fallback(base_raw_path)
    base_xq = _read_csv_fallback(base_xq_path)
    if len(base_raw) == 0 or "Ticker" not in base_raw.columns:
        raise ValueError("raw_market_daily.csv has no usable ticker rows")

    tickers = [_normalize_ticker(v) for v in base_raw["Ticker"].tolist()]
    tickers = sorted({t for t in tickers if t})

    target_dates = _target_business_dates(target_days=max(1, int(args.target_days)))
    existing = _collect_existing_dates()
    missing = [d for d in target_dates if d not in existing]

    if not missing and not args.force:
        print(f"[EXPAND] target_days={int(args.target_days)} existing={len(existing)} missing=0")
        print("[EXPAND] nothing_to_expand")
        return 0

    start_date = min(target_dates)
    end_date = max(target_dates)
    candles = _build_history_cache(
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        max_workers=int(args.max_workers),
        timeout_sec=float(args.timeout_sec),
    )

    rows: List[Dict[str, object]] = []
    for d in target_dates:
        if (d in existing) and (not args.force):
            rows.append({"asofdate": d, "run_stamp": "existing", "updated_raw_rows": 0, "xq_rows": 0, "status": "existing"})
            continue
        rows.append(
            _build_daily_files_for_date(
                asofdate=d,
                run_stamp=str(args.run_stamp),
                base_raw=base_raw,
                base_xq=base_xq,
                candles=candles,
                latest_daily_dir=latest_daily_dir,
                latest_ready_dir=latest_ready_dir,
            )
        )

    report = pd.DataFrame(rows)
    AI_TRADING_LATEST.mkdir(parents=True, exist_ok=True)
    report_path = AI_TRADING_LATEST / "historical_source_expander_report.csv"
    report.to_csv(report_path, index=False, encoding="utf-8-sig")

    available = int(report[report["status"].astype(str).isin(["existing", "expanded"])]["asofdate"].nunique()) if len(report) > 0 else 0

    print(f"[EXPAND] target_days={int(args.target_days)}")
    print(f"[EXPAND] min_days={int(args.min_days)}")
    print(f"[EXPAND] ticker_count={len(tickers)}")
    print(f"[EXPAND] available_days={available}")
    print(f"[EXPAND] report={report_path}")
    if available < int(args.min_days):
        print(f"[EXPAND] warning=still_below_min_days({available}<{int(args.min_days)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
