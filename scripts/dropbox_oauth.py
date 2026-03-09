from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DROPBOX_OAUTH_TOKEN_URL = "https://api.dropbox.com/oauth2/token"


def _http_post_form(url: str, form_data: dict[str, str]) -> tuple[bool, str]:
    body = urlencode(form_data).encode("utf-8")
    req = Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return True, resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return False, f"HTTP {exc.code}: {detail[:400]}"
    except URLError as exc:
        return False, f"URL error: {exc}"
    except (TimeoutError, OSError, ValueError) as exc:
        return False, str(exc)


def refresh_dropbox_access_token(
    app_key: str,
    app_secret: str,
    refresh_token: str,
    redirect_uri: str = "",
) -> tuple[bool, dict[str, Any] | str]:
    del redirect_uri

    form_data = {
        "refresh_token": str(refresh_token or "").strip(),
        "grant_type": "refresh_token",
        "client_id": str(app_key or "").strip(),
        "client_secret": str(app_secret or "").strip(),
    }
    missing = [key for key, value in form_data.items() if key != "grant_type" and not value]
    if missing:
        return False, f"missing required fields: {', '.join(missing)}"

    ok, result = _http_post_form(DROPBOX_OAUTH_TOKEN_URL, form_data)
    if not ok:
        return False, result
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return False, f"invalid JSON response: {result[:400]}"

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return False, f"response missing access_token: {result[:400]}"
    return True, payload
