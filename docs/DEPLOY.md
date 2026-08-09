# Running this system (and watching it from your iPhone)

## Can it run *on* an iPhone?

**No — and you should not try.** This isn't a limitation of the code, it's how
iOS works:

- iOS suspends background apps within seconds to a few minutes. This engine
  must stay awake and poll continuously from 09:15 to 15:10.
- Python environments on iOS (Pythonista, a-Shell, Pyto) stop executing the
  moment you lock the screen or switch apps.
- A phone switching between Wi-Fi and mobile data drops connections mid-session.
- If iOS kills the process while a position is open, nothing manages that
  position. The broker-side SL-M order would be your only protection.

A trading engine needs an always-on machine. **The iPhone is the right tool for
watching and controlling it — not for running it.**

The setup below gives you exactly that: the engine runs on a small server, and
your phone gets a Telegram alert on every entry, every exit and every halt,
plus SSH access if you need to intervene.

---

## Where to actually run it

Latency to Angel's servers matters, so prefer an Indian region.

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud Free Tier (Mumbai)** | Free | Genuinely free forever, 4 ARM cores / 24 GB. Best value; sign-up can be fussy |
| **AWS Lightsail (Mumbai)** | ~₹450/mo | Simple, reliable, ap-south-1 is close to Angel |
| **DigitalOcean (Bangalore)** | ~₹500/mo | Easy to use, good docs |
| **Raspberry Pi at home** | One-off ~₹5,000 | Cheap and always on — but your home internet and power become the risk |
| **Old laptop at home** | Free | Works, but disable sleep and expect the occasional Windows update reboot |

**Do not use:** GitHub Actions (job time limits, unreliable start times, and
broker credentials in CI is a bad idea), Heroku/Render free tiers (they sleep),
or anything that hibernates. A trading engine that halts mid-session while
holding a position is worse than one that never started.

Any of these is enough — the engine uses very little CPU and memory. 1 GB RAM
is plenty.

---

## Server setup (Ubuntu)

```bash
# 1. Set the clock to IST — TOTP login fails on a drifting clock, and the
#    systemd timer follows the server's timezone.
sudo timedatectl set-timezone Asia/Kolkata
sudo apt update && sudo apt install -y python3-venv python3-pip git
timedatectl                       # verify before continuing

# 2. Get the code
git clone https://github.com/vijaysonavane20-hash/AutomateX-Live.git
cd AutomateX-Live
python3 -m venv .venv
.venv/bin/pip install -r requirements-trading.txt

# 3. Configure
cp .env.example .env              # credentials
cp config.example.yaml config.yaml
nano .env                         # fill in API key, client code, MPIN, TOTP secret
chmod 600 .env                    # only you can read it

# 4. Verify before automating anything
.venv/bin/python run_trading.py --check
```

`--check` logs in, resolves every symbol and prints your margin, then exits
without trading. Get this passing before going further.

---

## Telegram alerts (this is the iPhone part)

### 1. Create the bot

1. Open Telegram on your iPhone, search **@BotFather**.
2. Send `/newbot`, pick any name. It replies with a **token** like
   `7712345678:AAH9x...`.
3. Search **@userinfobot**, send it any message. It replies with your numeric
   **chat id**.

### 2. Configure

In `.env`:

```
TELEGRAM_BOT_TOKEN=7712345678:AAH9x...
TELEGRAM_CHAT_ID=123456789
```

In `config.yaml`:

```yaml
notifications:
  enabled: true
```

### 3. Test

```bash
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
from trading.config import load_config
from trading.notifier import build_notifier
n = build_notifier(load_config('config.yaml', load_secrets=False))
n.send('Test from the trading engine'); n.close()
"
```

If the message doesn't arrive, send your bot a `/start` first — Telegram blocks
bots from messaging users who have never contacted them.

### What you'll receive

