from __future__ import annotations

import os
import shutil
from pathlib import Path

import certifi

_CA_FILE_NAME = "cacert.pem"
_CA_ENV_KEYS = ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE")


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _candidate_cert_dirs() -> list[Path]:
    override = str(os.getenv("YFINANCE_CA_BUNDLE_DIR", "")).strip()
    if override:
        return [Path(override)]

    if os.name == "nt":
        return [Path(r"C:\alpha_runtime"), Path(r"C:\alpha_tmp")]

    return [Path("/tmp/alpha_runtime"), Path("/var/tmp/alpha_runtime")]


def _resolve_ascii_cert_target(src_cert: Path) -> Path:
    for cert_dir in _candidate_cert_dirs():
        target = cert_dir / _CA_FILE_NAME
        if not _is_ascii_path(target):
            continue
        try:
            cert_dir.mkdir(parents=True, exist_ok=True)
            if (not target.exists()) or src_cert.stat().st_mtime > target.stat().st_mtime:
                shutil.copy2(src_cert, target)
            return target
        except OSError:
            continue
    return src_cert


def ensure_ascii_cert_bundle() -> str:
    src_cert = Path(certifi.where())
    target = _resolve_ascii_cert_target(src_cert)
    resolved = str(target)

    for env_key in _CA_ENV_KEYS:
        os.environ[env_key] = resolved

    return resolved


def get_active_ca_bundle_path() -> str:
    return str(os.getenv("CURL_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or "")