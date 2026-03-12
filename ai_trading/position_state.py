from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
from ai_trading.strategy_context import (
    HORIZON_INTRADAY_MONSTER,
    REGIME_NEUTRAL,
    STRATEGY_MONSTER_SWING,
    normalize_horizon_tag,
    normalize_regime_tag,
    normalize_strategy_profile,
)

from turso_state import (
    STATE_KEY_POSITIONS_LATEST,
    append_trade_ledger_row as append_trade_ledger_row_to_turso,
    load_runtime_df_with_fallback,
    sync_positions_latest as sync_positions_latest_to_turso,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "repo_outputs" / "backtest"
POSITIONS_FILE = BACKTEST_DIR / "positions_latest.csv"
TRADE_LEDGER_FILE = BACKTEST_DIR / "position_trade_log.csv"

POSITION_FIELDS = [
    "ticker",
    "horizon_tag",
    "strategy_profile",
    "regime_tag",
    "theme",
    "quantity",
    "avg_cost",
    "opened_at",
    "updated_at",
    "last_trade_price",
    "add_count",
    "realized_pnl",
    "last_signal_type",
    "entry_reason",
    "status",
]

TRADE_LEDGER_FIELDS = [
    "recorded_at",
    "ticker",
    "side",
    "quantity",
    "price",
    "position_effect",
    "before_qty",
    "after_qty",
    "avg_cost_after",
    "realized_pnl_delta",
    "horizon_tag",
    "strategy_profile",
    "signal_type",
    "regime_tag",
    "theme",
    "entry_reason",
    "exit_reason",
    "position_size_fraction",
    "entry_price",
    "exit_price",
    "holding_minutes",
    "holding_days",
    "mfe",
    "mae",
    "realized_R",
    "realized_pct",
    "slippage_bps",
    "source_decision_rank",
    "source_confidence",
    "source_api_final_score",
    "snapshot_json",
    "source",
    "note",
]


def _safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)


def load_positions(path: Path | None = None) -> pd.DataFrame:
    if path is None:
        df, _ = load_runtime_df_with_fallback(STATE_KEY_POSITIONS_LATEST, [POSITIONS_FILE])
    else:
        df = _safe_read_csv(path)
    if len(df) == 0:
        return pd.DataFrame(columns=POSITION_FIELDS)
    out = df.copy()
    for col in POSITION_FIELDS:
        if col not in out.columns:
            out[col] = ""
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out["horizon_tag"] = out["horizon_tag"].apply(lambda value: normalize_horizon_tag(value, HORIZON_INTRADAY_MONSTER))
    out["strategy_profile"] = out["strategy_profile"].apply(lambda value: normalize_strategy_profile(value, STRATEGY_MONSTER_SWING))
    out["regime_tag"] = out["regime_tag"].apply(lambda value: normalize_regime_tag(value, REGIME_NEUTRAL))
    out["theme"] = out["theme"].astype(str).str.strip()
    out["last_signal_type"] = out["last_signal_type"].astype(str).str.strip().str.lower()
    out["entry_reason"] = out["entry_reason"].astype(str).str.strip()
    out["quantity"] = pd.to_numeric(out["quantity"], errors="coerce").fillna(0.0)
    out["avg_cost"] = pd.to_numeric(out["avg_cost"], errors="coerce").fillna(0.0)
    out["last_trade_price"] = pd.to_numeric(out["last_trade_price"], errors="coerce").fillna(0.0)
    out["add_count"] = pd.to_numeric(out["add_count"], errors="coerce").fillna(0).astype(int)
    out["realized_pnl"] = pd.to_numeric(out["realized_pnl"], errors="coerce").fillna(0.0)
    out = out[out["ticker"] != ""].copy()
    out = out.drop_duplicates(subset=["ticker", "horizon_tag", "strategy_profile"], keep="last")
    return out[POSITION_FIELDS].sort_values(["ticker", "horizon_tag", "strategy_profile"]).reset_index(drop=True)


def save_positions(df: pd.DataFrame, path: Path = POSITIONS_FILE) -> Path:
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    out = df.copy() if df is not None else pd.DataFrame(columns=POSITION_FIELDS)
    if len(out) == 0:
        out = pd.DataFrame(columns=POSITION_FIELDS)
    for col in POSITION_FIELDS:
        if col not in out.columns:
            out[col] = ""
    out = out[POSITION_FIELDS].copy()
    out = out[pd.to_numeric(out["quantity"], errors="coerce").fillna(0.0) > 0].copy()
    out.to_csv(path, index=False, encoding="utf-8-sig")
    if Path(path) == POSITIONS_FILE:
        sync_positions_latest_to_turso(POSITIONS_FILE)
    return path