```
🟢 Engine started — PAPER
Accounts: 1
Capital: ₹100,000
Symbols: 6

🟩 LONG RELIANCE
Entry: ₹2,847.50  x17
Stop: ₹2,831.20   Target: ₹2,880.10
Score: 15/19
donchian_break, orb, ema_stack, vol x1.8, supertrend

✅ Closed LONG RELIANCE
Reason: target
PnL: ₹+554 (+2.00R)

🔵 Session over — PAPER
Trades: 3  (2W / 1L)
Win rate: 67%
Net PnL: ₹+812
```

Notifications are **one-way by design**. Accepting commands from a chat would
mean anyone who obtained your bot token could place orders in your account.

Also worth knowing: sending is queued on a background thread. If Telegram is
slow or down, the trading loop does not wait for it — a hung network call
between a stop trigger and an exit order is a real way to lose money.

---

## Running it automatically each morning

```bash
sudo cp deploy/angel-trading.service deploy/angel-trading.timer /etc/systemd/system/
sudo nano /etc/systemd/system/angel-trading.service   # fix User + paths
sudo systemctl daemon-reload
sudo systemctl enable --now angel-trading.timer
```

The timer starts the engine at 09:10 IST on weekdays. The engine squares off at
15:10 and exits by itself, so there is nothing to stop.

```bash
systemctl list-timers angel-trading.timer   # when does it fire next
systemctl status angel-trading.service      # is it running now
journalctl -u angel-trading -f              # live logs
journalctl -u angel-trading --since today   # today's session
```

Run it manually first for a few days before enabling the timer. You want to see
it behave before it starts on its own.

---

## Monitoring from the iPhone

**Telegram** covers the normal case — you get every entry, exit and halt without
doing anything.

**For a shell**, install one of:

- [**Termius**](https://apps.apple.com/app/termius/id549039908) — free tier is
  enough, saves connections, easiest to start with
- [**Blink Shell**](https://apps.apple.com/app/blink-shell/id1156707581) — paid,
  better if you live in a terminal

Useful commands once connected:

```bash
journalctl -u angel-trading -f              # watch live
systemctl status angel-trading              # is it alive
tail -20 state/trades_$(date +%F).csv       # today's trades
sudo systemctl stop angel-trading           # STOP — squares off first
```

That last one matters: `systemctl stop` sends SIGTERM, which triggers the
engine's square-off path. It flattens positions before exiting rather than
abandoning them. Don't `kill -9`.

### If something goes wrong while you're away

1. Telegram tells you something broke (`⚠️ Engine problem`).
2. SSH in from Termius, run `journalctl -u angel-trading -n 50`.
3. To stop trading immediately: `sudo systemctl stop angel-trading`.
4. **Then verify in the Angel One app that you are actually flat.** Never
   assume — confirm positions with your broker directly.

Keep the Angel One app on your phone. It is the authority on your positions;
this engine is not.

---

## Cost

| Item | Monthly |
|---|---|
| VPS (Oracle free tier) | ₹0 |
| VPS (Lightsail/DigitalOcean) | ₹450–500 |
| Angel One SmartAPI | ₹0 |
| Telegram | ₹0 |

Brokerage and taxes are per trade, not monthly — roughly ₹40–60 per round trip.
At 6 trades a day that's ₹250–350 daily, or ₹5,000–7,000 a month, which the
strategy has to clear before you make anything. This dwarfs the server cost and
is the number that actually matters.

---

## A realistic first month

1. **Week 1** — backtest on your symbols. Check the score-bucket table.
2. **Week 2** — paper trade on the server. Confirm Telegram alerts arrive and
   the timer fires correctly.
3. **Weeks 3–6** — keep paper trading. Compare against the backtest. If they
   disagree, the backtest is wrong; find out why before risking money.
4. **Only then** — go live with the smallest capital that can size one share,
   and leave it there for weeks.

Nobody has ever regretted paper trading for an extra month. Plenty of people
have regretted skipping it.
