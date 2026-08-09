"""Daily shortlist: choose which shares to trade this morning.

Run once at start-up, before the open. It pulls daily candles for every name in
the configured universe, applies hard filters, ranks the survivors and hands
the top N to the engine.

What it selects for, and why:

* **Liquidity** (average daily traded value). The single most important filter.
  An intraday edge is small; a wide spread eats it. Illiquid names also gap
  through stops, which turns a planned 1R loss into 3R.
* **Volatility** (daily ATR%). A stock that moves 0.3% a day cannot pay for a
  round trip. One that moves 8% is news-driven and not something a trend
  system should be guessing at.
* **Trend quality** (daily ADX). The strategy trades continuation, so it wants
  names that actually trend rather than oscillate.
* **Affordability.** With leverage at 1.0, a name whose single share exceeds
  the per-position budget is untradeable — it is filtered out rather than
  silently sized to zero every day.

The screen deliberately does *not* look at news, fundamentals or anyone's
recommendations. It selects for tradeable mechanics, not for opinions about
which company will do well.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from .broker import AngelBroker
from .config import ScreenerConfig
from .indicators import adx as adx_indicator, atr as atr_indicator
from .instruments import Instrument, InstrumentMaster
from .utils import get_logger, now_ist

log = get_logger("screener")

CRORE = 1e7


@dataclass
class ScreenResult:
    symbol: str
    instrument: Instrument
    price: float
    turnover_crore: float
    atr_pct: float
    adx: float
    momentum_pct: float
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.symbol:<14} ₹{self.price:>9,.1f}  "
            f"turnover {self.turnover_crore:>6.0f}cr  "
            f"atr {self.atr_pct:>4.1f}%  adx {self.adx:>4.1f}  "
            f"mom {self.momentum_pct:>+5.1f}%  score {self.score:.3f}"
        )


class Screener:
    def __init__(
        self,
        broker: AngelBroker,
        master: InstrumentMaster,
        cfg: ScreenerConfig,
        exchange: str = "NSE",
    ):
        self.broker = broker
        self.master = master
        self.cfg = cfg
        self.exchange = exchange

    # ---------------------------------------------------------------- metrics

    def _metrics(self, symbol: str, inst: Instrument) -> ScreenResult | None:
        """Daily-candle metrics for one name, or None if unusable."""
        end = now_ist()
        # Pad generously: lookback is in trading days, the request is in
        # calendar days, and holidays sit in between.
        start = end - timedelta(days=self.cfg.lookback_days * 2 + 15)

        try:
            df = self.broker.candles(inst.exchange, inst.token, "ONE_DAY", start, end)
        except Exception as exc:
            log.debug("%s: candle fetch failed (%s)", symbol, exc)
            return None

        if df is None or len(df) < 15:
            log.debug("%s: only %d daily bars", symbol, 0 if df is None else len(df))
            return None

        recent = df.tail(self.cfg.lookback_days)
        price = float(recent["close"].iloc[-1])
        if price <= 0:
            return None

        turnover = float((recent["close"] * recent["volume"]).mean()) / CRORE
        atr_val = float(atr_indicator(df["high"], df["low"], df["close"], 14).iloc[-1])
        atr_pct = 100.0 * atr_val / price
        adx_val = float(adx_indicator(df["high"], df["low"], df["close"], 14)["adx"].iloc[-1])

        lookback = min(5, len(recent) - 1)
        past = float(recent["close"].iloc[-1 - lookback])
        momentum = 100.0 * (price - past) / past if past > 0 else 0.0

        if not all(np.isfinite([turnover, atr_pct, adx_val, momentum])):
            return None

        return ScreenResult(
            symbol=symbol, instrument=inst, price=price,
            turnover_crore=turnover, atr_pct=atr_pct, adx=adx_val,
            momentum_pct=momentum,
        )

    # ---------------------------------------------------------------- filters

    def _passes(self, r: ScreenResult, position_budget: float | None) -> str | None:
        """Name of the first failed filter, or None if the candidate survives."""
        c = self.cfg
        if r.turnover_crore < c.min_turnover_crore:
            return f"illiquid ({r.turnover_crore:.0f}cr < {c.min_turnover_crore:.0f}cr)"
        if not (c.min_price <= r.price <= c.max_price):
            return f"price {r.price:,.0f} outside {c.min_price:,.0f}-{c.max_price:,.0f}"
        if not (c.min_atr_pct <= r.atr_pct <= c.max_atr_pct):
            return f"atr {r.atr_pct:.1f}% outside {c.min_atr_pct}-{c.max_atr_pct}%"
        if r.adx < c.min_adx:
            return f"adx {r.adx:.1f} < {c.min_adx}"
        if (
            c.require_affordable
            and position_budget is not None
            and r.price * r.instrument.lot_size > position_budget
        ):
            return (
                f"unaffordable (₹{r.price * r.instrument.lot_size:,.0f} per lot "
                f"> budget ₹{position_budget:,.0f})"
            )
        return None

    # ---------------------------------------------------------------- ranking

    @staticmethod
    def _percentile_rank(values: list[float]) -> list[float]:
        """Rank to [0, 1]. Ranks rather than raw values, so a single huge
        turnover cannot dominate the other criteria."""
        if not values:
            return []
        series = pd.Series(values)
        if series.nunique() == 1:
            return [0.5] * len(values)
        return list(series.rank(pct=True).to_numpy())

    def _rank(self, candidates: list[ScreenResult]) -> list[ScreenResult]:
        c = self.cfg
        total_weight = c.weight_liquidity + c.weight_volatility + c.weight_trend
        if total_weight <= 0:
            raise ValueError("Screener ranking weights must sum to more than zero.")

        liquidity = self._percentile_rank([r.turnover_crore for r in candidates])
        volatility = self._percentile_rank([r.atr_pct for r in candidates])
        # Trend blends ADX with the size of the recent move, direction-agnostic:
        # the strategy trades both sides, so a strong downtrend is as good as up.
        trend = self._percentile_rank(
            [r.adx + abs(r.momentum_pct) for r in candidates]
        )

        for i, r in enumerate(candidates):
            r.score = (
                c.weight_liquidity * liquidity[i]
                + c.weight_volatility * volatility[i]
                + c.weight_trend * trend[i]
            ) / total_weight
            r.reasons = [
                f"liq {liquidity[i]:.2f}",
                f"vol {volatility[i]:.2f}",
                f"trend {trend[i]:.2f}",
            ]

        return sorted(candidates, key=lambda r: r.score, reverse=True)

    # ------------------------------------------------------------------- run

    def shortlist(self, position_budget: float | None = None) -> list[ScreenResult]:
        """Screen the universe and return the top names, best first.

        `position_budget` is the rupee value one position may occupy. Pass the
        smallest across accounts so the shortlist is tradeable by all of them.
        """
        if not self.cfg.universe:
            raise ValueError(
                "screener.enabled is true but screener.universe is empty. "
                "Add symbols to screen, or disable the screener and use a "
                "fixed 'symbols' list."
            )

        universe = sorted({s.strip().upper() for s in self.cfg.universe if s.strip()})
        log.info(
            "Screening %d symbols (this makes one API call each and takes a "
            "minute or so)...", len(universe),
        )

        resolved: dict[str, Instrument] = {}
        for symbol in universe:
            try:
                resolved[symbol] = self.master.resolve(symbol, self.exchange)
            except ValueError as exc:
                # A renamed or delisted name in the universe is harmless.
                log.debug("%s: not resolvable (%s)", symbol, exc)

        if not resolved:
            raise RuntimeError("No symbol in screener.universe could be resolved.")

        candidates: list[ScreenResult] = []
        rejected: dict[str, str] = {}
        no_data = 0

        for symbol, inst in resolved.items():
            metrics = self._metrics(symbol, inst)
            if metrics is None:
                no_data += 1
                continue
            failure = self._passes(metrics, position_budget)
            if failure:
                rejected[symbol] = failure
            else:
                candidates.append(metrics)

        log.info(
            "Screened %d: %d passed, %d filtered out, %d had no usable data.",
            len(resolved), len(candidates), len(rejected), no_data,
        )

        if not candidates:
            sample = list(rejected.items())[:5]
            for symbol, reason in sample:
                log.warning("  %s rejected: %s", symbol, reason)
            log.warning(
                "Nothing passed the screen. Relax screener filters "
                "(min_turnover_crore, atr band) or check that the universe is "
                "correct — the engine has nothing to trade today."
            )
            return []

        ranked = self._rank(candidates)[: max(1, self.cfg.shortlist_size)]

        log.info("Today's shortlist:")
        for i, r in enumerate(ranked, 1):
            log.info("  %d. %s", i, r.describe())
        return ranked


def format_shortlist(results: list[ScreenResult]) -> str:
    """Telegram-friendly summary."""
    if not results:
        return "<b>📋 No shares passed today's screen</b>\nNothing will be traded."
    lines = ["<b>📋 Today's shortlist</b>"]
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. <b>{r.symbol}</b> ₹{r.price:,.0f} · "
            f"{r.turnover_crore:.0f}cr · atr {r.atr_pct:.1f}%"
        )
    return "\n".join(lines)