def append_trade_ledger(row: dict, path: Path = TRADE_LEDGER_FILE) -> Path:
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    out = pd.DataFrame([row], columns=TRADE_LEDGER_FIELDS)
    out.to_csv(path, mode="a", header=not exists, index=False, encoding="utf-8-sig")
    if Path(path) == TRADE_LEDGER_FILE:
        append_trade_ledger_row_to_turso(row)
    return path


def get_position(df: pd.DataFrame, ticker: str) -> Optional[pd.Series]:
    return get_position_by_profile(df, ticker)


def get_position_by_profile(
    df: pd.DataFrame,
    ticker: str,
    horizon_tag: str = "",
    strategy_profile: str = "",
) -> Optional[pd.Series]:
    if df is None or len(df) == 0:
        return None
    matches = df[df["ticker"] == str(ticker).strip().upper()]
    if str(horizon_tag).strip():
        matches = matches[matches["horizon_tag"] == normalize_horizon_tag(horizon_tag, HORIZON_INTRADAY_MONSTER)]
    if str(strategy_profile).strip():
        matches = matches[matches["strategy_profile"] == normalize_strategy_profile(strategy_profile, STRATEGY_MONSTER_SWING)]
    if len(matches) == 0:
        return None
    if len(matches) > 1 and not str(strategy_profile).strip():
        ordered = matches.sort_values(["strategy_profile", "updated_at"], ascending=[True, False], na_position="last")
        return ordered.iloc[0]
    return matches.iloc[0]


