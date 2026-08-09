#!/usr/bin/env python3
"""Entry point for the Angel One intraday trading engine.

    python run_trading.py --check              validate config + connectivity
    python run_trading.py                      run in paper mode
    python run_trading.py --live               run live (also needs confirm_live)

Live trading requires BOTH `execution.confirm_live: true` in the config AND
the --live flag. Either one alone leaves the engine in paper mode; that is
deliberate, so no single typo can start trading real money.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from trading.config import load_config
from trading.engine import TradingEngine
from trading.utils import get_logger, now_ist, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Angel One intraday trading engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config.yaml", help="Path to the YAML config.")
    parser.add_argument("--env", default=".env", help="Path to the environment file.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Arm live trading. Requires execution.confirm_live: true as well.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config, log in, resolve symbols and exit without trading.",
    )
    return parser


def run_check(cfg, log) -> int:
    from trading.broker import AccountPool
    from trading.instruments import InstrumentMaster

    log.info("Configuration parsed successfully.")
    log.info("Mode in config      : %s", cfg.execution.mode)
    log.info("Accounts enabled    : %d", len(cfg.trading_accounts))
    log.info("Combined capital    : %s", f"{cfg.total_capital:,.0f}")
    log.info("Risk per trade      : %.2f%% (%s per trade)",
             cfg.risk.risk_per_trade_pct,
             f"{cfg.total_capital * cfg.risk.risk_per_trade_pct / 100:,.0f}")
    log.info("Daily loss limit    : %.2f%% (%s)", cfg.risk.max_daily_loss_pct,
             f"{cfg.total_capital * cfg.risk.max_daily_loss_pct / 100:,.0f}")
    log.info("Signal threshold    : %d/19", cfg.strategy.min_score)

    pool = AccountPool(cfg.trading_accounts)
    try:
        pool.connect_all(cfg.data_account.name)
        for broker in pool.connected_brokers:
            margin = broker.available_margin()
            log.info("[%s] connected. Available margin: %s", broker.name,
                     f"{margin:,.0f}" if margin is not None else "unknown")

        master = InstrumentMaster(cfg.state_dir)
        master.load()
        instruments = master.resolve_all(cfg.symbols, cfg.exchange)
        for symbol, inst in instruments.items():
            ltp = pool.data.ltp(inst.exchange, inst.trading_symbol, inst.token)
            log.info("  %-12s token=%-8s tick=%.2f lot=%-4d ltp=%s",
                     symbol, inst.token, inst.tick_size, inst.lot_size,
                     f"{ltp:,.2f}" if ltp else "n/a")

        log.info("Check passed — the system is ready to run.")
        return 0
    except Exception as exc:
        log.error("Check failed: %s", exc)
        return 1
    finally:
        pool.disconnect_all()


def main() -> int:
    args = build_parser().parse_args()

    env_path = Path(args.env)
    if env_path.exists():
        load_dotenv(env_path)

    log = setup_logging(level="INFO")
    log = get_logger("main")
    log.info("Angel One intraday engine starting at %s IST",
             now_ist().strftime("%Y-%m-%d %H:%M:%S"))

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        log.error("Configuration error: %s", exc)
        return 1

    setup_logging(cfg.log_dir, cfg.log_level)

    if args.check:
        return run_check(cfg, log)

    going_live = args.live and cfg.execution.mode == "live" and cfg.execution.confirm_live
    if going_live:
        log.warning("!" * 74)
        log.warning("LIVE TRADING ARMED — orders will be placed with real money.")
        log.warning("Accounts: %s", ", ".join(a.name for a in cfg.trading_accounts))
        log.warning("Press Ctrl-C within 10 seconds to abort.")
        log.warning("!" * 74)
        try:
            import time

            time.sleep(10)
        except KeyboardInterrupt:
            log.info("Aborted before any order was placed.")
            return 0

    engine = TradingEngine(cfg, allow_live=args.live)
    try:
        engine.setup()
        engine.run()
    except KeyboardInterrupt:
        log.warning("Interrupted — squaring off and shutting down.")
        engine.shutdown()
    except Exception:
        log.exception("Fatal error. Attempting to square off before exit.")
        try:
            engine.shutdown()
        except Exception:
            log.exception("Shutdown also failed — CHECK YOUR POSITIONS MANUALLY.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
