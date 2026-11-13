"""Tests for the Version 2 momentum bot. No credentials, no network.

The paper engine is driven tick by tick with hand-built quotes, and the live
order layer runs against a fake broker that records every order — so the
property that matters most with real money can be asserted directly: the bot
never sells shares it no longer holds.

    python -m pytest tests/test_momentum_v2.py -v
    python tests/test_momentum_v2.py            # runs without pytest installed
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum_v2"))

import angel_momentum_bot as m  # noqa: E402

DAY = dt.date(2026, 11, 13)  # a Friday


# ------------------------------------------------------------------ fixtures

@contextmanager
def workdir():
    """Point the bot's output files at a throwaway folder."""
    old = m.BASE
    with tempfile.TemporaryDirectory() as tmp:
        m.BASE = tmp
        try:
            yield Path(tmp)
        finally:
            m.BASE = old


@contextmanager
def fast_clock():
    """Skip the order layer's waits without touching the real time module."""
    real = m.time
    m.time = types.SimpleNamespace(sleep=lambda s: None, time=real.time)
    try:
        yield
    finally:
        m.time = real


def cfg(**over):
    c = dict(m.DEFAULTS, api_key="k", client_code="c", pin="p", totp_secret="t")
    # zero costs keep the arithmetic in the assertions exact
    c.update(brokerage_per_order=0, charges_pct_per_side=0, slippage_pct=0)
    c.update(over)
    return c


def prep_row(**over):
    row = {"avg_vol": 1_000_000, "prev_high": 99.0, "prev_close": 98.0, "hi20": 99.0,
           "watch": True,
           "curve": {"09:30": 80_000, "09:45": 130_000, "10:00": 170_000, "15:30": 1_000_000}}
    row.update(over)
    return row


def quote(ltp, vwap=100.3, vol=400_000, high=None, low=99.5, prev_close=98.0, hi52=None):
    high = high if high is not None else max(ltp, 100.5)
    return {"ltp": ltp, "open": 100.0, "high": high, "low": low,
            "pct": (ltp - prev_close) / prev_close * 100, "vwap": vwap, "vol": vol,
            "hi52": hi52 if hi52 is not None else high}


NIFTY_UP = {"ltp": 25100.0, "open": 25000.0, "high": 25120.0, "low": 24990.0,
            "pct": 0.4, "vwap": 0.0, "vol": 0.0, "hi52": 26000.0}
NIFTY_DOWN = dict(NIFTY_UP, ltp=24900.0, pct=-0.4)


def at(hhmm):
    h, mnt = hhmm.split(":")
    return dt.datetime.combine(DAY, dt.time(int(h), int(mnt)))


def tick(bot, hhmm, **quotes):
    quotes.setdefault("NIFTY", NIFTY_UP)
    bot.on_tick(at(hhmm), quotes)


def warm_up(bot, sym="ABC", nifty=NIFTY_UP, breakout=True, **qkw):
    """Walk the bot from 09:25 to the 09:40 breakout bar."""
    for mm in range(25, 30):
        tick(bot, "09:%02d" % mm, NIFTY=nifty, **{sym: quote(100.0, **qkw)})
    for mm in range(30, 35):                       # opening range fixed at 100.5 here
        tick(bot, "09:%02d" % mm, NIFTY=nifty, **{sym: quote(100.2, **qkw)})
    for mm in range(35, 40):
        tick(bot, "09:%02d" % mm, NIFTY=nifty, **{sym: quote(100.8, high=100.8, **qkw)})
    if breakout:
        tick(bot, "09:40", NIFTY=nifty, **{sym: quote(100.9, high=101.0, **qkw)})


def read_csv(folder, name):
    path = Path(folder) / name
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def new_bot(**over):
    prep = over.pop("prep", {"ABC": prep_row()})
    news = over.pop("news", None)
    return m.Bot(cfg(**over), prep, news)


# ------------------------------------------------------------------- helpers

def test_clock_is_ist_regardless_of_server_timezone():
    utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    delta = (m.now_ist() - utc_now).total_seconds()
    assert abs(delta - 5.5 * 3600) < 5


def test_ema_needs_enough_values():
    assert m.ema([1, 2, 3], 5) is None
    assert abs(m.ema([10.0] * 25, 20) - 10.0) < 1e-9


def test_live_needs_all_three_switches():
    live = []
    for mode in ("paper", "live"):
        for confirm in (False, True):
            for flag in (False, True):
                if m.is_live({"mode": mode, "confirm_live": confirm}, flag):
                    live.append((mode, confirm, flag))
    assert live == [("live", True, True)]