def apply_trade_fill(
    positions_df: pd.DataFrame,
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    horizon_tag: str = HORIZON_INTRADAY_MONSTER,
    strategy_profile: str = STRATEGY_MONSTER_SWING,
    regime_tag: str = REGIME_NEUTRAL,
    signal_type: str = "",
    theme: str = "",
    entry_reason: str = "",
    exit_reason: str = "",
    position_size_fraction: float = 0.0,
    source_decision_rank: int = 0,
    source_confidence: float = 0.0,
    source_api_final_score: float = 0.0,
    snapshot_json: str = "",
    slippage_bps: float = 0.0,
    source: str = "discord_bot",
    note: str = "",
    recorded_at: str | None = None,
) -> Tuple[pd.DataFrame, dict]:
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    if price <= 0:
        raise ValueError("price must be > 0")

    ts = recorded_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ticker_norm = str(ticker).strip().upper()
    horizon_norm = normalize_horizon_tag(horizon_tag, HORIZON_INTRADAY_MONSTER)
    strategy_norm = normalize_strategy_profile(strategy_profile, STRATEGY_MONSTER_SWING)
    regime_norm = normalize_regime_tag(regime_tag, REGIME_NEUTRAL)
    side_norm = str(side).strip().lower()
    if side_norm not in {"buy", "add", "sell"}:
        raise ValueError("side must be buy, add, or sell")

    out = load_positions() if positions_df is None else positions_df.copy()
    if len(out) == 0:
        out = pd.DataFrame(columns=POSITION_FIELDS)
    for col in POSITION_FIELDS:
        if col not in out.columns:
            out[col] = ""

    existing = get_position_by_profile(out, ticker_norm, horizon_tag=horizon_norm, strategy_profile=strategy_norm)
    before_qty = float(existing["quantity"]) if existing is not None else 0.0
    avg_cost = float(existing["avg_cost"]) if existing is not None else 0.0
    add_count = int(existing["add_count"]) if existing is not None else 0
    realized_pnl = float(existing["realized_pnl"]) if existing is not None else 0.0
    opened_at = str(existing["opened_at"]) if existing is not None and str(existing["opened_at"]).strip() else ts

    if side_norm in {"buy", "add"}:
        after_qty = before_qty + float(quantity)
        new_avg_cost = ((before_qty * avg_cost) + (float(quantity) * float(price))) / after_qty
        position_effect = "open" if before_qty == 0 else "increase"
        realized_delta = 0.0
        updated_row = {
            "ticker": ticker_norm,
            "horizon_tag": horizon_norm,
            "strategy_profile": strategy_norm,
            "regime_tag": regime_norm,
            "theme": str(theme or "").strip(),
            "quantity": round(after_qty, 8),
            "avg_cost": round(new_avg_cost, 6),
            "opened_at": opened_at,
            "updated_at": ts,
            "last_trade_price": float(price),
            "add_count": add_count + (1 if before_qty > 0 else 0),
            "realized_pnl": realized_pnl,
            "last_signal_type": str(signal_type or "").strip().lower(),
            "entry_reason": str(entry_reason or "").strip(),
            "status": "open",
        }
    else:
        if before_qty <= 0:
            raise ValueError(f"no open position for {ticker_norm}")
        if float(quantity) > before_qty + 1e-9:
            raise ValueError(f"sell quantity exceeds open position for {ticker_norm}")
        after_qty = before_qty - float(quantity)
        realized_delta = (float(price) - avg_cost) * float(quantity)
        realized_total = realized_pnl + realized_delta
        position_effect = "close" if after_qty <= 1e-9 else "reduce"
        updated_row = {
            "ticker": ticker_norm,
            "horizon_tag": horizon_norm,
            "strategy_profile": strategy_norm,
            "regime_tag": regime_norm,
            "theme": str(theme or (existing.get("theme", "") if existing is not None else "")).strip(),
            "quantity": round(max(after_qty, 0.0), 8),
            "avg_cost": round(avg_cost, 6) if after_qty > 1e-9 else 0.0,
            "opened_at": opened_at,
            "updated_at": ts,
            "last_trade_price": float(price),
            "add_count": add_count,
            "realized_pnl": round(realized_total, 6),
            "last_signal_type": str(signal_type or "").strip().lower(),
            "entry_reason": str(existing.get("entry_reason", "") if existing is not None else entry_reason).strip(),
            "status": "open" if after_qty > 1e-9 else "closed",
        }

    remove_mask = (
        (out["ticker"] == ticker_norm)
        & (out["horizon_tag"] == horizon_norm)
        & (out["strategy_profile"] == strategy_norm)
    )
    out = out[~remove_mask].copy()
    if updated_row["quantity"] > 0:
        out = pd.concat([out, pd.DataFrame([updated_row])], ignore_index=True)
    out = out[POSITION_FIELDS].sort_values(["ticker", "horizon_tag", "strategy_profile"]).reset_index(drop=True)

    holding_minutes = 0.0
    holding_days = 0.0
    if opened_at:
        opened_ts = pd.to_datetime(opened_at, errors="coerce")
        current_ts = pd.to_datetime(ts, errors="coerce")
        if pd.notna(opened_ts) and pd.notna(current_ts):
            delta_minutes = (current_ts - opened_ts).total_seconds() / 60.0
            holding_minutes = max(0.0, float(delta_minutes))
            holding_days = holding_minutes / (60.0 * 24.0)

    realized_pct = 0.0
    if side_norm == "sell" and avg_cost > 0:
        realized_pct = ((float(price) / avg_cost) - 1.0) * 100.0

    ledger_row = {
        "recorded_at": ts,
        "ticker": ticker_norm,
        "side": side_norm,
        "quantity": float(quantity),
        "price": float(price),
        "position_effect": position_effect,
        "before_qty": before_qty,
        "after_qty": round(updated_row["quantity"], 8),
        "avg_cost_after": round(updated_row["avg_cost"], 6),
        "realized_pnl_delta": round(realized_delta, 6),
        "horizon_tag": horizon_norm,
        "strategy_profile": strategy_norm,
        "signal_type": str(signal_type or "").strip().lower(),
        "regime_tag": regime_norm,
        "theme": str(theme or "").strip(),
        "entry_reason": str(entry_reason or "").strip(),
        "exit_reason": str(exit_reason or "").strip(),
        "position_size_fraction": float(pd.to_numeric(position_size_fraction, errors="coerce") or 0.0),
        "entry_price": float(price) if side_norm in {"buy", "add"} else float(avg_cost or 0.0),
        "exit_price": float(price) if side_norm == "sell" else 0.0,
        "holding_minutes": round(holding_minutes, 2),
        "holding_days": round(holding_days, 4),
        "mfe": float("nan"),
        "mae": float("nan"),
        "realized_R": float("nan"),
        "realized_pct": round(realized_pct, 4),
        "slippage_bps": float(pd.to_numeric(slippage_bps, errors="coerce") or 0.0),
        "source_decision_rank": int(pd.to_numeric(source_decision_rank, errors="coerce") or 0),
        "source_confidence": float(pd.to_numeric(source_confidence, errors="coerce") or 0.0),
        "source_api_final_score": float(pd.to_numeric(source_api_final_score, errors="coerce") or 0.0),
        "snapshot_json": str(snapshot_json or "").strip(),
        "source": source,
        "note": str(note or "").strip(),
    }
    return out, ledger_row