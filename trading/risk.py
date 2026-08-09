"""Risk control: position sizing and the limits that stop a bad day.

The sizing rule is fixed-fractional: risk a constant small percentage of
capital per trade, and let the distance to the stop decide the quantity. This
is what keeps a run of losses survivable — the strategy's edge is irrelevant
if the sizing lets one day end the account.

Every limit here is a hard block, not a suggestion. `check_entry` is the only
gate between a signal and an order.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .config import AccountConfig, RiskConfig
from .utils import get_logger, now_ist

log = get_logger("risk")


@dataclass
class DayState:
    day: date
    realised_pnl: float = 0.0
    trades_taken: int = 0
    per_symbol: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, total_capital: float):
        self.cfg = cfg
        self.total_capital = float(total_capital)
        self.state = DayState(day=now_ist().date())

    # ------------------------------------------------------------- day cycle

    def roll_day_if_needed(self) -> None:
        today = now_ist().date()
        if today != self.state.day:
            log.info("New trading day %s — resetting risk counters.", today)
            self.state = DayState(day=today)

    @property
    def max_daily_loss(self) -> float:
        return self.total_capital * self.cfg.max_daily_loss_pct / 100.0

    @property
    def max_daily_profit(self) -> float:
        return self.total_capital * self.cfg.max_daily_profit_pct / 100.0

    def record_exit(self, pnl: float) -> None:
        self.state.realised_pnl += pnl
        if self.state.realised_pnl <= -self.max_daily_loss:
            self.halt(
                f"daily loss limit hit ({self.state.realised_pnl:,.0f} "
                f"<= -{self.max_daily_loss:,.0f})"
            )
        elif self.state.realised_pnl >= self.max_daily_profit:
            self.halt(
                f"daily profit target reached ({self.state.realised_pnl:,.0f} "
                f">= {self.max_daily_profit:,.0f})"
            )

    def record_entry(self, symbol: str) -> None:
        self.state.trades_taken += 1
        self.state.per_symbol[symbol] += 1

    def halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            log.warning("TRADING HALTED FOR THE DAY: %s", reason)

    # ----------------------------------------------------------------- gates

    def check_entry(self, symbol: str, open_positions: int) -> tuple[bool, str]:
        self.roll_day_if_needed()

        if self.state.halted:
            return False, f"halted ({self.state.halt_reason})"
        if open_positions >= self.cfg.max_open_positions:
            return False, f"max open positions ({self.cfg.max_open_positions})"
        if self.state.trades_taken >= self.cfg.max_trades_per_day:
            return False, f"max trades/day ({self.cfg.max_trades_per_day})"
        if self.state.per_symbol[symbol] >= self.cfg.max_trades_per_symbol_per_day:
            return False, (
                f"max trades/day for {symbol} ({self.cfg.max_trades_per_symbol_per_day})"
            )
        return True, ""

    # ---------------------------------------------------------------- sizing

    def position_size(
        self,
        account: AccountConfig,
        entry_price: float,
        stop_price: float,
        lot_size: int = 1,
        available_margin: float | None = None,
    ) -> tuple[int, str]:
        """Quantity for one account, and a note on what bound it.

        Bounded by three things in order: the risk budget, the maximum share of
        capital allowed in one position, and (live only) the margin actually
        available.
        """
        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit <= 0 or entry_price <= 0:
            return 0, "invalid stop distance"

        risk_budget = (
            account.capital * self.cfg.risk_per_trade_pct / 100.0 * account.risk_multiplier
        )
        qty_by_risk = risk_budget / risk_per_unit

        max_value = account.capital * self.cfg.max_position_value_pct / 100.0
        qty_by_value = max_value / entry_price

        qty = min(qty_by_risk, qty_by_value)
        bound = "risk" if qty_by_risk <= qty_by_value else "position cap"

        if available_margin is not None:
            # 5x is the usual intraday MIS leverage on cash; the broker's own
            # RMS is still the final authority.
            qty_by_margin = (available_margin * 5.0) / entry_price
            if qty_by_margin < qty:
                qty, bound = qty_by_margin, "available margin"

        if lot_size > 1:
            lots = math.floor(qty / lot_size)
            final = lots * lot_size
        else:
            final = math.floor(qty)

        if final <= 0:
            return 0, (
                f"sized to zero (risk budget {risk_budget:,.0f}, "
                f"stop distance {risk_per_unit:.2f}, lot {lot_size})"
            )
        return int(final), bound

    def summary(self) -> str:
        s = self.state
        return (
            f"trades={s.trades_taken}/{self.cfg.max_trades_per_day} "
            f"pnl={s.realised_pnl:,.0f} "
            f"(loss limit -{self.max_daily_loss:,.0f}, target +{self.max_daily_profit:,.0f})"
            + (f" HALTED: {s.halt_reason}" if s.halted else "")
        )
