import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import push_alerts_from_ai_decision as recap


def _sample_materialized_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "as_of_date": "2026-03-17",
                "ticker": "CXW",
                "final_priority": 1,
                "decision_status": "keep",
                "decision_reason": "entry=ignition_ready",
                "execution_window": "next_open_session",
                "avoid_chase_flag": False,
                "preferred_entry_type": "tactical_entry",
                "invalidation_rule": "跌破 VWAP 或收盤弱於開盤，次日不延續即撤退",
            },
            {
                "as_of_date": "2026-03-17",
                "ticker": "JTAI",
                "final_priority": 2,
                "decision_status": "keep",
                "decision_reason": "entry=pullback_watch",
                "execution_window": "next_open_session",
                "avoid_chase_flag": False,
                "preferred_entry_type": "pullback_entry",
                "invalidation_rule": "跌破 VWAP 或收盤弱於開盤，次日不延續即撤退",
            },
            {
                "as_of_date": "2026-03-17",
                "ticker": "ABSI",
                "final_priority": 3,
                "decision_status": "watch",
                "execution_window": "next_open_session",
                "avoid_chase_flag": False,
                "preferred_entry_type": "pullback_entry",
                "invalidation_rule": "回踩後量縮且守不住 VWAP，視為失效",
            },
            {
                "as_of_date": "2026-03-17",
                "ticker": "BIAF",
                "final_priority": 4,
                "decision_status": "watch",
                "execution_window": "next_open_session",
                "avoid_chase_flag": False,
                "preferred_entry_type": "unknown",
                "invalidation_rule": "回踩後量縮且守不住 VWAP，視為失效",
            },
            {
                "as_of_date": "2026-03-17",
                "ticker": "LIDR",
                "final_priority": 5,
                "decision_status": "watch",
                "execution_window": "next_open_session",
                "avoid_chase_flag": False,
                "preferred_entry_type": "unknown",
                "invalidation_rule": "開高失守盤前高且跌破 VWAP，視為隔夜動能失效",
            },
        ]
    )


def test_trade_command_message_only_outputs_user_visible_actions():
    to_recap_df = getattr(recap, "_materialized_to_recap_df")
    render_message = getattr(recap, "_render_message")
    df = to_recap_df(_sample_materialized_df())
    message = render_message(
        df=df,
        tv_map={},
        top_n=5,
        tags={"keep", "watch"},
        title_date="2026-03-17",
        mode="morning",
        recap_context={},
    )

    assert "CXW -> 分批買入進場" in message
    assert "JTAI -> 回踩分批買入" in message
    assert "ABSI" not in message
    assert "BIAF" not in message
    assert "LIDR" not in message
    assert "跌破 VWAP 或收盤弱於開盤，次日不延續即撤退 -> SELL_ALL_EXIT" in message


def test_pollution_terms_block_candidate_style_language():
    polluted = "Top 1 候選 watch 次選 再觀察"
    found = recap.find_pollution_terms(polluted)
    assert set(found) >= {"Top 1", "候選", "watch", "次選", "再觀察"}


def test_load_decision_df_requires_materialized_schema(tmp_path):
    fake_csv = tmp_path / "ai_decision_latest.csv"
    pd.DataFrame(
        [
            {
                "decision_date": "2026-03-17",
                "ticker": "AAPL",
                "rank": 1,
                "decision_tag": "keep",
            }
        ]
    ).to_csv(fake_csv, index=False, encoding="utf-8-sig")

    load_decision_df = getattr(recap, "_load_decision_df")
    loaded = load_decision_df(fake_csv)
    assert len(loaded) == 0