def test_confirm_live_must_be_a_real_true():
    # a quoted "true" in config.json is a string; it must not arm live trading
    assert not m.is_live({"mode": "live", "confirm_live": "true"}, True)


def test_csv_with_old_columns_is_set_aside_not_corrupted():
    with workdir() as folder:
        (folder / "trades.csv").write_text("date,symbol,score\n2026-11-01,OLD,5\n")
        m.append_csv("trades.csv", m.TRADE_HEADER, ["x"] * len(m.TRADE_HEADER))

        rows = read_csv(folder, "trades.csv")
        assert len(rows) == 1 and list(rows[0]) == m.TRADE_HEADER
        kept = [p for p in folder.iterdir() if p.name.startswith("trades_old_")]
        assert len(kept) == 1 and "OLD" in kept[0].read_text()


# ---------------------------------------------------------------------- prep

def _daily(n=30, last=None):
    rows = [["2026-10-%02dT00:00:00+05:30" % (i + 1), 100, 102, 98, 100, 1000] for i in range(n)]
    if last:
        rows[-1] = last
    return rows


def test_daily_analysis_flags_a_strong_close():
    rows = _daily(last=["2026-10-30T00:00:00+05:30", 100, 110, 99, 109.5, 5000])
    info = m.analyse_daily(rows, today=dt.date(2026, 11, 2), now=dt.datetime(2026, 11, 2, 8, 0))
    assert info["strong_close"] and info["watch"]
    assert info["prev_high"] == 110 and info["hi20"] == 110


def test_daily_analysis_flags_an_inside_day():
    rows = _daily(last=["2026-10-30T00:00:00+05:30", 100, 101, 99.5, 100, 500])
    info = m.analyse_daily(rows, today=dt.date(2026, 11, 2), now=dt.datetime(2026, 11, 2, 8, 0))
    assert info["inside"] and info["nr7"]


def test_daily_analysis_drops_todays_unfinished_candle():
    rows = _daily() + [["2026-11-02T00:00:00+05:30", 100, 150, 99, 149, 99999]]
    info = m.analyse_daily(rows, today=dt.date(2026, 11, 2), now=dt.datetime(2026, 11, 2, 11, 0))
    assert info["prev_high"] == 102          # the half-formed 150 high is ignored


def test_daily_analysis_needs_history():
    assert m.analyse_daily(_daily(n=10), today=dt.date(2026, 11, 2)) is None


def _intraday(days=5, first=100, later=20):
    rows = []
    for d in range(days):
        day = "2026-11-%02d" % (d + 2)
        rows.append([day + "T09:15:00+05:30", 1, 1, 1, 1, first])
        rows.append([day + "T09:30:00+05:30", 1, 1, 1, 1, later])
        rows.append([day + "T09:45:00+05:30", 1, 1, 1, 1, later])
    return rows


def test_volume_curve_is_cumulative_by_time_of_day():
    curve = m.volume_curve(_intraday(), today=DAY)
    assert curve == {"09:30": 100, "09:45": 120, "10:00": 140}


def test_volume_curve_ignores_today_and_needs_enough_days():
    rows = _intraday(days=2) + [["2026-11-13T09:15:00+05:30", 1, 1, 1, 1, 9_999_999]]
    assert m.volume_curve(rows, today=DAY) is None


def test_expected_volume_interpolates_between_slots():
    d = {"avg_vol": 1000, "curve": {"09:30": 100, "09:45": 130}}
    assert abs(m.expected_volume(d, dt.datetime(2026, 11, 13, 9, 30)) - 100) < 1e-6
    assert abs(m.expected_volume(d, dt.datetime(2026, 11, 13, 9, 40)) - 120) < 1e-6
    assert abs(m.expected_volume(d, dt.datetime(2026, 11, 13, 12, 0)) - 130) < 1e-6


def test_morning_volume_is_not_flattered_by_a_straight_line():
    """The first 15 minutes carry far more than 15/375 of a day's volume. A
    straight-line estimate therefore calls an ordinary open "3x volume"."""
    d = {"avg_vol": 1_000_000, "curve": {"09:30": 90_000, "15:30": 1_000_000}}
    now = dt.datetime(2026, 11, 13, 9, 30)
    todays_volume = 120_000                                  # a perfectly normal open

    straight_line = todays_volume / m.expected_volume({"avg_vol": 1_000_000}, now)
    by_time_of_day = todays_volume / m.expected_volume(d, now)

    assert straight_line >= 3.0                              # would pass the check
    assert by_time_of_day < 1.5                              # correctly does not


