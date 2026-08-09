"""Angel One SmartAPI wrapper.

Everything that touches the network lives here so the strategy and engine stay
testable without credentials. Three things this layer is responsible for:

* **Rate limiting.** SmartAPI answers limit violations with 403s that look like
  auth failures. Each endpoint family gets its own limiter, set below the
  published ceilings.
* **Shape normalisation.** SmartAPI has changed its return shapes between
  releases (``placeOrder`` has returned a bare string, a dict, and a wrapped
  response). Callers get one consistent shape regardless.
* **Retries.** Transient network errors are retried with backoff; business
  rejections (insufficient funds, bad symbol) are not — those are surfaced.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import pyotp

from .config import AccountConfig
from .utils import IST, RateLimiter, get_logger, now_ist

try:  # package name changed across smartapi-python releases
    from SmartApi import SmartConnect
except ImportError:  # pragma: no cover
    try:
        from smartapi import SmartConnect  # type: ignore
    except ImportError:  # pragma: no cover
        SmartConnect = None  # type: ignore

log = get_logger("broker")

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# Comfortably below Angel's published per-second ceilings.
LIMITS = {
    "historical": RateLimiter(2, 1.0, "historical"),
    "order": RateLimiter(8, 1.0, "order"),
    "quote": RateLimiter(8, 1.0, "quote"),
    "general": RateLimiter(5, 1.0, "general"),
}


class BrokerError(RuntimeError):
    """A call reached Angel and was rejected on its merits."""


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None
    raw: Any
    message: str = ""


class AngelBroker:
    """One authenticated SmartAPI session for one account."""

    def __init__(self, account: AccountConfig):
        if SmartConnect is None:
            raise ImportError(
                "smartapi-python is not installed. Run: pip install -r requirements-trading.txt"
            )
        self.account = account
        self.name = account.name
        self.api: Any = None
        self.feed_token: str | None = None
        self._connected = False

    # ---------------------------------------------------------------- session

    def connect(self, max_retries: int = 3) -> None:
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                self.api = SmartConnect(api_key=self.account.api_key)
                totp = pyotp.TOTP(self.account.totp_secret).now()
                LIMITS["general"].acquire()
                resp = self.api.generateSession(
                    self.account.client_code, self.account.mpin, totp
                )
                if not isinstance(resp, dict) or not resp.get("status"):
                    message = (resp or {}).get("message", "unknown error")
                    raise BrokerError(f"Login rejected for '{self.name}': {message}")

                self.feed_token = self.api.getfeedToken()
                self._connected = True
                log.info("Account '%s' connected (%s).", self.name, self.account.client_code)
                return
            except BrokerError:
                raise  # a rejection will not fix itself on retry
            except Exception as exc:  # network / TOTP drift
                last_error = exc
                wait = 2**attempt
                log.warning(
                    "Login attempt %d/%d for '%s' failed: %s. Retrying in %ds.",
                    attempt, max_retries, self.name, exc, wait,
                )
                time.sleep(wait)
        raise BrokerError(f"Could not connect account '{self.name}': {last_error}")

    def disconnect(self) -> None:
        if self._connected and self.api is not None:
            try:
                LIMITS["general"].acquire()
                self.api.terminateSession(self.account.client_code)
                log.info("Account '%s' logged out.", self.name)
            except Exception as exc:
                log.warning("Logout failed for '%s': %s", self.name, exc)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------- market data

    def candles(
        self,
        exchange: str,
        token: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Historical OHLCV as a tz-aware, ascending, de-duplicated frame."""
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }

        for attempt in range(1, 4):
            try:
                LIMITS["historical"].acquire()
                resp = self.api.getCandleData(params)
                if not isinstance(resp, dict) or not resp.get("status"):
                    message = (resp or {}).get("message", "no status")
                    raise BrokerError(f"Candle request failed: {message}")

                rows = resp.get("data") or []
                if not rows:
                    return pd.DataFrame(columns=CANDLE_COLUMNS[1:])

                df = pd.DataFrame(rows, columns=CANDLE_COLUMNS)
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
                df = df.set_index("timestamp").astype(float)
                return df[~df.index.duplicated(keep="last")].sort_index()
            except BrokerError:
                raise
            except Exception as exc:
                if attempt == 3:
                    raise BrokerError(f"Candle fetch failed for token {token}: {exc}") from exc
                time.sleep(2**attempt)
        return pd.DataFrame(columns=CANDLE_COLUMNS[1:])

    def ltp(self, exchange: str, trading_symbol: str, token: str) -> float | None:
        try:
            LIMITS["quote"].acquire()
            resp = self.api.ltpData(exchange, trading_symbol, token)
            if isinstance(resp, dict) and resp.get("status"):
                return float(resp["data"]["ltp"])
            log.warning("LTP unavailable for %s: %s", trading_symbol, resp)
        except Exception as exc:
            log.warning("LTP request failed for %s: %s", trading_symbol, exc)
        return None

    # ----------------------------------------------------------------- orders

    @staticmethod
    def _extract_order_id(resp: Any) -> str | None:
        """SmartAPI has returned a bare id, {'data': {'orderid': ...}}, and
        {'orderid': ...} across versions. Accept all of them."""
        if resp is None:
            return None
        if isinstance(resp, str):
            return resp or None
        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, dict) and data.get("orderid"):
                return str(data["orderid"])
            if resp.get("orderid"):
                return str(resp["orderid"])
        return None

    def _send(self, params: dict[str, Any]) -> OrderResult:
        try:
            LIMITS["order"].acquire()
            resp = self.api.placeOrder(params)
            order_id = self._extract_order_id(resp)
            if order_id:
                return OrderResult(True, order_id, resp)
            message = (
                resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
            )
            return OrderResult(False, None, resp, message)
        except Exception as exc:
            return OrderResult(False, None, None, str(exc))

    def place_market_order(
        self,
        *,
        exchange: str,
        trading_symbol: str,
        token: str,
        side: str,
        quantity: int,
        product_type: str = "INTRADAY",
    ) -> OrderResult:
        params = {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": exchange,
            "ordertype": "MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(quantity)),
        }
        result = self._send(params)
        log.info(
            "[%s] %s %s x%d MARKET -> %s",
            self.name, side.upper(), trading_symbol, quantity,
            result.order_id or f"FAILED ({result.message})",
        )
        return result

    def place_stoploss_order(
        self,
        *,
        exchange: str,
        trading_symbol: str,
        token: str,
        side: str,
        quantity: int,
        trigger_price: float,
        product_type: str = "INTRADAY",
    ) -> OrderResult:
        """Server-side SL-M. This is the protection that survives the script
        dying, the machine sleeping, or the network dropping."""
        params = {
            "variety": "STOPLOSS",
            "tradingsymbol": trading_symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": exchange,
            "ordertype": "STOPLOSS_MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "price": "0",
            "triggerprice": f"{trigger_price:.2f}",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(quantity)),
        }
        result = self._send(params)
        log.info(
            "[%s] SL-M %s %s x%d @ trigger %.2f -> %s",
            self.name, side.upper(), trading_symbol, quantity, trigger_price,
            result.order_id or f"FAILED ({result.message})",
        )
        return result

    def cancel_order(self, order_id: str, variety: str = "STOPLOSS") -> bool:
        try:
            LIMITS["order"].acquire()
            self.api.cancelOrder(order_id, variety)
            return True
        except Exception as exc:
            log.warning("[%s] Cancel failed for order %s: %s", self.name, order_id, exc)
            return False

    def order_book(self) -> list[dict]:
        try:
            LIMITS["general"].acquire()
            resp = self.api.orderBook()
            if isinstance(resp, dict) and resp.get("status"):
                return resp.get("data") or []
        except Exception as exc:
            log.warning("[%s] Order book fetch failed: %s", self.name, exc)
        return []

    def order_status(self, order_id: str) -> str | None:
        for order in self.order_book():
            if str(order.get("orderid")) == str(order_id):
                return str(order.get("status", "")).lower()
        return None

    # -------------------------------------------------------------- portfolio

    def positions(self) -> list[dict]:
        try:
            LIMITS["general"].acquire()
            resp = self.api.position()
            if isinstance(resp, dict) and resp.get("status"):
                return resp.get("data") or []
        except Exception as exc:
            log.warning("[%s] Positions fetch failed: %s", self.name, exc)
        return []

    def available_margin(self) -> float | None:
        try:
            LIMITS["general"].acquire()
            resp = self.api.rmsLimit()
            if isinstance(resp, dict) and resp.get("status"):
                data = resp.get("data") or {}
                for key in ("availablecash", "net", "availableintradaypayin"):
                    if data.get(key) not in (None, ""):
                        return float(data[key])
        except Exception as exc:
            log.warning("[%s] RMS fetch failed: %s", self.name, exc)
        return None

    def open_intraday_positions(self) -> list[dict]:
        return [
            p
            for p in self.positions()
            if int(float(p.get("netqty") or 0)) != 0
            and str(p.get("producttype", "")).upper() == "INTRADAY"
        ]

    def square_off_all_intraday(self) -> int:
        """Flatten every open intraday position. Called at the session cutoff
        and on shutdown — deliberately independent of internal state so it
        still works if the engine's own bookkeeping has drifted."""
        closed = 0
        for pos in self.open_intraday_positions():
            qty = int(float(pos.get("netqty") or 0))
            if qty == 0:
                continue
            result = self.place_market_order(
                exchange=str(pos.get("exchange", "NSE")),
                trading_symbol=str(pos["tradingsymbol"]),
                token=str(pos["symboltoken"]),
                side="SELL" if qty > 0 else "BUY",
                quantity=abs(qty),
            )
            if result.ok:
                closed += 1
            else:
                log.error(
                    "[%s] SQUARE-OFF FAILED for %s x%d: %s — CLOSE THIS MANUALLY.",
                    self.name, pos.get("tradingsymbol"), abs(qty), result.message,
                )
        return closed


