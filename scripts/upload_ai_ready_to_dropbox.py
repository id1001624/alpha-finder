r"""
Upload the latest ai_ready bundle to Dropbox as per-sheet Markdown files.

Examples:
    python scripts/upload_ai_ready_to_dropbox.py --dry-run
    python scripts/upload_ai_ready_to_dropbox.py --create-shared-links

Env/config:
    DROPBOX_UPLOAD_ENABLED=true
    DROPBOX_APP_KEY=...
    DROPBOX_APP_SECRET=...
    DROPBOX_REFRESH_TOKEN=...
    DROPBOX_ACCESS_TOKEN=...  # optional fallback
    DROPBOX_UPLOAD_ROOT=/Apps/AlphaFinder
    DROPBOX_COPY_DIR=C:\Users\you\Dropbox\AlphaFinder
    DROPBOX_CREATE_SHARED_LINK=true
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    DROPBOX_ACCESS_TOKEN,
    DROPBOX_APP_KEY,
    DROPBOX_APP_SECRET,
    DROPBOX_COPY_DIR,
    DROPBOX_CREATE_SHARED_LINK,
    DROPBOX_REDIRECT_URI,
    DROPBOX_REFRESH_TOKEN,
    DROPBOX_UPLOAD_ENABLED,
    DROPBOX_UPLOAD_ROOT,
)
from scripts.dropbox_oauth import refresh_dropbox_access_token  # noqa: E402

AI_READY_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready" / "latest"
MARKDOWN_EXPORT_DIR = AI_READY_LATEST_DIR / "ai_ready_bundle_md"
UPLOAD_LOG_DIR = PROJECT_ROOT / "repo_outputs" / "backtest" / "alerts"
DROPBOX_MANIFEST = UPLOAD_LOG_DIR / "dropbox_upload_latest.json"
CORE_UPLOAD_SHEETS = [
    "decision_signals_daily",
    "ranking_signals_daily",
    "ai_research_candidates",
    "event_signals_daily",
    "monster_radar_daily",
    "xq_short_term_updated",
    "raw_market_daily",
    "theme_heat_daily",
    "theme_leaders_daily",
    "ai_focus_list",
    "fusion_top_daily",
    "api_catalyst_analysis",
]
LEGACY_ROOT_FILES = [
    "ai_ready_bundle.xlsx",
    "Alpha-Sniper-Protocol-v8.md",
    "Alpha-Sniper-Protocol.md",
    "README_ai_quick_pack.json",
]


def _normalize_dropbox_root(raw_root: str) -> str:
    root = (raw_root or "/").strip()
    if not root:
        return "/"
    root = root.replace("\\", "/")
    if not root.startswith("/"):
        root = "/" + root
    parts = [part for part in root.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == "apps":
        parts = parts[2:]
    normalized = "/" + "/".join(parts)
    return normalized if normalized != "" else "/"


def _http_post(url: str, body: bytes, headers: dict) -> tuple[bool, str]:
    req = Request(url=url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return True, data
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return False, f"HTTP {exc.code}: {detail[:400]}"
    except URLError as exc:
        return False, f"URL error: {exc}"
    except (TimeoutError, OSError, ValueError) as exc:
        return False, str(exc)


def _safe_sheet_file_name(sheet_name: str) -> str:
    cleaned = str(sheet_name or "").strip().replace("/", "_").replace("\\", "_")
    return cleaned or "sheet"


def _render_sheet_markdown(sheet_name: str, df: pd.DataFrame) -> str:
    frame = df.copy()
    if len(frame) == 0:
        for col in frame.columns:
            frame[col] = frame[col].astype(str)
    frame = frame.fillna("")
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    csv_text = buffer.getvalue().strip("\n")
    lines = [
        f"# {sheet_name}",
        "",
        f"- rows: {len(frame)}",
        f"- columns: {len(frame.columns)}",
    ]
    if len(frame.columns) > 0:
        lines.append(f"- column_names: {', '.join(map(str, frame.columns))}")
    lines.extend([
        "",
        "```csv",
        csv_text,
        "```",
        "",
    ])
    return "\n".join(lines)


def _export_bundle_markdown_files(bundle_file: Path, export_dir: Path, sheet_names: List[str] | None = None) -> tuple[List[Path], List[str]]:
    export_dir.mkdir(parents=True, exist_ok=True)
    for old_file in export_dir.glob("*.md"):
        try:
            old_file.unlink()
        except OSError:
            pass

    workbook = pd.ExcelFile(bundle_file)
    available_sheet_names = list(workbook.sheet_names)
    selected_sheet_names = available_sheet_names
    if sheet_names:
        selected_sheet_names = [name for name in sheet_names if name in available_sheet_names]
    exported: List[Path] = []
    for sheet_name in selected_sheet_names:
        df = workbook.parse(sheet_name)
        file_name = f"{_safe_sheet_file_name(sheet_name)}.md"
        output_path = export_dir / file_name
        output_path.write_text(_render_sheet_markdown(sheet_name, df), encoding="utf-8")
        exported.append(output_path)
    return exported, available_sheet_names


def _dropbox_upload_file(local_file: Path, remote_path: str, access_token: str) -> tuple[bool, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/octet-stream",
        "Dropbox-API-Arg": json.dumps(
            {
                "path": remote_path,
                "mode": "overwrite",
                "autorename": False,
                "mute": True,
                "strict_conflict": False,
            }
        ),
    }
    return _http_post(
        "https://content.dropboxapi.com/2/files/upload",
        local_file.read_bytes(),
        headers,
    )


def _dropbox_create_shared_link(remote_path: str, access_token: str) -> tuple[bool, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "path": remote_path,
        "settings": {
            "requested_visibility": "public",
        },
    }
    ok, result = _http_post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        json.dumps(payload).encode("utf-8"),
        headers,
    )
    if ok:
        return True, result

    if "shared_link_already_exists" not in result:
        return False, result

    list_payload = {"path": remote_path, "direct_only": True}
    ok, list_result = _http_post(
        "https://api.dropboxapi.com/2/sharing/list_shared_links",
        json.dumps(list_payload).encode("utf-8"),
        headers,
    )
    return ok, list_result


def _dropbox_create_folder(remote_path: str, access_token: str) -> tuple[bool, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"path": remote_path, "autorename": False}
    ok, result = _http_post(
        "https://api.dropboxapi.com/2/files/create_folder_v2",
        json.dumps(payload).encode("utf-8"),
        headers,
    )
    if ok or "conflict" in result.lower():
        return True, result
    return False, result


def _dropbox_delete_path(remote_path: str, access_token: str) -> tuple[bool, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"path": remote_path}
    ok, result = _http_post(
        "https://api.dropboxapi.com/2/files/delete_v2",
        json.dumps(payload).encode("utf-8"),
        headers,
    )
    if ok or "not_found" in result.lower():
        return True, result
    return False, result


def _copy_to_local_dropbox(files: List[Path], target_dir: Path) -> List[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for file in files:
        dst = target_dir / file.name
        shutil.copy2(file, dst)
        copied.append(str(dst))
    return copied


def _remove_local_legacy_files(target_dir: Path) -> List[str]:
    deleted = []
    for file_name in LEGACY_ROOT_FILES:
        candidate = target_dir / file_name
        if not candidate.exists():
            continue
        try:
            candidate.unlink()
            deleted.append(str(candidate))
        except OSError:
            continue
    return deleted


def _resolve_dropbox_access_token() -> tuple[bool, str, str]:
    app_key = str(DROPBOX_APP_KEY or "").strip()
    app_secret = str(DROPBOX_APP_SECRET or "").strip()
    refresh_token = str(DROPBOX_REFRESH_TOKEN or "").strip()
    static_token = str(DROPBOX_ACCESS_TOKEN or "").strip()

    if app_key and app_secret and refresh_token:
        ok, result = refresh_dropbox_access_token(
            app_key=app_key,
            app_secret=app_secret,
            refresh_token=refresh_token,
            redirect_uri=DROPBOX_REDIRECT_URI,
        )
        if ok:
            payload = result if isinstance(result, dict) else {}
            return True, str(payload.get("access_token") or "").strip(), "refresh_token"
        if static_token:
            return True, static_token, f"static_access_token_fallback ({result})"
        return False, "", str(result)

    if static_token:
        return True, static_token, "static_access_token"

    return False, "", "missing Dropbox credentials: set DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN"


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload ai_ready bundle sheets as Markdown files to Dropbox")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without uploading")
    parser.add_argument("--create-shared-links", action="store_true", help="Create Dropbox shared links after upload")
    parser.add_argument("--dropbox-root", default="", help="Override remote Dropbox root path")
    parser.add_argument("--copy-dir", default="", help="Optional local folder to copy files into")
    parser.add_argument(
        "--remote-subdir",
        default="ai_ready_bundle_md",
        help="Remote subdirectory name for generated Markdown files",
    )
    parser.add_argument(
        "--sheet-mode",
        choices=["core", "all"],
        default="core",
        help="Upload only protocol-required core sheets or all workbook sheets",
    )
    args = parser.parse_args()

    bundle_file = AI_READY_LATEST_DIR / "ai_ready_bundle.xlsx"
    if not bundle_file.exists():
        print("No ai_ready bundle found to upload.")
        return 1

    selected_sheet_names = CORE_UPLOAD_SHEETS if args.sheet_mode == "core" else None
    upload_files, available_sheet_names = _export_bundle_markdown_files(bundle_file, MARKDOWN_EXPORT_DIR, selected_sheet_names)
    if not upload_files:
        print("No Markdown files were generated from ai_ready_bundle.xlsx.")
        return 1

    upload_file_names = {file.name for file in upload_files}
    stale_remote_markdown_names = [
        f"{_safe_sheet_file_name(sheet_name)}.md"
        for sheet_name in available_sheet_names
        if f"{_safe_sheet_file_name(sheet_name)}.md" not in upload_file_names
    ]

    remote_root = _normalize_dropbox_root(args.dropbox_root or DROPBOX_UPLOAD_ROOT or "/")
    copy_dir_raw = args.copy_dir or DROPBOX_COPY_DIR
    create_shared_links = bool(args.create_shared_links or DROPBOX_CREATE_SHARED_LINK)
    remote_subdir = str(args.remote_subdir or "ai_ready_bundle_md").strip().strip("/") or "ai_ready_bundle_md"
    remote_dir = f"{remote_root.rstrip('/')}/{remote_subdir}" if remote_root != "/" else f"/{remote_subdir}"

    print("=== Dropbox Upload Preview ===")
    for file in upload_files:
        print(f"- {file.name} -> {remote_dir}/{file.name}")

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "remote_root": remote_root,
        "remote_dir": remote_dir,
        "format": "bundle_markdown_sheets",
        "sheet_mode": args.sheet_mode,
        "selected_sheet_names": [file.stem for file in upload_files],
        "files": [],
        "local_copy": [],
        "local_deleted": [],
        "legacy_cleanup": [],
        "stale_markdown_cleanup": [],
    }

    if copy_dir_raw:
        copy_dir = Path(copy_dir_raw)
        local_target_dir = copy_dir / remote_subdir
        if args.dry_run:
            print(f"[DRY-RUN] local copy dir: {local_target_dir}")
        else:
            result["local_deleted"] = _remove_local_legacy_files(copy_dir)
            result["local_copy"] = _copy_to_local_dropbox(upload_files, local_target_dir)

    if args.dry_run:
        return 0

    token_ok, access_token, token_source = _resolve_dropbox_access_token()
    result["token_source"] = token_source

    if not DROPBOX_UPLOAD_ENABLED:
        print("DROPBOX_UPLOAD_ENABLED is false. Local copy may still have completed.")
    elif not token_ok:
        print(f"Dropbox token unavailable: {token_source}")
        UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
        DROPBOX_MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2
    else:
        folder_ok, folder_detail = _dropbox_create_folder(remote_dir, access_token)
        result["remote_dir_ok"] = folder_ok
        result["remote_dir_detail"] = folder_detail[:600]
        if not folder_ok:
            print(f"Failed to prepare remote dir: {remote_dir}")
            UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
            DROPBOX_MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return 3

        for legacy_name in LEGACY_ROOT_FILES:
            legacy_remote_path = f"{remote_root.rstrip('/')}/{legacy_name}" if remote_root != "/" else f"/{legacy_name}"
            cleanup_ok, cleanup_detail = _dropbox_delete_path(legacy_remote_path, access_token)
            result["legacy_cleanup"].append(
                {
                    "path": legacy_remote_path,
                    "delete_ok": cleanup_ok,
                    "detail": cleanup_detail[:300],
                }
            )

        for stale_name in stale_remote_markdown_names:
            stale_remote_path = f"{remote_dir.rstrip('/')}/{stale_name}"
            cleanup_ok, cleanup_detail = _dropbox_delete_path(stale_remote_path, access_token)
            result["stale_markdown_cleanup"].append(
                {
                    "path": stale_remote_path,
                    "delete_ok": cleanup_ok,
                    "detail": cleanup_detail[:300],
                }
            )

        for file in upload_files:
            remote_path = f"{remote_dir.rstrip('/')}/{file.name}"
            ok, detail = _dropbox_upload_file(file, remote_path, access_token)
            file_result = {
                "file": file.name,
                "remote_path": remote_path,
                "upload_ok": ok,
                "upload_detail": detail[:600],
            }
            if ok and create_shared_links:
                link_ok, link_detail = _dropbox_create_shared_link(remote_path, access_token)
                file_result["shared_link_ok"] = link_ok
                file_result["shared_link_detail"] = link_detail[:600]
            result["files"].append(file_result)
            print(f"[{file.name}] upload_ok={ok}")

            if result["files"] and not any(item.get("upload_ok") for item in result["files"]):
                UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
                DROPBOX_MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[DROPBOX_LOG] {DROPBOX_MANIFEST}")
                return 3

    UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    DROPBOX_MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DROPBOX_LOG] {DROPBOX_MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())