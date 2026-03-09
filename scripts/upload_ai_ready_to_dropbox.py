r"""
Upload the unified B input pack to Dropbox and optionally mirror it to a local folder.

Examples:
  python scripts/upload_ai_ready_to_dropbox.py --dry-run
  python scripts/upload_ai_ready_to_dropbox.py --create-shared-links

Env/config:
  DROPBOX_UPLOAD_ENABLED=true
  DROPBOX_ACCESS_TOKEN=...
  DROPBOX_UPLOAD_ROOT=/Apps/AlphaFinder
  DROPBOX_COPY_DIR=C:\Users\you\Dropbox\AlphaFinder
  DROPBOX_CREATE_SHARED_LINK=true
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    DROPBOX_ACCESS_TOKEN,
    DROPBOX_COPY_DIR,
    DROPBOX_CREATE_SHARED_LINK,
    DROPBOX_UPLOAD_ENABLED,
    DROPBOX_UPLOAD_ROOT,
)

AI_READY_LATEST_DIR = PROJECT_ROOT / "repo_outputs" / "ai_ready" / "latest"
PROTOCOL_FILE = PROJECT_ROOT / "Alpha-Sniper-Protocol-v8.md"
UPLOAD_LOG_DIR = PROJECT_ROOT / "repo_outputs" / "backtest" / "alerts"
DROPBOX_MANIFEST = UPLOAD_LOG_DIR / "dropbox_upload_latest.json"


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


def _copy_to_local_dropbox(files: List[Path], target_dir: Path) -> List[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for file in files:
        dst = target_dir / file.name
        shutil.copy2(file, dst)
        copied.append(str(dst))
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload ai_ready bundle and protocol to Dropbox")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without uploading")
    parser.add_argument("--create-shared-links", action="store_true", help="Create Dropbox shared links after upload")
    parser.add_argument("--dropbox-root", default="", help="Override remote Dropbox root path")
    parser.add_argument("--copy-dir", default="", help="Optional local folder to copy files into")
    args = parser.parse_args()

    bundle_file = AI_READY_LATEST_DIR / "ai_ready_bundle.xlsx"
    manifest_file = AI_READY_LATEST_DIR / "README_ai_quick_pack.json"
    upload_files = [file for file in [bundle_file, PROTOCOL_FILE, manifest_file] if file.exists()]
    if not upload_files:
        print("No ai_ready bundle/protocol files found to upload.")
        return 1

    remote_root = _normalize_dropbox_root(args.dropbox_root or DROPBOX_UPLOAD_ROOT or "/")
    copy_dir_raw = args.copy_dir or DROPBOX_COPY_DIR
    create_shared_links = bool(args.create_shared_links or DROPBOX_CREATE_SHARED_LINK)

    print("=== Dropbox Upload Preview ===")
    for file in upload_files:
        print(f"- {file.name} -> {remote_root}/{file.name}")

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "remote_root": remote_root,
        "files": [],
        "local_copy": [],
    }

    if copy_dir_raw:
        copy_dir = Path(copy_dir_raw)
        if args.dry_run:
            print(f"[DRY-RUN] local copy dir: {copy_dir}")
        else:
            result["local_copy"] = _copy_to_local_dropbox(upload_files, copy_dir)

    if args.dry_run:
        return 0

    if not DROPBOX_UPLOAD_ENABLED:
        print("DROPBOX_UPLOAD_ENABLED is false. Local copy may still have completed.")
    elif not DROPBOX_ACCESS_TOKEN:
        print("DROPBOX_ACCESS_TOKEN is missing.")
        return 2
    else:
        for file in upload_files:
            remote_path = f"{remote_root.rstrip('/')}/{file.name}" if remote_root != "/" else f"/{file.name}"
            ok, detail = _dropbox_upload_file(file, remote_path, DROPBOX_ACCESS_TOKEN)
            file_result = {
                "file": file.name,
                "remote_path": remote_path,
                "upload_ok": ok,
                "upload_detail": detail[:600],
            }
            if ok and create_shared_links:
                link_ok, link_detail = _dropbox_create_shared_link(remote_path, DROPBOX_ACCESS_TOKEN)
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