import hashlib
import hmac
import json

from fastapi import FastAPI, Header, HTTPException, Request

from config import (
    ALLOW_PLAIN_TEXT_WEBHOOK,
    SIGNAL_STORE_PATH,
    TV_WEBHOOK_SECRET,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
)
from signal_store import build_signal_event, log_raw_webhook, upsert_signal_event


app = FastAPI(title="Alpha Finder TradingView Webhook")


def _verify_secret(raw_body: bytes, signature_header: str | None, token_header: str | None) -> bool:
    secret = TV_WEBHOOK_SECRET or ""
    if not secret:
        return False

    if token_header and hmac.compare_digest(token_header, secret):
        return True

    if signature_header:
        provided = signature_header.strip()
        if provided.lower().startswith("sha256="):
            provided = provided.split("=", 1)[1].strip()
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    return False


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/tv/webhook")
async def tradingview_webhook(
    request: Request,
    x_tv_signature: str | None = Header(default=None, alias="X-TV-Signature"),
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
):
    raw_body = await request.body()
    if not _verify_secret(raw_body, x_tv_signature, x_webhook_token):
        raise HTTPException(status_code=401, detail="invalid secret")

    content_type = (request.headers.get("content-type") or "").lower()
    body_text = raw_body.decode("utf-8", errors="replace")

    payload = None
    if "application/json" in content_type:
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
    else:
        if not ALLOW_PLAIN_TEXT_WEBHOOK:
            raise HTTPException(status_code=415, detail="plain text webhook not allowed")
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            log_raw_webhook(SIGNAL_STORE_PATH, body_text, content_type)
            return {"ok": True, "status": "raw_logged"}

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be object")
    if not payload.get("symbol") or not payload.get("ts"):
        raise HTTPException(status_code=400, detail="missing required fields: symbol, ts")

    event = build_signal_event(payload, signature=x_tv_signature)
    upsert_signal_event(SIGNAL_STORE_PATH, event)
    return {"ok": True, "status": "accepted", "symbol": event.symbol}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=WEBHOOK_HOST, port=WEBHOOK_PORT, reload=False)