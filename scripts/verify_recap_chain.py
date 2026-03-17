from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import turso_state as ts
from scripts.push_alerts_from_ai_decision import (
    _persist_bedtime_plan,
    _persist_morning_plan,
    build_recap_message_preview,
)

OUTPUT_FILE = Path("repo_outputs/backtest/alerts/recap_chain_check_result.txt")
PREVIEW_TIMEOUT_SEC = 120
TURSO_TIMEOUT_SEC = 20


def _record(results: list[str], failures: list[tuple[str, str]], label: str, ok: bool, detail: str = "") -> None:
    results.append(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append((label, str(detail or "").strip()))


def _safe_preview(mode: str):
    def _run_preview():
        return build_recap_message_preview(
            mode=mode,
            top_n=5,
            tags={"keep", "watch"},
            respect_mode_window=False,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            preview = pool.submit(_run_preview).result(timeout=PREVIEW_TIMEOUT_SEC)
    except FutureTimeoutError:
        return None, f"preview_timeout>{PREVIEW_TIMEOUT_SEC}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"

    if not bool(preview.get("ok")):
        return None, f"preview_not_ok: {preview.get('skip_reason', 'unknown')}"
    return preview, ""


def _find_long_analysis_lines(msg: str) -> list[str]:
    bad: list[str] = []
    for raw in str(msg or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") or line.startswith("-") or line.endswith(":"):
            continue
        if len(line) > 50:
            bad.append(line)
    return bad


def run_checks() -> tuple[list[str], list[tuple[str, str]]]:
    results: list[str] = []
    failures: list[tuple[str, str]] = []
    messages: dict[str, str] = {}
    pipes: dict[str, dict] = {}

    bedtime_preview, bedtime_err = _safe_preview("bedtime")
    if bedtime_preview is None:
        _record(results, failures, "bedtime recap 格式正確", False, bedtime_err)
    else:
        bedtime_msg = str(bedtime_preview.get("message", ""))
        messages["bedtime"] = bedtime_msg
        has_sections = all(section in bedtime_msg for section in ["現在應該做什麼", "失效條件", "結論"])
        _record(results, failures, "bedtime recap 格式正確", has_sections, bedtime_msg[:1200])
        try:
            _persist_bedtime_plan(
                str(bedtime_preview.get("decision_date", "")),
                bedtime_preview.get("recap_context", {}) if isinstance(bedtime_preview.get("recap_context"), dict) else {},
                bedtime_msg,
                bedtime_preview.get("source_id", ""),
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(("bedtime plan persist", f"{type(exc).__name__}: {exc}"))

    morning_preview, morning_err = _safe_preview("morning")
    if morning_preview is None:
        _record(results, failures, "morning has_prior_bedtime_plan = true", False, morning_err)
    else:
        morning_msg = str(morning_preview.get("message", ""))
        messages["morning"] = morning_msg
        morning_pipe = morning_preview.get("pipeline_debug", {}) if isinstance(morning_preview.get("pipeline_debug"), dict) else {}
        pipes["morning"] = morning_pipe
        has_prior_bedtime = bool(morning_pipe.get("has_prior_bedtime_plan") is True)
        _record(results, failures, "morning has_prior_bedtime_plan = true", has_prior_bedtime, json.dumps(morning_pipe, ensure_ascii=False))
        try:
            _persist_morning_plan(
                str(morning_preview.get("decision_date", "")),
                morning_preview.get("recap_context", {}) if isinstance(morning_preview.get("recap_context"), dict) else {},
                morning_msg,
                morning_preview.get("source_id", ""),
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(("morning plan persist", f"{type(exc).__name__}: {exc}"))

    opening_preview, opening_err = _safe_preview("opening")
    if opening_preview is None:
        _record(results, failures, "opening has_prior_morning_plan = true", False, opening_err)
    else:
        opening_msg = str(opening_preview.get("message", ""))
        messages["opening"] = opening_msg
        opening_pipe = opening_preview.get("pipeline_debug", {}) if isinstance(opening_preview.get("pipeline_debug"), dict) else {}
        pipes["opening"] = opening_pipe
        has_prior_morning = bool(opening_pipe.get("has_prior_morning_plan") is True)
        _record(results, failures, "opening has_prior_morning_plan = true", has_prior_morning, json.dumps(opening_pipe, ensure_ascii=False))

    source_values = []
    for mode in ("morning", "opening"):
        source_value = str((pipes.get(mode) or {}).get("execution_summary_source", "")).strip()
        if source_value:
            source_values.append(f"{mode}:{source_value}")
    _record(
        results,
        failures,
        "execution_summary_source 欄位存在且不是空值",
        bool(source_values),
        f"source_values={source_values} raw={json.dumps(pipes, ensure_ascii=False)}",
    )

    bad_lines: dict[str, list[str]] = {}
    for mode in ("bedtime", "morning", "opening"):
        found = _find_long_analysis_lines(messages.get(mode, ""))
        if found:
            bad_lines[mode] = found

    _record(
        results,
        failures,
        "三個 recap 輸出裡不含超過 50 字連續分析段落",
        len(messages) == 3 and not bad_lines,
        json.dumps(bad_lines, ensure_ascii=False, indent=2) if bad_lines else f"message_count={len(messages)}",
    )

    def _check_turso() -> tuple[bool, str]:
        conn = ts._connect()  # noqa: SLF001
        if conn is None:
            return False, "turso_connect_failed"
        row = conn.execute(
            "SELECT updated_at, row_count FROM runtime_state_latest WHERE state_key = ?",
            (ts.STATE_KEY_AI_DECISION_LATEST,),
        ).fetchone()
        ts._safe_close(conn)  # noqa: SLF001
        if not row:
            return False, "state_row_missing"
        if isinstance(row, tuple):
            updated_at = str(row[0] or "").strip()
            row_count = int(row[1] or 0)
        else:
            updated_at = str(row["updated_at"] or "").strip()
            row_count = int(row["row_count"] or 0)
        df, source = ts.load_runtime_df(ts.STATE_KEY_AI_DECISION_LATEST)
        parsed = pd.to_datetime(updated_at, errors="coerce")
        is_today = bool(pd.notna(parsed) and parsed.date() == datetime.now().date())
        has_rows = bool(len(df) > 0 and row_count > 0)
        ok = bool(source and source.startswith("turso:") and has_rows and is_today)
        detail = json.dumps(
            {
                "source": source,
                "updated_at": updated_at,
                "row_count": row_count,
                "df_rows": int(len(df)),
                "is_today": is_today,
            },
            ensure_ascii=False,
        )
        return ok, detail

    turso_ok = False
    turso_detail = ""
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            turso_ok, turso_detail = pool.submit(_check_turso).result(timeout=TURSO_TIMEOUT_SEC)
    except FutureTimeoutError:
        turso_ok, turso_detail = False, f"turso_timeout>{TURSO_TIMEOUT_SEC}s"
    except Exception as exc:  # noqa: BLE001
        turso_ok, turso_detail = False, f"{type(exc).__name__}: {exc}"

    _record(results, failures, "Turso 裡有 ai_decision 記錄且 timestamp 在今天內", turso_ok, turso_detail)
    return results, failures


def main() -> int:
    results, failures = run_checks()
    pass_count = sum(1 for line in results if line.startswith("[PASS]"))
    fail_count = sum(1 for line in results if line.startswith("[FAIL]"))

    output_lines: list[str] = []
    output_lines.extend(results)
    output_lines.append("---")
    output_lines.append(f"總計：{pass_count} PASS / {fail_count} FAIL")
    if failures:
        output_lines.append("失敗項目實際輸出：")
        for label, detail in failures:
            output_lines.append(f"[{label}]")
            output_lines.append(detail if detail else "(無額外輸出)")
            output_lines.append("---")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(output_lines), encoding="utf-8")

    print("\n".join(output_lines))
    print(f"\n結果檔案: {OUTPUT_FILE}")
    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
