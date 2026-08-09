"""Tests for the trading system that run without credentials or a network.

Synthetic sessions are generated with a known character (trending up, trending
down, chopping) so the gates can be checked against what they are supposed to
reject. The lookahead test is the important one: it proves a signal computed on
a bar cannot change once later bars arrive, which is what makes the backtest
comparable to live behaviour.

    python -m pytest tests/test_trading.py -v
    python tests/test_trading.py            # runs without pytest installed
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading import indicators as ind
from trading.config import (
    AccountConfig, AppConfig, ExecutionConfig, RiskConfig, SessionConfig, StrategyConfig,
)
from trading.instruments import Instrument
from trading.positions import Leg, Position
from trading.risk import RiskManager
from trading.strategy import MAX_SCORE, evaluate, prepare
from trading.utils import IST, MarketCalendar, RateLimiter, round_to_tick


# --------------------------------------------------------------- fixtures

def make_session(
    days: int = 12,
    drift: float = 0.0,
    noise: float = 0.0025,
    start_price: float = 1000.0,
    seed: int = 7,
    bars_per_day: int = 75,
) -> pd.DataFrame:
    """Synthetic 5-minute NSE sessions (09:15-15:30 = 75 bars)."""
    rng = np.random.default_rng(seed)
    rows, price = [], start_price
    day = datetime(2025, 1, 6, 9, 15, tzinfo=IST)  # a Monday

    for d in range(days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        ts = day
        for _ in range(bars_per_day):
            ret = drift + rng.normal(0, noise)
            open_p = price
            close_p = max(open_p * (1 + ret), 1.0)
            wick = abs(rng.normal(0, noise)) * open_p
            rows.append(
                {
                    "timestamp": ts,
                    "open": open_p,
                    "high": max(open_p, close_p) + wick,
                    "low": min(open_p, close_p) - wick,
                    "close": close_p,
                    "volume": float(rng.integers(5_000, 60_000)),
                }
            )
            price = close_p
            ts += timedelta(minutes=5)
        day += timedelta(days=1)

    return pd.DataFrame(rows).set_index("timestamp").astype(float)


def default_config(**overrides) -> AppConfig:
    cfg = AppConfig(
        accounts=[AccountConfig(name="test", env_prefix="TEST", capital=200_000.0)],
        symbols=["TESTSYM"],
        strategy=StrategyConfig(),
        risk=RiskConfig(),
        session=SessionConfig(),
        execution=ExecutionConfig(),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


TEST_INSTRUMENT = Instrument(
    token="1", trading_symbol="TESTSYM-EQ", name="TESTSYM",
    exchange="NSE", lot_size=1, tick_size=0.05, instrument_type="",
)


# ------------------------------------------------------------- indicators

def test_ema_matches_pandas():
    s = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8])
    assert np.allclose(ind.ema(s, 3), s.ewm(span=3, adjust=False).mean())


def test_rsi_bounds_and_extremes():
    df = make_session(days=4)
    rsi = ind.rsi(df["close"], 14)
    assert rsi.between(0, 100).all()

    rising = pd.Series(np.arange(1.0, 60.0))
    assert ind.rsi(rising, 14).iloc[-1] == 100.0

    falling = pd.Series(np.arange(60.0, 1.0, -1.0))
    assert ind.rsi(falling, 14).iloc[-1] < 1.0


def test_atr_positive_and_tracks_volatility():
    calm = make_session(days=4, noise=0.0008, seed=1)
    wild = make_session(days=4, noise=0.006, seed=1)
    atr_calm = ind.atr(calm["high"], calm["low"], calm["close"], 14).iloc[-1]
    atr_wild = ind.atr(wild["high"], wild["low"], wild["close"], 14).iloc[-1]
    assert atr_calm > 0 and atr_wild > atr_calm


def test_adx_higher_in_trend_than_chop():
    trend = make_session(days=8, drift=0.0012, noise=0.0006, seed=3)
    chop = make_session(days=8, drift=0.0, noise=0.0006, seed=3)
    adx_trend = ind.adx(trend["high"], trend["low"], trend["close"], 14)["adx"].tail(60).mean()
    adx_chop = ind.adx(chop["high"], chop["low"], chop["close"], 14)["adx"].tail(60).mean()
    assert adx_trend > adx_chop


def test_vwap_resets_each_session():
    df = make_session(days=3)
    vwap = ind.session_vwap(df)
    first_of_day = df.groupby(df.index.normalize()).head(1).index
    for ts in first_of_day:
        typical = (df.loc[ts, "high"] + df.loc[ts, "low"] + df.loc[ts, "close"]) / 3.0
        # The first bar of a session is its own VWAP.
        assert abs(vwap.loc[ts] - typical) < 0.01


def test_opening_range_is_blank_until_the_range_completes():
    df = make_session(days=2)
    orr = ind.opening_range(df, 15)
    first_day = df.index.normalize()[0]
    day_slice = orr[orr.index.normalize() == first_day]
    assert day_slice["or_high"].iloc[:3].isna().all()   # 09:15, 09:20, 09:25
    assert not np.isnan(day_slice["or_high"].iloc[3])   # 09:30 onwards


def test_htf_alignment_has_no_lookahead():
    df = make_session(days=5)
    htf = ind.resample_ohlc(df, "15min")
    aligned = ind.align_htf(htf[["close"]].rename(columns={"close": "htf_close"}), df.index)
    for ts in df.index[20:60]:
        value = aligned.loc[ts, "htf_close"]
        if pd.isna(value):
            continue
        # The attached HTF bar must have closed at or before this timestamp.
        source = htf.index[htf.index <= ts]
        assert len(source) and abs(htf.loc[source[-1], "close"] - value) < 1e-9


# ---------------------------------------------------------------- strategy

def test_prepare_adds_every_column_the_rules_read():
    df = make_session(days=6)
    out = prepare(df, StrategyConfig())
    for col in (
        "ema_fast", "ema_slow", "rsi", "atr", "atr_pct", "vwap", "adx",
        "plus_di", "minus_di", "st_direction", "vol_ratio", "donchian_high",
        "donchian_low", "or_high", "or_low", "htf_ema_fast", "htf_ema_slow",
        "body_ratio", "close_position",
    ):
        assert col in out.columns, f"missing {col}"
    assert len(out) == len(df)


def test_no_signal_before_indicators_warm_up():
    df = make_session(days=6)
    out = prepare(df, StrategyConfig())
    for i in range(5):
        assert evaluate(out.iloc[i], "TESTSYM", StrategyConfig()).signal is None


def test_signals_do_not_change_when_future_bars_arrive():
    """The lookahead guarantee. A signal computed with only the first N bars
    must be identical to the one computed with the full history."""
    df = make_session(days=10, drift=0.0006, seed=11)
    cfg = StrategyConfig(min_score=8)

    full = prepare(df, cfg)
    cut = 400
    partial = prepare(df.iloc[:cut], cfg)

    checked = 0
    for i in range(cut - 30, cut):
        ts = df.index[i]
        a = evaluate(full.loc[ts], "TESTSYM", cfg)
        b = evaluate(partial.loc[ts], "TESTSYM", cfg)
        assert (a.signal is None) == (b.signal is None), f"signal presence differs at {ts}"
        if a.signal and b.signal:
            assert a.signal.side == b.signal.side
            assert abs(a.signal.stop - b.signal.stop) < 0.01
            assert abs(a.signal.target - b.signal.target) < 0.01
            assert a.signal.score == b.signal.score
        checked += 1
    assert checked == 30


def test_chop_produces_far_fewer_signals_than_trend():
    """The whole point of the gates: refuse to trade a directionless market."""
    cfg = StrategyConfig(min_score=10)

    trend = prepare(make_session(days=15, drift=0.0010, noise=0.0007, seed=5), cfg)
    chop = prepare(make_session(days=15, drift=0.0, noise=0.0007, seed=5), cfg)

    def count(df):
        return sum(
            evaluate(df.iloc[i], "TESTSYM", cfg).signal is not None
            for i in range(80, len(df))
        )

    trend_signals, chop_signals = count(trend), count(chop)
    assert trend_signals > chop_signals, (
        f"trend={trend_signals} chop={chop_signals} — the gates are not filtering chop"
    )


def test_raising_min_score_is_monotonic():
    df = prepare(make_session(days=15, drift=0.0008, seed=13), StrategyConfig())

    def count(threshold):
        cfg = StrategyConfig(min_score=threshold)
        return sum(
            evaluate(df.iloc[i], "TESTSYM", cfg).signal is not None
            for i in range(80, len(df))
        )

    counts = [count(t) for t in (6, 10, 14, 18)]
    assert counts == sorted(counts, reverse=True), counts


def test_signal_levels_are_sane():
    df = prepare(make_session(days=15, drift=0.0009, seed=17), StrategyConfig(min_score=8))
    cfg = StrategyConfig(min_score=8)

    found = 0
    for i in range(80, len(df)):
        sig = evaluate(df.iloc[i], "TESTSYM", cfg).signal
        if sig is None:
            continue
        found += 1
        assert sig.risk_per_unit > 0
        # The configured ratio (3.0/1.5 = 2:1) must survive a widened
        # structural stop; tick rounding is the only allowed deviation.
        expected_rr = cfg.atr_target_multiple / cfg.atr_stop_multiple
        assert abs(sig.reward_risk - expected_rr) < 0.05, (
            f"reward:risk {sig.reward_risk:.2f}, expected {expected_rr:.2f}"
        )
        if sig.side == "LONG":
            assert sig.stop < sig.entry_ref < sig.target
        else:
            assert sig.target < sig.entry_ref < sig.stop
        assert abs(round_to_tick(sig.stop) - sig.stop) < 1e-9
        assert 0 <= sig.score <= MAX_SCORE
    assert found > 0, "no signals generated in a strong trend — gates are too tight"


def test_gates_veto_regardless_of_score():
    """A bar that fails a hard gate must produce no signal even at min_score=0."""
    df = prepare(make_session(days=10, drift=0.0009, seed=21), StrategyConfig())
    cfg = StrategyConfig(min_score=0)

    row = df.iloc[200].copy()
    row["adx"] = 5.0  # well below adx_min
    assert evaluate(row, "TESTSYM", cfg).signal is None

    row2 = df.iloc[200].copy()
    row2["atr_pct"] = 99.0  # outside the volatility band
    assert evaluate(row2, "TESTSYM", cfg).signal is None


# -------------------------------------------------------------------- risk

def test_position_size_respects_the_risk_budget():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0), 100_000)
    account = AccountConfig(name="a", env_prefix="A", capital=100_000)

    qty, _ = rm.position_size(account, entry_price=100.0, stop_price=95.0)
    assert qty == 200  # 1000 risk budget / 5 per unit
    assert qty * 5.0 <= 1000.0 + 1e-9


def test_position_cap_overrides_risk_budget_on_tight_stops():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_value_pct=25.0), 100_000)
    account = AccountConfig(name="a", env_prefix="A", capital=100_000)

    qty, bound = rm.position_size(account, entry_price=100.0, stop_price=99.9)
    assert qty == 250  # 25% of 100k / 100
    assert bound == "position cap"


def test_size_is_zero_when_a_single_unit_exceeds_the_budget():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=0.5), 10_000)
    account = AccountConfig(name="a", env_prefix="A", capital=10_000)
    qty, reason = rm.position_size(account, entry_price=5_000.0, stop_price=4_000.0)
    assert qty == 0 and "zero" in reason


def test_lot_size_rounds_down():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0), 100_000)
    account = AccountConfig(name="a", env_prefix="A", capital=100_000)
    qty, _ = rm.position_size(account, entry_price=100.0, stop_price=95.0, lot_size=75)
    assert qty == 150 and qty % 75 == 0


def test_daily_loss_halts_trading():
    rm = RiskManager(RiskConfig(max_daily_loss_pct=2.0), 100_000)
    assert rm.check_entry("X", 0)[0]
    rm.record_exit(-2_100)
    allowed, reason = rm.check_entry("X", 0)
    assert not allowed and "halted" in reason


def test_daily_profit_target_also_halts():
    rm = RiskManager(RiskConfig(max_daily_profit_pct=4.0), 100_000)
    rm.record_exit(4_500)
    assert not rm.check_entry("X", 0)[0]


def test_trade_count_limits():
    rm = RiskManager(RiskConfig(max_trades_per_day=2, max_trades_per_symbol_per_day=1), 100_000)
    rm.record_entry("A")
    assert not rm.check_entry("A", 0)[0]   # symbol limit
    assert rm.check_entry("B", 0)[0]
    rm.record_entry("B")
    assert not rm.check_entry("C", 0)[0]   # daily limit


def test_max_open_positions():
    rm = RiskManager(RiskConfig(max_open_positions=2), 100_000)
    assert not rm.check_entry("X", 2)[0]


# --------------------------------------------------------------- positions

def _position(side="LONG", entry=100.0, stop=98.0, target=106.0, atr=2.0) -> Position:
    pos = Position(
        symbol="TESTSYM", instrument=TEST_INSTRUMENT, side=side,
        entry_time=datetime(2025, 1, 6, 10, 0, tzinfo=IST),
        entry_ref=entry, initial_stop=stop, stop=stop, target=target,
        atr=atr, score=15, high_water=entry, low_water=entry,
    )
    pos.legs["test"] = Leg(account="test", quantity=100, entry_price=entry)
    return pos


def test_stop_moves_to_breakeven_at_one_r():
    pos = _position()
    cfg = StrategyConfig(breakeven_at_r=1.0, trail_atr_multiple=2.0)

    pos.update_watermarks(101.0, 100.0)
    assert pos.next_stop(cfg) is None            # only 0.5R

    pos.update_watermarks(102.0, 100.0)          # 1R = entry + 2.0
    assert pos.next_stop(cfg) == 100.0
    assert pos.breakeven_done


def test_trailing_stop_only_moves_forward():
    pos = _position()
    cfg = StrategyConfig(breakeven_at_r=1.0, trail_atr_multiple=1.0)

    pos.update_watermarks(102.0, 100.0)
    pos.stop = pos.next_stop(cfg)

    pos.update_watermarks(110.0, 101.0)
    trailed = pos.next_stop(cfg)
    assert trailed == 108.0                      # high water 110 - 1 * ATR(2.0)
    pos.stop = trailed

    pos.update_watermarks(105.0, 104.0)          # price falls back
    assert pos.next_stop(cfg) is None             # stop must not loosen


def test_short_position_trails_downward():
    pos = _position(side="SHORT", entry=100.0, stop=102.0, target=94.0)
    cfg = StrategyConfig(breakeven_at_r=1.0, trail_atr_multiple=1.0)

    pos.update_watermarks(100.0, 98.0)           # 1R in profit
    assert pos.next_stop(cfg) == 100.0
    pos.stop = 100.0

    pos.update_watermarks(99.0, 90.0)
    assert pos.next_stop(cfg) == 92.0            # 90 + 1 * ATR


def test_pnl_and_r_multiple():
    pos = _position()
    assert pos.unrealised(105.0) == 500.0

    pos.legs["test"].exit_price = 106.0
    pos.legs["test"].closed = True
    assert pos.realised() == 600.0
    assert abs(pos._closed_r() - 3.0) < 1e-9     # 6 profit / 2 risk


def test_stop_and_target_detection():
    pos = _position()
    assert pos.stop_hit(low=97.0, high=101.0)
    assert not pos.stop_hit(low=99.0, high=101.0)
    assert pos.target_hit(low=99.0, high=107.0)


# ------------------------------------------------------------------- utils

def test_round_to_tick():
    assert round_to_tick(100.123) == 100.10
    assert round_to_tick(100.126) == 100.15
    assert round_to_tick(2503.77, 0.05) == 2503.75


def test_market_calendar_skips_weekends_and_holidays():
    cal = MarketCalendar(["2025-01-26"])
    assert cal.is_trading_day(datetime(2025, 1, 6).date())      # Monday
    assert not cal.is_trading_day(datetime(2025, 1, 11).date())  # Saturday
    assert not cal.is_trading_day(datetime(2025, 1, 26).date())  # holiday


def test_bar_boundaries_anchor_to_the_market_open():
    """Bars run from 09:15, not from the top of the hour. A 10-minute bar
    closes at 09:25, so anchoring to `minute % 10` would be wrong."""
    from trading.data import DataFeed

    feed = DataFeed.__new__(DataFeed)

    feed.interval_minutes = 5
    assert feed.next_bar_close(datetime(2025, 1, 6, 9, 17, tzinfo=IST)).minute == 20
    assert feed.next_bar_close(datetime(2025, 1, 6, 9, 20, tzinfo=IST)).minute == 25
    assert feed.next_bar_close(datetime(2025, 1, 6, 14, 3, tzinfo=IST)).minute == 5

    feed.interval_minutes = 10
    nxt = feed.next_bar_close(datetime(2025, 1, 6, 9, 20, tzinfo=IST))
    assert (nxt.hour, nxt.minute) == (9, 25)
    nxt = feed.next_bar_close(datetime(2025, 1, 6, 9, 26, tzinfo=IST))
    assert (nxt.hour, nxt.minute) == (9, 35)

    feed.interval_minutes = 15
    nxt = feed.next_bar_close(datetime(2025, 1, 6, 9, 16, tzinfo=IST))
    assert (nxt.hour, nxt.minute) == (9, 30)


def test_closed_bars_excludes_the_forming_bar():
    from trading.data import DataFeed

    feed = DataFeed.__new__(DataFeed)
    feed.interval_minutes = 5
    feed.store = {"X": make_session(days=1)}

    # A bar stamped 09:15 covers 09:15-09:20 and is only closed from 09:20 on.
    as_of = datetime(2025, 1, 6, 9, 23, tzinfo=IST)
    bars = feed.closed_bars("X", as_of)
    assert bars.index[-1] == pd.Timestamp("2025-01-06 09:15", tz=IST)

    as_of = datetime(2025, 1, 6, 9, 25, tzinfo=IST)
    assert feed.closed_bars("X", as_of).index[-1] == pd.Timestamp("2025-01-06 09:20", tz=IST)


def test_rate_limiter_enforces_its_window():
    import time

    limiter = RateLimiter(max_calls=3, period=0.4)
    start = time.monotonic()
    for _ in range(6):
        limiter.acquire()
    # 6 calls at 3 per 0.4s must take at least one extra window.
    assert time.monotonic() - start >= 0.35


# ---------------------------------------------------------------- backtest

def test_backtest_runs_and_reports_consistent_numbers():
    from trading.backtest import Backtester

    cfg = default_config()
    cfg.strategy.min_score = 8
    cfg.risk.max_trades_per_day = 20
    cfg.risk.max_trades_per_symbol_per_day = 10

    data = {
        "TESTSYM": make_session(days=25, drift=0.0007, noise=0.0012, seed=29),
        "OTHERSYM": make_session(days=25, drift=-0.0006, noise=0.0012, seed=31),
    }
    results = Backtester(cfg).run(data)
    stats = results.summary()

    assert stats["trades"] > 0, "backtest produced no trades on a trending series"

    closed = [t for t in results.trades if t.exit_price is not None]
    # Net PnL must equal the sum of the individual trades.
    assert abs(stats["net_pnl"] - sum(t.pnl for t in closed)) < 0.01
    assert 0 <= stats["win_rate_pct"] <= 100
    for t in closed:
        assert t.exit_time >= t.entry_time
        assert t.quantity > 0
        assert t.exit_reason in ("stop", "target", "square off", "end of data")


def test_backtest_respects_max_open_positions():
    from trading.backtest import Backtester

    cfg = default_config()
    cfg.strategy.min_score = 6
    cfg.risk.max_open_positions = 1
    cfg.risk.max_trades_per_day = 50
    cfg.risk.max_trades_per_symbol_per_day = 10

    data = {
        f"SYM{i}": make_session(days=20, drift=0.0008, noise=0.0012, seed=40 + i)
        for i in range(3)
    }
    results = Backtester(cfg).run(data)

    # Walk the trades in time order; concurrency must never exceed the limit.
    events = []
    for t in results.trades:
        if t.exit_time is None:
            continue
        events.append((t.entry_time, 1))
        events.append((t.exit_time, -1))
    events.sort(key=lambda e: (e[0], e[1]))

    concurrent = peak = 0
    for _, delta in events:
        concurrent += delta
        peak = max(peak, concurrent)
    assert peak <= cfg.risk.max_open_positions, f"peak concurrency {peak}"


def test_backtest_stop_beats_target_in_the_same_bar():
    """When a bar's range covers both levels the loss must be booked."""
    from trading.backtest import Backtester

    cfg = default_config()
    bt = Backtester(cfg)

    class T:
        side = "LONG"
        direction = 1
        entry_price = 100.0
        stop = 98.0
        target = 104.0
        initial_risk = 2.0
        high_water = 100.0
        low_water = 100.0
        breakeven_done = False

    bar = pd.Series({"open": 100.0, "high": 105.0, "low": 97.0, "close": 103.0, "atr": 2.0})
    ts = pd.Timestamp("2025-01-06 11:00", tz=IST)
    price, reason = bt._check_exit(T(), bar, ts, cfg.session, cfg.strategy, 0.05)
    assert reason == "stop" and price <= 98.0


