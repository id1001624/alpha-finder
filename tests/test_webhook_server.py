import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import server
from signal_store import get_latest_signals, init_signal_store


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_webhook_rejects_invalid_secret(tmp_path, monkeypatch):
    db_path = str(tmp_path / "signals.db")
    init_signal_store(db_path)
    monkeypatch.setattr(server, "SIGNAL_STORE_PATH", db_path)
    monkeypatch.setattr(server, "TV_WEBHOOK_SECRET", "abc123")

    client = TestClient(server.app)
    payload = {"symbol": "AAPL", "ts": "2026-02-26T10:00:00Z", "timeframe": "1D", "event": "update"}
    response = client.post("/tv/webhook", json=payload, headers={"X-TV-Signature": "bad"})
    assert response.status_code == 401


def test_webhook_missing_required_field(tmp_path, monkeypatch):
    db_path = str(tmp_path / "signals.db")
    init_signal_store(db_path)
    monkeypatch.setattr(server, "SIGNAL_STORE_PATH", db_path)
    monkeypatch.setattr(server, "TV_WEBHOOK_SECRET", "abc123")

    client = TestClient(server.app)
    body = json.dumps({"symbol": "AAPL", "timeframe": "1D", "event": "update"}).encode("utf-8")
    sig = _sign("abc123", body)
    response = client.post(
        "/tv/webhook",
        data=body,
        headers={"Content-Type": "application/json", "X-TV-Signature": sig},
    )
    assert response.status_code == 400


def test_webhook_success_writes_signal(tmp_path, monkeypatch):
    db_path = str(tmp_path / "signals.db")
    init_signal_store(db_path)
    monkeypatch.setattr(server, "SIGNAL_STORE_PATH", db_path)
    monkeypatch.setattr(server, "TV_WEBHOOK_SECRET", "abc123")

    client = TestClient(server.app)
    payload = {
        "schema_version": 1,
        "source": "tradingview",
        "symbol": "AAPL",
        "exchange": "NASDAQ",
        "timeframe": "1D",
        "ts": "2026-02-26T10:00:00Z",
        "close": 188.2,
        "vwap": 187.9,
        "sqz_on": True,
        "sqzmom_value": 0.25,
        "sqzmom_color": "green",
        "event": "entry",
    }
    body = json.dumps(payload).encode("utf-8")
    sig = _sign("abc123", body)
    response = client.post(
        "/tv/webhook",
        data=body,
        headers={"Content-Type": "application/json", "X-TV-Signature": sig},
    )
    assert response.status_code == 200

    latest = get_latest_signals(
        db_path,
        asof=datetime(2026, 2, 26, 11, 0, tzinfo=timezone.utc),
        max_age_minutes=240,
        require_same_day=True,
    )
    assert "AAPL" in latest
    assert latest["AAPL"].sqzmom_color == "green"