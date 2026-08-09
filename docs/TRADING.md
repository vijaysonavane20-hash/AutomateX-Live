# Angel One Intraday Trading Engine

An automated intraday system for Angel One (SmartAPI) that trades one
high-selectivity setup across one or more accounts, with hard risk limits, a
paper mode, and a backtester that runs the identical rules.

---

## Read this first

**There is no "most accurate" intraday setup.** Any script, service or course
that claims one is either selling something or measuring wrong. What actually
separates systems that survive from ones that don't:

| Myth | Reality |
|---|---|
| A better indicator combination finds the winning trades | Filters mostly remove bad trades; they don't create good ones |
| High win rate means profitable | A 90% win rate loses money if the 10% are 10x larger |
| The backtest returned 200%, so it works | Most backtests are broken. Even a correct one is an optimistic upper bound |
| More symbols and more trades means more profit | More trades means more costs and more marginal setups |

What this system actually optimises for is **expectancy** — average R per
trade — and **survivability**. It is built to lose small and often, and win
larger when conditions genuinely line up. It will have losing days and losing
weeks. The risk limits exist so those don't become a losing account.

Costs matter more than most people expect. Brokerage, STT, exchange fees, GST,
stamp duty and slippage together run roughly ₹40–60 per round trip on a
typical intraday equity order. At 6 trades a day that is a real hurdle the
strategy must clear before the first rupee of profit. **The backtester models
slippage but not brokerage** — subtract your own cost per trade from the
reported expectancy before believing any number.

---

## Picking the day's shares automatically

With `screener.enabled: true` the engine chooses what to trade each morning
instead of using a fixed list. At start-up it pulls daily candles for every
name in `screener.universe`, applies hard filters, ranks the survivors and
trades the top `shortlist_size`.

| Filter | Default | Why |
|---|---|---|
| Average daily turnover | ≥ ₹50 cr | The most important one. Wide spreads eat an intraday edge, and illiquid names gap through stops |
| Price band | ₹100–5,000 | Skips penny stocks and names too expensive to size |
| Daily ATR | 1–6% | Must move enough to pay for a round trip, but not be news-driven chaos |
| Daily ADX | ≥ 15 | Must actually trend — this strategy trades continuation |
| Affordable | on | One share must fit the per-position budget. Without leverage, anything pricier is untradeable |

Survivors are ranked on liquidity (40%), volatility (30%) and trend quality
(30%), using **percentile ranks** rather than raw values so one enormous
turnover can't drown out the other criteria.

The screen selects for **tradeable mechanics — not opinions**. It reads no
news, no fundamentals and no recommendations. A stock appearing on the
shortlist means "this is liquid and moving enough to trade", not "this will go
up".

Two behaviours worth knowing:

- **If nothing passes, nothing trades.** An empty shortlist means the engine
  idles for the day. That is the correct outcome, not a failure — relax the
  filters only if you've checked *why* it's empty.
- **If screening fails** (API trouble), it falls back to your fixed `symbols`
  list rather than dying.

Cost is one API call per universe symbol, so ~70 names adds about a minute to
start-up. You get the shortlist on Telegram before the market opens.

To go back to a fixed list, set `screener.enabled: false`.

---

## Leverage: off by default

`execution.leverage_multiple: 1.0` means **cash only**. Position sizes are
capped at the balance you actually hold — the engine never uses Angel's MIS
margin, even though the product type allows it.

Four independent ceilings apply to every position; the smallest wins:

1. **Risk budget** — 0.5% of capital ÷ stop distance (the usual driver)
2. **Per-position cap** — 25% of buying power
3. **Total exposure** — 95% of buying power, *minus what open positions
   already cost*
4. **Available cash** — live only, straight from Angel's RMS

Point 3 matters more than it looks. Without it, three positions could each
claim 25% of the full balance while ignoring each other, and per-position caps
would quietly compound into leverage. The engine subtracts capital already
committed, so the combined value of everything open can never exceed your cash:

```
Capital ₹100,000, RELIANCE @ ₹2,500, stop ₹2,450
  -> 10 shares = ₹25,000        (bound by risk)
With ₹90,000 already deployed
  -> 2 shares                   (bound by total exposure)
```

