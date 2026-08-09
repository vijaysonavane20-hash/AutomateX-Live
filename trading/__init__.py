"""Angel One (SmartAPI) intraday trading system.

Layout:
    config.py       typed YAML + env configuration
    utils.py        IST clock, market calendar, rate limiting, logging
    instruments.py  scrip master download, cache, symbol -> token
    broker.py       SmartAPI wrapper and the multi-account pool
    data.py         candle store; closed-bar access
    indicators.py   pandas-only EMA/RSI/ATR/ADX/VWAP/Supertrend
    strategy.py     gated, score-based signal engine (the trading rules)
    risk.py         position sizing and daily limits
    positions.py    position/leg model and trailing-stop logic
    execution.py    paper and live executors
    engine.py       the live loop
    backtest.py     historical simulation of the same rules
"""

__version__ = "1.0.0"