# -------------------------------------------------------------- paper engine

def test_winning_trade_end_to_end():
    with workdir() as folder:
        bot = new_bot()
        warm_up(bot)

        assert "ABC" in bot.pos, "breakout with score 5 should have been bought"
        p = bot.pos["ABC"]
        assert p["score"] == 5 and p["checks"]["news"] is False
        assert abs(p["sl"] - 100.3 * 0.998) < 1e-6           # VWAP stop, the one that fits
        assert p["qty"] == 495                               # capped at capital / 2, no leverage
        assert p["qty"] * p["entry"] <= 100_000 / 2

        tick(bot, "09:41", ABC=quote(101.75, high=101.75))   # +1R
        assert p["be"] and p["sl"] == p["entry"]

        tick(bot, "09:42", ABC=quote(102.6, high=102.6))     # +2R
        assert p["half"] and p["qty"] == 495 - 247

        tick(bot, "09:43", ABC=quote(100.85, high=102.6))    # back through breakeven
        assert "ABC" not in bot.pos

        trades = read_csv(folder, "trades.csv")
        assert len(trades) == 1
        t = trades[0]
        assert t["reason"] == "Trail SL" and t["mode"] == "PAPER" and t["qty"] == "495"
        expected = (102.6 - 100.9) * 247 + (100.85 - 100.9) * 248
        assert abs(float(t["pnl_rs"]) - expected) < 0.01
        assert float(t["r_multiple"]) > 1.0
        assert bot.losses == 0 and abs(bot.realised - expected) < 0.01

        signals = read_csv(folder, "signals.csv")
        assert len(signals) == 1 and signals[0]["traded"] == "True" and signals[0]["score"] == "5"


def test_stop_loss_exit_counts_as_a_loss():
    with workdir() as folder:
        bot = new_bot()
        warm_up(bot)
        sl = bot.pos["ABC"]["sl"]
        tick(bot, "09:41", ABC=quote(sl - 0.05, high=101.0))

        assert "ABC" not in bot.pos and bot.losses == 1
        t = read_csv(folder, "trades.csv")[0]
        assert t["reason"] == "SL" and float(t["r_multiple"]) < -0.9


def test_time_stop_after_45_minutes_without_1r():
    with workdir() as folder:
        bot = new_bot()
        warm_up(bot)
        tick(bot, "10:26", ABC=quote(101.0, high=101.0))
        assert read_csv(folder, "trades.csv")[0]["reason"] == "Time stop"


def test_exit_when_a_5min_bar_closes_below_vwap():
    with workdir() as folder:
        bot = new_bot()
        warm_up(bot)
        for mm in range(41, 45):
            tick(bot, "09:%02d" % mm, ABC=quote(100.6, vwap=100.3, high=101.0))
        tick(bot, "09:45", ABC=quote(100.6, vwap=100.95, high=101.0))  # bar closed under VWAP
        assert "ABC" not in bot.pos
        assert "VWAP" in read_csv(folder, "trades.csv")[0]["reason"]


def test_square_off_at_cutoff():
    with workdir() as folder:
        bot = new_bot(time_stop_min=9999)
        warm_up(bot)
        tick(bot, "15:10", ABC=quote(101.0, high=101.0))
        assert "square-off" in read_csv(folder, "trades.csv")[0]["reason"]


def test_square_off_still_happens_when_the_quote_is_missing():
    with workdir() as folder:
        bot = new_bot(time_stop_min=9999)
        warm_up(bot)
        tick(bot, "15:10")                                   # only Nifty came back
        assert "ABC" not in bot.pos
        assert float(read_csv(folder, "trades.csv")[0]["exit"]) == 100.9   # last known price


def test_low_score_is_logged_but_not_traded():
    with workdir() as folder:
        bot = new_bot(prep={"ABC": prep_row(watch=False)})
        warm_up(bot, vol=10_000, hi52=200.0)                 # no watchlist, no volume

        assert not bot.pos and "ABC" in bot.done
        s = read_csv(folder, "signals.csv")[0]
        assert s["traded"] == "False" and s["volume"] == "False" and int(s["score"]) < 5


def test_news_adds_the_sixth_point():
    with workdir():
        bot = new_bot(news=["ABC"])
        warm_up(bot)
        assert bot.pos["ABC"]["score"] == 6


