from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as app_config
from ai_trading.decision_risk import apply_decision_risk_layer

AI_TRADING_DIR = PROJECT_ROOT / "repo_outputs" / "ai_trading"
LATEST_DIR = AI_TRADING_DIR / "latest"
DAILY_REFRESH_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh"


@dataclass
class SnapshotRun:
    asofdate: str
    run_stamp: str
    dataset_path: Path


def _collect_snapshot_runs(max_days: int) -> List[SnapshotRun]:
    out: List[SnapshotRun] = []
    if not AI_TRADING_DIR.exists():
        return out

    day_dirs = sorted(
        [p for p in AI_TRADING_DIR.iterdir() if p.is_dir() and p.name[:4].isdigit()],
        reverse=True,
    )
    for day_dir in day_dirs:
        run_dirs = sorted(
            [p for p in day_dir.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        picked = None
        for run_dir in run_dirs:
            dataset_path = run_dir / "market_dataset_daily.csv"
            if dataset_path.exists():
                picked = SnapshotRun(asofdate=day_dir.name, run_stamp=run_dir.name, dataset_path=dataset_path)
                break
        if picked is not None:
            out.append(picked)
        if len(out) >= max_days:
            break

    return list(reversed(out))


def _get_decision_param_snapshot() -> Dict[str, object]:
    keys = [
        "AI_DECISION_KEEP_MIN_SCORE",
        "AI_DECISION_WATCH_MIN_SCORE",
        "AI_DECISION_KEEP_MIN_LIVE_RELAX",
        "AI_DECISION_FOLLOWTHROUGH_KEEP_MIN",
        "AI_DECISION_FOLLOWTHROUGH_WATCH_MIN",
        "AI_DECISION_KEEP_LIVE_RELAX",
        "AI_DECISION_KEEP_PROMOTE_VERIF_MIN",
        "AI_DECISION_EXHAUSTION_AVOID_MIN",
        "AI_DECISION_EXHAUSTION_WATCH_MIN",
        "AI_DECISION_HIGH_GAP_BLOCK",
        "AI_DECISION_PROXY_GAP_RELAX",
        "AI_DECISION_KEEP_MAX_COUNT",
        "AI_DECISION_WATCH_MAX_COUNT",
        "AI_DECISION_TOTAL_MAX_COUNT",
        "AI_DECISION_CAP_RELAX_LIVE_VERIFIED",
    ]
    return {k: getattr(app_config, k, None) for k in keys}


def _build_scenario_overrides() -> Dict[str, Dict[str, object]]:
    base_keep = float(getattr(app_config, "AI_DECISION_KEEP_MIN_SCORE", 42.0))
    base_follow_keep = float(getattr(app_config, "AI_DECISION_FOLLOWTHROUGH_KEEP_MIN", 30.0))
    base_live_relax = float(getattr(app_config, "AI_DECISION_KEEP_MIN_LIVE_RELAX", 8.0))

    return {
        "baseline_v1": {
            "AI_DECISION_CAP_RELAX_LIVE_VERIFIED": False,
        },
        "cap_relax_live_verified_v1": {
            "AI_DECISION_CAP_RELAX_LIVE_VERIFIED": True,
        },
        "cap_relax_plus_keep_soft_v1": {
            "AI_DECISION_CAP_RELAX_LIVE_VERIFIED": True,
            "AI_DECISION_KEEP_MIN_SCORE": max(0.0, base_keep - 3.0),
            "AI_DECISION_FOLLOWTHROUGH_KEEP_MIN": max(0.0, base_follow_keep - 4.0),
            "AI_DECISION_KEEP_MIN_LIVE_RELAX": max(0.0, base_live_relax + 2.0),
        },
    }


def _run_decision_with_overrides(dataset: pd.DataFrame, overrides: Dict[str, object]) -> tuple[pd.DataFrame, pd.DataFrame, Dict[str, object]]:
    old_values = {k: getattr(app_config, k, None) for k in overrides}
    for key, value in overrides.items():
        setattr(app_config, key, value)
    try:
        return apply_decision_risk_layer(dataset.copy())
    finally:
        for key, value in old_values.items():
            setattr(app_config, key, value)


def _as_utc_ts(dt_str: str) -> datetime:
    return datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _fetch_finnhub_daily_candles(ticker: str, start_dt: datetime, end_dt: datetime, api_key: str) -> pd.DataFrame:
    if not api_key:
        return pd.DataFrame()

    params = {
        "symbol": ticker,
        "resolution": "D",
        "from": int(start_dt.timestamp()),
        "to": int(end_dt.timestamp()),
        "token": api_key,
    }
    try:
        resp = requests.get("https://finnhub.io/api/v1/stock/candle", params=params, timeout=10)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
    except (requests.RequestException, ValueError):
        return pd.DataFrame()

    if not isinstance(payload, dict) or payload.get("s") != "ok":
        return pd.DataFrame()

    t_vals = payload.get("t") or []
    if not t_vals:
        return pd.DataFrame()

    df = pd.DataFrame(
        {
            "ts": t_vals,
            "open": payload.get("o") or [],
            "high": payload.get("h") or [],
            "low": payload.get("l") or [],
            "close": payload.get("c") or [],
        }
    )
    if len(df) == 0:
        return df
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_local_price_history() -> Dict[str, pd.DataFrame]:
    cache: Dict[str, pd.DataFrame] = {}
    if not DAILY_REFRESH_DIR.exists():
        return cache

    day_dirs = sorted([p for p in DAILY_REFRESH_DIR.iterdir() if p.is_dir() and p.name[:4].isdigit()])
    for day_dir in day_dirs:
        run_dirs = sorted([p for p in day_dir.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
        if not run_dirs:
            continue
        raw_path = run_dirs[0] / "raw_market_daily.csv"
        if not raw_path.exists():
            continue
        try:
            raw = pd.read_csv(raw_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            raw = pd.read_csv(raw_path)

        if len(raw) == 0 or "Ticker" not in raw.columns:
            continue

        raw["ticker"] = raw["Ticker"].astype(str).str.strip().str.upper().str.replace(".US", "", regex=False)
        raw["close"] = pd.to_numeric(raw.get("Price"), errors="coerce")
        raw["daily_change"] = pd.to_numeric(raw.get("Daily_Change"), errors="coerce")
        raw = raw[["ticker", "close", "daily_change"]].dropna(subset=["ticker", "close"]).copy()
        raw["asofdate"] = day_dir.name

        for ticker, group in raw.groupby("ticker"):
            rec = group[["asofdate", "close", "daily_change"]].copy()
            if ticker not in cache:
                cache[ticker] = rec
            else:
                cache[ticker] = pd.concat([cache[ticker], rec], ignore_index=True)

    for ticker in list(cache.keys()):
        cache[ticker] = (
            cache[ticker]
            .sort_values("asofdate")
            .drop_duplicates(subset=["asofdate"], keep="last")
            .reset_index(drop=True)
        )
    return cache


def _compute_outcome_row_from_local(
    local_price_df: pd.DataFrame,
    asofdate: str,
    final_decision: str,
) -> Dict[str, object]:
    out = {
        "day1_return_open_to_close": pd.NA,
        "day1_return_close_to_close": pd.NA,
        "day2_return_close_to_close": pd.NA,
        "max_runup_day1": pd.NA,
        "max_drawdown_day1": pd.NA,
        "keep_quality_score": pd.NA,
    }

    if len(local_price_df) == 0:
        return out

    hits = local_price_df.index[local_price_df["asofdate"].astype(str) == str(asofdate)].tolist()
    if not hits:
        return out

    i0 = int(hits[0])
    if i0 + 1 >= len(local_price_df):
        return out

    c0 = float(local_price_df.iloc[i0]["close"])
    c1 = float(local_price_df.iloc[i0 + 1]["close"])
    if c0 > 0:
        d1 = (c1 - c0) / c0 * 100.0
        out["day1_return_close_to_close"] = d1
        out["day1_return_open_to_close"] = d1
        out["max_runup_day1"] = max(0.0, d1)
        out["max_drawdown_day1"] = min(0.0, d1)

    if i0 + 2 < len(local_price_df):
        c2 = float(local_price_df.iloc[i0 + 2]["close"])
        if c1 > 0:
            out["day2_return_close_to_close"] = (c2 - c1) / c1 * 100.0

    if str(final_decision) == "keep":
        d1 = pd.to_numeric(out["day1_return_close_to_close"], errors="coerce")
        ru = pd.to_numeric(out["max_runup_day1"], errors="coerce")
        dd = pd.to_numeric(out["max_drawdown_day1"], errors="coerce")
        if pd.notna(d1) and pd.notna(ru) and pd.notna(dd):
            out["keep_quality_score"] = float(d1) + float(ru) * 0.30 + float(dd) * 0.20

    return out


def _compute_outcome_row(
    candle_df: pd.DataFrame,
    asofdate: str,
    final_decision: str,
) -> Dict[str, object]:
    out = {
        "day1_return_open_to_close": pd.NA,
        "day1_return_close_to_close": pd.NA,
        "day2_return_close_to_close": pd.NA,
        "max_runup_day1": pd.NA,
        "max_drawdown_day1": pd.NA,
        "keep_quality_score": pd.NA,
    }

    if len(candle_df) == 0:
        return out

    target_date = datetime.strptime(asofdate, "%Y-%m-%d").date()
    hits = candle_df.index[candle_df["date"] == target_date].tolist()
    if not hits:
        return out

    i0 = int(hits[0])
    if i0 + 1 >= len(candle_df):
        return out

    day0 = candle_df.iloc[i0]
    day1 = candle_df.iloc[i0 + 1]

    c0 = float(day0["close"]) if pd.notna(day0["close"]) else None
    o1 = float(day1["open"]) if pd.notna(day1["open"]) else None
    h1 = float(day1["high"]) if pd.notna(day1["high"]) else None
    l1 = float(day1["low"]) if pd.notna(day1["low"]) else None
    c1 = float(day1["close"]) if pd.notna(day1["close"]) else None

    if o1 and c1:
        out["day1_return_open_to_close"] = (c1 - o1) / o1 * 100.0
    if c0 and c1:
        out["day1_return_close_to_close"] = (c1 - c0) / c0 * 100.0
    if o1 and h1:
        out["max_runup_day1"] = (h1 - o1) / o1 * 100.0
    if o1 and l1:
        out["max_drawdown_day1"] = (l1 - o1) / o1 * 100.0

    if i0 + 2 < len(candle_df):
        day2 = candle_df.iloc[i0 + 2]
        c2 = float(day2["close"]) if pd.notna(day2["close"]) else None
        if c1 and c2:
            out["day2_return_close_to_close"] = (c2 - c1) / c1 * 100.0

    if str(final_decision) == "keep":
        d1 = pd.to_numeric(out["day1_return_close_to_close"], errors="coerce")
        ru = pd.to_numeric(out["max_runup_day1"], errors="coerce")
        dd = pd.to_numeric(out["max_drawdown_day1"], errors="coerce")
        if pd.notna(d1) and pd.notna(ru) and pd.notna(dd):
            out["keep_quality_score"] = float(d1) + float(ru) * 0.30 + float(dd) * 0.20

    return out


def _build_outcome_audit(
    decision_frames: Dict[str, pd.DataFrame],
    target_days: int,
) -> pd.DataFrame:
    api_key = str(getattr(app_config, "FINNHUB_API_KEY", "") or "").strip()
    all_rows: List[Dict[str, object]] = []
    local_price_cache = _load_local_price_history()

    all_dates: List[str] = []
    all_tickers: set[str] = set()
    for df in decision_frames.values():
        if len(df) == 0:
            continue
        all_dates.extend(df["asofdate"].astype(str).tolist())
        all_tickers.update(df["ticker"].astype(str).str.upper().tolist())

    if not all_dates or not all_tickers:
        return pd.DataFrame()

    min_dt = _as_utc_ts(min(all_dates)) - timedelta(days=7)
    max_dt = _as_utc_ts(max(all_dates)) + timedelta(days=7)

    fallback_tickers = sorted([t for t in all_tickers if len(local_price_cache.get(t, pd.DataFrame())) < 3])
    candle_cache: Dict[str, pd.DataFrame] = {}
    for ticker in fallback_tickers:
        candle_cache[ticker] = _fetch_finnhub_daily_candles(ticker=ticker, start_dt=min_dt, end_dt=max_dt, api_key=api_key)

    for scenario, df in decision_frames.items():
        if len(df) == 0:
            continue
        coverage_days = len(sorted(set(df["asofdate"].astype(str).tolist())))
        snapshot_coverage = round(float(coverage_days) / float(max(1, target_days)), 4)

        for _, row in df.iterrows():
            ticker = str(row.get("ticker", "")).strip().upper()
            asofdate = str(row.get("asofdate", "")).strip()
            final_decision = str(row.get("post_gate_decision", row.get("decision_tag_v1", ""))).strip().lower()
            reason = str(row.get("protocol_gate_reason", "")).strip()

            outcome = _compute_outcome_row_from_local(
                local_price_df=local_price_cache.get(ticker, pd.DataFrame()),
                asofdate=asofdate,
                final_decision=final_decision,
            )
            if pd.isna(pd.to_numeric(outcome.get("day1_return_close_to_close"), errors="coerce")):
                outcome = _compute_outcome_row(
                    candle_df=candle_cache.get(ticker, pd.DataFrame()),
                    asofdate=asofdate,
                    final_decision=final_decision,
                )
            all_rows.append(
                {
                    "scenario": scenario,
                    "ticker": ticker,
                    "asofdate": asofdate,
                    "final_decision": final_decision,
                    "day1_return_open_to_close": outcome["day1_return_open_to_close"],
                    "day1_return_close_to_close": outcome["day1_return_close_to_close"],
                    "day2_return_close_to_close": outcome["day2_return_close_to_close"],
                    "max_runup_day1": outcome["max_runup_day1"],
                    "max_drawdown_day1": outcome["max_drawdown_day1"],
                    "kept_or_downgraded_reason": reason,
                    "keep_quality_score": outcome["keep_quality_score"],
                    "final_elimination_owner": row.get("final_elimination_owner", ""),
                    "cap_hit_flag": bool(row.get("cap_hit_flag", False)),
                    "decision_rank_after_gate": int(pd.to_numeric(row.get("decision_rank_after_gate", 0), errors="coerce") or 0),
                    "outcome_data_available": pd.notna(outcome["day1_return_close_to_close"]),
                    "historical_snapshot_coverage": snapshot_coverage,
                }
            )

    return pd.DataFrame(all_rows)


def _build_attribution_summary(full_frames: Dict[str, pd.DataFrame], target_days: int) -> pd.DataFrame:
    all_rows: List[Dict[str, object]] = []
    all_owner_tokens: set[str] = set()

    for scenario, frame in full_frames.items():
        if len(frame) == 0:
            continue
        all_owner_tokens.update(frame.get("final_elimination_owner", pd.Series(dtype=str)).astype(str).str.strip().tolist())

    owner_tokens = sorted([t for t in all_owner_tokens if t])

    for scenario, frame in full_frames.items():
        if len(frame) == 0:
            continue
        used_days = len(sorted(set(frame["asofdate"].astype(str).tolist())))
        coverage = round(float(used_days) / float(max(1, target_days)), 4)

        for asofdate, group in frame.groupby("asofdate"):
            g = group.copy()
            owner_counts = g.get("final_elimination_owner", pd.Series(dtype=str)).astype(str).value_counts(dropna=False).to_dict()
            row = {
                "scenario": scenario,
                "asofdate": str(asofdate),
                "total_candidates": int(len(g)),
                "selected_keep_count": int(g.get("decision_tag_v1", pd.Series(dtype=str)).astype(str).eq("keep").sum()),
                "selected_watch_count": int(g.get("decision_tag_v1", pd.Series(dtype=str)).astype(str).eq("watch").sum()),
                "eliminated_count": int(g.get("decision_tag_v1", pd.Series(dtype=str)).astype(str).eq("replace_candidate").sum()),
                "cap_hit_count": int(pd.to_numeric(g.get("cap_hit_flag", False), errors="coerce").fillna(0).astype(bool).sum()),
                "hard_block_count": int(g.get("gate_stage", pd.Series(dtype=str)).astype(str).eq("hard_block").sum()),
                "downgraded_count": int(g.get("gate_stage", pd.Series(dtype=str)).astype(str).eq("downgrade_watch").sum()),
                "live_rows": int(pd.to_numeric(g.get("market_snapshot_live", False), errors="coerce").fillna(0).astype(bool).sum()),
                "proxy_rows": int((~pd.to_numeric(g.get("market_snapshot_live", False), errors="coerce").fillna(0).astype(bool)).sum()),
                "historical_snapshot_coverage": coverage,
            }
            for token in owner_tokens:
                row[f"owner_{token}"] = int(owner_counts.get(token, 0))
            all_rows.append(row)

    return pd.DataFrame(all_rows)


def _top_metrics(decision_df: pd.DataFrame, audit_df: pd.DataFrame, scenario: str) -> Dict[str, object]:
    dec = decision_df.copy()
    aud = audit_df[audit_df["scenario"] == scenario].copy() if len(audit_df) > 0 else pd.DataFrame()

    top1_total = 0
    top1_hit = 0
    top5_total = 0
    top5_hit = 0

    if len(dec) > 0 and len(aud) > 0:
        merged = dec.merge(
            aud[["ticker", "asofdate", "day1_return_close_to_close", "max_drawdown_day1"]],
            on=["ticker", "asofdate"],
            how="left",
        )
        for _, group in merged.groupby("asofdate"):
            g = group.sort_values("decision_rank_after_gate")
            top1 = g[g["decision_rank_after_gate"] == 1]
            if len(top1) > 0:
                v = pd.to_numeric(top1.iloc[0].get("day1_return_close_to_close"), errors="coerce")
                if pd.notna(v):
                    top1_total += 1
                    if float(v) > 0:
                        top1_hit += 1

            top5 = g[g["decision_rank_after_gate"].between(1, 5)]
            for _, row in top5.iterrows():
                v = pd.to_numeric(row.get("day1_return_close_to_close"), errors="coerce")
                if pd.notna(v):
                    top5_total += 1
                    if float(v) > 0:
                        top5_hit += 1

    keep_dd = pd.Series(dtype=float)
    top5_ret = pd.Series(dtype=float)
    if len(aud) > 0:
        keep_dd = pd.to_numeric(
            aud[aud["final_decision"] == "keep"]["max_drawdown_day1"],
            errors="coerce",
        ).dropna()
        top5_ret = pd.to_numeric(
            aud[aud["decision_rank_after_gate"].between(1, 5)]["day1_return_close_to_close"],
            errors="coerce",
        ).dropna()

    return {
        "scenario": scenario,
        "top1_next_day_continuation_rate": round(float(top1_hit) / float(top1_total), 4) if top1_total > 0 else pd.NA,
        "top5_hit_rate": round(float(top5_hit) / float(top5_total), 4) if top5_total > 0 else pd.NA,
        "top5_avg_day1_return": round(float(top5_ret.mean()), 4) if len(top5_ret) > 0 else pd.NA,
        "keep_avg_max_drawdown": round(float(keep_dd.mean()), 4) if len(keep_dd) > 0 else pd.NA,
        "top1_samples": int(top1_total),
        "top5_samples": int(top5_total),
    }


def _build_readiness_report(
    scenario_decisions: Dict[str, pd.DataFrame],
    scenario_full: Dict[str, pd.DataFrame],
    outcome_df: pd.DataFrame,
    target_days: int,
    min_days: int,
) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for scenario, dec in scenario_decisions.items():
        full = scenario_full.get(scenario, pd.DataFrame())
        aud = outcome_df[outcome_df["scenario"] == scenario].copy() if len(outcome_df) > 0 else pd.DataFrame()

        used_days = len(sorted(set(dec.get("asofdate", pd.Series(dtype=str)).astype(str).tolist()))) if len(dec) > 0 else 0
        coverage = round(float(used_days) / float(max(1, target_days)), 4)
        sample_sufficiency = used_days >= int(min_days)

        if len(dec) > 0:
            day_group = dec.groupby("asofdate")
            top1_days = int(sum((group.get("decision_rank_after_gate", pd.Series(dtype=float)) == 1).any() for _, group in day_group))
            top5_days = int(sum((group.get("decision_rank_after_gate", pd.Series(dtype=float)).between(1, 5)).sum() > 0 for _, group in day_group))
        else:
            top1_days = 0
            top5_days = 0

        top1_availability = round(float(top1_days) / float(max(1, used_days)), 4) if used_days > 0 else 0.0
        top5_availability = round(float(top5_days) / float(max(1, used_days)), 4) if used_days > 0 else 0.0

        live_ratio = 0.0
        if len(full) > 0:
            live_ratio = round(
                float(pd.to_numeric(full.get("market_snapshot_live", False), errors="coerce").fillna(0).astype(bool).mean()),
                4,
            )

        outcome_completeness = 0.0
        if len(aud) > 0:
            outcome_completeness = round(
                float(pd.to_numeric(aud.get("outcome_data_available", False), errors="coerce").fillna(0).astype(bool).mean()),
                4,
            )

        if used_days < int(min_days):
            status = "insufficient_history"
        elif top1_availability <= 0.0:
            status = "provisional"
        else:
            status = "ready"

        rows.append(
            {
                "scenario": scenario,
                "target_days": int(target_days),
                "minimum_required_days": int(min_days),
                "used_days": int(used_days),
                "historical_snapshot_coverage": coverage,
                "sample_sufficiency": bool(sample_sufficiency),
                "live_ratio": live_ratio,
                "top1_availability": top1_availability,
                "top5_availability": top5_availability,
                "outcome_audit_completeness": outcome_completeness,
                "release_allowed": bool(status == "ready"),
                "ai_decision_status": status,
            }
        )
    return pd.DataFrame(rows)


def _safe_float(value: object) -> float | None:
    num = pd.to_numeric(value, errors="coerce")
    if pd.isna(num):
        return None
    return float(num)


def _build_threshold_monitor_report(
    metrics_df: pd.DataFrame,
    readiness_df: pd.DataFrame,
    min_days: int,
) -> pd.DataFrame:
    top1_drop_threshold = float(getattr(app_config, "RC_MONITOR_TOP1_DROP_THRESHOLD", 0.05))
    top5_drop_threshold = float(getattr(app_config, "RC_MONITOR_TOP5_DROP_THRESHOLD", 0.05))
    keep_dd_worsen_threshold = float(getattr(app_config, "RC_MONITOR_KEEP_DD_WORSEN_THRESHOLD", 0.5))

    if len(metrics_df) == 0:
        return pd.DataFrame()

    readiness_cols = [
        c
        for c in ["scenario", "used_days", "historical_snapshot_coverage", "ai_decision_status"]
        if c in readiness_df.columns
    ]
    merged = metrics_df.copy()
    if readiness_cols:
        merged = merged.merge(readiness_df[readiness_cols], on="scenario", how="left")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    merged["generated_at"] = generated_at

    history_cols = [
        "generated_at",
        "scenario",
        "top1_next_day_continuation_rate",
        "top5_hit_rate",
        "keep_avg_max_drawdown",
        "used_days",
        "historical_snapshot_coverage",
        "ai_decision_status",
    ]
    for col in history_cols:
        if col not in merged.columns:
            merged[col] = pd.NA

    history_path = LATEST_DIR / "release_metrics_history.csv"
    if history_path.exists():
        try:
            history_df = pd.read_csv(history_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            history_df = pd.read_csv(history_path)
    else:
        history_df = pd.DataFrame(columns=history_cols)

    updated_history = pd.concat([history_df, merged[history_cols]], ignore_index=True)
    updated_history.to_csv(history_path, index=False, encoding="utf-8-sig")

    rows: List[Dict[str, object]] = []
    for _, row in merged.iterrows():
        scenario = str(row.get("scenario", ""))
        if "scenario" in history_df.columns:
            prev = history_df[history_df["scenario"].astype(str) == scenario]
        else:
            prev = pd.DataFrame(columns=history_cols)
        prev_row = prev.iloc[-1] if len(prev) > 0 else pd.Series(dtype=object)

        cur_top1 = _safe_float(row.get("top1_next_day_continuation_rate"))
        prev_top1 = _safe_float(prev_row.get("top1_next_day_continuation_rate")) if len(prev_row) > 0 else None
        cur_top5 = _safe_float(row.get("top5_hit_rate"))
        prev_top5 = _safe_float(prev_row.get("top5_hit_rate")) if len(prev_row) > 0 else None
        cur_keep_dd = _safe_float(row.get("keep_avg_max_drawdown"))
        prev_keep_dd = _safe_float(prev_row.get("keep_avg_max_drawdown")) if len(prev_row) > 0 else None

        top1_drop = (prev_top1 - cur_top1) if (prev_top1 is not None and cur_top1 is not None) else pd.NA
        top5_drop = (prev_top5 - cur_top5) if (prev_top5 is not None and cur_top5 is not None) else pd.NA
        keep_dd_worsen = (prev_keep_dd - cur_keep_dd) if (prev_keep_dd is not None and cur_keep_dd is not None) else pd.NA

        top1_alert = bool(pd.notna(top1_drop) and float(top1_drop) > top1_drop_threshold)
        top5_alert = bool(pd.notna(top5_drop) and float(top5_drop) > top5_drop_threshold)
        keep_dd_alert = bool(pd.notna(keep_dd_worsen) and float(keep_dd_worsen) > keep_dd_worsen_threshold)

        used_days = int(pd.to_numeric(row.get("used_days"), errors="coerce") or 0)
        coverage_alert = used_days < int(min_days)
        metric_alert = top1_alert or top5_alert or keep_dd_alert
        auto_status = "degraded" if (coverage_alert or metric_alert) else "ready"

        rows.append(
            {
                "generated_at": generated_at,
                "scenario": scenario,
                "minimum_required_days": int(min_days),
                "used_days": used_days,
                "historical_snapshot_coverage": pd.to_numeric(row.get("historical_snapshot_coverage"), errors="coerce"),
                "ai_decision_status": str(row.get("ai_decision_status", "")).strip().lower(),
                "top1_next_day_continuation_rate": cur_top1,
                "top5_hit_rate": cur_top5,
                "keep_avg_max_drawdown": cur_keep_dd,
                "delta_top1_drop_vs_prev": top1_drop,
                "delta_top5_drop_vs_prev": top5_drop,
                "delta_keep_drawdown_worsen_vs_prev": keep_dd_worsen,
                "coverage_below_min_flag": coverage_alert,
                "top1_deterioration_flag": top1_alert,
                "top5_deterioration_flag": top5_alert,
                "keep_drawdown_deterioration_flag": keep_dd_alert,
                "metrics_deterioration_flag": metric_alert,
                "auto_release_gate": auto_status,
                "threshold_top1_drop": top1_drop_threshold,
                "threshold_top5_drop": top5_drop_threshold,
                "threshold_keep_drawdown_worsen": keep_dd_worsen_threshold,
            }
        )

    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze baseline and run historical gate backfill analysis.")
    parser.add_argument("--target-days", type=int, default=20)
    parser.add_argument("--min-days", type=int, default=20)
    parser.add_argument("--baseline-tag", type=str, default="baseline_v1")
    args = parser.parse_args()

    LATEST_DIR.mkdir(parents=True, exist_ok=True)

    snapshots = _collect_snapshot_runs(max_days=max(int(args.target_days), 1))
    coverage = round(float(len(snapshots)) / float(max(1, int(args.target_days))), 4)

    scenarios = _build_scenario_overrides()
    if args.baseline_tag in scenarios and args.baseline_tag != "baseline_v1":
        scenarios["baseline_v1"] = scenarios.pop(args.baseline_tag)

    daily_rows: List[Dict[str, object]] = []
    scenario_decision_frames: Dict[str, List[pd.DataFrame]] = {k: [] for k in scenarios}
    scenario_full_frames: Dict[str, List[pd.DataFrame]] = {k: [] for k in scenarios}

    for snap in snapshots:
        try:
            dataset = pd.read_csv(snap.dataset_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            dataset = pd.read_csv(snap.dataset_path)

        for scenario, overrides in scenarios.items():
            full_df, decision_df, meta = _run_decision_with_overrides(dataset=dataset, overrides=overrides)
            if len(full_df) > 0:
                full_df = full_df.copy()
                full_df["asofdate"] = snap.asofdate
                scenario_full_frames[scenario].append(full_df)
            if len(decision_df) > 0:
                decision_df = decision_df.copy()
                decision_df["asofdate"] = snap.asofdate
                scenario_decision_frames[scenario].append(decision_df)

            daily_rows.append(
                {
                    "scenario": scenario,
                    "date": snap.asofdate,
                    "run_stamp": snap.run_stamp,
                    "keep": int(meta.get("keep_count", 0)),
                    "watch": int(meta.get("watch_count", 0)),
                    "total": int(meta.get("decision_rows", 0)),
                    "historical_snapshot_coverage": coverage,
                }
            )

    daily_df = pd.DataFrame(daily_rows)
    daily_path = LATEST_DIR / "baseline_v1_vs_variants_daily.csv"
    daily_df.to_csv(daily_path, index=False, encoding="utf-8-sig")

    scenario_decisions = {
        k: (pd.concat(v, ignore_index=True) if v else pd.DataFrame())
        for k, v in scenario_decision_frames.items()
    }
    scenario_full = {
        k: (pd.concat(v, ignore_index=True) if v else pd.DataFrame())
        for k, v in scenario_full_frames.items()
    }

    outcome_df = _build_outcome_audit(decision_frames=scenario_decisions, target_days=int(args.target_days))
    outcome_path = LATEST_DIR / "decision_outcome_audit_daily.csv"
    outcome_df.to_csv(outcome_path, index=False, encoding="utf-8-sig")

    attribution_df = _build_attribution_summary(full_frames=scenario_full, target_days=int(args.target_days))
    attribution_path = LATEST_DIR / "attribution_summary_daily.csv"
    attribution_df.to_csv(attribution_path, index=False, encoding="utf-8-sig")

    metrics_rows = [_top_metrics(scenario_decisions[name], outcome_df, name) for name in scenarios.keys()]
    metrics_df = pd.DataFrame(metrics_rows)
    metrics_path = LATEST_DIR / "baseline_v1_vs_variants_metrics.csv"
    metrics_df.to_csv(metrics_path, index=False, encoding="utf-8-sig")

    readiness_df = _build_readiness_report(
        scenario_decisions=scenario_decisions,
        scenario_full=scenario_full,
        outcome_df=outcome_df,
        target_days=int(args.target_days),
        min_days=int(args.min_days),
    )
    readiness_path = LATEST_DIR / "release_readiness_report.csv"
    readiness_df.to_csv(readiness_path, index=False, encoding="utf-8-sig")

    threshold_df = _build_threshold_monitor_report(
        metrics_df=metrics_df,
        readiness_df=readiness_df,
        min_days=int(args.min_days),
    )
    threshold_path = LATEST_DIR / "release_threshold_monitor_report.csv"
    threshold_df.to_csv(threshold_path, index=False, encoding="utf-8-sig")

    # Backward-compatible two-scenario compare artifacts.
    legacy_base = daily_df[daily_df["scenario"] == "baseline_v1"].copy()
    legacy_var = daily_df[daily_df["scenario"] == "cap_relax_live_verified_v1"].copy()
    legacy_compare = legacy_base.merge(
        legacy_var,
        on=["date", "run_stamp", "historical_snapshot_coverage"],
        how="outer",
        suffixes=("_baseline", "_variant"),
    )
    for col in ["keep_baseline", "watch_baseline", "total_baseline", "keep_variant", "watch_variant", "total_variant"]:
        legacy_compare[col] = pd.to_numeric(legacy_compare.get(col), errors="coerce").fillna(0).astype(int)
    legacy_compare["delta_keep"] = legacy_compare["keep_variant"] - legacy_compare["keep_baseline"]
    legacy_compare["delta_watch"] = legacy_compare["watch_variant"] - legacy_compare["watch_baseline"]
    legacy_compare["delta_total"] = legacy_compare["total_variant"] - legacy_compare["total_baseline"]
    compare_path = LATEST_DIR / "baseline_v1_vs_cap_relax_live_verified_v1.csv"
    legacy_compare.to_csv(compare_path, index=False, encoding="utf-8-sig")

    legacy_metrics = metrics_df[metrics_df["scenario"].isin(["baseline_v1", "cap_relax_live_verified_v1"])].copy()
    legacy_metrics_path = LATEST_DIR / "baseline_v1_vs_cap_relax_live_verified_v1_metrics.csv"
    legacy_metrics.to_csv(legacy_metrics_path, index=False, encoding="utf-8-sig")

    baseline_payload = {
        "tag": "baseline_v1",
        "frozen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_days": int(args.target_days),
        "minimum_required_days": int(args.min_days),
        "used_days": int(len(snapshots)),
        "historical_snapshot_coverage": coverage,
        "params": _get_decision_param_snapshot(),
        "variants": {k: v for k, v in scenarios.items() if k != "baseline_v1"},
        "files": [
            daily_path.name,
            compare_path.name,
            outcome_path.name,
            attribution_path.name,
            metrics_path.name,
            readiness_path.name,
            threshold_path.name,
        ],
    }
    baseline_path = LATEST_DIR / "baseline_v1_config.json"
    baseline_path.write_text(json.dumps(baseline_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    baseline_cfg_rows = [
        {"section": "meta", "key": "tag", "value": "baseline_v1"},
        {"section": "meta", "key": "target_days", "value": int(args.target_days)},
        {"section": "meta", "key": "minimum_required_days", "value": int(args.min_days)},
        {"section": "meta", "key": "used_days", "value": int(len(snapshots))},
        {"section": "meta", "key": "historical_snapshot_coverage", "value": coverage},
    ]
    for key, value in _get_decision_param_snapshot().items():
        baseline_cfg_rows.append({"section": "baseline_params", "key": key, "value": value})
    for variant, ov in scenarios.items():
        if variant == "baseline_v1":
            continue
        for key, value in ov.items():
            baseline_cfg_rows.append({"section": f"variant:{variant}", "key": key, "value": value})
    baseline_cfg_path = LATEST_DIR / "baseline_v1_config.csv"
    pd.DataFrame(baseline_cfg_rows).to_csv(baseline_cfg_path, index=False, encoding="utf-8-sig")

    print("[BASELINE] tag=baseline_v1")
    print(f"[BASELINE] used_days={len(snapshots)}/{int(args.target_days)} coverage={coverage}")
    print(f"[BASELINE] daily={daily_path}")
    print(f"[BASELINE] compare={compare_path}")
    print(f"[BASELINE] outcome={outcome_path}")
    print(f"[BASELINE] attribution={attribution_path}")
    print(f"[BASELINE] metrics={metrics_path}")
    print(f"[BASELINE] readiness={readiness_path}")
    print(f"[BASELINE] threshold_monitor={threshold_path}")
    print(f"[BASELINE] freeze={baseline_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