# --------------------------------------------------------------- execution

def test_paper_slippage_always_hurts():
    from trading.execution import PaperExecutor

    ex = PaperExecutor(ExecutionConfig(slippage_bps=10.0), ["test"])
    assert ex._fill(100.0, "BUY", 0.05) > 100.0
    assert ex._fill(100.0, "SELL", 0.05) < 100.0


def test_paper_round_trip_pnl():
    from trading.execution import PaperExecutor

    ex = PaperExecutor(ExecutionConfig(slippage_bps=0.0), ["test"])
    pos = _position()
    pos.legs.clear()

    assert ex.open_position(pos, {"test": 50}, 100.0)
    pnl = ex.close_position(pos, 104.0, "target")
    assert abs(pnl - 200.0) < 0.01
    assert all(leg.closed for leg in pos.legs.values())


def test_order_id_extraction_handles_every_known_shape():
    from trading.broker import AngelBroker

    assert AngelBroker._extract_order_id("123") == "123"
    assert AngelBroker._extract_order_id({"data": {"orderid": "456"}}) == "456"
    assert AngelBroker._extract_order_id({"orderid": "789"}) == "789"
    assert AngelBroker._extract_order_id({"status": False, "message": "rejected"}) is None
    assert AngelBroker._extract_order_id(None) is None