def test_no_signal_below_the_minimum_gap():
    with workdir() as folder:
        bot = new_bot()
        warm_up(bot, prev_close=100.5)                       # up only ~0.4% on the day
        assert not bot.pos and not read_csv(folder, "signals.csv")


def test_no_entry_when_stretched_too_far_from_vwap():
    with workdir() as folder:
        bot = new_bot(news=["ABC"])
        warm_up(bot, vwap=97.0)                              # ~4% above VWAP
        assert not bot.pos
        assert read_csv(folder, "signals.csv")[0]["vwap"] == "False"


def test_market_filter_blocks_longs_when_nifty_is_weak():
    with workdir() as folder:
        bot = new_bot()
        warm_up(bot, nifty=NIFTY_DOWN)
        assert not bot.pos and read_csv(folder, "signals.csv")[0]["market"] == "False"


def test_missing_nifty_quote_fails_closed():
    bot = new_bot()
    assert bot.market_ok({}) is False


def test_nifty_must_hold_above_its_running_average():
    bot = new_bot()
    bot.nifty_sum, bot.nifty_n = 25200.0 * 10, 10            # averaged 25200 so far today
    assert bot.market_ok({"NIFTY": NIFTY_UP}) is False       # 25100 is green but fading


def test_sector_filter_uses_the_other_stocks_in_the_sector():
    bot = new_bot(sectors={"BANK": ["ABC", "P1", "P2"]})
    weak = {"P1": quote(99.0, prev_close=100.0), "P2": quote(99.5, prev_close=100.0)}
    strong = {"P1": quote(101.0, prev_close=100.0), "P2": quote(100.5, prev_close=100.0)}
    assert bot.sector_ok("ABC", weak) is False
    assert bot.sector_ok("ABC", strong) is True
    assert bot.sector_ok("NOSECTOR", weak) is True           # unknown sector: Nifty decides


def test_resistance_inside_2r_costs_the_point():
    with workdir() as folder:
        bot = new_bot(prep={"ABC": prep_row(hi20=101.8)})    # old high between entry and target
        warm_up(bot)
        assert not bot.pos
        assert read_csv(folder, "signals.csv")[0]["sl_ok"] == "False"


def test_stop_too_wide_is_never_traded_even_with_a_full_score():
    with workdir():
        bot = new_bot(news=["ABC"], min_score=1)
        warm_up(bot, vwap=98.5)                              # both stop candidates out of band
        assert not bot.pos


def test_daily_loss_limit_blocks_new_trades():
    with workdir() as folder:
        bot = new_bot()
        bot.losses = 2
        warm_up(bot)
        assert not bot.pos and read_csv(folder, "signals.csv")[0]["traded"] == "False"


def test_max_positions_blocks_a_third_trade():
    with workdir():
        bot = new_bot()
        bot.pos = {"X": {}, "Y": {}}
        for mm in range(25, 40):
            price = 100.0 if mm < 30 else (100.2 if mm < 35 else 100.8)
            bot._update_bars(at("09:%02d" % mm), {"ABC": quote(price)})
        bot.orng["ABC"] = (100.5, 99.5)
        bot._entries(at("09:40"), {"ABC": quote(100.9, high=101.0), "NIFTY": NIFTY_UP}, {"ABC"})
        assert "ABC" not in bot.pos


def test_one_signal_per_stock_per_day():
    with workdir() as folder:
        bot = new_bot()
        warm_up(bot)
        tick(bot, "09:41", ABC=quote(bot.pos["ABC"]["sl"] - 0.05, high=101.0))   # stopped out
        for mm in range(42, 51):
            tick(bot, "09:%02d" % mm, ABC=quote(101.2, high=101.2))
        assert not bot.pos and len(read_csv(folder, "signals.csv")) == 1


def test_breakeven_scratch_is_not_counted_as_a_loss():
    with workdir():
        bot = new_bot(brokerage_per_order=20)
        warm_up(bot)
        tick(bot, "09:41", ABC=quote(101.75, high=101.75))   # +1R -> breakeven stop
        tick(bot, "09:42", ABC=quote(100.9, high=101.75))    # scratched; only charges lost
        assert "ABC" not in bot.pos and bot.realised < 0 and bot.losses == 0