class AccountPool:
    """All enabled accounts. Signals come from one account's data feed; orders
    fan out to every account, each sized against its own capital."""

    def __init__(self, accounts: list[AccountConfig]):
        self.brokers: dict[str, AngelBroker] = {a.name: AngelBroker(a) for a in accounts}
        self._data_account_name: str | None = None

    def connect_all(self, data_account_name: str) -> None:
        self._data_account_name = data_account_name
        failures = []
        for name, broker in self.brokers.items():
            try:
                broker.connect()
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                log.error("Account '%s' failed to connect: %s", name, exc)

        if self._data_account_name not in self.brokers or not self.data.connected:
            raise BrokerError(
                f"The data-source account could not connect, so there is no market "
                f"data to trade on. Errors: {'; '.join(failures) or 'unknown'}"
            )
        if failures:
            log.warning(
                "Continuing with %d of %d accounts. Failed: %s",
                len(self.connected_brokers), len(self.brokers), "; ".join(failures),
            )

    @property
    def data(self) -> AngelBroker:
        if self._data_account_name is None:
            raise RuntimeError("connect_all() has not been called.")
        return self.brokers[self._data_account_name]

    @property
    def connected_brokers(self) -> list[AngelBroker]:
        return [b for b in self.brokers.values() if b.connected]

    def disconnect_all(self) -> None:
        for broker in self.brokers.values():
            broker.disconnect()


def default_warmup_window(days: int, end: datetime | None = None) -> tuple[datetime, datetime]:
    end = end or now_ist()
    # Pad for weekends/holidays; extra candles are harmless, missing ones are not.
    return end - timedelta(days=max(days * 2, 5)), end
