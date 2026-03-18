from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_trading.contracts import DataPaths
from ai_trading.market_data_pipeline import MarketDataPipeline

DAILY_REFRESH_DIR = PROJECT_ROOT / "repo_outputs" / "daily_refresh"
AI_READY_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready"
AI_TRADING_DIR = PROJECT_ROOT / "repo_outputs" / "ai_trading"


def _latest_run_dir(base_dir: Path) -> Path | None:
    if not base_dir.exists() or not base_dir.is_dir():
        return None
    runs = sorted([p for p in base_dir.iterdir() if p.is_dir()], reverse=True)
    return runs[0] if runs else None


def _collect_backfill_inputs(target_days: int) -> List[Tuple[str, Path, Path]]:
    dates = sorted(
        [p.name for p in DAILY_REFRESH_DIR.iterdir() if p.is_dir() and p.name[:4].isdigit()],
        reverse=True,
    ) if DAILY_REFRESH_DIR.exists() else []

    rows: List[Tuple[str, Path, Path]] = []
    for asofdate in dates:
        daily_run = _latest_run_dir(DAILY_REFRESH_DIR / asofdate)
        ready_run = _latest_run_dir(AI_READY_DIR / asofdate)
        if daily_run is None or ready_run is None:
            continue

        required = [
            daily_run / "raw_market_daily.csv",
            daily_run / "monster_radar_daily.csv",
            daily_run / "ai_focus_list.csv",
            daily_run / "fusion_top_daily.csv",
            ready_run / "xq_short_term_updated.csv",
        ]
        if any(not p.exists() for p in required):
            continue

        rows.append((asofdate, daily_run, ready_run))
        if len(rows) >= max(1, target_days):
            break

    return list(reversed(rows))


def _write_funnel_csv(dataset: pd.DataFrame, output_path: Path) -> None:
    funnel_cols = [
        "ticker",
        "pre_gate_rank",
        "rank_score_v2_adjusted",
        "rankscorev2adjusted",
        "overnight_followthrough_score",
        "open_exhaustion_risk_score",
        "market_data_source",
        "market_snapshot_live",
        "protocol_gate_reason",
        "post_gate_decision",
        "gate_stage",
        "decision_rank_after_gate",
        "cap_hit_flag",
        "final_elimination_owner",
        "promote_to_keep_reason",
        "as_of_date",
    ]
    out = dataset.copy()
    if "rankscorev2adjusted" not in out.columns and "rank_score_v2_adjusted" in out.columns:
        out["rankscorev2adjusted"] = out["rank_score_v2_adjusted"]
    for col in funnel_cols:
        if col not in out.columns:
            out[col] = ""
    out = out[funnel_cols].rename(columns={"as_of_date": "asofdate"})
    out.to_csv(output_path, index=False, encoding="utf-8-sig")