def test_extension_rule_tightens_the_stop_on_an_open_position():
    with workdir():
        bot = new_bot()
        warm_up(bot)
        for mm in range(41, 50):                             # >3% above VWAP throughout
            tick(bot, "09:%02d" % mm, ABC=quote(104.5, vwap=100.4, high=104.5))
        assert bot.pos["ABC"]["sl"] < 101.0                  # the 09:40 bar still holds the entry low
        tick(bot, "09:50", ABC=quote(104.6, vwap=100.4, high=104.6))
        assert bot.pos["ABC"]["sl"] > 104.0                  # now trailed under a bar made up there


def test_state_survives_a_restart():
    with workdir():
        bot = new_bot()
        warm_up(bot)
        m_now = m.now_ist
        m.now_ist = lambda: at("09:41")
        try:
            bot.save_state()
            again = new_bot()
            assert again.load_state() is True
        finally:
            m.now_ist = m_now

        assert set(again.pos) == {"ABC"} and again.pos["ABC"]["qty"] == 495
        assert again.pos["ABC"]["time"] == at("09:40")
        assert again.orng["ABC"] == (100.5, 99.5) and "ABC" in again.done


def test_yesterdays_state_is_not_loaded():
    with workdir() as folder:
        (folder / "state.json").write_text(json.dumps(
            {"date": "2020-01-01", "mode": "PAPER", "pos": {"OLD": {"time": "2020-01-01T10:00:00"}}}))
        bot = new_bot()
        assert bot.load_state() is False and not bot.pos


def test_report_runs_on_recorded_trades():
    with workdir():
        bot = new_bot()
        warm_up(bot)
        tick(bot, "09:41", ABC=quote(bot.pos["ABC"]["sl"] - 0.05, high=101.0))
        m.cmd_report()                                       # must not raise


# ----------------------------------------------------------- live order layer

class FakeApi:
    """Stands in for SmartConnect and keeps a broker-side view of the position."""

    def __init__(self, price=100.9):
        self.price = price
        self.orders = []
        self.book = {}
        self.net = {}
        self.cash = 1_000_000.0
        self.reject_sl = False
        self.reject_sell = False
        self._next = 1

    def placeOrder(self, params):
        oid = str(self._next)
        self._next += 1
        row = dict(params, orderid=oid, averageprice=0, text="")
        sym, qty = params["tradingsymbol"], int(params["quantity"])
        if params["ordertype"] == "MARKET":
            if params["transactiontype"] == "SELL" and self.reject_sell:
                row.update(status="rejected", text="RMS rejected")
            else:
                row.update(status="complete", averageprice=self.price)
                self.net[sym] = self.net.get(sym, 0) + (qty if params["transactiontype"] == "BUY" else -qty)
        else:
            row["status"] = "rejected" if self.reject_sl else "trigger pending"
        self.orders.append(row)
        self.book[oid] = row
        return oid

    def orderBook(self):
        return {"status": True, "data": list(self.book.values())}

    def cancelOrder(self, oid, variety):
        if self.book[oid]["status"] not in ("complete", "rejected"):
            self.book[oid]["status"] = "cancelled"

    def position(self):
        return {"status": True, "data": [
            {"tradingsymbol": s, "producttype": "INTRADAY", "netqty": str(n)}
            for s, n in self.net.items()]}

    def rmsLimit(self):
        return {"status": True, "data": {"availablecash": str(self.cash)}}

    # test controls
    def trigger_stop(self, sym, price):
        for row in self.book.values():
            if row["tradingsymbol"] == sym + "-EQ" and row["status"] == "trigger pending":
                row.update(status="complete", averageprice=price)
                self.net[sym + "-EQ"] -= int(row["quantity"])
                return
        raise AssertionError("no working stop order for " + sym)

    def market_sells(self):
        return [o for o in self.orders
                if o["ordertype"] == "MARKET" and o["transactiontype"] == "SELL"]

    def working_stops(self):
        return [o for o in self.orders if o["status"] == "trigger pending"]


def live_exec(api=None):
    api = api or FakeApi()
    return api, m.LiveExec(api, {"ABC": "111"}, {"ABC": 0.05})


def test_live_entry_places_buy_then_a_protective_stop():
    with fast_clock():
        api, ex = live_exec()
        assert ex.enter("ABC", 100, 100.9, 100.0994) == (100.9, 100)

    buy, stop = api.orders
    assert (buy["transactiontype"], buy["ordertype"]) == ("BUY", "MARKET")
    assert (stop["transactiontype"], stop["ordertype"], stop["variety"]) == (
        "SELL", "STOPLOSS_MARKET", "STOPLOSS")
    assert stop["triggerprice"] == "100.05"                  # rounded DOWN to the 0.05 tick
    assert stop["quantity"] == "100"