Raising `leverage_multiple` above 1.0 borrows from the broker. At 2x a 2%
adverse move becomes a 4% account loss, and Angel's auto-square-off — not your
stop — starts deciding when you exit. The engine logs a warning if you do it.

---

## What the strategy actually does

A **gated, score-based trend-continuation** setup on 5-minute bars, confirmed
by the 15-minute trend.

### Layer 1 — hard gates (any failure discards the bar)

These describe the only conditions the setup should trade in at all:

| Gate | Long requires | Why |
|---|---|---|
| Higher-timeframe trend | 15m EMA9 > EMA21 **and** 15m close > EMA21 | Never fight the dominant trend |
| ADX | `adx >= 20` and +DI > −DI | Confirms a trend exists; this is the main chop filter |
| VWAP | close > session VWAP | The intraday reference institutions trade against |
| Volatility | ATR between 0.25% and 4% of price | Skips symbols too quiet to pay for the spread, and news-driven chaos |

### Layer 2 — weighted confirmations (19 points; default threshold 14)

| Confirmation | Points |
|---|---|
| Donchian breakout of the last 12 bars | 3 |
| Opening-range (first 15 min) breakout | 3 |
| 5m EMA9 > EMA21 | 2 |
| RSI inside 52–72 (not yet exhausted) | 2 |
| Volume ≥ 1.3x its 20-bar average | 2 |
| Supertrend direction agrees | 2 |
| Strong candle (body ≥ 50% of range, close near the high) | 2 |
| Close beyond the 15m EMA9 | 2 |
| Higher-low continuation structure | 1 |

Shorts are the exact mirror. `min_score: 14` is a deliberately high bar — it
produces few trades. That is the intent, not a bug.

### Exits

- **Stop** — the wider of 1.5x ATR and the recent swing extreme, capped at
  3x ATR.
- **Target** — set from the *actual* stop distance at the configured 2:1
  ratio, so a widened structural stop can't silently degrade the reward:risk.
- **Breakeven** — once 1R is banked, the stop moves to entry.
- **Trail** — after that, a chandelier trail 2 ATRs behind the extreme.
- **Time** — everything is flattened at 15:10.

### Session shape

```
09:15  market opens — no entries (opening auction noise)
09:30  the opening range has formed; entries begin
14:45  no new entries
15:10  square off everything
15:30  market closes
```

---

## Setup

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-trading.txt
```

### 2. Get API credentials

1. Log in at <https://smartapi.angelbroking.com/> with your Angel One client ID.
2. **My Apps → Create App**, type **Trading API**. Note the **API key**.
3. Enable **TOTP** at <https://smartapi.angelbroking.com/enable-totp>. When the
   QR code appears, save the **base32 secret string** — not the 6-digit code.
   You need the secret so the script can generate codes itself.

### 3. Configure

```bash
cp .env.example .env               # credentials
cp config.example.yaml config.yaml # strategy, risk, symbols
```

Fill in `.env`:

```
ANGEL_PRIMARY_API_KEY=your_api_key
ANGEL_PRIMARY_CLIENT_CODE=A123456
ANGEL_PRIMARY_MPIN=1234
ANGEL_PRIMARY_TOTP_SECRET=BASE32SECRETSTRING
```

Both files are gitignored. **Never commit credentials, and never paste them
into an issue or a chat window.** Anyone with these can trade your account.

### 4. Verify

```bash
python run_trading.py --check
```

This logs in, resolves every symbol to its token, prints your available
margin and current prices, then exits without trading.

---

## Using it

### Backtest first

```bash
# 60 days of history pulled from Angel
python -m trading.backtest --days 60

# or offline from <SYMBOL>.csv files
python -m trading.backtest --csv-dir data/

