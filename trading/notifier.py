"""Push notifications, so a session running on a server can be watched from a
phone.

Two rules govern everything here:

1. **A notification must never delay a trade.** Messages are queued and sent by
   a background thread. If Telegram is slow, unreachable, or rate-limiting, the
   trading loop does not wait for it — a hung HTTP call between a stop trigger
   and the exit order is a real way to lose money.
2. **A notification must never crash the engine.** Every failure is swallowed
   and logged. A dead notifier degrades to silence, never to an exception.

Sending is one-way by design. Accepting commands from a chat would mean anyone
who obtains the bot token could place orders in your account.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from .utils import get_logger

log = get_logger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
SHUTDOWN = object()


class Notifier(ABC):
    @abstractmethod
    def send(self, text: str) -> None: ...

    def close(self) -> None:
        return None


class NullNotifier(Notifier):
    """Used when notifications are disabled or misconfigured."""

    def send(self, text: str) -> None:
        return None


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, timeout: int = 10):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._failures = 0
        self._worker = threading.Thread(
            target=self._run, name="telegram-notifier", daemon=True
        )
        self._worker.start()

    def send(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            # Dropping a message is always better than blocking the caller.
            log.warning("Notification queue full — message dropped.")

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is SHUTDOWN:
                self._queue.task_done()
                return
            self._deliver(str(item))
            self._queue.task_done()

    def _deliver(self, text: str) -> None:
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:4000],  # Telegram's hard limit is 4096
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()

        try:
            request = urllib.request.Request(
                TELEGRAM_API.format(token=self.token), data=payload
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
            if not body.get("ok"):
                self._log_failure(body.get("description", "unknown error"))
            else:
                self._failures = 0
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._log_failure(str(exc))

    def _log_failure(self, reason: str) -> None:
        self._failures += 1
        # Log the first few, then go quiet so a persistent outage cannot flood
        # the log file for the rest of the session.
        if self._failures <= 3:
            log.warning("Telegram delivery failed (%s). Trading is unaffected.", reason)
        elif self._failures == 4:
            log.warning("Telegram still failing — suppressing further notices.")

    def close(self) -> None:
        try:
            self._queue.put_nowait(SHUTDOWN)
            self._worker.join(timeout=8)
        except Exception:
            pass


def build_notifier(cfg, load_env: bool = True) -> Notifier:
    """Construct from config. Any missing piece degrades to NullNotifier with
    a clear log line rather than failing start-up."""
    notif = getattr(cfg, "notifications", None)
    if notif is None or not notif.enabled:
        return NullNotifier()

    if not load_env:
        return NullNotifier()

    token = os.getenv(f"{notif.env_prefix}_BOT_TOKEN", "").strip()
    chat_id = os.getenv(f"{notif.env_prefix}_CHAT_ID", "").strip()

    if not token or not chat_id:
        log.warning(
            "Notifications are enabled but %s_BOT_TOKEN / %s_CHAT_ID are not set — "
            "running without them.",
            notif.env_prefix, notif.env_prefix,
        )
        return NullNotifier()

    log.info("Telegram notifications enabled.")
    return TelegramNotifier(token, chat_id)


# ---------------------------------------------------------------- formatting
# Kept here so message wording stays in one place rather than scattered
# through the engine.


def fmt_startup(mode: str, accounts: int, capital: float, symbols: int) -> str:
    return (
        f"<b>🟢 Engine started — {mode}</b>\n"
        f"Accounts: {accounts}\n"
        f"Capital: ₹{capital:,.0f}\n"
        f"Symbols: {symbols}"
    )


def fmt_entry(position, quantity: int) -> str:
    arrow = "🟩 LONG" if position.side == "LONG" else "🟥 SHORT"
    return (
        f"<b>{arrow} {position.symbol}</b>\n"
        f"Entry: ₹{position.entry_ref:,.2f}  x{quantity}\n"
        f"Stop: ₹{position.stop:,.2f}   Target: ₹{position.target:,.2f}\n"
        f"Score: {position.score}/19\n"
        f"<i>{', '.join(position.reasons)}</i>"
    )


def fmt_exit(position, pnl: float, reason: str) -> str:
    icon = "✅" if pnl > 0 else "❌"
    return (
        f"<b>{icon} Closed {position.side} {position.symbol}</b>\n"
        f"Reason: {reason}\n"
        f"PnL: <b>₹{pnl:+,.0f}</b> ({position._closed_r():+.2f}R)"
    )


def fmt_halt(reason: str, pnl: float) -> str:
    return (
        f"<b>🛑 Trading halted for the day</b>\n"
        f"{reason}\n"
        f"Realised: ₹{pnl:+,.0f}"
    )


def fmt_summary(trades: int, wins: int, pnl: float, mode: str) -> str:
    if trades == 0:
        return f"<b>🔵 Session over — {mode}</b>\nNo trades taken."
    return (
        f"<b>🔵 Session over — {mode}</b>\n"
        f"Trades: {trades}  ({wins}W / {trades - wins}L)\n"
        f"Win rate: {100.0 * wins / trades:.0f}%\n"
        f"Net PnL: <b>₹{pnl:+,.0f}</b>"
    )


def fmt_error(message: str) -> str:
    return f"<b>⚠️ Engine problem</b>\n{message}"
