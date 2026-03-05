from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_EXE = PROJECT_ROOT / '.venv' / 'Scripts' / 'python.exe'
BUILD_SCRIPT = PROJECT_ROOT / 'scripts' / 'build_ai_trading_dataset.py'
AI_TRADING_DIR = PROJECT_ROOT / 'repo_outputs' / 'ai_trading'
COMPARE_BASE_DIR = AI_TRADING_DIR / 'profile_compare'


def _latest_ai_trading_run() -> Path | None:
    if not AI_TRADING_DIR.exists():
        return None

    run_dirs: List[Path] = []
    for day_dir in AI_TRADING_DIR.iterdir():
        if not day_dir.is_dir() or day_dir.name in {'latest', 'profile_compare'}:
            continue
        for run_dir in day_dir.iterdir():
            if run_dir.is_dir():
                run_dirs.append(run_dir)

    if not run_dirs:
        return None
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return run_dirs[0]


def _extract_top_tickers(decision_path: Path, top_n: int = 5) -> List[str]:
    if not decision_path.exists():
        return []
    try:
        df = pd.read_csv(decision_path, encoding='utf-8-sig')
    except UnicodeDecodeError:
        df = pd.read_csv(decision_path)
    if len(df) == 0 or 'ticker' not in df.columns:
        return []
    return [str(v).strip().upper() for v in df['ticker'].head(max(top_n, 1)).tolist()]


def _run_one_profile(profile: str, research_mode: str, enable_catalyst: bool, stamp_prefix: str) -> Dict[str, object]:
    run_stamp = f"{stamp_prefix}_{profile}"
    env = os.environ.copy()
    env['SCANNER_PROFILE'] = profile
    env['AI_RESEARCH_MODE'] = research_mode
    env['CATALYST_DETECTOR_ENABLED'] = 'true' if enable_catalyst else 'false'
    env['AI_BUILD_RUN_STAMP'] = run_stamp

    cmd = [str(PYTHON_EXE), str(BUILD_SCRIPT)]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True)

    latest_run = _latest_ai_trading_run()
    if latest_run is None:
        return {
            'profile': profile,
            'exit_code': proc.returncode,
            'error': 'no_output_dir',
            'stdout': proc.stdout[-2000:],
            'stderr': proc.stderr[-2000:],
        }

    manifest_path = latest_run / 'pipeline_manifest.json'
    if not manifest_path.exists():
        return {
            'profile': profile,
            'exit_code': proc.returncode,
            'error': 'missing_manifest',
            'run_dir': str(latest_run),
            'stdout': proc.stdout[-2000:],
            'stderr': proc.stderr[-2000:],
        }

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    stats = manifest.get('stats', {}) if isinstance(manifest, dict) else {}
    top_tickers = _extract_top_tickers(latest_run / 'decision_signals_daily.csv', top_n=5)

    return {
        'profile': profile,
        'exit_code': proc.returncode,
        'run_dir': str(latest_run),
        'scan_date': manifest.get('scan_date', ''),
        'research_mode': manifest.get('research_mode', research_mode),
        'decision_rows': int(stats.get('decision_rows', 0)),
        'decision_keep_count': int(stats.get('decision_keep_count', 0)),
        'decision_watch_count': int(stats.get('decision_watch_count', 0)),
        'scanner_pass_count': int(stats.get('scanner_pass_count', 0)),
        'rank_regime': stats.get('rank_regime', ''),
        'rank_breadth': stats.get('rank_breadth', 0.0),
        'api_catalyst_rows': int((manifest.get('api_catalyst') or {}).get('rows', 0)),
        'top5_tickers': '|'.join(top_tickers),
    }


def _build_compare_outputs(rows: List[Dict[str, object]], compare_dir: Path, research_mode: str) -> Dict[str, object]:
    compare_dir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(rows)
    csv_path = compare_dir / 'profile_compare_summary.csv'
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')

    best_profile = ''
    if len(df) > 0 and 'decision_keep_count' in df.columns:
        sort_df = df.sort_values(
            ['decision_keep_count', 'decision_rows', 'scanner_pass_count'],
            ascending=[False, False, False],
        )
        best_profile = str(sort_df.iloc[0].get('profile', ''))

    md_lines = [
        f"# AI Profile Compare ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
        '',
        f"Research mode: {research_mode}",
        '',
        '| profile | decision_rows | keep | watch | scanner_pass | regime | breadth | top5 |',
        '|---|---:|---:|---:|---:|---|---:|---|',
    ]

    for _, row in df.iterrows():
        md_lines.append(
            f"| {row.get('profile','')} | {int(row.get('decision_rows',0))} | {int(row.get('decision_keep_count',0))} | {int(row.get('decision_watch_count',0))} | {int(row.get('scanner_pass_count',0))} | {row.get('rank_regime','')} | {row.get('rank_breadth',0)} | {row.get('top5_tickers','')} |"
        )

    if best_profile:
        md_lines.extend(['', f"Best profile (today): {best_profile}"])

    md_path = compare_dir / 'profile_compare_summary.md'
    md_path.write_text('\n'.join(md_lines), encoding='utf-8')

    meta = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'research_mode': research_mode,
        'profiles': df['profile'].tolist() if 'profile' in df.columns else [],
        'best_profile_today': best_profile,
        'files': ['profile_compare_summary.csv', 'profile_compare_summary.md'],
    }
    with open(compare_dir / 'profile_compare_manifest.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    latest_compare = COMPARE_BASE_DIR / 'latest'
    latest_compare.mkdir(parents=True, exist_ok=True)
    for src in [csv_path, md_path, compare_dir / 'profile_compare_manifest.json']:
        dst = latest_compare / src.name
        dst.write_bytes(src.read_bytes())

    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description='Run AI trading build for multiple scanner profiles and compare outputs.')
    parser.add_argument('--research-mode', choices=['web', 'api'], default='web')
    parser.add_argument('--profiles', default='balanced,monster_v1', help='Comma-separated profiles')
    parser.add_argument('--enable-catalyst', action='store_true', help='Enable Tavily+Gemini API detector in api mode')
    args = parser.parse_args()

    if not PYTHON_EXE.exists():
        print(f'[COMPARE] python not found: {PYTHON_EXE}')
        return 2

    research_mode = args.research_mode
    profiles = [p.strip() for p in args.profiles.split(',') if p.strip()]
    if not profiles:
        print('[COMPARE] no valid profiles')
        return 2

    stamp_prefix = datetime.now().strftime('%H%M%S')
    run_rows: List[Dict[str, object]] = []

    for profile in profiles:
        enabled = bool(args.enable_catalyst and research_mode == 'api')
        row = _run_one_profile(profile=profile, research_mode=research_mode, enable_catalyst=enabled, stamp_prefix=stamp_prefix)
        run_rows.append(row)
        print(f"[COMPARE] profile={profile} exit={row.get('exit_code')} decision_rows={row.get('decision_rows', 0)} keep={row.get('decision_keep_count', 0)}")

    compare_dir = COMPARE_BASE_DIR / datetime.now().strftime('%Y-%m-%d') / stamp_prefix
    meta = _build_compare_outputs(run_rows, compare_dir=compare_dir, research_mode=research_mode)

    print(f"[COMPARE] output={compare_dir}")
    print(f"[COMPARE] best_profile_today={meta.get('best_profile_today', '')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
