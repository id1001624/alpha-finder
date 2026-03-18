from __future__ import annotations

from typing import List, Sequence

import pandas as pd

ACTION_BUY_SCALE_IN = "BUY_SCALE_IN"
ACTION_BUY_AGGRESSIVE = "BUY_AGGRESSIVE"
ACTION_SELL_SCALE_OUT = "SELL_SCALE_OUT"
ACTION_SELL_ALL_EXIT = "SELL_ALL_EXIT"
ACTION_NO_TRADE = "NO_TRADE"
ACTION_BOT_ONLY = "BOT_ONLY"

USER_VISIBLE = "user_visible"
USER_BOT_ONLY = "bot_only"

ALLOWED_USER_ACTIONS = {
    ACTION_BUY_SCALE_IN,
    ACTION_BUY_AGGRESSIVE,
    ACTION_SELL_SCALE_OUT,
    ACTION_SELL_ALL_EXIT,
    ACTION_NO_TRADE,
}

DEFAULT_BANNED_WORDS = ["Top 1", "次選", "watch", "候選", "再觀察"]


def _clean_text(value: object, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in {"", "nan", "none", "null", "na", "n/a"}:
        return default
    return text


def _to_bool(value: object) -> bool:
    text = _clean_text(value, "").lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return False
    return bool(int(parsed))


def _default_execution_action(decision_status: str, avoid_chase: bool, execution_window: str, preferred_entry_type: str) -> str:
    status = _clean_text(decision_status, "watch").lower()
    if status == "watch":
        return ACTION_BOT_ONLY
    if status != "keep":
        return ACTION_NO_TRADE
    if avoid_chase:
        return ACTION_NO_TRADE
    if _clean_text(execution_window, "").lower() != "next_open_session":
        return ACTION_NO_TRADE

    preferred = _clean_text(preferred_entry_type, "").lower()
    if preferred in {"tactical_entry", "pullback_entry", "breakout_or_reclaim", "unknown", ""}:
        return ACTION_BUY_SCALE_IN
    return ACTION_BUY_SCALE_IN


def _default_position_plan(execution_action: str, preferred_entry_type: str) -> str:
    action = _clean_text(execution_action, ACTION_NO_TRADE).upper()
    preferred = _clean_text(preferred_entry_type, "").lower()
    if action == ACTION_BUY_AGGRESSIVE:
        return "良好訊號大量買入"
    if action == ACTION_BUY_SCALE_IN:
        if preferred == "pullback_entry":
            return "回踩分批買入"
        return "分批買入進場"
    if action == ACTION_SELL_SCALE_OUT:
        return "分批賣出"
    if action == ACTION_SELL_ALL_EXIT:
        return "警告全賣退場"
    if action == ACTION_BOT_ONLY:
        return ""
    return "今日空手"


def _default_exit_action(invalidation_rule: str) -> str:
    if _clean_text(invalidation_rule, ""):
        return ACTION_SELL_ALL_EXIT
    return ACTION_SELL_SCALE_OUT


def _normalize_visibility(decision_status: str, execution_action: str, visibility: str) -> str:
    current = _clean_text(visibility, "").lower()
    if current in {USER_VISIBLE, USER_BOT_ONLY}:
        return current

    status = _clean_text(decision_status, "watch").lower()
    action = _clean_text(execution_action, ACTION_NO_TRADE).upper()
    if status == "watch" or action == ACTION_BOT_ONLY:
        return USER_BOT_ONLY
    return USER_VISIBLE


def enrich_trade_command_fields(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        cols = [
            "as_of_date",
            "ticker",
            "final_priority",
            "decision_status",
            "avoid_chase_flag",
            "execution_window",
            "preferred_entry_type",
            "invalidation_rule",
            "execution_action",
            "position_plan",
            "exit_action",
            "user_visibility",
        ]
        return pd.DataFrame(columns=cols)

    out = df.copy()
    defaults = {
        "as_of_date": "",
        "ticker": "",
        "final_priority": 9999,
        "decision_status": "watch",
        "avoid_chase_flag": False,
        "execution_window": "next_open_session",
        "preferred_entry_type": "unknown",
        "invalidation_rule": "",
        "execution_action": "",
        "position_plan": "",
        "exit_action": "",
        "user_visibility": "",
    }
    for col, default in defaults.items():
        if col not in out.columns:
            out[col] = default

    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["final_priority"] = pd.to_numeric(out.get("final_priority"), errors="coerce").fillna(9999).astype(int)
    out["decision_status"] = out["decision_status"].astype(str).str.strip().str.lower().replace("", "watch")
    out["avoid_chase_flag"] = out["avoid_chase_flag"].apply(_to_bool)
    out["execution_window"] = out["execution_window"].astype(str).str.strip().str.lower()
    out["preferred_entry_type"] = out["preferred_entry_type"].astype(str).str.strip().str.lower()
    out["invalidation_rule"] = out["invalidation_rule"].astype(str).str.strip()

    normalized_actions: List[str] = []
    normalized_position_plans: List[str] = []
    normalized_exit_actions: List[str] = []
    normalized_visibility: List[str] = []

    for _, row in out.iterrows():
        existing_action = _clean_text(row.get("execution_action"), "").upper()
        if existing_action not in ALLOWED_USER_ACTIONS | {ACTION_BOT_ONLY}:
            existing_action = ""

        mapped_action = existing_action or _default_execution_action(
            decision_status=str(row.get("decision_status", "watch")),
            avoid_chase=bool(row.get("avoid_chase_flag", False)),
            execution_window=str(row.get("execution_window", "")),
            preferred_entry_type=str(row.get("preferred_entry_type", "")),
        )

        mapped_plan = _clean_text(row.get("position_plan"), "") or _default_position_plan(
            execution_action=mapped_action,
            preferred_entry_type=str(row.get("preferred_entry_type", "")),
        )
        mapped_exit = _clean_text(row.get("exit_action"), "")
        if mapped_exit not in ALLOWED_USER_ACTIONS:
            mapped_exit = _default_exit_action(str(row.get("invalidation_rule", "")))
        mapped_visibility = _normalize_visibility(
            decision_status=str(row.get("decision_status", "watch")),
            execution_action=mapped_action,
            visibility=str(row.get("user_visibility", "")),
        )

        if mapped_visibility == USER_BOT_ONLY:
            mapped_action = ACTION_BOT_ONLY

        normalized_actions.append(mapped_action)
        normalized_position_plans.append(mapped_plan)
        normalized_exit_actions.append(mapped_exit)
        normalized_visibility.append(mapped_visibility)

    out["execution_action"] = normalized_actions
    out["position_plan"] = normalized_position_plans
    out["exit_action"] = normalized_exit_actions
    out["user_visibility"] = normalized_visibility

    return out


def build_user_visible_command_df(df: pd.DataFrame) -> pd.DataFrame:
    normalized = enrich_trade_command_fields(df)
    visible = normalized[
        (normalized["user_visibility"].astype(str).str.lower() != USER_BOT_ONLY)
        & (normalized["execution_action"].astype(str).str.upper().isin(ALLOWED_USER_ACTIONS))
    ].copy()

    visible = visible.sort_values(["final_priority", "ticker"], ascending=[True, True]).reset_index(drop=True)
    if len(visible) > 0:
        return visible

    return pd.DataFrame(
        [
            {
                "as_of_date": "",
                "ticker": "NO_TRADE",
                "final_priority": 1,
                "decision_status": "watch",
                "avoid_chase_flag": True,
                "execution_window": "next_open_session",
                "preferred_entry_type": "unknown",
                "invalidation_rule": "",
                "execution_action": ACTION_NO_TRADE,
                "position_plan": "今日空手",
                "exit_action": ACTION_SELL_ALL_EXIT,
                "user_visibility": USER_VISIBLE,
            }
        ]
    )


def find_pollution_terms(text: str, banned_words: Sequence[str] | None = None) -> List[str]:
    source = str(text or "")
    words = list(banned_words or DEFAULT_BANNED_WORDS)
    found: List[str] = []
    lowered = source.lower()
    for word in words:
        token = str(word or "").strip()
        if not token:
            continue
        token_lower = token.lower()
        if token_lower in lowered:
            found.append(token)
    return found
