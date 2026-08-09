"""Instrument master (scrip master) download, cache and symbol lookup.

Angel publishes the full contract list as a single JSON file that is refreshed
each morning. Tokens are not stable across days for derivatives, so the cache
is keyed by date and re-downloaded on the first run of each session.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .utils import get_logger, now_ist

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

log = get_logger("instruments")


@dataclass(frozen=True)
class Instrument:
    token: str
    trading_symbol: str
    name: str
    exchange: str
    lot_size: int
    tick_size: float
    instrument_type: str

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.trading_symbol}"


class InstrumentMaster:
    def __init__(self, cache_dir: str | Path = "state", timeout: int = 60):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._raw: list[dict] = []
        self._by_key: dict[str, Instrument] = {}

    def _cache_path(self, day: date) -> Path:
        return self.cache_dir / f"scrip_master_{day.isoformat()}.json"

    def load(self, force_refresh: bool = False) -> None:
        today = now_ist().date()
        cache = self._cache_path(today)

        if cache.exists() and not force_refresh:
            log.info("Loading cached instrument master: %s", cache.name)
            self._raw = json.loads(cache.read_text())
        else:
            log.info("Downloading instrument master from Angel (~10 MB)...")
            with urllib.request.urlopen(SCRIP_MASTER_URL, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8")
            self._raw = json.loads(payload)
            cache.write_text(payload)
            log.info("Instrument master cached: %d contracts", len(self._raw))
            self._prune_old_caches(today)

        self._index()

    def _prune_old_caches(self, today: date) -> None:
        for old in self.cache_dir.glob("scrip_master_*.json"):
            if old.name != self._cache_path(today).name:
                old.unlink(missing_ok=True)

    def _index(self) -> None:
        self._by_key.clear()
        for row in self._raw:
            try:
                # tick_size arrives in hundredths of a rupee (500 -> 0.05)
                tick = float(row.get("tick_size") or 5) / 100.0
                instrument = Instrument(
                    token=str(row["token"]),
                    trading_symbol=str(row["symbol"]).upper(),
                    name=str(row.get("name") or "").upper(),
                    exchange=str(row["exch_seg"]).upper(),
                    lot_size=int(float(row.get("lotsize") or 1)),
                    tick_size=tick if tick > 0 else 0.05,
                    instrument_type=str(row.get("instrumenttype") or ""),
                )
            except (KeyError, ValueError, TypeError):
                continue
            self._by_key[instrument.key] = instrument

    def resolve(self, symbol: str, exchange: str = "NSE") -> Instrument:
        """Resolve a user-supplied symbol to a tradable contract.

        Accepts 'SBIN' or 'SBIN-EQ' for cash, and exact trading symbols for
        derivatives (e.g. 'NIFTY28AUG25FUT').
        """
        if not self._by_key:
            raise RuntimeError("Instrument master not loaded — call load() first.")

        exchange = exchange.upper()
        symbol = symbol.strip().upper()

        for candidate in (symbol, f"{symbol}-EQ"):
            found = self._by_key.get(f"{exchange}:{candidate}")
            if found:
                return found

        # Fall back to a name match restricted to the requested exchange.
        matches = [
            i
            for i in self._by_key.values()
            if i.exchange == exchange and i.name == symbol
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            options = ", ".join(sorted(m.trading_symbol for m in matches)[:8])
            raise ValueError(
                f"'{symbol}' is ambiguous on {exchange}. Use an exact trading "
                f"symbol, e.g. one of: {options}"
            )

        raise ValueError(f"Symbol '{symbol}' not found on {exchange}.")

    def resolve_all(self, symbols: list[str], exchange: str = "NSE") -> dict[str, Instrument]:
        resolved: dict[str, Instrument] = {}
        for sym in symbols:
            try:
                resolved[sym] = self.resolve(sym, exchange)
            except ValueError as exc:
                log.error("Skipping %s: %s", sym, exc)
        if not resolved:
            raise RuntimeError("None of the configured symbols could be resolved.")
        return resolved
