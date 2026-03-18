from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as app_config

AI_TRADING_LATEST = PROJECT_ROOT / "repo_outputs" / "ai_trading" / "latest"
AI_READY_LATEST = PROJECT_ROOT / "repo_outputs" / "ai_ready" / "latest"
DAILY_REFRESH_LATEST = PROJECT_ROOT / "repo_outputs" / "daily_refresh" / "latest"

REQUIRED_OUTPUT_CONTRACT: Dict[str, list[str]] = {
    "market_dataset_daily.csv": [
        "ticker",
        "as_of_date",
        "rank_score_v2_adjusted",
        "decision_tag_v1",
        "decision_rank_after_gate",
        "market_data_source",
        "market_snapshot_live",
        "protocol_gate_reason",
        "final_elimination_owner",
    ],
    "ranking_signals_daily.csv": [
        "ticker",
        "rank_engine_rank",
        "rank_engine_tier",
        "rank_score_v2_adjusted",
        "as_of_date",
    ],
    "decision_signals_daily.csv": [
        "ticker",
        "decision_tag_v1",
        "decision_action",
        "risk_level",
        "invalidation_rule",
        "rank_score_v2_adjusted",
        "rank_engine_rank",
        "decision_rank_after_gate",
        "protocol_gate_reason",
        "market_data_source",
        "market_snapshot_live",
        "final_elimination_owner",
        "as_of_date",
    ],
    "ai_research_candidates.csv": [
        "rank",
        "ticker",
        "decision_tag_v1",
        "decision_action",
        "risk_level",
        "invalidation_rule",
        "market_data_source",
        "market_snapshot_live",
    ],
    "feature_signals_daily.csv": [
        "ticker",
        "feature_priority_tier",
        "feature_alpha_score_v1",
        "daily_change_pct",
        "rel_volume",
        "as_of_date",
    ],
    "radar_signals_daily.csv": [
        "ticker",
        "radar_tag",
        "radar_priority_tier",
        "multi_radar_score",
        "as_of_date",
    ],
    "pre_event_watchlist.csv": [
        "ticker",
        "event_date",
        "days_to_event",
        "pre_event_score",
        "source_ts",
    ],
    "live_event_feed.csv": [
        "event_id",
        "ts",
        "ticker",
        "source_name",
        "primary_event_type",
        "dedupe_key",
    ],
    "event_score_log.csv": [
        "event_id",
        "ticker",
        "trigger_score",
        "high_priority_flag",
        "scoring_version",
    ],
    "trade_trigger_queue.csv": [
        "event_id",
        "ticker",
        "trigger_score",
        "entry_signal_status",
        "queue_rank",
    ],
    "bundle_contract_status.csv": [
        "scan_date",
        "status",
        "severity",
        "error_code",
        "message",
    ],
    "ai_decision_contract_v2_template.csv": [
        "as_of_date",
        "ticker",
        "decision_mode",
        "decision_status",
        "decision_score",
        "entry_plan",
        "invalidation_rule",
        "decision_ts",
    ],
}


