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
    def __init__(
        self, cfg: RiskConfig, total_capital: float, leverage_multiple: float = 1.0
    ):
        self.cfg = cfg
        self.total_capital = float(total_capital)
        self.leverage_multiple = max(1.0, float(leverage_multiple))
        self.state = DayState(day=now_ist().date())
        if self.leverage_multiple > 1.0:
            log.warning(
                "Leverage is set to %.1fx — losses are magnified by the same factor "
                "and the broker's auto-square-off may act before your stop does.",
                self.leverage_multiple,
            )

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

    def buying_power(self, account: AccountConfig) -> float:
        """Gross rupee value this account can command.

        With the default ``leverage_multiple`` of 1.0 this is exactly the
        account's own cash — no broker margin is used. Raising it above 1.0
        means borrowing: a 2% adverse move becomes a 4% account loss at 2x, and
        the broker's auto-square-off, not your stop, starts deciding when you
        exit. The default is deliberate.

        Both the per-position cap and the total-exposure cap are shares of this
        figure, so the two scale together and cannot contradict each other.
        """
        return account.capital * self.leverage_multiple

    def max_exposure(self, account: AccountConfig) -> float:
        """Ceiling on the combined value of all open positions."""
        return self.buying_power(account) * self.cfg.max_total_exposure_pct / 100.0

    def position_size(
        self,
        account: AccountConfig,
        entry_price: float,
        stop_price: float,
        lot_size: int = 1,
        available_cash: float | None = None,
        deployed_value: float = 0.0,
    ) -> tuple[int, str]:
        """Quantity for one account, and a note on which limit bound it.

        Four independent ceilings; the smallest wins:

        1. **Risk budget** — the usual driver: risk ÷ stop distance.
        2. **Per-position cap** — no single name dominates the account.
        3. **Total exposure** — capital already committed to open positions is
           subtracted, so three positions cannot each claim the full balance.
           Without this, per-position caps silently compound into leverage.
        4. **Available cash** — live only, straight from the broker's RMS.

        `deployed_value` is what this account already holds at cost. Passing 0
        (the default) treats the account as flat.
        """
        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit <= 0 or entry_price <= 0:
            return 0, "invalid stop distance"

        risk_budget = (
            account.capital * self.cfg.risk_per_trade_pct / 100.0 * account.risk_multiplier
        )
        limits: list[tuple[float, str]] = [(risk_budget / risk_per_unit, "risk")]

        gross = self.buying_power(account)
        max_value = gross * self.cfg.max_position_value_pct / 100.0
        limits.append((max_value / entry_price, "position cap"))

        remaining = max(0.0, self.max_exposure(account) - max(0.0, deployed_value))
        limits.append((remaining / entry_price, "total exposure"))

        if available_cash is not None:
            usable = max(0.0, available_cash) * self.leverage_multiple
            limits.append((usable / entry_price, "available cash"))

        qty, bound = min(limits, key=lambda pair: pair[0])

        if lot_size > 1:
            final = math.floor(qty / lot_size) * lot_size
        else:
            final = math.floor(qty)

        if final <= 0:
            return 0, (
                f"sized to zero — bound by {bound} "
                f"(price {entry_price:,.2f}, stop distance {risk_per_unit:.2f}, "
                f"risk budget {risk_budget:,.0f}, lot {lot_size})"
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