def _build_one_snapshot(asofdate: str, daily_run: Path, ready_run: Path, run_stamp: str) -> Dict[str, object]:
    out_dir = AI_TRADING_DIR / asofdate / run_stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = DataPaths(
        raw_market_csv=str(daily_run / "raw_market_daily.csv"),
        monster_radar_csv=str(daily_run / "monster_radar_daily.csv"),
        xq_updated_csv=str(ready_run / "xq_short_term_updated.csv"),
        ai_focus_csv=str(daily_run / "ai_focus_list.csv"),
        fusion_csv=str(daily_run / "fusion_top_daily.csv"),
    )

    pipeline = MarketDataPipeline(paths)
    artifacts = pipeline.build(as_of_date=asofdate)

    artifacts.dataset.to_csv(out_dir / "market_dataset_daily.csv", index=False, encoding="utf-8-sig")
    artifacts.feature_signals.to_csv(out_dir / "feature_signals_daily.csv", index=False, encoding="utf-8-sig")
    artifacts.radar_signals.to_csv(out_dir / "radar_signals_daily.csv", index=False, encoding="utf-8-sig")
    artifacts.event_signals.to_csv(out_dir / "event_signals_daily.csv", index=False, encoding="utf-8-sig")
    artifacts.ranking_signals.to_csv(out_dir / "ranking_signals_daily.csv", index=False, encoding="utf-8-sig")
    artifacts.decision_signals.to_csv(out_dir / "decision_signals_daily.csv", index=False, encoding="utf-8-sig")
    _write_funnel_csv(artifacts.dataset, out_dir / "decision_funnel_daily.csv")

    manifest = {
        "scan_date": asofdate,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "historical_snapshot_backfill",
        "inputs": {
            "daily_refresh_run": str(daily_run),
            "ai_ready_run": str(ready_run),
        },
        "outputs": [
            "market_dataset_daily.csv",
            "feature_signals_daily.csv",
            "radar_signals_daily.csv",
            "event_signals_daily.csv",
            "ranking_signals_daily.csv",
            "decision_signals_daily.csv",
            "decision_funnel_daily.csv",
        ],
        "stats": {
            "rows": int(len(artifacts.dataset)),
            "ranking_rows": int(len(artifacts.ranking_signals)),
            "decision_rows": int(len(artifacts.decision_signals)),
        },
    }
    (out_dir / "pipeline_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "asofdate": asofdate,
        "run_stamp": run_stamp,
        "rows": int(len(artifacts.dataset)),
        "decision_rows": int(len(artifacts.decision_signals)),
        "status": "built",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill historical ai_trading snapshots from daily_refresh + ai_ready archives.")
    parser.add_argument("--target-days", type=int, default=60)
    parser.add_argument("--min-days", type=int, default=20)
    parser.add_argument("--run-stamp", type=str, default="rc_backfill_v1")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    inputs = _collect_backfill_inputs(target_days=max(1, int(args.target_days)))
    report_rows: List[Dict[str, object]] = []

    for asofdate, daily_run, ready_run in inputs:
        out_dir = AI_TRADING_DIR / asofdate / args.run_stamp
        dataset_path = out_dir / "market_dataset_daily.csv"
        if dataset_path.exists() and not args.force:
            report_rows.append(
                {
                    "asofdate": asofdate,
                    "run_stamp": args.run_stamp,
                    "rows": int(pd.read_csv(dataset_path, encoding="utf-8-sig").shape[0]),
                    "decision_rows": pd.NA,
                    "status": "skipped_existing",
                }
            )
            continue

        try:
            report_rows.append(_build_one_snapshot(asofdate=asofdate, daily_run=daily_run, ready_run=ready_run, run_stamp=args.run_stamp))
        except (FileNotFoundError, PermissionError, ValueError, KeyError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            report_rows.append(
                {
                    "asofdate": asofdate,
                    "run_stamp": args.run_stamp,
                    "rows": 0,
                    "decision_rows": 0,
                    "status": f"failed:{type(exc).__name__}",
                }
            )

    report_df = pd.DataFrame(report_rows)
    latest_dir = AI_TRADING_DIR / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    report_path = latest_dir / "historical_snapshot_backfill_report.csv"
    report_df.to_csv(report_path, index=False, encoding="utf-8-sig")

    built_days = int(report_df[report_df["status"].astype(str).str.startswith("built")]["asofdate"].nunique()) if len(report_df) > 0 else 0
    available_days = int(report_df[~report_df["status"].astype(str).str.startswith("failed")]["asofdate"].nunique()) if len(report_df) > 0 else 0

    print(f"[BACKFILL] target_days={int(args.target_days)}")
    print(f"[BACKFILL] min_days={int(args.min_days)}")
    print(f"[BACKFILL] available_days={available_days}")
    print(f"[BACKFILL] built_days={built_days}")
    print(f"[BACKFILL] report={report_path}")
    if available_days < int(args.min_days):
        print(f"[BACKFILL] warning=insufficient_available_history({available_days}<{int(args.min_days)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