def _clean_text(value: object, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in {"", "nan", "none", "null", "na", "n/a"}:
        return default
    return text


def _run_python_script(script_rel_path: str, args: list[str]) -> None:
    cmd = [sys.executable, str(PROJECT_ROOT / script_rel_path), *args]
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def _read_csv_fallback(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)


def _resolve_scan_date() -> str:
    decision_df = _read_csv_fallback(AI_TRADING_LATEST / "decision_signals_daily.csv")
    if len(decision_df) > 0 and "as_of_date" in decision_df.columns:
        val = str(decision_df.iloc[0].get("as_of_date", "")).strip()
        if val:
            return val

    now_dt = datetime.now()
    weekday = now_dt.weekday()
    if weekday == 0:
        delta = 3
    elif weekday == 6:
        delta = 2
    else:
        delta = 1
    return (now_dt - timedelta(days=delta)).strftime("%Y-%m-%d")


def _load_release_status() -> Tuple[str, Dict[str, object]]:
    report = _read_csv_fallback(AI_TRADING_LATEST / "release_readiness_report.csv")
    if len(report) == 0:
        return "provisional", {}

    baseline = report[report.get("scenario", "").astype(str) == "baseline_v1"].copy() if "scenario" in report.columns else pd.DataFrame()
    row = baseline.iloc[0] if len(baseline) > 0 else report.iloc[0]
    status = str(row.get("ai_decision_status", "provisional")).strip().lower()
    if status not in {"ready", "provisional", "insufficient_history"}:
        status = "provisional"
    return status, row.to_dict()


def _normalize_sentiment(val: object) -> str:
    text = _clean_text(val, "neutral").lower()
    if text in {"hard_positive", "soft_positive", "positive", "bullish"}:
        return "positive"
    if text in {"hard_negative", "soft_negative", "negative", "bearish"}:
        return "negative"
    return "neutral"


def _confidence_tier(status: str, followthrough: float, verif: float) -> str:
    if status == "insufficient_history":
        return "low"
    score = (float(followthrough) * 0.55) + (float(verif) * 0.45)
    if score >= 75:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _build_decision_source() -> Tuple[pd.DataFrame, str]:
    decision_df = _read_csv_fallback(AI_TRADING_LATEST / "decision_signals_daily.csv")
    ranking_df = _read_csv_fallback(AI_TRADING_LATEST / "ranking_signals_daily.csv")
    dataset_df = _read_csv_fallback(AI_TRADING_LATEST / "market_dataset_daily.csv")

    selected = pd.DataFrame()
    if len(decision_df) > 0:
        selected = decision_df[decision_df.get("decision_tag_v1", "").astype(str).isin(["keep", "watch"])].copy()
        if len(selected) > 0:
            rank_col = "decision_rank_after_gate" if "decision_rank_after_gate" in selected.columns else "rank_engine_rank"
            selected[rank_col] = pd.to_numeric(selected.get(rank_col), errors="coerce").fillna(9999)
            selected = selected.sort_values([rank_col, "rank_score_v2_adjusted", "rank_score_v1"], ascending=[True, False, False])

    top1_exists = len(selected[selected.get("decision_rank_after_gate", 0).astype(str) == "1"]) > 0 if len(selected) > 0 and "decision_rank_after_gate" in selected.columns else (len(selected) > 0)

    if top1_exists and len(selected) > 0:
        return selected.head(5).copy(), "top1_top5"

    # Top 1 不存在時，回退 Top 5 only。
    base = ranking_df.copy() if len(ranking_df) > 0 else dataset_df.copy()
    if len(base) == 0:
        return pd.DataFrame(), "top5_only"

    if "rank_engine_rank" in base.columns:
        base = base.sort_values("rank_engine_rank", ascending=True)
    elif "rank_score_v2_adjusted" in base.columns:
        base = base.sort_values("rank_score_v2_adjusted", ascending=False)

    base = base.head(5).copy()
    if len(dataset_df) > 0:
        keep_cols = [c for c in [
            "ticker",
            "decision_action",
            "tomorrow_entry_readiness",
            "invalidation_rule",
            "protocol_gate_reason",
            "market_data_source",
            "market_snapshot_live",
            "rank_score_v1",
            "rank_score_v2_adjusted",
            "overnight_followthrough_score",
            "catalyst_verifiability_score",
            "event_score_v1",
            "monster_score",
            "risk_level",
            "open_exhaustion_risk_score",
            "overnight_catalyst",
            "sector",
            "industry",
        ] if c in dataset_df.columns]
        base = base.merge(dataset_df[keep_cols].drop_duplicates(subset=["ticker"], keep="first"), on="ticker", how="left", suffixes=("", "_d"))

    base["decision_tag_v1"] = "watch"
    base["decision_action"] = base.get("decision_action", "").astype(str).replace("", "等回踩 1-2% 再評估")
    base["tomorrow_entry_readiness"] = base.get("tomorrow_entry_readiness", "").astype(str).replace("", "neutral")
    base["protocol_gate_reason"] = base.get("protocol_gate_reason", "").astype(str).replace("", "top5_only_fallback")
    base["market_data_source"] = base.get("market_data_source", "").astype(str).replace("", "fallback_ranking")
    base["market_snapshot_live"] = base.get("market_snapshot_live", False)
    if "decision_rank_after_gate" not in base.columns:
        base["decision_rank_after_gate"] = list(range(1, len(base) + 1))

    return base, "top5_only"


def _validate_output_schema(scan_date: str) -> Dict[str, object]:
    rows = []
    hard_fail = False

    for csv_name, required_cols in REQUIRED_OUTPUT_CONTRACT.items():
        csv_path = AI_TRADING_LATEST / csv_name
        exists = csv_path.exists()
        if exists:
            try:
                header = pd.read_csv(csv_path, nrows=0, encoding="utf-8-sig")
            except UnicodeDecodeError:
                header = pd.read_csv(csv_path, nrows=0)
            columns = [str(c) for c in header.columns.tolist()]
        else:
            columns = []

        missing_cols = [c for c in required_cols if c not in columns]
        file_ok = bool(exists and not missing_cols)
        if not file_ok:
            hard_fail = True

        rows.append(
            {
                "scan_date": scan_date,
                "file_name": csv_name,
                "required_column_count": len(required_cols),
                "actual_column_count": len(columns),
                "file_exists": exists,
                "missing_columns": "|".join(missing_cols),
                "schema_status": "ok" if file_ok else "failed",
            }
        )

    report_df = pd.DataFrame(rows)
    report_path = AI_TRADING_LATEST / "output_schema_stability_report.csv"
    report_df.to_csv(report_path, index=False, encoding="utf-8-sig")

    if hard_fail:
        failed = report_df[report_df["schema_status"] != "ok"]
        fail_list = ", ".join(failed["file_name"].astype(str).tolist())
        raise RuntimeError(f"Output schema validation failed: {fail_list}")

    return {
        "schema_report": str(report_path),
        "schema_ok": True,
        "checked_files": len(rows),
    }


def _cleanup_legacy_decision_files() -> None:
    protected_names = {
        "ai_decision_latest.csv",
        "ai_decision_contract_v2_template.csv",
    }
    for directory in [AI_TRADING_LATEST, AI_READY_LATEST, DAILY_REFRESH_LATEST]:
        if not directory.exists():
            continue
        for path in directory.glob("ai_decision_*.csv"):
            if path.name in protected_names:
                continue
            try:
                path.unlink()
            except OSError:
                continue


def _build_ai_decision_file(scan_date: str, status: str, readiness_meta: Dict[str, object]) -> Path:
    source_df, selection_mode = _build_decision_source()
    readiness_note = f"status={status}; coverage={_clean_text(readiness_meta.get('historical_snapshot_coverage'), 'n/a')}"

    if len(source_df) == 0:
        out = pd.DataFrame(
            [
                {
                    "decision_date": scan_date,
                    "rank": 1,
                    "ticker": "NO_CANDIDATE",
                    "short_score_final": 0,
                    "swing_score": 0,
                    "core_score": 0,
                    "risk_level": "",
                    "tech_status": "",
                    "theme": "",
                    "decision_tag": "watch",
                    "reason_summary": "今日無高品質候選，建議空手",
                    "source_ref": f"decision_signals_daily.csv;ranking_signals_daily.csv;{readiness_note}",
                    "research_mode": str(getattr(app_config, "AI_RESEARCH_MODE", "web")).strip().lower() or "web",
                    "catalyst_type": "none",
                    "catalyst_sentiment": "neutral",
                    "explosion_probability": 0,
                    "hype_score": 0,
                    "confidence": 0,
                    "api_final_score": 0,
                    "catalyst_source": "bundle",
                    "catalyst_summary": "",
                    "decision_action": "",
                    "tomorrow_entry_readiness": "",
                    "invalidation_rule": "",
                    "protocol_gate_reason": "no_candidate",
                    "market_data_source": "none",
                    "market_snapshot_live": False,
                    "ai_decision_status": status,
                    "confidence_tier": "low",
                    "selection_mode": "top5_only",
                }
            ]
        )
    else:
        rows = []
        for idx, (_, row) in enumerate(source_df.iterrows(), 1):
            follow = float(pd.to_numeric(row.get("overnight_followthrough_score"), errors="coerce") or 0.0)
            verif = float(pd.to_numeric(row.get("catalyst_verifiability_score"), errors="coerce") or 0.0)
            decision_tag = _clean_text(row.get("decision_tag_v1"), "watch").lower()
            if decision_tag not in {"keep", "watch"}:
                decision_tag = "watch"

            short_score = float(pd.to_numeric(row.get("rank_score_v2_adjusted"), errors="coerce") or pd.to_numeric(row.get("rank_score_v1"), errors="coerce") or 0.0)
            swing_score = follow
            core_score = float(pd.to_numeric(row.get("rank_score_v1"), errors="coerce") or 0.0)
            explosion_probability = float(pd.to_numeric(row.get("monster_score"), errors="coerce") or 0.0)
            hype_score = float(pd.to_numeric(row.get("event_score_v1"), errors="coerce") or 0.0)
            confidence = verif
            api_final_score = follow
            tech_status = _clean_text(row.get("tomorrow_entry_readiness"), "")
            gate_reason = _clean_text(row.get("protocol_gate_reason"), "none")
            market_source = _clean_text(row.get("market_data_source"), "unknown")
            snapshot_live = bool(pd.to_numeric(row.get("market_snapshot_live"), errors="coerce") or False)
            overnight = _clean_text(row.get("overnight_catalyst"), "neutral").lower()
            theme = _clean_text(row.get("sector"), "") or _clean_text(row.get("industry"), "")

            if selection_mode == "top5_only":
                reason = f"Top5 only fallback; gate_reason={gate_reason}; data_source={market_source}"
            else:
                reason = f"entry={tech_status or 'neutral'}; continuation_prob={int(round(max(0.0, min(99.0, 25 + follow * 0.6))))}; gate_reason={gate_reason}; data_source={market_source}"

            rows.append(
                {
                    "decision_date": scan_date,
                    "rank": idx,
                    "ticker": str(row.get("ticker", "")).strip().upper(),
                    "short_score_final": round(short_score, 2),
                    "swing_score": round(swing_score, 2),
                    "core_score": round(core_score, 2),
                    "risk_level": _clean_text(row.get("risk_level"), ""),
                    "tech_status": tech_status,
                    "theme": theme,
                    "decision_tag": decision_tag,
                    "reason_summary": reason,
                    "source_ref": f"decision_signals_daily.csv;market_dataset_daily.csv;release_readiness_report.csv;{readiness_note}",
                    "research_mode": _clean_text(getattr(app_config, "AI_RESEARCH_MODE", "web"), "web").lower(),
                    "catalyst_type": _clean_text(row.get("overnight_catalyst_type", overnight), "local_only"),
                    "catalyst_sentiment": _normalize_sentiment(overnight),
                    "explosion_probability": round(max(0.0, min(99.0, explosion_probability)), 2),
                    "hype_score": round(max(0.0, min(99.0, hype_score)), 2),
                    "confidence": round(max(0.0, min(99.0, confidence)), 2),
                    "api_final_score": round(max(0.0, min(99.0, api_final_score)), 2),
                    "catalyst_source": market_source,
                    "catalyst_summary": "",
                    "decision_action": _clean_text(row.get("decision_action"), ""),
                    "tomorrow_entry_readiness": tech_status,
                    "invalidation_rule": _clean_text(row.get("invalidation_rule"), ""),
                    "protocol_gate_reason": gate_reason,
                    "market_data_source": market_source,
                    "market_snapshot_live": snapshot_live,
                    "ai_decision_status": status,
                    "confidence_tier": _confidence_tier(status=status, followthrough=follow, verif=verif),
                    "selection_mode": selection_mode,
                }
            )

        out = pd.DataFrame(rows)

    out["decision_author"] = "pipeline_preview"
    out["decision_mode"] = "preview"
    out["decision_is_final"] = False

    out_path = AI_TRADING_LATEST / f"ai_decision_seed_{scan_date}.csv"
    preview_path = AI_TRADING_LATEST / f"protocol_release_preview_{scan_date}.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    out.to_csv(preview_path, index=False, encoding="utf-8-sig")
    out.to_csv(AI_TRADING_LATEST / "ai_decision_seed_latest.csv", index=False, encoding="utf-8-sig")
    # Keep this file name for bundle compatibility, but it is explicitly preview.
    out.to_csv(AI_TRADING_LATEST / "ai_decision_latest.csv", index=False, encoding="utf-8-sig")

    AI_READY_LATEST.mkdir(parents=True, exist_ok=True)
    DAILY_REFRESH_LATEST.mkdir(parents=True, exist_ok=True)

    shutil.copy2(out_path, AI_READY_LATEST / out_path.name)
    shutil.copy2(out_path, DAILY_REFRESH_LATEST / out_path.name)
    shutil.copy2(preview_path, AI_READY_LATEST / preview_path.name)
    shutil.copy2(preview_path, DAILY_REFRESH_LATEST / preview_path.name)

    return out_path


def _refresh_bundle(scan_date: str) -> Dict[str, object]:
    build_script = PROJECT_ROOT / "scripts" / "build_ai_trading_dataset.py"
    spec = importlib.util.spec_from_file_location("build_ai_trading_dataset", build_script)
    if spec is None or spec.loader is None:
        return {"bundle_updated": False, "reason": "load_build_module_failed"}

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    include_api = (AI_TRADING_LATEST / "api_catalyst_analysis_daily.csv").exists()
    return mod.refresh_unified_ai_ready_bundle(
        scan_date=scan_date,
        ai_ready_latest_dir=AI_READY_LATEST,
        ai_trading_latest_dir=AI_TRADING_LATEST,
        include_api_catalyst=include_api,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot Protocol Release Candidate pipeline runner.")
    parser.add_argument("--target-days", type=int, default=60)
    parser.add_argument("--min-days", type=int, default=20)
    parser.add_argument("--backfill-run-stamp", type=str, default="000000_backfill_rc_v1")
    parser.add_argument("--skip-backfill", action="store_true")
    args = parser.parse_args()

    if not args.skip_backfill:
        _run_python_script(
            "scripts/historical_snapshot_backfill.py",
            [
                "--target-days", str(int(args.target_days)),
                "--min-days", str(int(args.min_days)),
                "--run-stamp", str(args.backfill_run_stamp),
            ],
        )

    _run_python_script("scripts/build_ai_trading_dataset.py", [])
    _run_python_script(
        "scripts/run_baseline_backfill_analysis.py",
        [
            "--target-days", str(int(args.target_days)),
            "--min-days", str(int(args.min_days)),
            "--baseline-tag", "baseline_v1",
        ],
    )

    scan_date = _resolve_scan_date()
    _cleanup_legacy_decision_files()
    schema_meta = _validate_output_schema(scan_date=scan_date)
    status, readiness_meta = _load_release_status()
    decision_path = _build_ai_decision_file(scan_date=scan_date, status=status, readiness_meta=readiness_meta)

    bundle_meta = _refresh_bundle(scan_date=scan_date)

    manifest = {
        "release_name": "Protocol Release Candidate v1",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scan_date": scan_date,
        "target_days": int(args.target_days),
        "minimum_required_days": int(args.min_days),
        "ai_decision_status": status,
        "decision_file": str(decision_path),
        "decision_mode": "preview",
        "decision_author": "pipeline_preview",
        "schema": schema_meta,
        "bundle": bundle_meta,
        "outputs": [
            "ai_decision_seed_latest.csv",
            "protocol_release_preview_YYYY-MM-DD.csv",
            "ai_decision_latest.csv",
            "decision_funnel_daily.csv",
            "pre_event_watchlist.csv",
            "live_event_feed.csv",
            "event_score_log.csv",
            "trade_trigger_queue.csv",
            "bundle_contract_status.csv",
            "ai_decision_contract_v2_template.csv",
            "decision_outcome_audit_daily.csv",
            "attribution_summary_daily.csv",
            "baseline_v1_vs_variants_metrics.csv",
            "release_readiness_report.csv",
            "release_threshold_monitor_report.csv",
            "output_schema_stability_report.csv",
            "baseline_v1_config.json",
            "baseline_v1_config.csv",
        ],
    }
    (AI_TRADING_LATEST / "protocol_release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[RELEASE] protocol=Protocol Release Candidate v1")
    print(f"[RELEASE] scan_date={scan_date}")
    print(f"[RELEASE] ai_decision_status={status}")
    print("[RELEASE] decision_mode=preview")
    print(f"[RELEASE] ai_decision_preview_file={decision_path}")
    print(f"[RELEASE] schema_report={schema_meta.get('schema_report')}")
    print(f"[RELEASE] bundle_updated={bundle_meta.get('bundle_updated', False)}")
    print(f"[RELEASE] bundle_sheets={bundle_meta.get('bundle_sheet_count', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
