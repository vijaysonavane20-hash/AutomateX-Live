"""Vectorised indicators built on pandas only — no TA-Lib/system deps.

Every function returns a Series/DataFrame aligned to the input index and is
side-effect free, so the same code paths serve the live engine and the
backtester. Wilder's smoothing (alpha = 1/n) is used wherever the classical
definition calls for it, which is what charting platforms display.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # A stretch with no losses is a genuine 100, not a missing value.
    return out.where(avg_loss != 0.0, 100.0).fillna(50.0)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return _wilder(true_range(high, low, close), period)


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    """Returns adx, plus_di, minus_di — the trend-strength gate."""
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index
    )

    tr_smooth = _wilder(true_range(high, low, close), period).replace(0.0, np.nan)
    plus_di = 100.0 * _wilder(plus_dm, period) / tr_smooth
    minus_di = 100.0 * _wilder(minus_dm, period) / tr_smooth

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return pd.DataFrame(
        {
            "adx": _wilder(dx.fillna(0.0), period),
            "plus_di": plus_di.fillna(0.0),
            "minus_di": minus_di.fillna(0.0),
        }
    )


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price anchored to each trading session.

    Intraday VWAP must reset at the open — a rolling or continuous VWAP is a
    different (and for this purpose, wrong) indicator.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.normalize()
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum().replace(0.0, np.nan)
    return (pv / vol).ffill()


def supertrend(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
) -> pd.DataFrame:
    """Classic Supertrend. The band-carry rules need sequential state, so this
    is an explicit loop over numpy arrays rather than a vector expression."""
    atr_series = atr(df["high"], df["low"], df["close"], period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper = (hl2 + multiplier * atr_series).to_numpy()
    lower = (hl2 - multiplier * atr_series).to_numpy()
    close = df["close"].to_numpy()

    n = len(df)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.ones(n)  # 1 = uptrend, -1 = downtrend

    for i in range(n):
        if i == 0 or np.isnan(upper[i]) or np.isnan(final_upper[i - 1]):
            final_upper[i], final_lower[i] = upper[i], lower[i]
            continue

        final_upper[i] = (
            upper[i]
            if upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]
            else final_upper[i - 1]
        )
        final_lower[i] = (
            lower[i]
            if lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]
            else final_lower[i - 1]
        )

        if close[i] > final_upper[i]:
            direction[i] = 1.0
        elif close[i] < final_lower[i]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1]

    line = np.where(direction == 1, final_lower, final_upper)
    return pd.DataFrame(
        {"supertrend": line, "st_direction": direction}, index=df.index
    )


def opening_range(df: pd.DataFrame, minutes: int, session_start: str = "09:15") -> pd.DataFrame:
    """High/low of the first `minutes` of each session, forward-filled.

    Bars inside the opening range itself get NaN so a breakout can never be
    detected against a range that has not finished forming yet.
    """
    start = pd.Timestamp(session_start).time()
    day = df.index.normalize()

    minutes_from_open = (
        df.index.hour * 60 + df.index.minute - (start.hour * 60 + start.minute)
    )
    in_range = pd.Series(
        (minutes_from_open >= 0) & (minutes_from_open < minutes), index=df.index
    )

    or_high = df["high"].where(in_range).groupby(day).cummax()
    or_low = df["low"].where(in_range).groupby(day).cummin()

    # Freeze the values at the end of the range, then carry them forward.
    or_high = or_high.groupby(day).ffill().where(~in_range)
    or_low = or_low.groupby(day).ffill().where(~in_range)
    return pd.DataFrame({"or_high": or_high, "or_low": or_low})


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate to a higher timeframe, right-labelled and right-closed so a
    bar is stamped with the time it *finished* — this is what makes the
    forward-fill in `align_htf` non-lookahead."""
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df.resample(rule, label="right", closed="right").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


def align_htf(htf: pd.DataFrame, target_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Project completed higher-timeframe values onto the entry timeframe.

    `merge_asof` with the default 'backward' direction attaches, to each entry
    bar, the most recent HTF bar that had already closed. Nothing from the
    future leaks in.
    """
    left = pd.DataFrame(index=target_index).reset_index(names="ts")
    right = htf.reset_index(names="ts")
    merged = pd.merge_asof(
        left.sort_values("ts"), right.sort_values("ts"), on="ts", direction="backward"
    )
    return merged.set_index("ts").reindex(target_index)
