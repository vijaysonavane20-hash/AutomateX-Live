"""The signal engine: a gated, score-based intraday trend-continuation setup.

Design intent — selectivity over frequency
------------------------------------------
The setup is deliberately hard to trigger. It is built in two layers:

1. **Hard gates (veto).** Regime filters that describe the only conditions in
   which the setup has any business trading at all: the higher timeframe must
   be trending in our direction, ADX must confirm there *is* a trend, price
   must be on the correct side of session VWAP, and volatility must sit in a
   usable band. Fail any one and the bar is discarded outright, no matter how
   good everything else looks. This is what filters out chop, and chop is
   where intraday systems bleed.

2. **Weighted confirmations (score).** Nine independent confirmations, worth
   19 points in total. `strategy.min_score` sets the bar (default 14). Raising
   it trades less and, historically, more cleanly; lowering it does the
   reverse. Tune it against `backtest.py` on your own symbols — not by
   intuition.

Every rule reads only from bars that have already closed. `prepare()` shifts
each backward-looking window by one bar, so a signal computed on the last
closed bar in the live engine is the identical signal the backtester computes
for that same bar. That equivalence is the whole point: it is what makes the
backtest numbers mean something.

No setup is "accurate" in the sense of being reliably right. This one is built
to lose small and often, and win larger when the gates line up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd

from . import indicators as ind
from .config import StrategyConfig
from .utils import round_to_tick

Side = Literal["LONG", "SHORT"]

MAX_SCORE = 19

# weight table, kept here so the docstring above and the code cannot drift apart
WEIGHTS = {
    "ema_stack": 2,
    "rsi_band": 2,
    "volume_surge": 2,
    "donchian_break": 3,
    "opening_range_break": 3,
    "supertrend": 2,
    "candle_strength": 2,
    "htf_close": 2,
    "structure": 1,
}


@dataclass
class Signal:
    symbol: str
    side: Side
    timestamp: datetime
    entry_ref: float
    stop: float
    target: float
    atr: float
    score: int
    reasons: list[str] = field(default_factory=list)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_ref - self.stop)

    @property
    def reward_risk(self) -> float:
        risk = self.risk_per_unit
        return abs(self.target - self.entry_ref) / risk if risk > 0 else 0.0


@dataclass
class Evaluation:
    """Result of examining one bar — carries the rejection reason so the logs
    explain *why* nothing traded, which is most of what tuning needs."""

    signal: Signal | None
    rejected_by: str | None = None
    long_score: int = 0
    short_score: int = 0


def prepare(df: pd.DataFrame, cfg: StrategyConfig, opening_range_minutes: int = 15) -> pd.DataFrame:
    """Attach every indicator the rules need. Input must be a tz-aware,
    ascending, de-duplicated OHLCV frame on the entry timeframe."""
    if df.empty:
        return df.copy()

    out = df.copy()

    out["ema_fast"] = ind.ema(out["close"], cfg.ema_fast)
    out["ema_slow"] = ind.ema(out["close"], cfg.ema_slow)
    out["rsi"] = ind.rsi(out["close"], cfg.rsi_period)
    out["atr"] = ind.atr(out["high"], out["low"], out["close"], cfg.atr_period)
    out["atr_pct"] = 100.0 * out["atr"] / out["close"].replace(0.0, np.nan)
    out["vwap"] = ind.session_vwap(out)

    adx_df = ind.adx(out["high"], out["low"], out["close"], cfg.adx_period)
    out[["adx", "plus_di", "minus_di"]] = adx_df

    st = ind.supertrend(out, cfg.supertrend_period, cfg.supertrend_multiplier)
    out["st_direction"] = st["st_direction"]

    # Volume baseline excludes the current bar so a surge is measured against
    # history rather than against itself.
    out["vol_avg"] = out["volume"].rolling(cfg.volume_lookback).mean().shift(1)
    out["vol_ratio"] = out["volume"] / out["vol_avg"].replace(0.0, np.nan)

    # Breakout references: prior N bars, current bar excluded.
    out["donchian_high"] = out["high"].rolling(cfg.breakout_lookback).max().shift(1)
    out["donchian_low"] = out["low"].rolling(cfg.breakout_lookback).min().shift(1)

    # Structure stops: recent swing extremes, current bar excluded.
    out["swing_low"] = out["low"].rolling(5).min().shift(1)
    out["swing_high"] = out["high"].rolling(5).max().shift(1)
    out["prev_low"] = out["low"].shift(1)
    out["prev_low2"] = out["low"].shift(2)
    out["prev_high"] = out["high"].shift(1)
    out["prev_high2"] = out["high"].shift(2)

    out[["or_high", "or_low"]] = ind.opening_range(out, opening_range_minutes)

    # Higher timeframe trend, projected back without lookahead.
    rule = f"{_interval_minutes(cfg.entry_interval) * cfg.htf_multiplier}min"
    htf = ind.resample_ohlc(out[["open", "high", "low", "close", "volume"]], rule)
    if not htf.empty:
        htf_feat = pd.DataFrame(index=htf.index)
        htf_feat["htf_ema_fast"] = ind.ema(htf["close"], cfg.htf_ema_fast)
        htf_feat["htf_ema_slow"] = ind.ema(htf["close"], cfg.htf_ema_slow)
        htf_feat["htf_close"] = htf["close"]
        out[["htf_ema_fast", "htf_ema_slow", "htf_close"]] = ind.align_htf(
            htf_feat, out.index
        )
    else:
        out[["htf_ema_fast", "htf_ema_slow", "htf_close"]] = np.nan

    # Candle geometry.
    rng = (out["high"] - out["low"]).replace(0.0, np.nan)
    out["body_ratio"] = (out["close"] - out["open"]).abs() / rng
    out["close_position"] = (out["close"] - out["low"]) / rng

    return out


def _interval_minutes(interval: str) -> int:
    mapping = {
        "ONE_MINUTE": 1,
        "THREE_MINUTE": 3,
        "FIVE_MINUTE": 5,
        "TEN_MINUTE": 10,
        "FIFTEEN_MINUTE": 15,
        "THIRTY_MINUTE": 30,
        "ONE_HOUR": 60,
        "ONE_DAY": 375,
    }
    if interval not in mapping:
        raise ValueError(f"Unsupported interval '{interval}'. One of {sorted(mapping)}.")
    return mapping[interval]


def _f(row: pd.Series, key: str) -> float:
    value = row.get(key, np.nan)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _gates(row: pd.Series, cfg: StrategyConfig, side: Side) -> str | None:
    """Return the name of the first failed gate, or None if all pass."""
    close = _f(row, "close")
    atr_pct = _f(row, "atr_pct")
    adx_val = _f(row, "adx")
    vwap = _f(row, "vwap")
    htf_fast, htf_slow = _f(row, "htf_ema_fast"), _f(row, "htf_ema_slow")
    htf_close = _f(row, "htf_close")

    if not np.isfinite([close, atr_pct, adx_val, vwap, htf_fast, htf_slow, htf_close]).all():
        return "warmup"
    if adx_val < cfg.adx_min:
        return f"adx<{cfg.adx_min}"
    if not (cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct):
        return "atr_pct_out_of_band"

    if side == "LONG":
        if close <= vwap:
            return "below_vwap"
        if not (htf_fast > htf_slow and htf_close > htf_slow):
            return "htf_not_bullish"
        if _f(row, "plus_di") <= _f(row, "minus_di"):
            return "di_not_bullish"
    else:
        if close >= vwap:
            return "above_vwap"
        if not (htf_fast < htf_slow and htf_close < htf_slow):
            return "htf_not_bearish"
        if _f(row, "minus_di") <= _f(row, "plus_di"):
            return "di_not_bearish"
    return None


def _score(row: pd.Series, cfg: StrategyConfig, side: Side) -> tuple[int, list[str]]:
    total = 0
    reasons: list[str] = []

    def award(key: str, condition: bool, label: str) -> None:
        nonlocal total
        if condition:
            total += WEIGHTS[key]
            reasons.append(label)

    close = _f(row, "close")
    long_side = side == "LONG"

    award(
        "ema_stack",
        (_f(row, "ema_fast") > _f(row, "ema_slow"))
        if long_side
        else (_f(row, "ema_fast") < _f(row, "ema_slow")),
        "ema_stack",
    )

    rsi_val = _f(row, "rsi")
    lo, hi = cfg.rsi_long_band if long_side else cfg.rsi_short_band
    award("rsi_band", lo <= rsi_val <= hi, f"rsi={rsi_val:.0f}")

    vol_ratio = _f(row, "vol_ratio")
    award("volume_surge", vol_ratio >= cfg.volume_multiple, f"vol x{vol_ratio:.1f}")

    donchian = _f(row, "donchian_high") if long_side else _f(row, "donchian_low")
    award(
        "donchian_break",
        (close > donchian) if long_side else (close < donchian),
        "donchian_break",
    )

    or_level = _f(row, "or_high") if long_side else _f(row, "or_low")
    award(
        "opening_range_break",
        np.isfinite(or_level) and ((close > or_level) if long_side else (close < or_level)),
        "orb",
    )

    award(
        "supertrend",
        _f(row, "st_direction") == (1.0 if long_side else -1.0),
        "supertrend",
    )

    body_ok = _f(row, "body_ratio") >= 0.5
    pos = _f(row, "close_position")
    award(
        "candle_strength",
        body_ok and (pos >= 0.65 if long_side else pos <= 0.35),
        "strong_candle",
    )

    htf_fast = _f(row, "htf_ema_fast")
    award(
        "htf_close",
        (close > htf_fast) if long_side else (close < htf_fast),
        "beyond_htf_ema",
    )

    # Continuation structure: the pullback before this bar held higher (long)
    # or lower (short) than the one before it.
    if long_side:
        structure = _f(row, "prev_low") > _f(row, "prev_low2")
    else:
        structure = _f(row, "prev_high") < _f(row, "prev_high2")
    award("structure", bool(structure), "structure")

    return total, reasons


def _levels(row: pd.Series, cfg: StrategyConfig, side: Side, tick: float) -> tuple[float, float]:
    """Stop and target.

    The stop is the wider of an ATR stop and the recent swing extreme — a stop
    placed just inside structure gets hunted — then capped at 2x the ATR stop
    so one ragged swing cannot blow the risk budget open.

    The target is then derived from the *actual* stop distance rather than
    from ATR directly, so the reward:risk ratio
    (``atr_target_multiple / atr_stop_multiple``) survives that widening. Set
    the target from ATR alone and a structural stop silently degrades a 2:1
    setup into a 1:1 one — which is exactly enough to turn a winning
    expectancy negative without any rule appearing to have changed.
    """
    close = _f(row, "close")
    atr_val = _f(row, "atr")
    buffer = 0.15 * atr_val
    reward_ratio = cfg.atr_target_multiple / cfg.atr_stop_multiple

    if side == "LONG":
        atr_stop = close - cfg.atr_stop_multiple * atr_val
        swing_stop = _f(row, "swing_low") - buffer
        stop = min(atr_stop, swing_stop) if np.isfinite(swing_stop) else atr_stop
        stop = max(stop, close - 2.0 * cfg.atr_stop_multiple * atr_val)
        target = close + reward_ratio * (close - stop)
    else:
        atr_stop = close + cfg.atr_stop_multiple * atr_val
        swing_stop = _f(row, "swing_high") + buffer
        stop = max(atr_stop, swing_stop) if np.isfinite(swing_stop) else atr_stop
        stop = min(stop, close + 2.0 * cfg.atr_stop_multiple * atr_val)
        target = close - reward_ratio * (stop - close)

    return round_to_tick(stop, tick), round_to_tick(target, tick)


def evaluate(
    row: pd.Series,
    symbol: str,
    cfg: StrategyConfig,
    tick: float = 0.05,
    allowed_sides: tuple[Side, ...] = ("LONG", "SHORT"),
) -> Evaluation:
    """Examine one **closed** bar and return a signal, or the reason there
    isn't one. This is the single place trade decisions are made — the live
    engine and the backtester both call it and nothing else."""
    scores: dict[Side, int] = {"LONG": 0, "SHORT": 0}
    rejections: list[str] = []
    best: Signal | None = None

    for side in allowed_sides:
        failed = _gates(row, cfg, side)
        if failed:
            rejections.append(f"{side.lower()}:{failed}")
            continue

        score, reasons = _score(row, cfg, side)
        scores[side] = score
        if score < cfg.min_score:
            rejections.append(f"{side.lower()}:score {score}/{MAX_SCORE}")
            continue

        stop, target = _levels(row, cfg, side, tick)
        close = _f(row, "close")
        if (side == "LONG" and stop >= close) or (side == "SHORT" and stop <= close):
            rejections.append(f"{side.lower()}:degenerate_stop")
            continue

        candidate = Signal(
            symbol=symbol,
            side=side,
            timestamp=row.name.to_pydatetime() if hasattr(row.name, "to_pydatetime") else row.name,
            entry_ref=round_to_tick(close, tick),
            stop=stop,
            target=target,
            atr=_f(row, "atr"),
            score=score,
            reasons=reasons,
        )
        # Both directions passing at once means the tape is confused; keep the
        # stronger one rather than trading a contradiction.
        if best is None or candidate.score > best.score:
            best = candidate

    return Evaluation(
        signal=best,
        rejected_by=None if best else "; ".join(rejections) or "no_signal",
        long_score=scores["LONG"],
        short_score=scores["SHORT"],
    )
