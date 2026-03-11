from __future__ import annotations

from types import SimpleNamespace

import turso_state


class _FakeConnection:
    def __init__(self, *, fail_executemany: bool = False):
        self.fail_executemany = fail_executemany
        self.rollback_called = False
        self.commit_count = 0
        self.closed = False

    def execute(self, *_args, **_kwargs):
        return self

    def executemany(self, *_args, **_kwargs):
        if self.fail_executemany:
            raise RuntimeError("boom")
        return self

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_called = True

    def close(self):
        self.closed = True


def test_connect_retries_once_then_succeeds(monkeypatch):
    attempts = {"count": 0}
    conn = _FakeConnection()

    def _connect(**_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary")
        return conn

    monkeypatch.setattr(turso_state, "TURSO_ENABLED", True)
    monkeypatch.setattr(turso_state, "TURSO_DATABASE_URL", "libsql://example")
    monkeypatch.setattr(turso_state, "TURSO_AUTH_TOKEN", "token")
    monkeypatch.setattr(turso_state, "TURSO_CONNECT_RETRY_COUNT", 1)
    monkeypatch.setattr(turso_state.importlib, "import_module", lambda _name: SimpleNamespace(connect=_connect))

    connected = turso_state._connect()

    assert connected is conn
    assert attempts["count"] == 2


def test_append_execution_log_rows_rolls_back_on_failure(monkeypatch):
    conn = _FakeConnection(fail_executemany=True)

    monkeypatch.setattr(turso_state, "_connect", lambda: conn)

    result = turso_state.append_execution_log_rows(
        [
            {
                "recorded_at": "2026-03-11 09:35:00",
                "execution_date": "2026-03-11",
                "execution_time": "09:35:00",
                "decision_date": "2026-03-11",
                "ticker": "AAPL",
                "rank": 1,
                "action": "entry",
            }
        ]
    )

    assert result is None
    assert conn.rollback_called is True
    assert conn.closed is True