"""Execution layer — one interface, two backends.

`PaperExecutor` and `LiveExecutor` are interchangeable, so the engine has no
idea whether it is trading real money. Paper mode is the default everywhere;
live mode requires both a config flag and an explicit command-line switch.

Paper fills apply configurable slippage in the direction that hurts, because a
backtest or dry run that fills at the exact close price flatters every result
it produces.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .broker import AccountPool
from .config import ExecutionConfig
from .positions import Leg, Position
from .utils import get_logger, round_to_tick

log = get_logger("execution")


class Executor(ABC):
    @abstractmethod
    def open_position(self, position: Position, sizes: dict[str, int], ref_price: float) -> bool:
        """Fill legs on `position`. Return False if nothing was opened."""

    @abstractmethod
    def close_position(self, position: Position, price: float, reason: str) -> float:
        """Flatten every open leg. Return realised PnL for the position."""

    @abstractmethod
    def update_stop(self, position: Position, new_stop: float) -> None:
        """Move protection to `new_stop`."""

    def square_off_everything(self) -> None:  # pragma: no cover - live only
        return None


class PaperExecutor(Executor):
    def __init__(self, cfg: ExecutionConfig, account_names: list[str]):
        self.cfg = cfg
        self.account_names = account_names

    def _fill(self, price: float, side: str, tick: float) -> float:
        """Slippage always works against us."""
        adjustment = price * self.cfg.slippage_bps / 10_000.0
        filled = price + adjustment if side == "BUY" else price - adjustment
        return round_to_tick(filled, tick)

    def open_position(self, position: Position, sizes: dict[str, int], ref_price: float) -> bool:
        entry_side = "BUY" if position.side == "LONG" else "SELL"
        tick = position.instrument.tick_size
        fill = self._fill(ref_price, entry_side, tick)

        for account, qty in sizes.items():
            if qty > 0:
                position.legs[account] = Leg(
                    account=account, quantity=qty, entry_price=fill
                )

        if not position.legs:
            return False

        log.info(
            "[PAPER] OPEN %s %s x%d @ %.2f | stop %.2f target %.2f | score %d/%d [%s]",
            position.side, position.symbol, position.total_quantity, fill,
            position.stop, position.target, position.score, 19,
            ", ".join(position.reasons),
        )
        return True

    def close_position(self, position: Position, price: float, reason: str) -> float:
        exit_side = "SELL" if position.side == "LONG" else "BUY"
        tick = position.instrument.tick_size
        fill = self._fill(price, exit_side, tick)

        for leg in position.legs.values():
            if not leg.closed:
                leg.exit_price = fill
                leg.exit_reason = reason
                leg.closed = True

        pnl = position.realised()
        log.info(
            "[PAPER] CLOSE %s %s @ %.2f (%s) | PnL %+,.0f (%+.2fR)",
            position.side, position.symbol, fill, reason, pnl, position._closed_r(),
        )
        return pnl

    def update_stop(self, position: Position, new_stop: float) -> None:
        log.info(
            "[PAPER] TRAIL %s stop %.2f -> %.2f", position.symbol, position.stop, new_stop
        )


class LiveExecutor(Executor):
    """Real orders.

    Entry is a market order per account. Immediately after a fill we place a
    server-side SL-M so the position is protected even if this process dies —
    that order is the real stop; the engine's own stop logic is the faster of
    the two, not the only one.
    """

    def __init__(self, cfg: ExecutionConfig, pool: AccountPool):
        self.cfg = cfg
        self.pool = pool

    def open_position(self, position: Position, sizes: dict[str, int], ref_price: float) -> bool:
        entry_side = "BUY" if position.side == "LONG" else "SELL"
        inst = position.instrument

        for account_name, qty in sizes.items():
            if qty <= 0:
                continue
            broker = self.pool.brokers.get(account_name)
            if broker is None or not broker.connected:
                continue

            result = broker.place_market_order(
                exchange=inst.exchange,
                trading_symbol=inst.trading_symbol,
                token=inst.token,
                side=entry_side,
                quantity=qty,
                product_type=self.cfg.product_type,
            )
            if not result.ok:
                log.error(
                    "[%s] Entry rejected for %s: %s", account_name, position.symbol,
                    result.message,
                )
                continue

            # Market orders fill at an unknown price; the reference price is a
            # working estimate until the trade book confirms the real average.
            leg = Leg(
                account=account_name,
                quantity=qty,
                entry_price=ref_price,
                entry_order_id=result.order_id,
            )
            position.legs[account_name] = leg

            if self.cfg.place_broker_stoploss:
                self._place_protective_stop(broker, position, leg)

        if not position.legs:
            log.error("No account accepted an entry for %s — signal dropped.", position.symbol)
            return False

        log.info(
            "[LIVE] OPEN %s %s x%d across %d account(s) | stop %.2f target %.2f | score %d",
            position.side, position.symbol, position.total_quantity, len(position.legs),
            position.stop, position.target, position.score,
        )
        return True

    def _place_protective_stop(self, broker, position: Position, leg: Leg) -> None:
        exit_side = "SELL" if position.side == "LONG" else "BUY"
        result = broker.place_stoploss_order(
            exchange=position.instrument.exchange,
            trading_symbol=position.instrument.trading_symbol,
            token=position.instrument.token,
            side=exit_side,
            quantity=leg.quantity,
            trigger_price=position.stop,
            product_type=self.cfg.product_type,
        )
        if result.ok:
            leg.sl_order_id = result.order_id
        else:
            log.error(
                "[%s] PROTECTIVE STOP REJECTED for %s (%s). The position is running "
                "unprotected at the broker — the engine stop is the only cover.",
                leg.account, position.symbol, result.message,
            )

    def update_stop(self, position: Position, new_stop: float) -> None:
        """Cancel-and-replace: SmartAPI's modify path for SL orders is
        inconsistent across varieties, so replacing is the reliable route."""
        for leg in position.legs.values():
            if leg.closed:
                continue
            broker = self.pool.brokers.get(leg.account)
            if broker is None or not broker.connected:
                continue

            if leg.sl_order_id:
                broker.cancel_order(leg.sl_order_id, variety="STOPLOSS")
                leg.sl_order_id = None

            if self.cfg.place_broker_stoploss:
                saved_stop = position.stop
                position.stop = new_stop
                self._place_protective_stop(broker, position, leg)
                position.stop = saved_stop

    def close_position(self, position: Position, price: float, reason: str) -> float:
        exit_side = "SELL" if position.side == "LONG" else "BUY"
        inst = position.instrument

        for leg in position.legs.values():
            if leg.closed:
                continue
            broker = self.pool.brokers.get(leg.account)
            if broker is None or not broker.connected:
                continue

            # Cancel protection first, or the SL-M can fire against a flat book.
            if leg.sl_order_id:
                broker.cancel_order(leg.sl_order_id, variety="STOPLOSS")
                leg.sl_order_id = None

            result = broker.place_market_order(
                exchange=inst.exchange,
                trading_symbol=inst.trading_symbol,
                token=inst.token,
                side=exit_side,
                quantity=leg.quantity,
                product_type=self.cfg.product_type,
            )
            if not result.ok:
                log.error(
                    "[%s] EXIT FAILED for %s x%d: %s — CLOSE THIS MANUALLY.",
                    leg.account, position.symbol, leg.quantity, result.message,
                )
                continue

            leg.exit_price = price
            leg.exit_reason = reason
            leg.closed = True

        pnl = position.realised()
        log.info(
            "[LIVE] CLOSE %s %s @ ~%.2f (%s) | PnL %+,.0f",
            position.side, position.symbol, price, reason, pnl,
        )
        return pnl

    def square_off_everything(self) -> None:
        for broker in self.pool.connected_brokers:
            count = broker.square_off_all_intraday()
            if count:
                log.info("[%s] Squared off %d intraday position(s).", broker.name, count)


def build_executor(cfg: ExecutionConfig, pool: AccountPool | None, account_names: list[str]) -> Executor:
    if cfg.mode == "live":
        if pool is None:
            raise RuntimeError("Live mode needs a connected account pool.")
        return LiveExecutor(cfg, pool)
    return PaperExecutor(cfg, account_names)