# ------------------------------------------------------------------ config

def test_config_rejects_a_reward_risk_below_one():
    import pytest

    from trading.config import _validate

    cfg = default_config()
    cfg.strategy.atr_target_multiple = 1.0
    cfg.strategy.atr_stop_multiple = 2.0
    with pytest.raises(ValueError, match="reward:risk"):
        _validate(cfg)


def test_config_rejects_daily_loss_below_trade_risk():
    import pytest

    from trading.config import _validate

    cfg = default_config()
    cfg.risk.risk_per_trade_pct = 3.0
    cfg.risk.max_daily_loss_pct = 2.0
    with pytest.raises(ValueError, match="max_daily_loss_pct"):
        _validate(cfg)


def test_config_rejects_out_of_order_session_times():
    import pytest

    from trading.config import _validate
    from datetime import time as dtime

    cfg = default_config()
    cfg.session.entry_end = dtime(9, 0)
    with pytest.raises(ValueError, match="session times"):
        _validate(cfg)


# ------------------------------------------------------------- notifications

def test_notifier_disabled_by_default():
    from trading.notifier import NullNotifier, build_notifier

    cfg = default_config()
    assert isinstance(build_notifier(cfg), NullNotifier)
    build_notifier(cfg).send("anything")  # must not raise


def test_notifier_falls_back_when_credentials_are_absent():
    import os

    from trading.notifier import NullNotifier, build_notifier

    cfg = default_config()
    cfg.notifications.enabled = True
    cfg.notifications.env_prefix = "NOTAREALPREFIX"
    os.environ.pop("NOTAREALPREFIX_BOT_TOKEN", None)
    os.environ.pop("NOTAREALPREFIX_CHAT_ID", None)

    # Enabling notifications without credentials must degrade, not fail start-up.
    assert isinstance(build_notifier(cfg), NullNotifier)


