"""Candle store: warm-up history, incremental refresh, and closed-bar access.

The single most important job here is `closed_bars`. Angel stamps a candle with
the time it *opened*, so the most recent row returned by the API is usually a
bar that is still forming. Acting on a forming bar means acting on a price that
can still move against you before the bar completes — and it makes live
behaviour diverge from the backtest. Everything downstream reads closed bars
only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .broker import AngelBroker
from .instruments import Instrument
from .strategy import _interval_minutes
from .utils import get_logger, now_ist

log = get_logger("data")


class DataFeed:
    def __init__(
        self,
        broker: AngelBroker,
        instruments: dict[str, Instrument],
        interval: str = "FIVE_MINUTE",
        warmup_days: int = 5,
    ):
        self.broker = broker
        self.instruments = instruments
        self.interval = interval
        self.interval_minutes = _interval_minutes(interval)
        self.warmup_days = warmup_days
        self.store: dict[str, pd.DataFrame] = {}

    # -------------------------------------------------------------- fetching

    def warmup(self) -> None:
        """Load enough history for the slowest indicator to stabilise.

        Wilder-smoothed series (ATR, ADX, RSI) need several times their period
        before their values settle, so we pull calendar days rather than an
        exact bar count and let the extra bars absorb holidays.
        """
        end = now_ist()
        start = end - timedelta(days=max(self.warmup_days * 2, 7))

        for symbol, inst in self.instruments.items():
            try:
                df = self.broker.candles(inst.exchange, inst.token, self.interval, start, end)
                self.store[symbol] = df
                log.info("Warmed %s: %d bars (%s -> %s)", symbol, len(df),
                         df.index[0] if len(df) else "-", df.index[-1] if len(df) else "-")
            except Exception as exc:
                log.error("Warm-up failed for %s: %s", symbol, exc)
                self.store[symbol] = pd.DataFrame()

    def refresh(self, symbol: str) -> None:
        """Pull the recent window and merge. Overlap is intentional: the last
        few bars can be revised by the exchange feed, and merging on the index
        lets later values win."""
        inst = self.instruments.get(symbol)
        if inst is None:
            return

        end = now_ist()
        start = end - timedelta(minutes=self.interval_minutes * 12 + 5)

        try:
            fresh = self.broker.candles(inst.exchange, inst.token, self.interval, start, end)
        except Exception as exc:
            log.warning("Refresh failed for %s: %s", symbol, exc)
            return

        if fresh.empty:
            return

        existing = self.store.get(symbol)
        if existing is None or existing.empty:
            self.store[symbol] = fresh
            return

        combined = pd.concat([existing, fresh])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        self.store[symbol] = combined.tail(3000)

    def refresh_all(self) -> None:
        for symbol in self.instruments:
            self.refresh(symbol)

    # --------------------------------------------------------------- reading

    def closed_bars(self, symbol: str, as_of: datetime | None = None) -> pd.DataFrame:
        """History with any still-forming bar removed."""
        df = self.store.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame()

        as_of = as_of or now_ist()
        cutoff = as_of - timedelta(minutes=self.interval_minutes)
        # A bar stamped T covers [T, T+interval); it is closed once now >= T+interval.
        return df[df.index <= cutoff]

    def last_closed_bar(self, symbol: str, as_of: datetime | None = None) -> pd.Series | None:
        bars = self.closed_bars(symbol, as_of)
        return None if bars.empty else bars.iloc[-1]

    def next_bar_close(self, as_of: datetime | None = None) -> datetime:
        """Wall-clock time the current bar completes — used to schedule work
        just after each close instead of polling blindly."""
        as_of = as_of or now_ist()
        step = self.interval_minutes
        # Bars are aligned to the 09:15 open, not to the top of the hour.
        anchor = as_of.replace(hour=9, minute=15, second=0, microsecond=0)
        if as_of < anchor:
            return anchor + timedelta(minutes=step)
        elapsed = (as_of - anchor).total_seconds() / 60.0
        completed = int(elapsed // step) + 1
        return anchor + timedelta(minutes=completed * step)