def test_every_live_order_is_intraday():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        ex.reduce("ABC", 50, 102.0, 50, 100.9)
        ex.exit("ABC", 50, 101.0)
    assert {o["producttype"] for o in api.orders} == {"INTRADAY"}


def test_stop_already_filled_at_broker_is_never_sold_again():
    """The dangerous race: the broker stop fires between two polls. Selling
    again would open a short position in a long-only system."""
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        api.trigger_stop("ABC", 100.05)

        assert ex.exit("ABC", 100, 99.8) == 100.05           # reports the broker's fill

    assert api.market_sells() == []
    assert api.net["ABC-EQ"] == 0


def test_broker_exit_reports_a_filled_stop_once():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        ex.refresh()
        assert ex.broker_exit("ABC") is None
        api.trigger_stop("ABC", 100.0)
        ex.refresh()
        assert ex.broker_exit("ABC") == 100.0
        assert ex.broker_exit("ABC") is None


def test_normal_exit_cancels_the_stop_before_selling():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        api.price = 102.0
        assert ex.exit("ABC", 100, 102.0) == 102.0

    assert api.working_stops() == []                         # nothing left that could fire later
    assert len(api.market_sells()) == 1 and api.net["ABC-EQ"] == 0


def test_exit_does_not_sell_a_position_closed_by_hand():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        api.net["ABC-EQ"] = 0                                # closed in the Angel app
        ex.exit("ABC", 100, 101.0)
    assert api.market_sells() == []


def test_exit_sells_only_what_the_broker_still_shows():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        api.net["ABC-EQ"] = 40
        ex.exit("ABC", 100, 101.0)
    assert api.market_sells()[0]["quantity"] == "40" and api.net["ABC-EQ"] == 0


def test_rejected_stop_at_entry_closes_the_position_immediately():
    with fast_clock():
        api, ex = live_exec()
        api.reject_sl = True
        assert ex.enter("ABC", 100, 100.9, 100.1) is None
    assert api.net["ABC-EQ"] == 0                            # not left running unprotected


def test_partial_booking_re_protects_the_remainder():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        api.price = 102.6
        assert ex.reduce("ABC", 50, 102.6, 50, 100.9) == (102.6, True)

    stops = api.working_stops()
    assert len(stops) == 1 and stops[0]["quantity"] == "50" and stops[0]["triggerprice"] == "100.90"
    assert api.net["ABC-EQ"] == 50


def test_partial_booking_after_a_filled_stop_places_no_orders():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        api.trigger_stop("ABC", 100.05)
        before = len(api.orders)

        assert ex.reduce("ABC", 50, 102.6, 50, 100.9) is None
        assert ex.protect("ABC", 100, 100.9) is True         # must not place a fresh sell stop
        assert ex.broker_exit("ABC") == 100.05

    assert len(api.orders) == before and api.net["ABC-EQ"] == 0


def test_failed_exit_puts_the_stop_back():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        api.reject_sell = True
        assert ex.exit("ABC", 100, 101.0) is None

    assert api.net["ABC-EQ"] == 100                          # still long...
    assert len(api.working_stops()) == 1                     # ...and still protected


def test_moving_the_stop_replaces_rather_than_stacks():
    with fast_clock():
        api, ex = live_exec()
        ex.enter("ABC", 100, 100.9, 100.1)
        assert ex.protect("ABC", 100, 100.9)
    stops = api.working_stops()
    assert len(stops) == 1 and stops[0]["triggerprice"] == "100.90"


def test_cash_comes_from_the_broker():
    api, ex = live_exec()
    api.cash = 43210.5
    assert ex.cash() == 43210.5


# ------------------------------------------------------ bot + live together

def live_bot(api, **over):
    c = cfg(**over)
    ex = m.LiveExec(api, {"ABC": "111"}, {"ABC": 0.05})
    return m.Bot(c, {"ABC": prep_row()}, None, ex)


def test_live_trade_closed_by_the_broker_stop_never_goes_short():
    with workdir() as folder, fast_clock():
        api = FakeApi()
        bot = live_bot(api)
        warm_up(bot)
        assert "ABC" in bot.pos and bot.tag == "LIVE"

        tick(bot, "09:41", ABC=quote(101.75, high=101.75))   # +1R: stop moved to breakeven
        assert api.working_stops()[0]["triggerprice"] == "100.90"

        api.trigger_stop("ABC", 100.9)                       # fires between polls
        tick(bot, "09:42", ABC=quote(100.7, high=101.75))

        assert "ABC" not in bot.pos
        assert api.market_sells() == [] and api.net["ABC-EQ"] == 0
        t = read_csv(folder, "trades.csv")[0]
        assert t["mode"] == "LIVE" and "broker" in t["reason"] and float(t["exit"]) == 100.9