def test_notifier_send_never_blocks_the_caller():
    """The trading loop must not wait on the network. A send() into a notifier
    whose delivery hangs has to return immediately."""
    import time

    from trading.notifier import TelegramNotifier

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier._queue = __import__("queue").Queue(maxsize=200)
    notifier._failures = 0

    start = time.monotonic()
    for i in range(50):
        notifier.send(f"message {i}")
    assert time.monotonic() - start < 0.5


def test_notifier_drops_messages_instead_of_blocking_when_full():
    import queue as queue_mod

    from trading.notifier import TelegramNotifier

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier._queue = queue_mod.Queue(maxsize=2)
    notifier._failures = 0

    for i in range(10):
        notifier.send(f"m{i}")  # must not raise or hang
    assert notifier._queue.qsize() == 2


def test_notifier_survives_delivery_failure():
    from trading.notifier import TelegramNotifier

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier._queue = __import__("queue").Queue()
    notifier._failures = 0
    notifier.token = "invalid"
    notifier.chat_id = "0"
    notifier.timeout = 1

    # An unreachable host must be swallowed, never raised.
    notifier._log_failure("simulated outage")
    assert notifier._failures == 1


def test_message_formatting_is_safe_for_all_fields():
    from trading import notifier as n

    pos = _position()
    pos.reasons = ["ema_stack", "orb"]

    assert "TESTSYM" in n.fmt_entry(pos, 100)
    assert "LONG" in n.fmt_entry(pos, 100)

    pos.legs["test"].exit_price = 104.0
    pos.legs["test"].closed = True
    assert "✅" in n.fmt_exit(pos, 400.0, "target")
    assert "❌" in n.fmt_exit(pos, -200.0, "stop")

    assert "No trades" in n.fmt_summary(0, 0, 0.0, "PAPER")
    assert "50%" in n.fmt_summary(2, 1, 100.0, "PAPER")
    assert n.fmt_halt("daily loss limit", -2000.0)
    assert n.fmt_startup("PAPER", 1, 100000.0, 6)


def _run_without_pytest() -> int:
    """Minimal runner so the suite works even where pytest is absent."""
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failed.append(name)
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_without_pytest())