# sweep the selectivity threshold
for s in 10 12 14 16; do python -m trading.backtest --days 60 --min-score $s; done
```

The report breaks results down by exit reason, by symbol, and **by score
bucket**. That last table is the one that matters: if higher scores don't show
better average R, the scoring isn't earning its keep on your universe and
`min_score` is an arbitrary number.

### Paper trade

```bash
python run_trading.py            # paper mode is the default
```

Run this for **at least a month of live sessions**. Paper results that don't
resemble the backtest mean the backtest is wrong — investigate before risking
anything.

### Go live

Live trading needs two independent switches, so no single typo can start it:

1. `execution.mode: live` **and** `execution.confirm_live: true` in `config.yaml`
2. the `--live` flag

```bash
python run_trading.py --live
```

You get a 10-second abort window before the first order.

Start with the smallest capital that lets a single share be sized, and leave
it there for weeks. Scale only on live evidence.

### Run it on a server, watch it from your phone

The engine needs an always-on machine — it cannot run on a phone, because iOS
and Android both suspend background apps. **[docs/DEPLOY.md](DEPLOY.md)** covers
where to host it, the systemd service and timer, and Telegram alerts so every
entry, exit and halt reaches your phone.

---

## Multiple accounts

Add a block per account. Each one trades the same signals, sized independently
against its own capital:

```yaml
accounts:
  - name: primary
    env_prefix: ANGEL_PRIMARY
    capital: 100000
    risk_multiplier: 1.0
    is_data_source: true      # exactly one account supplies market data

  - name: secondary
    env_prefix: ANGEL_SECONDARY
    capital: 50000
    risk_multiplier: 0.5      # this account takes half the risk
```

Then add `ANGEL_SECONDARY_*` variables to `.env`.

Notes:
- Exactly one account is the data source. If it can't connect the engine
  refuses to start — there would be no prices to trade on.
- If a *non-data* account fails to log in, the engine continues with the rest
  and logs the failure.
- Orders go out sequentially, rate-limited. Fills across accounts will differ
  by a few paise; that's normal.
- **Trading multiple accounts on behalf of other people is a regulated
  activity in India.** Running your own accounts is fine. Managing someone
  else's money, or distributing signals for a fee, requires SEBI registration.

---

## Risk controls

Every one of these is a hard block, not a warning.

| Control | Default | Effect |
|---|---|---|
| `risk_per_trade_pct` | 0.5% | Position size = risk budget ÷ stop distance |
| `max_daily_loss_pct` | 2% | Halts trading for the rest of the day |
| `max_daily_profit_pct` | 4% | Also halts — protects a good day from being given back |
| `max_open_positions` | 3 | Caps correlated exposure |
| `max_trades_per_day` | 6 | Stops revenge-trading a bad morning |
| `max_trades_per_symbol_per_day` | 2 | Stops one choppy symbol draining the day |
| `max_position_value_pct` | 25% | No single position dominates the account |
| `max_total_exposure_pct` | 95% | Combined value of all open positions. This is what makes "no leverage" real |
| `leverage_multiple` | 1.0 | Cash only. Above 1.0 borrows from the broker |

Position sizing is fixed-fractional: risk a constant small fraction, and let
the stop distance decide the quantity. A wide stop gets a small position, a
tight stop a larger one — so every trade risks the same rupee amount.

**The 0.5% default is not conservative, it is normal.** At 2% per trade, an
ordinary run of six losses costs a quarter of the account.

### What protects you if the script dies

When `place_broker_stoploss: true` (the default), a server-side SL-M order is
placed at Angel immediately after every entry. If this process crashes, your
machine sleeps, or your internet drops, **that order is still live at the
exchange**. The engine's own stop logic is the faster of the two, not the only
one.

This still isn't total cover. If an entry fills but the SL order is rejected,
the log says so loudly — read your logs. And always confirm your broker's own
auto-square-off time; `square_off` is set to 15:10 to run comfortably ahead of
it.

---

## Tuning

Change **one thing at a time**, and re-run the backtest across at least 60
days before and after.

| Symptom | Try |
|---|---|
| Shortlist is empty every day | Lower `screener.min_turnover_crore`, or widen the ATR band. Check the log for which filter rejected what |
| Shortlist picks names you can't size | Lower `screener.max_price`, or raise capital. `require_affordable` already drops the worst cases |
| Too few trades | Lower `min_score` (14 → 12), or `adx_min` (20 → 18) |
| Too many marginal trades | Raise `min_score` (14 → 16) |
| Stopped out then price runs your way | Raise `atr_stop_multiple` (1.5 → 2.0) |
| Winners give back profit | Lower `trail_atr_multiple` (2.0 → 1.5) |
| Good entries, weak follow-through | Raise `adx_min` to 25 |
| Chopped up midday | Narrow the window: `entry_end: "13:30"` |

**The trap:** tune long enough and the backtest will look excellent on that
period and fail forward. Guard against it — hold out the most recent 20% of
your data, tune on the rest, then check the holdout once. If the holdout
disagrees, the tuning was curve-fitting.

---

## Files

```
run_trading.py            entry point (--check / paper / --live)
config.example.yaml       annotated configuration template
.env.example              credential template
requirements-trading.txt  dependencies
trading/
  config.py               typed YAML + env config, with validation
  utils.py                IST clock, market calendar, rate limiting, logging
  instruments.py          scrip master download/cache, symbol -> token
  broker.py               SmartAPI wrapper + multi-account pool
  data.py                 candle store; closed-bar access
  indicators.py           pandas-only EMA/RSI/ATR/ADX/VWAP/Supertrend
  screener.py             daily shortlist — picks which shares to trade
  strategy.py             the trading rules (gates + scoring)
  risk.py                 position sizing, leverage and daily limits
  positions.py            position model and trailing-stop logic
  execution.py            paper and live executors
  engine.py               the live loop
  notifier.py             Telegram alerts (non-blocking, fail-safe)
  backtest.py             historical simulation of the same rules
