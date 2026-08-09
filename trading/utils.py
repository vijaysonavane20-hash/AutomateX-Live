"""Shared helpers: IST clock, market calendar, rate limiting, logging."""
from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from collections import deque
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(value: str | dtime) -> dtime:
    """Accept 'HH:MM' (or datetime.time) and return a time.

    YAML can silently turn an unquoted 9:20 into an integer, so we reject
    anything that is not a string or a time rather than guessing.
    """
    if isinstance(value, dtime):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"Time value {value!r} must be a quoted 'HH:MM' string in the config"
        )
    hh, mm = value.strip().split(":")[:2]
    return dtime(int(hh), int(mm))


def combine_ist(day: date, t: dtime) -> datetime:
    return datetime.combine(day, t).replace(tzinfo=IST)


class MarketCalendar:
    """Weekend + explicit holiday list. NSE holidays change yearly, so they
    live in the config file rather than being hardcoded here."""

    def __init__(self, holidays: Iterable[str] = ()):
        self.holidays: set[date] = set()
        for h in holidays:
            self.holidays.add(date.fromisoformat(str(h)))

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def is_market_open(self, ts: datetime | None = None) -> bool:
        ts = ts or now_ist()
        return self.is_trading_day(ts.date()) and MARKET_OPEN <= ts.time() < MARKET_CLOSE

    def previous_trading_day(self, day: date) -> date:
        d = day - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d


class RateLimiter:
    """Thread-safe sliding-window limiter.

    Angel's SmartAPI enforces per-endpoint limits (historical candles are the
    tightest at ~3 requests/second) and answers violations with 403s that look
    like auth failures, so every API call goes through one of these.
    """

    def __init__(self, max_calls: int, period: float = 1.0, name: str = ""):
        self.max_calls = max(1, int(max_calls))
        self.period = float(period)
        self.name = name
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0])
            time.sleep(max(sleep_for, 0.01))


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO") -> logging.Logger:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("angel")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    fh = logging.handlers.TimedRotatingFileHandler(
        log_path / "trading.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"angel.{name}")


def round_to_tick(price: float, tick: float = 0.05) -> float:
    """Exchanges reject prices off the tick grid; always round before sending."""
    if tick <= 0:
        return round(float(price), 2)
    return round(round(float(price) / tick) * tick, 2)
