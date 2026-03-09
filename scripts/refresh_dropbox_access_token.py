from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    DROPBOX_APP_KEY,
    DROPBOX_APP_SECRET,
    DROPBOX_REDIRECT_URI,
    DROPBOX_REFRESH_TOKEN,
)
from scripts.dropbox_oauth import refresh_dropbox_access_token  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Dropbox access token with refresh token")
    parser.add_argument("--app-key", default=DROPBOX_APP_KEY, help="Dropbox app key")
    parser.add_argument("--app-secret", default=DROPBOX_APP_SECRET, help="Dropbox app secret")
    parser.add_argument("--refresh-token", default=DROPBOX_REFRESH_TOKEN, help="Dropbox refresh token")
    parser.add_argument("--redirect-uri", default=DROPBOX_REDIRECT_URI, help="Stored redirect URI for reference")
    parser.add_argument("--access-token-only", action="store_true", help="Print only access_token")
    args = parser.parse_args()

    ok, result = refresh_dropbox_access_token(
        app_key=args.app_key,
        app_secret=args.app_secret,
        refresh_token=args.refresh_token,
        redirect_uri=args.redirect_uri,
    )
    if not ok:
        print(f"Dropbox token refresh failed: {result}")
        return 1

    payload = result if isinstance(result, dict) else {}
    if args.access_token_only:
        print(str(payload.get("access_token") or ""))
        return 0

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())