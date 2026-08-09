"""Position model.

One *logical* position (born of one signal) spans every trading account, each
holding its own leg with its own quantity and fills. Stop/target management is
decided once at the position level and applied to all legs, so accounts never
drift apart in their risk view.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import StrategyConfig
from .instruments import Instrument
from .strategy import Side, Signal
from .utils import round_to_tick


@dataclass
class Leg:
    account: str
    quantity: int
    entry_price: float
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    closed: bool = False

    def pnl(self, side: Side, mark: float | None = None) -> float:
        price = self.exit_price if self.closed else mark
        if price is None:
            return 0.0
        direction = 1 if side == "LONG" else -1
        return (price - self.entry_price) * self.quantity * direction


@dataclass
class Position:
    symbol: str
    instrument: Instrument
    side: Side
    entry_time: datetime
    entry_ref: float
    initial_stop: float
    stop: float
    target: float
    atr: float
    score: int
    reasons: list[str] = field(default_factory=list)
    legs: dict[str, Leg] = field(default_factory=dict)

    high_water: float = 0.0
    low_water: float = 0.0
    breakeven_done: bool = False
    closed: bool = False
    exit_time: datetime | None = None
    exit_reason: str = ""

    @classmethod
    def from_signal(cls, signal: Signal, instrument: Instrument) -> "Position":
        return cls(
            symbol=signal.symbol,
            instrument=instrument,
            side=signal.side,
            entry_time=signal.timestamp,
            entry_ref=signal.entry_ref,
            initial_stop=signal.stop,
            stop=signal.stop,
            target=signal.target,
            atr=signal.atr,
            score=signal.score,
            reasons=list(signal.reasons),
            high_water=signal.entry_ref,
            low_water=signal.entry_ref,
        )

    # ------------------------------------------------------------------ state

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def total_quantity(self) -> int:
        return sum(leg.quantity for leg in self.legs.values() if not leg.closed)

    @property
    def avg_entry(self) -> float:
        open_legs = [leg for leg in self.legs.values()]
        qty = sum(leg.quantity for leg in open_legs)
        if qty == 0:
            return self.entry_ref
        return sum(leg.entry_price * leg.quantity for leg in open_legs) / qty

    @property
    def risk_per_unit(self) -> float:
        return abs(self.avg_entry - self.initial_stop)

    def r_multiple(self, price: float) -> float:
        risk = self.risk_per_unit
        if risk <= 0:
            return 0.0
        return (price - self.avg_entry) * self.direction / risk

    def unrealised(self, mark: float) -> float:
        return sum(leg.pnl(self.side, mark) for leg in self.legs.values() if not leg.closed)

    def realised(self) -> float:
        return sum(leg.pnl(self.side) for leg in self.legs.values() if leg.closed)

    # ----------------------------------------------------------- stop control

    def update_watermarks(self, high: float, low: float) -> None:
        self.high_water = max(self.high_water, high)
        self.low_water = min(self.low_water, low)

    def next_stop(self, cfg: StrategyConfig) -> float | None:
        """Proposed new stop, or None if it should not move.

        Two stages: once the trade clears `breakeven_at_r`, the stop jumps to
        entry so the trade can no longer lose; after that a chandelier trail
        follows the extreme by `trail_atr_multiple` ATRs. Stops only ever move
        in the profitable direction.
        """
        tick = self.instrument.tick_size
        proposed = self.stop

        if not self.breakeven_done:
            extreme = self.high_water if self.side == "LONG" else self.low_water
            if self.r_multiple(extreme) >= cfg.breakeven_at_r:
                proposed = self.avg_entry
                self.breakeven_done = True

        if self.breakeven_done:
            if self.side == "LONG":
                proposed = max(proposed, self.high_water - cfg.trail_atr_multiple * self.atr)
            else:
                proposed = min(proposed, self.low_water + cfg.trail_atr_multiple * self.atr)

        proposed = round_to_tick(proposed, tick)

        improved = proposed > self.stop if self.side == "LONG" else proposed < self.stop
        return proposed if improved else None

    def stop_hit(self, low: float, high: float) -> bool:
        return low <= self.stop if self.side == "LONG" else high >= self.stop

    def target_hit(self, low: float, high: float) -> bool:
        return high >= self.target if self.side == "LONG" else low <= self.target

    def to_record(self, mark: float | None = None) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_time": self.entry_time.isoformat() if self.entry_time else "",
            "exit_time": self.exit_time.isoformat() if self.exit_time else "",
            "avg_entry": round(self.avg_entry, 2),
            "initial_stop": round(self.initial_stop, 2),
            "final_stop": round(self.stop, 2),
            "target": round(self.target, 2),
            "quantity": sum(leg.quantity for leg in self.legs.values()),
            "score": self.score,
            "reasons": "|".join(self.reasons),
            "exit_reason": self.exit_reason,
            "pnl": round(self.realised() + (self.unrealised(mark) if mark else 0.0), 2),
            "r_multiple": round(
                self.r_multiple(mark) if mark and not self.closed else self._closed_r(), 2
            ),
        }

    def _closed_r(self) -> float:
        risk = self.risk_per_unit
        qty = sum(leg.quantity for leg in self.legs.values())
        if risk <= 0 or qty == 0:
            return 0.0
        return self.realised() / (risk * qty)
