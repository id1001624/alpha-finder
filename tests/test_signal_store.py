import sqlite3
from datetime import datetime, timedelta, timezone

from signal_store import SignalEvent, cleanup_signal_store, get_latest_signals, init_signal_store, log_raw_webhook, upsert_signal_event


def test_upsert_same_key_keeps_latest(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signal_store(db_path)

    event1 = SignalEvent(
        schema_version=1,
        symbol="AAPL",
        timeframe="1D",
        ts="2026-02-26T14:30:00Z",
        vwap=100.0,
        sqz_on=True,
        sqzmom_value=0.5,
        sqzmom_color="green",
        event="update",
        signature="sig1",
        raw={"symbol": "AAPL"},
        received_at="2026-02-26T14:31:00+00:00",
    )
    event2 = SignalEvent(
        schema_version=1,
        symbol="AAPL",
        timeframe="1D",
        ts="2026-02-26T14:30:00Z",
        vwap=101.0,
        sqz_on=False,
        sqzmom_value=-0.1,
        sqzmom_color="red",
        event="update",
        signature="sig2",
        raw={"symbol": "AAPL", "rev": 2},
        received_at="2026-02-26T14:33:00+00:00",
    )

    upsert_signal_event(db_path, event1)
    upsert_signal_event(db_path, event2)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM signals WHERE symbol='AAPL'").fetchone()[0]
    conn.close()
    assert count == 1

    latest = get_latest_signals(
        db_path,
        asof=datetime(2026, 2, 26, 14, 40, tzinfo=timezone.utc),
        max_age_minutes=60,
        require_same_day=True,
    )
    assert "AAPL" in latest
    assert latest["AAPL"].vwap == 101.0
    assert latest["AAPL"].sqz_on is False


def test_get_latest_signals_filters_stale(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signal_store(db_path)

    now = datetime(2026, 2, 26, 15, 0, tzinfo=timezone.utc)
    stale_time = (now - timedelta(minutes=121)).isoformat()
    fresh_time = (now - timedelta(minutes=30)).isoformat()

    stale = SignalEvent(
        schema_version=1,
        symbol="CRCL",
        timeframe="1D",
        ts=now.isoformat(),
        event="update",
        raw={},
        received_at=stale_time,
    )
    fresh = SignalEvent(
        schema_version=1,
        symbol="NVDA",
        timeframe="1D",
        ts=now.isoformat(),
        event="update",
        raw={},
        received_at=fresh_time,
    )
    upsert_signal_event(db_path, stale)
    upsert_signal_event(db_path, fresh)

    latest = get_latest_signals(
        db_path,
        asof=now,
        max_age_minutes=120,
        require_same_day=True,
    )
    assert "NVDA" in latest
    assert "CRCL" not in latest


def test_cleanup_signal_store_prunes_old_rows(tmp_path):
    db_path = str(tmp_path / "signals.db")
    init_signal_store(db_path)

    now = datetime.now(timezone.utc)
    old_received = (now - timedelta(days=10)).isoformat()
    fresh_received = (now - timedelta(hours=2)).isoformat()

    stale = SignalEvent(
        schema_version=1,
        symbol="OLD",
        timeframe="1D",
        ts=old_received,
        event="update",
        raw={},
        received_at=old_received,
    )
    fresh = SignalEvent(
        schema_version=1,
        symbol="NEW",
        timeframe="1D",
        ts=fresh_received,
        event="update",
        raw={},
        received_at=fresh_received,
    )
    upsert_signal_event(db_path, stale)
    upsert_signal_event(db_path, fresh)
    log_raw_webhook(db_path, '{"symbol":"OLD"}', "application/json")
    log_raw_webhook(db_path, '{"symbol":"NEW"}', "application/json")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE signals SET received_at = ?, updated_at = ? WHERE symbol = 'OLD'",
            (old_received, old_received),
        )
        conn.execute(
            "UPDATE raw_webhook_logs SET received_at = ? WHERE id = (SELECT MIN(id) FROM raw_webhook_logs)",
            (old_received,),
        )
        conn.commit()
    finally:
        conn.close()

    cleanup_signal_store(db_path, signal_retention_days=4, raw_log_retention_days=4)

    conn = sqlite3.connect(db_path)
    try:
        signal_symbols = {row[0] for row in conn.execute("SELECT symbol FROM signals").fetchall()}
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_webhook_logs").fetchone()[0]
    finally:
        conn.close()

    assert signal_symbols == {"NEW"}
    assert raw_count == 1