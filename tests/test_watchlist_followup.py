from ai_trading import watchlist_brief as wb


def test_load_all_saved_watchlist_tickers_merges_users_by_recent_update(monkeypatch):
    monkeypatch.setattr(wb, "load_all_saved_watchlist_states", wb.pd.DataFrame)
    monkeypatch.setattr(
        wb,
        "_load_watchlist_store",
        lambda: {
            "users": {
                "1": {"tickers": ["AAOI", "NVDA"], "updated_at": "2026-03-11 09:00:00"},
                "2": {"tickers": ["LITE", "AAOI"], "updated_at": "2026-03-11 10:00:00"},
            }
        },
    )

    assert wb.load_all_saved_watchlist_tickers() == ["LITE", "AAOI", "NVDA"]


def test_load_saved_watchlist_prefers_shared_state(monkeypatch):
    monkeypatch.setattr(wb, "load_saved_watchlist_state", lambda user_id: ["AAOI", "NVDA"])

    assert wb.load_saved_watchlist("123") == ["AAOI", "NVDA"]


def test_load_saved_watchlist_fallback_syncs_local_to_shared(monkeypatch):
    synced = []
    monkeypatch.setattr(wb, "load_saved_watchlist_state", lambda user_id: None)
    monkeypatch.setattr(
        wb,
        "_load_watchlist_store",
        lambda: {"users": {"123": {"tickers": ["CRDU", "MULL"], "updated_at": "2026-03-12 00:30:00"}}},
    )
    monkeypatch.setattr(wb, "_sync_saved_watchlist_to_shared", lambda user_id, tickers, updated_at="": synced.append((user_id, tickers, updated_at)))

    assert wb.load_saved_watchlist("123") == ["CRDU", "MULL"]
    assert synced == [("123", ["CRDU", "MULL"], "2026-03-12 00:30:00")]


def test_fallback_saved_watchlist_followup_summary_marks_reentry_and_risk():
    payload = {
        "items": [
            {
                "ticker": "AAOI",
                "engine": {
                    "has_data": True,
                    "action": "entry",
                    "reason": "已回到 AVWAP 上方且動能轉強。",
                    "close": 20.5,
                    "dynamic_avwap": 20.0,
                    "sqzmom_hist": 0.6,
                },
                "tv_signal": {},
                "news": [{"title": "Catalyst"}],
                "decision": {"rank": 9999},
                "position": {"has_position": False},
            },
            {
                "ticker": "NVDA",
                "engine": {
                    "has_data": True,
                    "action": "stop_loss",
                    "reason": "短線結構還沒修回來，先別追。",
                    "close": 110.0,
                    "dynamic_avwap": 112.0,
                    "sqzmom_hist": -0.4,
                },
                "tv_signal": {},
                "news": [],
                "decision": {"rank": 9999},
                "position": {"has_position": False},
            },
        ]
    }

    followup_summary = getattr(wb, "_fallback_saved_watchlist_followup_summary")
    summary = followup_summary(payload)

    assert any("AAOI" in line and "再進場觀察" in line for line in summary["priority_order"])
    assert any("NVDA" in line and "先別追" in line for line in summary["risk_flags"])


def test_build_saved_watchlist_followup_message_uses_saved_names(monkeypatch):
    monkeypatch.setattr(wb, "_build_watch_payload", lambda tickers, saved_tickers, extra_tickers: {"generated_at": "2026-03-11 23:22:00", "items": []})
    monkeypatch.setattr(wb, "_gemini_saved_watchlist_followup_summary", lambda payload: {})
    monkeypatch.setattr(
        wb,
        "_fallback_saved_watchlist_followup_summary",
        lambda payload: {
            "headline": "Watchlist追蹤",
            "summary": "這份只追蹤你 watchadd 的票，不等於正式買點指令。",
            "priority_order": ["AAOI: 重回站上，可列再進場觀察。"],
            "risk_flags": [],
            "action_plan": ["AAOI: 保留觀察名單，不直接當正式新倉指令。"],
        },
    )

    message = wb.build_saved_watchlist_followup_message(["AAOI", "NVDA"])

    assert "追蹤名單: AAOI, NVDA" in message
    assert "AAOI: 重回站上，可列再進場觀察。" in message