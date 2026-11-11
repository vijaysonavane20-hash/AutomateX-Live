"""The live/paper trading engine.

Loop shape, once per `session.poll_seconds`:

1. Roll the risk day if the date changed.
2. Manage open positions first — exits always take priority over entries.
3. On a newly closed bar, refresh candles and look for entries.
4. At `session.square_off`, flatten everything and stop.

Exits are checked twice over: against the last closed bar's high/low (which
catches a level touched inside the bar) and against the live LTP between bars.
"""
from __future__ import annotations

import csv
import json
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .broker import AccountPool
from .config import AppConfig
from .data import DataFeed
from .execution import Executor, build_executor
from .instruments import InstrumentMaster
from .notifier import Notifier, NullNotifier, build_notifier
from .positions import Position
from .risk import RiskManager
from .screener import Screener, format_shortlist
from . import notifier as notify
from .strategy import Evaluation, Signal, evaluate, prepare
from .utils import MarketCalendar, combine_ist, get_logger, now_ist

log = get_logger("engine")

TRADE_LOG_FIELDS = [
    "symbol", "side", "entry_time", "exit_time", "avg_entry", "initial_stop",
    "final_stop", "target", "quantity", "score", "reasons", "exit_reason",
    "pnl", "r_multiple",
]


class TradingEngine:
    def __init__(self, cfg: AppConfig, allow_live: bool = False):
        self.cfg = cfg
        self.calendar = MarketCalendar(cfg.holidays)
        self.pool = AccountPool(cfg.trading_accounts)
        self.risk = RiskManager(
            cfg.risk, cfg.total_capital, cfg.execution.leverage_multiple
        )
        self.shortlist: list = []
        # Trading symbols this engine has opened today. Persisted, so a
        # restart still knows what is its own to close — and what is yours.
        self.owned_symbols: set[str] = set()

        self.instruments: dict = {}
        self.feed: DataFeed | None = None
        self.executor: Executor | None = None
        self.notifier: Notifier = NullNotifier()

        self.open_positions: dict[str, Position] = {}
        self.closed_positions: list[Position] = []
        self.last_bar_seen: dict[str, pd.Timestamp] = {}

        self._stop_requested = False
        self._squared_off = False

        # Live trading needs the config flag AND the command-line switch.
        # Either one alone leaves the engine in paper mode.
        self.is_live = (
            cfg.execution.mode == "live" and cfg.execution.confirm_live and allow_live
        )
        if cfg.execution.mode == "live" and not self.is_live:
            log.warning(
                "Config requests live mode but %s — running in PAPER mode.",
                "the --live flag was not passed" if cfg.execution.confirm_live
                else "execution.confirm_live is false",
            )

        self.state_dir = Path(cfg.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.trade_log = self.state_dir / f"trades_{now_ist().date().isoformat()}.csv"
        self.owned_file = self.state_dir / f"owned_{now_ist().date().isoformat()}.json"

    # ------------------------------------------------------- owned positions

    def _load_owned(self) -> None:
        """Recover the set of positions this engine opened today.

        Without this, a crash and restart would leave the engine unable to
        tell its own open positions from ones you placed by hand — and it
        would either abandon its own or close yours.
        """
        if not self.owned_file.exists():
            return
        try:
            self.owned_symbols = set(json.loads(self.owned_file.read_text()))
            if self.owned_symbols:
                log.info(
                    "Recovered %d position(s) opened earlier today: %s",
                    len(self.owned_symbols), ", ".join(sorted(self.owned_symbols)),
                )
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s) — starting with none.", self.owned_file, exc)

    def _remember_owned(self, trading_symbol: str) -> None:
        self.owned_symbols.add(trading_symbol)
        try:
            self.owned_file.write_text(json.dumps(sorted(self.owned_symbols)))
        except OSError as exc:
            log.warning("Could not persist owned positions (%s).", exc)

    # ----------------------------------------------------------------- setup

    def setup(self) -> None:
        self.notifier = build_notifier(self.cfg)
        self._load_owned()

        log.info("=" * 78)
        log.info("MODE: %s", "LIVE — REAL MONEY" if self.is_live else "PAPER (simulated)")
        log.info(
            "Accounts: %s | Combined capital: %s",
            ", ".join(a.name for a in self.cfg.trading_accounts),
            f"{self.cfg.total_capital:,.0f}",
        )
        log.info("=" * 78)

        self.pool.connect_all(self.cfg.data_account.name)

        master = InstrumentMaster(self.cfg.state_dir)
        master.load()
        self.instruments = self._select_symbols(master)
        if not self.instruments:
            log.warning("No tradeable symbols today — the engine will idle.")
        else:
            log.info("Tracking %d symbol(s): %s", len(self.instruments),
                     ", ".join(self.instruments))

        self.feed = DataFeed(
            self.pool.data, self.instruments,
            interval=self.cfg.strategy.entry_interval,
            warmup_days=self.cfg.warmup_days,
        )
        self.feed.warmup()

        self.executor = build_executor(
            self.cfg.execution,
            self.pool if self.is_live else None,
            [a.name for a in self.cfg.trading_accounts],
        )

        if self.is_live:
            for broker in self.pool.connected_brokers:
                margin = broker.available_margin()
                log.info("[%s] Available margin: %s", broker.name,
                         f"{margin:,.0f}" if margin is not None else "unknown")
                stale = broker.open_intraday_positions()
                if stale:
                    names = ", ".join(str(p.get("tradingsymbol", "?")) for p in stale)
                    unknown = [
                        str(p.get("tradingsymbol", "?"))
                        for p in stale
                        if str(p.get("tradingsymbol", "")) not in self.owned_symbols
                    ]
                    log.warning(
                        "[%s] %d intraday position(s) already open: %s",
                        broker.name, len(stale), names,
                    )
                    if unknown and not self.cfg.execution.square_off_untracked:
                        log.warning(
                            "[%s] %s were not opened by this engine. They will be left "
                            "alone, including at the square-off — close them yourself.",
                            broker.name, ", ".join(unknown),
                        )
                    elif unknown:
                        log.warning(
                            "[%s] square_off_untracked is true, so %s WILL be closed at "
                            "the cutoff even though this engine did not open them.",
                            broker.name, ", ".join(unknown),
                        )

                holdings = broker.holdings_symbols()
                clash = holdings & {i.trading_symbol for i in self.instruments.values()}
                if clash:
                    log.warning(
                        "[%s] Today's list includes %s, which you hold in delivery. "
                        "Intraday orders are a separate product and will not sell your "
                        "holdings, but add them to exclude_symbols if you would rather "
                        "the engine left them out entirely.",
                        broker.name, ", ".join(sorted(clash)),
                    )

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self.notifier.send(
            notify.fmt_startup(
                "LIVE" if self.is_live else "PAPER",
                len(self.pool.connected_brokers),
                self.cfg.total_capital,
                len(self.instruments),
            )
        )

    def _position_budget(self) -> float:
        """Rupee value one position may occupy, taken from the *smallest*
        enabled account so the shortlist is tradeable by every account."""
        smallest = min(a.capital for a in self.cfg.trading_accounts)
        return smallest * self.cfg.risk.max_position_value_pct / 100.0

    def _select_symbols(self, master: InstrumentMaster) -> dict:
        """Today's tradeable set: screened each morning, or the fixed list."""
        excluded = {s.strip().upper() for s in self.cfg.exclude_symbols}

        if not self.cfg.screener.enabled:
            wanted = [s for s in self.cfg.symbols if s.upper() not in excluded]
            return master.resolve_all(wanted, self.cfg.exchange)

        screener = Screener(self.pool.data, master, self.cfg.screener, self.cfg.exchange)
        budget = self._position_budget()
        log.info(
            "Screening for today (per-position budget ₹%s, leverage %.1fx).",
            f"{budget:,.0f}", self.cfg.execution.leverage_multiple,
        )

        try:
            results = screener.shortlist(budget)
        except Exception as exc:
            log.error("Screening failed (%s).", exc)
            if self.cfg.symbols:
                log.warning("Falling back to the configured 'symbols' list.")
                wanted = [s for s in self.cfg.symbols if s.upper() not in excluded]
                return master.resolve_all(wanted, self.cfg.exchange)
            raise

        dropped = [r.symbol for r in results if r.symbol.upper() in excluded]
        if dropped:
            log.info("Excluded by config (exclude_symbols): %s", ", ".join(dropped))
        results = [r for r in results if r.symbol.upper() not in excluded]

        self.shortlist = results
        self.notifier.send(format_shortlist(results))
        return {r.symbol: r.instrument for r in results}

    def _deployed_value(self, account_name: str) -> float:
        """What this account already holds at cost, across open positions.
        Subtracted from buying power so positions cannot each claim the full
        balance and quietly add up to leverage."""
        total = 0.0
        for position in self.open_positions.values():
            leg = position.legs.get(account_name)
            if leg is not None and not leg.closed:
                total += leg.entry_price * leg.quantity
        return total

    def _handle_signal(self, signum, _frame) -> None:
        log.warning("Signal %s received — shutting down after the current cycle.", signum)
        self._stop_requested = True

    # ------------------------------------------------------------- main loop

    def run(self) -> None:
        today = now_ist().date()
        if not self.calendar.is_trading_day(today):
            log.info("%s is not a trading day. Nothing to do.", today)
            return

        square_off_at = combine_ist(today, self.cfg.session.square_off)
        log.info("Session running. Square-off at %s.", square_off_at.strftime("%H:%M"))

        while not self._stop_requested:
            try:
                now = now_ist()

                if now >= square_off_at:
                    self._square_off_all("session cutoff")
                    break

                if not self.calendar.is_market_open(now):
                    if now.time() < self.cfg.session.entry_start:
                        log.info("Waiting for the market to open...")
                        time.sleep(30)
                        continue
                    log.info("Market closed. Ending session.")
                    break

                self._tick(now)
            except Exception as exc:
                # A single bad cycle must never take the process down while
                # positions are open — log it and carry on to the next tick.
                log.exception("Unhandled error in cycle; continuing.")
                if self.cfg.notifications.notify_errors:
                    self.notifier.send(notify.fmt_error(f"Cycle error: {exc}"))

            time.sleep(self.cfg.session.poll_seconds)

        self.shutdown()

    def _tick(self, now: datetime) -> None:
        self.risk.roll_day_if_needed()
        assert self.feed is not None

        new_bar = self._refresh_if_new_bar(now)

        if self.open_positions:
            self._manage_positions(now, new_bar)

        if new_bar and self._entries_allowed(now):
            self._look_for_entries(now)

    def _has_unevaluated_bar(self, now: datetime) -> bool:
        assert self.feed is not None
        for symbol in self.instruments:
            bar = self.feed.last_closed_bar(symbol, now)
            if bar is not None and self.last_bar_seen.get(symbol) != bar.name:
                return True
        return False

    def _refresh_if_new_bar(self, now: datetime) -> bool:
        """Refresh candles shortly after each bar close. Returns True if any
        symbol now has a closed bar we have not evaluated yet."""
        assert self.feed is not None
        buffer_s = self.cfg.session.bar_close_buffer_seconds

        # Bars are anchored to the 09:15 open, so seconds-since-close must be
        # derived from that anchor. Using `now.minute % interval` only happens
        # to work for 5-minute bars and is wrong for 3, 10 or 30.
        last_close = self.feed.next_bar_close(now) - timedelta(
            minutes=self.feed.interval_minutes
        )
        seconds_since_close = (now - last_close).total_seconds()
        window_end = buffer_s + self.cfg.session.poll_seconds + 5

        if not (buffer_s <= seconds_since_close <= window_end):
            # Not just after a close; still report a bar left over from
            # start-up or from a cycle that could not act on it.
            return self._has_unevaluated_bar(now)

        self.feed.refresh_all()
        return self._has_unevaluated_bar(now)

    def _entries_allowed(self, now: datetime) -> bool:
        sess = self.cfg.session
        if not (sess.entry_start <= now.time() < sess.entry_end):
            return False
        # Per-symbol and per-count limits are enforced in _look_for_entries;
        # here we only need the day-level halt.
        self.risk.roll_day_if_needed()
        return not self.risk.state.halted

    # ------------------------------------------------------------- positions

    def _mark_price(self, symbol: str, fallback: float) -> float:
        inst = self.instruments[symbol]
        ltp = self.pool.data.ltp(inst.exchange, inst.trading_symbol, inst.token)
        return ltp if ltp else fallback

    def _manage_positions(self, now: datetime, new_bar: bool) -> None:
        assert self.feed is not None and self.executor is not None

        for symbol, position in list(self.open_positions.items()):
            bar = self.feed.last_closed_bar(symbol, now)
            mark = self._mark_price(symbol, position.entry_ref if bar is None else float(bar["close"]))

            # Widen the touched range with the bar's own extremes, but only for
            # bars that formed after entry.
            high = low = mark
            if bar is not None and bar.name.to_pydatetime() >= position.entry_time:
                high = max(high, float(bar["high"]))
                low = min(low, float(bar["low"]))

            position.update_watermarks(high, low)

            if position.stop_hit(low, high):
                self._close(position, position.stop, "stop")
                continue
            if position.target_hit(low, high):
                self._close(position, position.target, "target")
                continue

            new_stop = position.next_stop(self.cfg.strategy)
            if new_stop is not None:
                self.executor.update_stop(position, new_stop)
                position.stop = new_stop
                log.info("%s stop moved to %.2f (%.2fR locked)", symbol, new_stop,
                         position.r_multiple(new_stop))

    def _close(self, position: Position, price: float, reason: str) -> None:
        assert self.executor is not None
        pnl = self.executor.close_position(position, price, reason)
        position.closed = True
        position.exit_time = now_ist()
        position.exit_reason = reason

        self.open_positions.pop(position.symbol, None)
        self.closed_positions.append(position)

        was_halted = self.risk.state.halted
        self.risk.record_exit(pnl)
        self._append_trade_log(position)
        log.info("Risk status: %s", self.risk.summary())

        if self.cfg.notifications.notify_exits:
            self.notifier.send(notify.fmt_exit(position, pnl, reason))
        if self.risk.state.halted and not was_halted:
            self.notifier.send(
                notify.fmt_halt(self.risk.state.halt_reason, self.risk.state.realised_pnl)
            )

    # ---------------------------------------------------------------- entries

    def _look_for_entries(self, now: datetime) -> None:
        assert self.feed is not None and self.executor is not None

        for symbol, inst in self.instruments.items():
            bars = self.feed.closed_bars(symbol, now)
            if bars.empty:
                continue

            last_ts = bars.index[-1]
            if self.last_bar_seen.get(symbol) == last_ts:
                continue
            self.last_bar_seen[symbol] = last_ts

            if symbol in self.open_positions:
                continue

            allowed, reason = self.risk.check_entry(symbol, len(self.open_positions))
            if not allowed:
                log.debug("%s skipped: %s", symbol, reason)
                continue

            enriched = prepare(bars, self.cfg.strategy, self.cfg.session.opening_range_minutes)
            if len(enriched) < 60:
                continue

            result: Evaluation = evaluate(
                enriched.iloc[-1], symbol, self.cfg.strategy, inst.tick_size
            )
            if result.signal is None:
                log.debug(
                    "%s no setup (L=%d S=%d): %s",
                    symbol, result.long_score, result.short_score, result.rejected_by,
                )
                continue

            self._enter(result.signal)

    def _enter(self, sig: Signal) -> None:
        assert self.executor is not None
        inst = self.instruments[sig.symbol]

        sizes: dict[str, int] = {}
        for account in self.cfg.trading_accounts:
            broker = self.pool.brokers.get(account.name)
            cash = (
                broker.available_margin() if (self.is_live and broker and broker.connected) else None
            )
            qty, bound = self.risk.position_size(
                account, sig.entry_ref, sig.stop, inst.lot_size,
                available_cash=cash,
                deployed_value=self._deployed_value(account.name),
            )
            if qty > 0:
                sizes[account.name] = qty
            else:
                log.warning("[%s] %s not sized: %s", account.name, sig.symbol, bound)

        if not sizes:
            log.warning("%s signal dropped — no account could size it.", sig.symbol)
            return

        position = Position.from_signal(sig, inst)
        if not self.executor.open_position(position, sizes, sig.entry_ref):
            return

        self.open_positions[sig.symbol] = position
        self.risk.record_entry(sig.symbol)
        self._remember_owned(inst.trading_symbol)

        if self.cfg.notifications.notify_entries:
            self.notifier.send(notify.fmt_entry(position, position.total_quantity))

        log.info(
            "ENTERED %s %s | R:R %.2f | risk/unit %.2f | %s",
            sig.side, sig.symbol, sig.reward_risk, sig.risk_per_unit, self.risk.summary(),
        )

    # --------------------------------------------------------------- teardown

    def _square_off_all(self, reason: str) -> None:
        if self._squared_off:
            return
        self._squared_off = True

        if self.open_positions:
            log.info("Squaring off %d open position(s): %s", len(self.open_positions), reason)
            for symbol, position in list(self.open_positions.items()):
                bar = self.feed.last_closed_bar(symbol, now_ist()) if self.feed else None
                fallback = float(bar["close"]) if bar is not None else position.entry_ref
                self._close(position, self._mark_price(symbol, fallback), reason)

        if self.is_live and self.executor is not None:
            # Belt and braces: flatten anything the broker still shows open,
            # including legs whose bookkeeping drifted from reality. By
            # default this is limited to symbols this engine opened, so
            # trades you placed by hand in the same account are left alone.
            only = None if self.cfg.execution.square_off_untracked else self.owned_symbols
            self.executor.square_off_everything(only)

    def _append_trade_log(self, position: Position) -> None:
        exists = self.trade_log.exists()
        with self.trade_log.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_LOG_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(position.to_record())

    def shutdown(self) -> None:
        self._square_off_all("shutdown")
        self.pool.disconnect_all()
        self.report()

        if self.cfg.notifications.daily_summary:
            pnls = [p.realised() for p in self.closed_positions]
            self.notifier.send(
                notify.fmt_summary(
                    len(pnls), sum(1 for p in pnls if p > 0), sum(pnls),
                    "LIVE" if self.is_live else "PAPER",
                )
            )
        # Flush queued messages before the process exits.
        self.notifier.close()

    def report(self) -> None:
        trades = self.closed_positions
        log.info("=" * 78)
        log.info("SESSION SUMMARY (%s)", "LIVE" if self.is_live else "PAPER")
        if not trades:
            log.info("No trades taken. %s", self.risk.summary())
            log.info("=" * 78)
            return

        pnls = [p.realised() for p in trades]
        wins = [p for p in pnls if p > 0]
        gross_win = sum(wins)
        gross_loss = -sum(p for p in pnls if p <= 0)

        log.info("Trades         : %d", len(trades))
        log.info("Win rate       : %.1f%% (%d W / %d L)",
                 100.0 * len(wins) / len(trades), len(wins), len(trades) - len(wins))
        log.info("Net PnL        : %+,.0f", sum(pnls))
        log.info("Average trade  : %+,.0f", sum(pnls) / len(trades))
        log.info("Profit factor  : %s",
                 f"{gross_win / gross_loss:.2f}" if gross_loss > 0 else "n/a (no losses)")
        log.info("Trade log      : %s", self.trade_log)
        log.info("=" * 78)
