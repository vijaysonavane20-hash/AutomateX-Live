"""Portfolio backtester for the same strategy the live engine trades.

Honesty features — the reason a backtest is worth running at all:

* **No lookahead.** A signal is computed on a bar's close and filled at the
  *next* bar's open, exactly as the live engine behaves when it wakes just
  after a bar closes.
* **Slippage on every fill**, in the direction that hurts.
* **Stop-before-target.** When a bar's range covers both levels, the loss is
  assumed. Intraday bars hide their path; assuming the good outcome is the
  single most common way a backtest lies.
* **Portfolio-level limits.** Max open positions, max trades/day and the daily
  loss halt all apply across symbols, so the results reflect the capital you
  actually have rather than each symbol trading in its own universe.

Results are still an optimistic upper bound: no queue position, no partial
fills, no widened spreads at the moment everyone else's stops trigger.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import AppConfig, load_config
from .strategy import MAX_SCORE, Signal, evaluate, prepare
from .utils import IST, get_logger, now_ist, round_to_tick, setup_logging

log = get_logger("backtest")


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_time: datetime
    entry_price: float
    stop: float
    target: float
    quantity: int
    score: int
    reasons: list[str]
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    initial_risk: float = 0.0

    high_water: float = 0.0
    low_water: float = 0.0
    breakeven_done: bool = False

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.quantity * self.direction

    @property
    def r_multiple(self) -> float:
        if self.exit_price is None or self.initial_risk <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) * self.direction / self.initial_risk


@dataclass
class Results:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    starting_capital: float = 0.0
    skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.exit_price is not None]
        if not closed:
            return {"trades": 0}

        pnls = np.array([t.pnl for t in closed])
        rs = np.array([t.r_multiple for t in closed])
        wins, losses = pnls[pnls > 0], pnls[pnls <= 0]

        equity = np.array([e for _, e in self.equity_curve]) if self.equity_curve else np.array([self.starting_capital])
        peak = np.maximum.accumulate(equity)
        drawdown = (equity - peak) / np.where(peak == 0, 1, peak)

        gross_win, gross_loss = wins.sum(), -losses.sum()
        return {
            "trades": len(closed),
            "win_rate_pct": 100.0 * len(wins) / len(closed),
            "net_pnl": pnls.sum(),
            "return_pct": 100.0 * pnls.sum() / self.starting_capital,
            "avg_win": wins.mean() if len(wins) else 0.0,
            "avg_loss": losses.mean() if len(losses) else 0.0,
            "expectancy_r": rs.mean(),
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "max_drawdown_pct": 100.0 * drawdown.min() if len(drawdown) else 0.0,
            "best_r": rs.max(),
            "worst_r": rs.min(),
            "avg_score": np.mean([t.score for t in closed]),
        }


class Backtester:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.capital = cfg.total_capital

    def run(self, data: dict[str, pd.DataFrame], tick: float = 0.05) -> Results:
        strat, risk, sess = self.cfg.strategy, self.cfg.risk, self.cfg.session
        results = Results(starting_capital=self.capital)

        enriched = {}
        for symbol, df in data.items():
            if df is None or df.empty or len(df) < 80:
                log.warning("Skipping %s — needs at least 80 bars, got %d.",
                            symbol, 0 if df is None else len(df))
                continue
            enriched[symbol] = prepare(df, strat, sess.opening_range_minutes)
        if not enriched:
            raise RuntimeError("No symbol had enough data to backtest.")

        timeline = sorted(set().union(*(set(df.index) for df in enriched.values())))
        rows = {s: df for s, df in enriched.items()}

        open_trades: dict[str, BacktestTrade] = {}
        pending: dict[str, Signal] = {}
        equity = self.capital

        day = None
        day_pnl = 0.0
        day_trades = 0
        day_symbol_trades: dict[str, int] = defaultdict(int)
        halted = False

        for ts in timeline:
            if day != ts.date():
                day, day_pnl, day_trades, halted = ts.date(), 0.0, 0, False
                day_symbol_trades = defaultdict(int)

            # --- 1. manage open trades on this bar -----------------------
            for symbol, trade in list(open_trades.items()):
                if ts not in rows[symbol].index:
                    continue
                bar = rows[symbol].loc[ts]
                exit_price, reason = self._check_exit(trade, bar, ts, sess, strat, tick)
                if exit_price is not None:
                    trade.exit_price, trade.exit_time, trade.exit_reason = exit_price, ts, reason
                    equity += trade.pnl
                    day_pnl += trade.pnl
                    open_trades.pop(symbol)
                    results.equity_curve.append((ts, equity))
                    if day_pnl <= -self.capital * risk.max_daily_loss_pct / 100.0:
                        halted = True
                    elif day_pnl >= self.capital * risk.max_daily_profit_pct / 100.0:
                        halted = True

            # --- 2. fill entries signalled on the previous bar -----------
            for symbol, sig in list(pending.items()):
                if ts not in rows[symbol].index:
                    continue
                pending.pop(symbol)
                if halted or symbol in open_trades:
                    continue
                if len(open_trades) >= risk.max_open_positions:
                    results.skipped["max_open_positions"] += 1
                    continue

                bar = rows[symbol].loc[ts]
                fill = self._slip(float(bar["open"]), sig.side == "LONG", tick)
                risk_per_unit = abs(fill - sig.stop)
                if risk_per_unit <= 0:
                    continue

                # A gap through the stop means the setup is already invalid.
                if (sig.side == "LONG" and fill <= sig.stop) or (
                    sig.side == "SHORT" and fill >= sig.stop
                ):
                    results.skipped["gapped_past_stop"] += 1
                    continue

                budget = self.capital * risk.risk_per_trade_pct / 100.0
                qty = int(min(budget / risk_per_unit,
                              self.capital * risk.max_position_value_pct / 100.0 / fill))
                if qty <= 0:
                    results.skipped["sized_to_zero"] += 1
                    continue

                trade = BacktestTrade(
                    symbol=symbol, side=sig.side, entry_time=ts, entry_price=fill,
                    stop=sig.stop, target=sig.target, quantity=qty, score=sig.score,
                    reasons=list(sig.reasons), initial_risk=risk_per_unit,
                    high_water=fill, low_water=fill,
                )
                open_trades[symbol] = trade
                results.trades.append(trade)
                day_trades += 1
                day_symbol_trades[symbol] += 1

            # --- 3. look for new signals on this closed bar --------------
            if halted or day_trades >= risk.max_trades_per_day:
                continue
            if not (sess.entry_start <= ts.time() < sess.entry_end):
                continue

            for symbol, df in rows.items():
                if ts not in df.index or symbol in open_trades or symbol in pending:
                    continue
                if day_symbol_trades[symbol] >= risk.max_trades_per_symbol_per_day:
                    continue

                result = evaluate(df.loc[ts], symbol, strat, tick)
                if result.signal is not None:
                    pending[symbol] = result.signal

        # Any trade still open at the end is closed at the last available price.
        for symbol, trade in open_trades.items():
            last = rows[symbol].iloc[-1]
            trade.exit_price = float(last["close"])
            trade.exit_time = rows[symbol].index[-1]
            trade.exit_reason = "end of data"
            equity += trade.pnl
            results.equity_curve.append((trade.exit_time, equity))

        return results

    def _slip(self, price: float, is_buy: bool, tick: float) -> float:
        adj = price * self.cfg.execution.slippage_bps / 10_000.0
        return round_to_tick(price + adj if is_buy else price - adj, tick)

    def _check_exit(self, trade, bar, ts, sess, strat, tick) -> tuple[float | None, str]:
        high, low = float(bar["high"]), float(bar["low"])
        trade.high_water = max(trade.high_water, high)
        trade.low_water = min(trade.low_water, low)
        long_side = trade.side == "LONG"

        # Stop is evaluated before target: when a bar covers both, assume the
        # worse path. Optimism here is what makes backtests unrepeatable live.
        if (low <= trade.stop) if long_side else (high >= trade.stop):
            return self._slip(trade.stop, not long_side, tick), "stop"
        if (high >= trade.target) if long_side else (low <= trade.target):
            return self._slip(trade.target, not long_side, tick), "target"
        if ts.time() >= sess.square_off:
            return self._slip(float(bar["close"]), not long_side, tick), "square off"

        # Trail after the bar's levels have been checked, never before.
        atr_val = float(bar.get("atr", 0.0) or 0.0)
        if atr_val > 0:
            extreme = trade.high_water if long_side else trade.low_water
            r_now = (extreme - trade.entry_price) * trade.direction / trade.initial_risk
            if not trade.breakeven_done and r_now >= strat.breakeven_at_r:
                trade.stop = trade.entry_price
                trade.breakeven_done = True
            if trade.breakeven_done:
                trail = (
                    trade.high_water - strat.trail_atr_multiple * atr_val
                    if long_side
                    else trade.low_water + strat.trail_atr_multiple * atr_val
                )
                trade.stop = max(trade.stop, trail) if long_side else min(trade.stop, trail)
        return None, ""


# ------------------------------------------------------------------ reporting


def print_report(results: Results, cfg: AppConfig) -> None:
    stats = results.summary()
    line = "=" * 74
    print(f"\n{line}\nBACKTEST RESULTS\n{line}")

    if stats.get("trades", 0) == 0:
        print("No trades were taken.")
        print(
            "\nThe gates are strict by design. If this is empty over a long window, "
            "lower strategy.min_score (currently "
            f"{cfg.strategy.min_score}/{MAX_SCORE}) or strategy.adx_min "
            f"(currently {cfg.strategy.adx_min}) and re-run."
        )
        return

    print(f"Capital           : {results.starting_capital:>14,.0f}")
    print(f"Trades            : {stats['trades']:>14d}")
    print(f"Win rate          : {stats['win_rate_pct']:>13.1f}%")
    print(f"Net PnL           : {stats['net_pnl']:>+14,.0f}")
    print(f"Return            : {stats['return_pct']:>+13.2f}%")
    print(f"Profit factor     : {stats['profit_factor']:>14.2f}")
    print(f"Expectancy        : {stats['expectancy_r']:>+13.3f}R per trade")
    print(f"Average win       : {stats['avg_win']:>+14,.0f}")
    print(f"Average loss      : {stats['avg_loss']:>+14,.0f}")
    print(f"Best / worst      : {stats['best_r']:>+8.2f}R / {stats['worst_r']:+.2f}R")
    print(f"Max drawdown      : {stats['max_drawdown_pct']:>13.2f}%")
    print(f"Average score     : {stats['avg_score']:>14.1f} / {MAX_SCORE}")

    closed = [t for t in results.trades if t.exit_price is not None]

    print(f"\n{'By exit reason':<20}{'n':>6}{'win%':>8}{'net':>14}")
    by_reason: dict[str, list] = defaultdict(list)
    for t in closed:
        by_reason[t.exit_reason].append(t)
    for reason, group in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for t in group if t.pnl > 0)
        print(f"{reason:<20}{len(group):>6}{100.0 * wins / len(group):>7.1f}%"
              f"{sum(t.pnl for t in group):>+14,.0f}")

    print(f"\n{'By symbol':<20}{'n':>6}{'win%':>8}{'net':>14}")
    by_symbol: dict[str, list] = defaultdict(list)
    for t in closed:
        by_symbol[t.symbol].append(t)
    for symbol, group in sorted(by_symbol.items(), key=lambda kv: -sum(t.pnl for t in kv[1])):
        wins = sum(1 for t in group if t.pnl > 0)
        print(f"{symbol:<20}{len(group):>6}{100.0 * wins / len(group):>7.1f}%"
              f"{sum(t.pnl for t in group):>+14,.0f}")

    # Does a higher score actually mean a better trade? If this table is flat,
    # the scoring is not earning its keep and min_score is arbitrary.
    print(f"\n{'By score bucket':<20}{'n':>6}{'win%':>8}{'avg R':>10}")
    buckets: dict[str, list] = defaultdict(list)
    for t in closed:
        buckets[f"{t.score}-{t.score + 1}" if t.score < MAX_SCORE else str(MAX_SCORE)].append(t)
    for bucket, group in sorted(buckets.items()):
        wins = sum(1 for t in group if t.pnl > 0)
        avg_r = float(np.mean([t.r_multiple for t in group]))
        print(f"{bucket:<20}{len(group):>6}{100.0 * wins / len(group):>7.1f}%{avg_r:>+10.3f}")

    if results.skipped:
        print("\nSignals dropped:")
        for reason, count in sorted(results.skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {reason:<24}{count:>6}")

    print(f"\n{line}")
    print(
        "These are simulated results on past data. Slippage, liquidity and the\n"
        "market's behaviour all change. Forward-test in paper mode before risking\n"
        "capital, and expect live results to be worse than this."
    )
    print(line)


def save_trades(results: Results, path: Path) -> None:
    rows = [
        {
            "symbol": t.symbol, "side": t.side,
            "entry_time": t.entry_time, "exit_time": t.exit_time,
            "entry": round(t.entry_price, 2), "exit": round(t.exit_price or 0, 2),
            "stop": round(t.stop, 2), "target": round(t.target, 2),
            "qty": t.quantity, "score": t.score, "exit_reason": t.exit_reason,
            "pnl": round(t.pnl, 2), "r_multiple": round(t.r_multiple, 3),
            "reasons": "|".join(t.reasons),
        }
        for t in results.trades if t.exit_price is not None
    ]
    if rows:
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"\nTrade-by-trade detail written to {path}")


# ------------------------------------------------------------------ data load


def fetch_history(cfg: AppConfig, days: int) -> dict[str, pd.DataFrame]:
    """Pull candles from Angel in chunks (their per-request window is limited)."""
    from .broker import AccountPool
    from .instruments import InstrumentMaster

    pool = AccountPool(cfg.trading_accounts)
    pool.connect_all(cfg.data_account.name)
    try:
        master = InstrumentMaster(cfg.state_dir)
        master.load()
        instruments = master.resolve_all(cfg.symbols, cfg.exchange)

        end = now_ist()
        start = end - timedelta(days=days)
        out: dict[str, pd.DataFrame] = {}

        for symbol, inst in instruments.items():
            frames, cursor = [], start
            while cursor < end:
                chunk_end = min(cursor + timedelta(days=25), end)
                try:
                    frames.append(
                        pool.data.candles(
                            inst.exchange, inst.token,
                            cfg.strategy.entry_interval, cursor, chunk_end,
                        )
                    )
                except Exception as exc:
                    log.warning("Chunk fetch failed for %s: %s", symbol, exc)
                cursor = chunk_end

            if frames:
                df = pd.concat(frames)
                df = df[~df.index.duplicated(keep="last")].sort_index()
                out[symbol] = df
                log.info("%s: %d bars", symbol, len(df))
        return out
    finally:
        pool.disconnect_all()


def load_csv_dir(directory: Path) -> dict[str, pd.DataFrame]:
    """Load `<SYMBOL>.csv` files with timestamp/open/high/low/close/volume."""
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(directory.glob("*.csv")):
        df = pd.read_csv(path)
        cols = {c.lower().strip(): c for c in df.columns}
        ts_col = next((cols[c] for c in ("timestamp", "date", "datetime", "time") if c in cols), None)
        if ts_col is None:
            log.warning("%s has no timestamp column — skipped.", path.name)
            continue

        df["timestamp"] = pd.to_datetime(df[ts_col])
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(IST)

        df = df.rename(columns={cols[c]: c for c in ("open", "high", "low", "close", "volume") if c in cols})
        missing = {"open", "high", "low", "close", "volume"} - set(df.columns)
        if missing:
            log.warning("%s is missing %s — skipped.", path.name, sorted(missing))
            continue

        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]].astype(float)
        out[path.stem.upper()] = df[~df.index.duplicated(keep="last")].sort_index()
        log.info("%s: %d bars from %s", path.stem.upper(), len(df), path.name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the intraday strategy.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=60, help="Calendar days of history to fetch.")
    parser.add_argument("--csv-dir", type=Path, help="Backtest offline from <SYMBOL>.csv files instead.")
    parser.add_argument("--out", type=Path, default=Path("state/backtest_trades.csv"))
    parser.add_argument("--min-score", type=int, help="Override strategy.min_score for this run.")
    args = parser.parse_args()

    setup_logging(level="INFO")
    needs_broker = args.csv_dir is None
    cfg = load_config(args.config, load_secrets=needs_broker)
    if args.min_score is not None:
        cfg.strategy.min_score = args.min_score

    data = load_csv_dir(args.csv_dir) if args.csv_dir else fetch_history(cfg, args.days)
    if not data:
        print("No data loaded.")
        return 1

    results = Backtester(cfg).run(data)
    print_report(results, cfg)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    save_trades(results, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
