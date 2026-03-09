from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


def _require_columns(df: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")


def calc_sqzmom_lb(
    df: pd.DataFrame,
    bb_length: int = 20,
    bb_mult: float = 2.0,
    kc_length: int = 20,
    kc_mult: float = 1.5,
) -> pd.DataFrame:
    _require_columns(df, ["High", "Low", "Close"])
    out = df.copy()

    tr0 = (out["High"] - out["Low"]).abs()
    tr1 = (out["High"] - out["Close"].shift(1)).abs()
    tr2 = (out["Low"] - out["Close"].shift(1)).abs()
    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)

    kc_basis = out["Close"].rolling(window=kc_length, min_periods=kc_length).mean()
    atr = tr.rolling(window=kc_length, min_periods=kc_length).mean()
    lower_kc = kc_basis - kc_mult * atr
    upper_kc = kc_basis + kc_mult * atr

    bb_basis = out["Close"].rolling(window=bb_length, min_periods=bb_length).mean()
    std = out["Close"].rolling(window=bb_length, min_periods=bb_length).std(ddof=0)
    lower_bb = bb_basis - bb_mult * std
    upper_bb = bb_basis + bb_mult * std

    sqz_on = (lower_bb > lower_kc) & (upper_bb < upper_kc)
    out["sqz_on"] = sqz_on.fillna(False)
    out["sqz_release"] = (~out["sqz_on"]) & out["sqz_on"].shift(1).fillna(False)

    hh = out["High"].rolling(window=kc_length, min_periods=kc_length).max()
    ll = out["Low"].rolling(window=kc_length, min_periods=kc_length).min()
    avg_price = ((hh + ll) / 2.0 + kc_basis) / 2.0
    delta = out["Close"] - avg_price

    weights = (6 * np.arange(1, kc_length + 1) - 2 * kc_length - 2) / (kc_length * (kc_length + 1))
    hist = delta.rolling(window=kc_length, min_periods=kc_length).apply(lambda x: float(np.dot(x, weights)), raw=True)
    out["sqzmom_hist"] = hist
    out["sqzmom_delta"] = out["sqzmom_hist"].diff()

    conditions = [
        (out["sqzmom_hist"] >= 0) & (out["sqzmom_delta"] >= 0),
        (out["sqzmom_hist"] >= 0) & (out["sqzmom_delta"] < 0),
        (out["sqzmom_hist"] < 0) & (out["sqzmom_delta"] < 0),
        (out["sqzmom_hist"] < 0) & (out["sqzmom_delta"] >= 0),
    ]
    choices = ["lime", "green", "red", "maroon"]
    out["sqzmom_color"] = np.select(conditions, choices, default="")
    return out


def calc_dynamic_swing_avwap(
    df: pd.DataFrame,
    swing_period: int = 5,
    apt_period: int = 14,
    atr_period: int = 14,
    atr_baseline_period: int = 50,
) -> pd.DataFrame:
    _require_columns(df, ["High", "Low", "Close", "Volume"])
    out = df.copy()
    tp = (out["High"] + out["Low"] + out["Close"]) / 3.0
    vol = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0)
    n = len(out)

    tr = pd.concat(
        [
            out["High"] - out["Low"],
            (out["High"] - out["Close"].shift(1)).abs(),
            (out["Low"] - out["Close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period, min_periods=atr_period).mean()
    avg_atr = atr.rolling(atr_baseline_period, min_periods=atr_baseline_period).mean()

    avwap_vals = np.full(n, np.nan)
    anchor_flags = np.zeros(n, dtype=bool)
    swing_types = np.zeros(n, dtype=int)

    last_swing_type = 0
    sum_pv = 0.0
    sum_v = 0.0

    for i in range(swing_period * 2, n):
        window_high = out["High"].iloc[i - 2 * swing_period : i + 1]
        window_low = out["Low"].iloc[i - 2 * swing_period : i + 1]
        mid_idx = i - swing_period

        is_swing_high = bool(out["High"].iloc[mid_idx] == window_high.max())
        is_swing_low = bool(out["Low"].iloc[mid_idx] == window_low.min())

        new_anchor = False
        if is_swing_high and last_swing_type != 1:
            last_swing_type = 1
            new_anchor = True
        elif is_swing_low and last_swing_type != -1:
            last_swing_type = -1
            new_anchor = True

        curr_atr = atr.iloc[i]
        avg_curr_atr = avg_atr.iloc[i]
        vol_ratio = 1.0
        if pd.notna(curr_atr) and pd.notna(avg_curr_atr) and avg_curr_atr > 0:
            vol_ratio = float(np.clip(curr_atr / avg_curr_atr, 0.5, 2.0))

        dynamic_apt = apt_period / vol_ratio
        alpha = 2.0 / (dynamic_apt + 1.0)

        curr_tp = float(tp.iloc[i])
        curr_vol = float(vol.iloc[i])
        if new_anchor or sum_v <= 0:
            sum_pv = curr_tp * curr_vol
            sum_v = curr_vol
            anchor_flags[i] = True
        else:
            sum_pv = sum_pv * (1.0 - alpha) + curr_tp * curr_vol
            sum_v = sum_v * (1.0 - alpha) + curr_vol

        avwap_vals[i] = sum_pv / sum_v if sum_v > 0 else curr_tp
        swing_types[i] = last_swing_type

    out["dynamic_avwap"] = avwap_vals
    out["swing_anchor_reset"] = anchor_flags
    out["swing_direction"] = swing_types
    return out


def add_intraday_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = calc_sqzmom_lb(df)
    out = calc_dynamic_swing_avwap(out)
    out["above_avwap"] = out["Close"] > out["dynamic_avwap"]
    out["below_avwap"] = out["Close"] < out["dynamic_avwap"]
    out["sqzmom_positive"] = out["sqzmom_hist"] > 0
    out["sqzmom_rising"] = out["sqzmom_delta"] > 0
    out["sqzmom_falling"] = out["sqzmom_delta"] < 0
    out["long_trigger"] = out["sqz_release"] & out["sqzmom_positive"] & out["above_avwap"]
    return out