deploy/                   systemd service + timer for a server
docs/DEPLOY.md            server setup, Telegram alerts, phone monitoring
tests/test_trading.py     60 tests, no credentials or network needed
```

Run the tests with `python -m pytest tests/test_trading.py -v`, or
`python tests/test_trading.py` if you don't have pytest.

---

## Why you can trust the backtest more than most

Backtests usually lie in predictable ways. These are handled:

- **Lookahead** — every backward-looking window is shifted by one bar, and a
  test asserts a signal computed with partial history is byte-identical to one
  computed with the full series.
- **Fill timing** — signals are computed on a bar's close and filled at the
  *next* bar's open, matching what the live engine does when it wakes after a
  close.
- **Optimistic fills** — slippage is applied to every fill in the direction
  that hurts. When a bar's range covers both stop and target, the **stop** is
  assumed.
- **Per-symbol fantasy** — limits apply across the portfolio, so results
  reflect the capital you actually have.
- **Gap risk** — a signal that gaps past its stop before entry is discarded,
  not filled at a price that was never available.

Still unmodelled: brokerage and taxes, queue position, partial fills, and
spreads widening exactly when everyone's stops trigger. Treat the reported
expectancy as an **upper bound**.

---

## Troubleshooting

| Problem | Cause / fix |
|---|---|
| `Invalid TOTP` | Server clock drift. Sync time (`w32tm /resync`, or `ntpdate`). Check you saved the *secret*, not a 6-digit code |
| `AB1007` / auth errors during the day | Usually rate limiting, not auth. The limiters are already conservative; reduce your symbol count |
| `Symbol 'X' not found` | Use the exact NSE symbol. Cash symbols resolve as `X-EQ` automatically |
| No trades for days | Expected at `min_score: 14`. Confirm with a backtest before lowering it |
| `sized to zero` in the logs | Stop distance too wide for your capital. Add capital, or trade lower-priced symbols |
| Orders rejected — insufficient funds | Angel's RMS is the final authority. The 5x intraday leverage assumption in sizing is a guess, not a promise |

---

## Legal and practical

- Automated trading on a personal account through the official SmartAPI is
  permitted. Angel's terms and SEBI's algo-trading framework govern what you
  may do; read them.
- Running strategies for **other people**, or selling signals, needs SEBI
  registration.
- Most retail intraday traders lose money. SEBI's own studies put the figure
  around 70% for equity intraday and higher in F&O. Automation removes
  emotional errors; it does not create an edge.
- Nothing here is investment advice. You are responsible for every order this
  code places.

**Start in paper mode. Stay there until the numbers are boring.**