def test_live_full_winner_ends_flat_with_no_working_orders():
    with workdir(), fast_clock():
        api = FakeApi()
        bot = live_bot(api)
        warm_up(bot)
        qty = bot.pos["ABC"]["qty"]

        api.price = 101.75
        tick(bot, "09:41", ABC=quote(101.75, high=101.75))
        api.price = 102.6
        tick(bot, "09:42", ABC=quote(102.6, high=102.6))
        assert api.net["ABC-EQ"] == qty - qty // 2
        api.price = 100.85
        tick(bot, "09:43", ABC=quote(100.85, high=102.6))

        assert not bot.pos and api.net["ABC-EQ"] == 0 and api.working_stops() == []


def test_live_size_is_capped_by_cash_actually_available():
    with workdir(), fast_clock():
        api = FakeApi()
        api.cash = 20_000.0
        bot = live_bot(api)
        warm_up(bot)
        qty = bot.pos["ABC"]["qty"]
        assert qty == int(20_000 * 0.98 // 100.9) and qty * 100.9 <= 20_000


def test_live_skips_the_trade_when_cash_is_unknown():
    with workdir(), fast_clock():
        api = FakeApi()
        api.rmsLimit = lambda: {"status": False}
        bot = live_bot(api)
        warm_up(bot)
        assert not bot.pos and api.orders == []


def test_live_failed_exit_keeps_the_position_and_retries():
    with workdir(), fast_clock():
        api = FakeApi()
        bot = live_bot(api, time_stop_min=9999)
        warm_up(bot)
        api.reject_sell = True
        tick(bot, "15:10", ABC=quote(101.0, high=101.0))
        assert "ABC" in bot.pos and len(api.working_stops()) == 1    # still tracked, still protected

        api.reject_sell = False
        tick(bot, "15:11", ABC=quote(101.0, high=101.0))
        assert "ABC" not in bot.pos and api.net["ABC-EQ"] == 0


def test_live_state_restores_the_stop_order_link():
    with workdir(), fast_clock():
        api = FakeApi()
        bot = live_bot(api)
        warm_up(bot)
        m_now = m.now_ist
        m.now_ist = lambda: at("09:41")
        try:
            bot.save_state()
            again = live_bot(api)
            assert again.load_state()
        finally:
            m.now_ist = m_now

        api.trigger_stop("ABC", 100.05)
        tick(again, "09:42", ABC=quote(99.9, high=101.0))
        assert not again.pos and api.market_sells() == [] and api.net["ABC-EQ"] == 0


# ------------------------------------------------- whole day, through cmd_run

class SimClock:
    """A clock that only moves when the bot sleeps, so a full session runs instantly."""

    def __init__(self, start):
        self.now = start

    def sleep(self, seconds):
        self.now += dt.timedelta(seconds=seconds)

    def time(self):
        return (self.now - dt.datetime(1970, 1, 1)).total_seconds()


class SimMarket:
    """Serves getMarketData from a price script keyed on the simulated clock."""

    def __init__(self, clock, script):
        self.clock, self.script, self.high = clock, script, 0.0
        self.calls = 0

    def price(self):
        t = self.clock.now.strftime("%H:%M")
        current = None
        for start, value in self.script:
            if t >= start:
                current = value
        return current

    def getMarketData(self, mode, tokens):
        self.calls += 1
        p = self.price()
        self.high = max(self.high, p)
        rows = [{"symbolToken": "111", "ltp": p, "open": 100.0, "high": self.high, "low": 99.5,
                 "percentChange": (p - 98.0) / 98.0 * 100, "avgPrice": 100.3,
                 "tradeVolume": 400_000, "52WeekHigh": self.high},
                {"symbolToken": m.NIFTY_TOKEN, "ltp": 25100.0, "open": 25000.0, "high": 25120.0,
                 "low": 24990.0, "percentChange": 0.4, "avgPrice": 0, "tradeVolume": 0,
                 "52WeekHigh": 26000.0}]
        return {"status": True, "data": {"fetched": rows}}


@contextmanager
def simulated_session(start, script):
    clock = SimClock(start)
    market = SimMarket(clock, script)
    saved = (m.time, m.now_ist, m.login, m.load_tokens)
    m.time = clock
    m.now_ist = lambda: clock.now
    m.login = lambda cfg, tries=3: market
    m.load_tokens = lambda symbols: ({"ABC": "111"}, {"ABC": 0.05})
    try:
        with workdir() as folder:
            (folder / "prep.json").write_text(json.dumps(
                {"date": clock.now.date().isoformat(), "data": {"ABC": prep_row()}}))
            yield folder, market, clock
    finally:
        m.time, m.now_ist, m.login, m.load_tokens = saved


def test_full_paper_session_from_open_to_close():
    script = [("09:00", 100.0), ("09:30", 100.2), ("09:35", 100.8), ("09:40", 100.9),
              ("09:41", 101.75), ("09:42", 102.6), ("09:43", 102.0)]
    with simulated_session(at("09:14"), script) as (folder, market, clock):
        m.cmd_run(cfg(symbols=["ABC"]))

        assert clock.now.time() > dt.time(15, 12)            # ran the whole day and stopped itself
        assert market.calls > 300                            # roughly one poll a minute

        signals = read_csv(folder, "signals.csv")
        assert len(signals) == 1 and signals[0]["time"] == "09:40" and signals[0]["traded"] == "True"

        t = read_csv(folder, "trades.csv")
        assert len(t) == 1
        assert t[0]["entry_time"] == "09:40" and t[0]["exit_time"] == "15:10"
        assert "square-off" in t[0]["reason"]
        expected = (102.6 - 100.9) * 247 + (102.0 - 100.9) * 248      # half at 2R, rest at the close
        assert abs(float(t[0]["pnl_rs"]) - expected) < 0.01


def test_weekend_does_nothing_and_does_not_even_log_in():
    saturday = dt.datetime(2026, 11, 14, 9, 0)
    with simulated_session(saturday, [("09:00", 100.0)]) as (folder, market, clock):
        m.cmd_run(cfg(symbols=["ABC"]))
        assert market.calls == 0 and not read_csv(folder, "trades.csv")


def test_holiday_from_config_does_nothing():
    with simulated_session(at("09:00"), [("09:00", 100.0)]) as (folder, market, clock):
        m.cmd_run(cfg(symbols=["ABC"], holidays=[DAY.isoformat()]))
        assert market.calls == 0


def test_live_flag_alone_stays_in_paper():
    script = [("09:00", 100.0), ("09:30", 100.2), ("09:35", 100.8), ("09:40", 100.9)]
    with simulated_session(at("09:14"), script) as (folder, market, clock):
        market.placeOrder = lambda params: (_ for _ in ()).throw(AssertionError("real order sent"))
        m.cmd_run(cfg(symbols=["ABC"], mode="paper"), want_live=True)
        assert read_csv(folder, "trades.csv")[0]["mode"] == "PAPER"


# --------------------------------------------------------------------- config

def _write_config(folder, **values):
    base = {"api_key": "realkey", "client_code": "A1", "pin": "1234", "totp_secret": "SECRET"}
    base.update(values)
    path = Path(folder) / "config.json"
    path.write_text(json.dumps(base))
    os.chmod(path, 0o600)


def _exits(fn):
    try:
        fn()
    except SystemExit as exc:
        return str(exc)
    raise AssertionError("expected the bot to stop with a message")


def test_config_with_placeholders_is_refused():
    with workdir() as folder:
        _write_config(folder, api_key="SMARTAPI_DASHBOARD_SE_API_KEY")
        assert "api_key" in _exits(m.load_config)


def test_config_missing_file_explains_what_to_do():
    with workdir():
        assert "config.example.json" in _exits(m.load_config)


def test_config_rejects_an_unknown_mode():
    with workdir() as folder:
        _write_config(folder, mode="real")
        assert "mode" in _exits(m.load_config)


def test_config_defaults_to_paper_and_keeps_user_values():
    with workdir() as folder:
        _write_config(folder, capital=50000, pin=4321)
        c = m.load_config()
        assert c["mode"] == "paper" and c["confirm_live"] is False
        assert c["capital"] == 50000 and c["pin"] == "4321" and c["rvol_min"] == 3.0


def _run_without_pytest() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            with open(os.devnull, "w") as quiet:             # the bot prints as it trades
                old, sys.stdout = sys.stdout, quiet
                try:
                    fn()
                finally:
                    sys.stdout = old
            print("  PASS  " + name)
        except Exception as exc:
            failed.append(name)
            print("  FAIL  %s: %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_without_pytest())
