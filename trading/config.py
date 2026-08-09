"""Typed configuration loaded from YAML + environment variables.

Credentials never live in the YAML file. Each account declares an
``env_prefix`` and the four secrets are read from the environment
(or a local .env), so the config file stays safe to commit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import Any

import yaml

from .utils import parse_hhmm

REQUIRED_SECRETS = ("API_KEY", "CLIENT_CODE", "MPIN", "TOTP_SECRET")


@dataclass
class AccountConfig:
    name: str
    env_prefix: str
    enabled: bool = True
    capital: float = 100_000.0
    risk_multiplier: float = 1.0
    is_data_source: bool = False

    # Populated from the environment by ``load_credentials``.
    api_key: str = field(default="", repr=False)
    client_code: str = field(default="", repr=False)
    mpin: str = field(default="", repr=False)
    totp_secret: str = field(default="", repr=False)

    def load_credentials(self) -> None:
        missing = []
        for suffix in REQUIRED_SECRETS:
            var = f"{self.env_prefix}_{suffix}"
            value = os.getenv(var, "").strip()
            if not value:
                missing.append(var)
            setattr(self, suffix.lower(), value)
        if missing:
            raise RuntimeError(
                f"Account '{self.name}' is missing environment variables: "
                f"{', '.join(missing)}. Copy .env.example to .env and fill them in."
            )


@dataclass
class StrategyConfig:
    entry_interval: str = "FIVE_MINUTE"
    htf_multiplier: int = 3  # 5-minute entry bars -> 15-minute trend bars

    ema_fast: int = 9
    ema_slow: int = 21
    htf_ema_fast: int = 9
    htf_ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    volume_lookback: int = 20
    breakout_lookback: int = 12
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0

    # Gates. A setup must clear every one of these before scoring even starts.
    adx_min: float = 20.0
    volume_multiple: float = 1.3
    rsi_long_band: tuple[float, float] = (52.0, 72.0)
    rsi_short_band: tuple[float, float] = (28.0, 48.0)
    min_atr_pct: float = 0.25  # skip dead symbols: ATR as % of price
    max_atr_pct: float = 4.0   # skip news-driven chaos

    min_score: int = 14  # out of max_score (19)

    # Exits
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 3.0
    breakeven_at_r: float = 1.0
    trail_atr_multiple: float = 2.0

    def __post_init__(self) -> None:
        self.rsi_long_band = tuple(self.rsi_long_band)  # type: ignore[assignment]
        self.rsi_short_band = tuple(self.rsi_short_band)  # type: ignore[assignment]


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_daily_profit_pct: float = 4.0  # stop trading once the day is made
    max_open_positions: int = 3
    max_trades_per_day: int = 6
    max_position_value_pct: float = 25.0
    max_trades_per_symbol_per_day: int = 2
    # Ceiling on the combined value of all open positions, as a percentage of
    # buying power. Below 100 with leverage_multiple at 1.0 this guarantees the
    # account never commits more than the cash it holds.
    max_total_exposure_pct: float = 95.0


@dataclass
class SessionConfig:
    entry_start: dtime = dtime(9, 30)
    entry_end: dtime = dtime(14, 45)
    square_off: dtime = dtime(15, 10)
    opening_range_minutes: int = 15
    poll_seconds: int = 20
    bar_close_buffer_seconds: int = 8


@dataclass
class ExecutionConfig:
    mode: str = "paper"  # paper | live
    product_type: str = "INTRADAY"
    order_type: str = "MARKET"
    slippage_bps: float = 5.0            # paper-fill realism
    place_broker_stoploss: bool = True   # server-side SL-M survives a crash
    confirm_live: bool = False           # must be true AND --live to trade real money
    # 1.0 = cash only, no broker margin. Anything higher borrows from the
    # broker and magnifies losses by the same factor.
    leverage_multiple: float = 1.0


@dataclass
class ScreenerConfig:
    """Daily shortlist. When enabled, the engine screens `universe` each
    morning and trades only what survives, instead of a fixed `symbols` list."""

    enabled: bool = False
    universe: list[str] = field(default_factory=list)
    shortlist_size: int = 6
    lookback_days: int = 20

    # Hard filters, all applied to daily candles before ranking.
    min_turnover_crore: float = 50.0  # average daily traded value
    min_price: float = 100.0
    max_price: float = 5000.0
    min_atr_pct: float = 1.0
    max_atr_pct: float = 6.0
    min_adx: float = 15.0

    # Reject anything one share of which exceeds the per-position budget.
    # Without leverage this is what stops the shortlist filling with names the
    # account cannot actually buy.
    require_affordable: bool = True

    # Ranking weights (liquidity, volatility, trend). Normalised internally.
    weight_liquidity: float = 0.4
    weight_volatility: float = 0.3
    weight_trend: float = 0.3


@dataclass
class NotificationConfig:
    """Push alerts to a phone. The bot token and chat id come from the
    environment (``<env_prefix>_BOT_TOKEN`` / ``<env_prefix>_CHAT_ID``), never
    from this file."""

    enabled: bool = False
    env_prefix: str = "TELEGRAM"
    notify_entries: bool = True
    notify_exits: bool = True
    notify_errors: bool = True
    daily_summary: bool = True


@dataclass
class AppConfig:
    accounts: list[AccountConfig]
    symbols: list[str]
    exchange: str = "NSE"
    holidays: list[str] = field(default_factory=list)
    warmup_days: int = 5
    log_level: str = "INFO"
    state_dir: str = "state"
    log_dir: str = "logs"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)

    @property
    def data_account(self) -> AccountConfig:
        enabled = [a for a in self.accounts if a.enabled]
        if not enabled:
            raise RuntimeError("No enabled accounts in config.")
        for a in enabled:
            if a.is_data_source:
                return a
        return enabled[0]

    @property
    def trading_accounts(self) -> list[AccountConfig]:
        return [a for a in self.accounts if a.enabled]

    @property
    def total_capital(self) -> float:
        return sum(a.capital for a in self.trading_accounts)


def _build_session(raw: dict[str, Any]) -> SessionConfig:
    cfg = SessionConfig()
    for key in ("entry_start", "entry_end", "square_off"):
        if key in raw:
            setattr(cfg, key, parse_hhmm(raw[key]))
    for key in ("opening_range_minutes", "poll_seconds", "bar_close_buffer_seconds"):
        if key in raw:
            setattr(cfg, key, int(raw[key]))
    return cfg


def _build(cls, raw: dict[str, Any]):
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**raw)


def load_config(path: str | Path, load_secrets: bool = True) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Start from config.example.yaml."
        )
    raw = yaml.safe_load(path.read_text()) or {}

    accounts_raw = raw.pop("accounts", None)
    if not accounts_raw:
        raise ValueError("Config must define at least one account.")
    accounts = [_build(AccountConfig, dict(a)) for a in accounts_raw]

    names = [a.name for a in accounts]
    if len(names) != len(set(names)):
        raise ValueError("Account names must be unique.")
    if sum(1 for a in accounts if a.is_data_source) > 1:
        raise ValueError("At most one account may set is_data_source: true.")

    symbols = raw.pop("symbols", None) or []

    strategy = _build(StrategyConfig, raw.pop("strategy", {}) or {})
    risk = _build(RiskConfig, raw.pop("risk", {}) or {})
    session = _build_session(raw.pop("session", {}) or {})
    execution = _build(ExecutionConfig, raw.pop("execution", {}) or {})
    screener = _build(ScreenerConfig, raw.pop("screener", {}) or {})
    notifications = _build(NotificationConfig, raw.pop("notifications", {}) or {})

    if not symbols and not (screener.enabled and screener.universe):
        raise ValueError(
            "Config must define a non-empty 'symbols' list, or enable the "
            "screener with a non-empty 'universe'."
        )

    cfg = AppConfig(
        accounts=accounts,
        symbols=[str(s).strip().upper() for s in symbols],
        strategy=strategy,
        risk=risk,
        session=session,
        execution=execution,
        screener=screener,
        notifications=notifications,
        **raw,
    )

    if load_secrets:
        for account in cfg.trading_accounts:
            account.load_credentials()

    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    s, r, sess = cfg.strategy, cfg.risk, cfg.session
    if cfg.execution.mode not in ("paper", "live"):
        raise ValueError("execution.mode must be 'paper' or 'live'.")
    if not (0 < r.risk_per_trade_pct <= 5):
        raise ValueError("risk.risk_per_trade_pct must be between 0 and 5.")
    if r.max_daily_loss_pct <= r.risk_per_trade_pct:
        raise ValueError("risk.max_daily_loss_pct must exceed risk_per_trade_pct.")
    if not (sess.entry_start < sess.entry_end < sess.square_off):
        raise ValueError("session times must satisfy entry_start < entry_end < square_off.")
    if s.atr_stop_multiple <= 0 or s.atr_target_multiple <= 0:
        raise ValueError("ATR stop/target multiples must be positive.")
    if s.atr_target_multiple < s.atr_stop_multiple:
        raise ValueError(
            "atr_target_multiple below atr_stop_multiple gives a reward:risk under 1:1."
        )
    if any(a.capital <= 0 for a in cfg.trading_accounts):
        raise ValueError("Every enabled account needs a positive 'capital'.")

    if cfg.execution.leverage_multiple < 1.0:
        raise ValueError("execution.leverage_multiple cannot be below 1.0.")
    if not (0 < r.max_total_exposure_pct <= 100.0):
        raise ValueError(
            "risk.max_total_exposure_pct must be between 0 and 100. It is a "
            "share of buying power — use execution.leverage_multiple to borrow."
        )
    if cfg.screener.enabled:
        s = cfg.screener
        if not s.universe:
            raise ValueError("screener.enabled is true but screener.universe is empty.")
        if s.shortlist_size < 1:
            raise ValueError("screener.shortlist_size must be at least 1.")
        if s.min_price >= s.max_price:
            raise ValueError("screener.min_price must be below max_price.")
        if s.min_atr_pct >= s.max_atr_pct:
            raise ValueError("screener.min_atr_pct must be below max_atr_pct.")
        if s.weight_liquidity + s.weight_volatility + s.weight_trend <= 0:
            raise ValueError("Screener ranking weights must sum to more than zero.")
