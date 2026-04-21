"""
Layer-1 Finviz momentum scanner (independent from bundle).

Output files:
- repo_outputs/daily_refresh/latest/finviz_momentum_pool.csv
- repo_outputs/daily_refresh/latest/finviz_momentum_pool_YYYY-MM-DD.csv
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as app_config
from app_logging import install_builtin_print_logging

install_builtin_print_logging()


def _parse_finviz_value(value_str: object) -> float:
    text = str(value_str or "").strip()
    if not text or text == "-":
        return 0.0

    text = text.strip("%").strip()

    multiplier = 1.0
    if text.endswith("T"):
        multiplier = 1e12
        text = text[:-1]
    elif text.endswith("B"):
        multiplier = 1e9
        text = text[:-1]
    elif text.endswith("M"):
        multiplier = 1e6
        text = text[:-1]
    elif text.endswith("K"):
        multiplier = 1e3
        text = text[:-1]

    try:
        return float(text) * multiplier
    except (TypeError, ValueError):
        return 0.0


def _screener_to_dataframe(screener: Any) -> pd.DataFrame:
    to_dataframe = getattr(screener, "to_dataframe", None)
    if callable(to_dataframe):
        return to_dataframe()
    return pd.DataFrame([stock for stock in screener])


def _scan_finviz() -> pd.DataFrame:
    from finviz.screener import Screener

    filters: list[str] = []
    exchanges = list(getattr(app_config, "SELECTED_EXCHANGES", []) or [])
    indices = list(getattr(app_config, "SELECTED_INDICES", []) or [])

    if exchanges:
        filters.extend(exchanges)
    if indices:
        filters.extend(indices)

    # Required by v3 radar policy.
    filters.extend([
        "geo_usa",        # Country = USA
        "sh_avgvol_o500", # Avg volume > 500K
        "ta_perf_1w_o5",  # Week performance > 5%
        "sh_price_o2",    # Price > 2
    ])

    screener_perf: Any = Screener(filters=filters, table="Performance")
    screener_overview: Any = Screener(filters=filters, table="Overview")

    df_perf = _screener_to_dataframe(screener_perf)
    df_overview = _screener_to_dataframe(screener_overview)

    if len(df_perf) == 0 and len(df_overview) == 0:
        return pd.DataFrame()

    if len(df_perf) > 0 and len(df_overview) > 0:
        perf_cols = ["Ticker"]
        for col in [
            "Perf Day",
            "Perf Week",
            "Change",
            "Rel Volume",
            "Avg Volume",
        ]:
            if col in df_perf.columns:
                perf_cols.append(col)
        df_perf_subset = df_perf[perf_cols].copy()

        overlap_cols = [c for c in df_perf_subset.columns if c in df_overview.columns and c != "Ticker"]
        df_overview = df_overview.drop(columns=overlap_cols, errors="ignore")
        merged = df_overview.merge(df_perf_subset, on="Ticker", how="left")
    else:
        merged = df_overview if len(df_overview) > 0 else df_perf

    rows = []
    for _, row in merged.iterrows():
        ticker = str(row.get("Ticker", "")).strip().upper()
        if not ticker:
            continue

        daily_change = _parse_finviz_value(row.get("Change", row.get("Perf Day", "0%")))
        perf_week = _parse_finviz_value(row.get("Perf Week", "0%"))
        rel_volume = _parse_finviz_value(row.get("Rel Volume", "1"))

        rows.append(
            {
                "ticker": ticker,
                "company": str(row.get("Company", "")).strip(),
                "sector": str(row.get("Sector", "")).strip(),
                "industry": str(row.get("Industry", "")).strip(),
                "market_cap": str(row.get("Market Cap", "")).strip(),
                "market_cap_raw": _parse_finviz_value(row.get("Market Cap", "0")),
                "price": _parse_finviz_value(row.get("Price", "0")),
                "volume": _parse_finviz_value(row.get("Volume", "0")),
                "rel_volume": rel_volume,
                "daily_change": daily_change,
                "perf_week": perf_week,
                "candidate_origin": "finviz",
                "theme_label": str(row.get("Sector", "")).strip(),
                "catalyst_flag": bool((daily_change > 5.0) or (perf_week > 8.0) or (rel_volume >= 1.8)),
            }
        )

    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out

    out["signal_score"] = (
        out["daily_change"].clip(lower=0) * 2.0
        + (out["rel_volume"] - 1.0).clip(lower=0) * 3.0
        + out["perf_week"].clip(lower=0) * 0.5
    ).round(2)
    out = out.sort_values(["signal_score", "daily_change", "rel_volume"], ascending=[False, False, False]).reset_index(drop=True)
    out.insert(0, "input_date", datetime.now().strftime("%Y-%m-%d"))
    return out


def _write_outputs(df: pd.DataFrame) -> list[Path]:
    base_dir = Path(getattr(app_config, "LOCAL_OUTPUT_DIR", "repo_outputs/daily_refresh"))
    if not base_dir.is_absolute():
        base_dir = PROJECT_ROOT / base_dir

    latest_dir = base_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    date_tag = datetime.now().strftime("%Y-%m-%d")
    latest_file = latest_dir / "finviz_momentum_pool.csv"
    dated_file = latest_dir / f"finviz_momentum_pool_{date_tag}.csv"

    df.to_csv(latest_file, index=False, encoding="utf-8-sig")
    df.to_csv(dated_file, index=False, encoding="utf-8-sig")
    return [latest_file, dated_file]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Layer-1 Finviz momentum scan and export separated radar CSV files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _build_arg_parser().parse_args(argv)
    print("[FINVIZ] start momentum scan")
    try:
        df = _scan_finviz()
    except ImportError:
        print("[FINVIZ] package not installed: pip install finviz>=2.0.0")
        return 2
    except KeyboardInterrupt:
        print("[FINVIZ] interrupted by user")
        return 130
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError) as exc:
        print(f"[FINVIZ] scan failed: {exc}")
        return 1

    if len(df) == 0:
        print("[FINVIZ] no rows returned")
        return 3

    outputs = _write_outputs(df)
    print(f"[FINVIZ] rows={len(df)}")
    for path in outputs:
        print(f"[FINVIZ] output={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
