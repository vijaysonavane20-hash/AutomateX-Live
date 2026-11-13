"""
Angel One Momentum Bot  -  Version 2 setup  (end to end)
=========================================================
Yeh script Angel One SmartAPI se live data leta hai, Version 2 setup ke rules
check karta hai aur trade karta hai. DEFAULT PAPER MODE hai (koi real order
nahi jaata). Live ke liye teen cheezein ek saath chahiye - neeche dekho.

Chalane ka tarika:
    python angel_momentum_bot.py check    -> login + data test (koi order nahi)
    python angel_momentum_bot.py prep     -> raat ko / subah 9:00 se pehle (watchlist)
    python angel_momentum_bot.py run      -> subah 9:10 tak start karo, 3:12 PM par khud band
    python angel_momentum_bot.py report   -> ab tak ke trades ka hisaab (Sudhar 5)

LIVE (asli paisa) sirf tab, jab teeno ho:
    config.json mein  "mode": "live"  aur  "confirm_live": true
    aur command:      python angel_momentum_bot.py run --live

Output files (isi folder mein):
    prep.json     raat ka watchlist data
    signals.csv   har breakout signal aur uska score (trade hua ya nahi)
    trades.csv    trades ka result (R aur Rs mein)
    state.json    aaj ki open positions (bot restart ho to yaad rahe)

Zaroorat:  pip install smartapi-python pyotp logzero websocket-client requests
"""
import csv
import datetime as dt
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_TOKEN = "99926000"  # NSE Nifty 50 index

# India mein DST nahi hota, isliye fixed +5:30 hamesha sahi hai. Server kisi bhi
# desh mein ho (VPS aksar UTC par hota hai), bot hamesha IST mein sochta hai.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Liquid large-cap list. Apni marzi se config.json ke "symbols" mein badal sakte ho.
DEFAULT_SYMBOLS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "KOTAKBANK",
    "LT", "ITC", "BHARTIARTL", "BAJFINANCE", "MARUTI", "M&M", "TATAMOTORS", "TATASTEEL",
    "SUNPHARMA", "HCLTECH", "WIPRO", "TECHM", "NTPC", "POWERGRID", "ONGC", "COALINDIA",
    "ADANIENT", "ADANIPORTS", "JSWSTEEL", "HINDALCO", "ULTRACEMCO", "TITAN", "ASIANPAINT",
    "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "DRREDDY", "CIPLA", "INDUSINDBK", "BEL",
    "HAL", "TRENT", "DLF", "VEDL", "TATAPOWER", "PNB", "BANKBARODA", "CANBK", "IRCTC",
    "DIXON", "POLYCAB", "SIEMENS",
]

# Sector check ke liye (Sudhar 2). Sector index ka VWAP API se nahi milta, isliye
# sector ki taqat usi sector ke baaki stocks ke average % change se naapi jaati hai.
# Jis stock ka sector yahan nahi hai, uska sirf Nifty check hota hai.
DEFAULT_SECTORS = {
    "BANK": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK",
             "PNB", "BANKBARODA", "CANBK"],
    "IT": ["INFY", "TCS", "HCLTECH", "WIPRO", "TECHM"],
    "AUTO": ["MARUTI", "M&M", "TATAMOTORS", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT"],
    "METAL": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA"],
    "ENERGY": ["RELIANCE", "ONGC", "NTPC", "POWERGRID", "TATAPOWER"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA"],
    "DEFENCE": ["BEL", "HAL"],
    "CAPGOODS": ["LT", "SIEMENS", "POLYCAB", "DIXON"],
    "CONSUMER": ["ITC", "TITAN", "ASIANPAINT", "TRENT"],
    "ADANI": ["ADANIENT", "ADANIPORTS"],
}

DEFAULTS = {
    "mode": "paper",            # "paper" ya "live"
    "confirm_live": False,      # live ke liye yeh bhi true hona chahiye (+ --live)
    "capital": 100000,          # is setup ke liye kitna paisa (Rs)
    "risk_pct": 1.0,            # ek trade mein capital ka kitna % risk
    "min_score": 5,             # 6 mein se kitne point par trade (news manual hai)
    "max_positions": 2,
    "max_losses_per_day": 2,
    "gap_min_pct": 1.5,         # entry ke waqt stock kam se kam itna % upar ho
    "gap_max_pct": 6.0,         # isse zyada chadh chuka ho to entry nahi
    "rvol_min": 3.0,            # relative volume (usi time ke average ke mukable)
    "max_sl_pct": 1.0,          # SL entry se isse zyada door ho to trade nahi
    "min_sl_pct": 0.3,
    "max_ext_from_vwap_pct": 3.0,
    "time_stop_min": 45,
    "entry_start": "09:30",
    "entry_end": "11:30",
    "square_off": "15:10",
    "poll_seconds": 60,
    "brokerage_per_order": 20,  # approx
    "charges_pct_per_side": 0.03,  # approx STT + exchange + GST, % of turnover
    "slippage_pct": 0.03,       # paper fill ko thoda kharab maano (asli market jaisa)
    "holidays": [],             # ["2026-11-25", ...] in dino bot nahi chalega
    "telegram_token": "",       # optional: phone par alert
    "telegram_chat_id": "",
    "symbols": DEFAULT_SYMBOLS,
    "sectors": DEFAULT_SECTORS,
}

CHECKS = ["watchlist", "news", "volume", "market", "vwap", "sl_ok"]
SIGNAL_HEADER = ["date", "time", "symbol", "sector", "ltp", "pct", "rvol", "score"] + CHECKS + ["traded"]
TRADE_HEADER = (["date", "mode", "symbol", "sector", "score", "entry_time", "exit_time", "qty",
                 "entry", "sl", "exit", "reason", "pnl_rs", "r_multiple"] + CHECKS)
PLACEHOLDERS = ("SMARTAPI_DASHBOARD_SE_API_KEY", "ANGEL_ONE_CLIENT_ID",
                "ANGEL_ONE_LOGIN_PIN", "ENABLE_TOTP_PAGE_KA_SECRET_KEY")


# ----------------------------------------------------------------- helpers
def now_ist():
    """Abhi ka IST time (bina timezone tag ke, taaki compare karna aasaan rahe)."""
    return dt.datetime.now(IST).replace(tzinfo=None)


def load_config():
    path = os.path.join(BASE, "config.json")
    if not os.path.exists(path):
        sys.exit("config.json nahi mila. config.example.json ko copy karke config.json banao.")
    with open(path, encoding="utf-8") as f:
        user = json.load(f)
    cfg = dict(DEFAULTS)
    cfg.update(user)
    for key in ("api_key", "client_code", "pin", "totp_secret"):
        value = str(cfg.get(key, "")).strip()
        if not value or value in PLACEHOLDERS:
            sys.exit("config.json mein '%s' abhi bhara nahi hai. Apni asli value daalo." % key)
        cfg[key] = value
    if cfg["mode"] not in ("paper", "live"):
        sys.exit('config.json: "mode" sirf "paper" ya "live" ho sakta hai.')
    # config.json mein password jaisi cheezein hain - koi aur na padh sake
    if os.name == "posix" and os.stat(path).st_mode & 0o077:
        print("DHYAN: config.json doosre users bhi padh sakte hain. Chalao: chmod 600 config.json")
    return cfg


def hhmm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def append_csv(name, header, row):
    path = os.path.join(BASE, name)
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            old_header = next(csv.reader(f), None)
        if old_header != header:
            # purane bot ki file ke columns alag hain - mila kar kharab mat karo
            stamp = now_ist().strftime("%Y%m%d_%H%M%S")
            os.replace(path, os.path.join(BASE, name.replace(".csv", "_old_%s.csv" % stamp)))
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def beep():
    try:
        import winsound
        winsound.Beep(1200, 400)
    except Exception:
        pass


def ema(values, n):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def make_notifier(cfg):
    """Telegram alert. Alag thread mein jaata hai taaki trading kabhi iske liye na ruke."""
    token, chat = str(cfg.get("telegram_token") or ""), str(cfg.get("telegram_chat_id") or "")
    if not token or not chat:
        return lambda text: None

    def _send(text):
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900]}).encode()
            urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token,
                                   data=data, timeout=10).read()
        except Exception:
            pass  # alert fail ho to bhi trading chalti rahe

    return lambda text: threading.Thread(target=_send, args=(text,), daemon=True).start()


# ----------------------------------------------------------------- Angel One
def login(cfg, tries=3):
    from SmartApi import SmartConnect
    import pyotp
    last = "no response"
    for attempt in range(tries):
        try:
            api = SmartConnect(api_key=cfg["api_key"])
            totp = pyotp.TOTP(cfg["totp_secret"]).now()
            data = api.generateSession(cfg["client_code"], cfg["pin"], totp)
            if data and data.get("status"):
                print("Login OK:", cfg["client_code"])
                return api
            last = (data or {}).get("message") or "no response"
        except Exception as e:
            last = str(e)[:120]
        if attempt < tries - 1:
            print("Login fail (%s). 30 sec baad dobara..." % last)
            time.sleep(31)  # naya TOTP code aane do
    sys.exit("Login fail: %s\nCheck karo: api_key, client_code, pin, totp_secret, computer ka time, "
             "aur SmartAPI app mein registered static IP." % last)


def load_tokens(symbols):
    """Return (symbol -> NSE token, symbol -> tick size). Scrip master din mein ek baar."""
    import requests
    cache = os.path.join(BASE, "scrip_master_%s.json" % now_ist().date().isoformat())
    if not os.path.exists(cache):
        print("Scrip master download ho raha hai (bada file hai, 1-2 min)...")
        r = requests.get(SCRIP_URL, timeout=120)
        r.raise_for_status()
        small = [{"symbol": x["symbol"], "token": str(x["token"]), "tick": x.get("tick_size")}
                 for x in r.json()
                 if x.get("exch_seg") == "NSE" and str(x.get("symbol", "")).endswith("-EQ")]
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(small, f)
        for name in os.listdir(BASE):  # purani files hatao
            if name.startswith("scrip_master_") and os.path.join(BASE, name) != cache:
                os.remove(os.path.join(BASE, name))
    with open(cache, encoding="utf-8") as f:
        rows = {x["symbol"][:-3]: x for x in json.load(f)}
    tokens, ticks = {}, {}
    for s in symbols:
        if s in rows:
            tokens[s] = rows[s]["token"]
            try:  # tick_size paise mein aata hai (5 -> 0.05)
                ticks[s] = float(rows[s].get("tick") or 5) / 100 or 0.05
            except (TypeError, ValueError):
                ticks[s] = 0.05
    missing = [s for s in symbols if s not in tokens]
    if missing:
        print("In symbols ka token nahi mila (skip):", ", ".join(missing))
    return tokens, ticks


def get_candles(api, token, interval, days):
    to = now_ist()
    frm = to - dt.timedelta(days=days)
    params = {"exchange": "NSE", "symboltoken": token, "interval": interval,
              "fromdate": frm.strftime("%Y-%m-%d 09:15"), "todate": to.strftime("%Y-%m-%d %H:%M")}
    for attempt in range(4):
        try:
            resp = api.getCandleData(params)
            if resp and resp.get("status") and resp.get("data"):
                return resp["data"]
        except Exception as e:  # rate limit / network
            print("   retry (%s)" % str(e)[:60])
        time.sleep(2 + attempt * 3)
    return None


def fetch_quotes(api, tokens_by_symbol):
    """FULL mode bulk quote (50 per call). Return: symbol -> quote dict."""
    by_token = {t: s for s, t in tokens_by_symbol.items()}
    by_token[NIFTY_TOKEN] = "NIFTY"
    all_tokens = list(by_token)
    out = {}
    for i in range(0, len(all_tokens), 50):
        chunk = all_tokens[i:i + 50]
        try:
            resp = api.getMarketData("FULL", {"NSE": chunk})
        except Exception as e:
            print("Quote error:", str(e)[:80])
            continue
        for q in ((resp or {}).get("data") or {}).get("fetched", []) or []:
            sym = by_token.get(str(q.get("symbolToken")))
            if sym:
                out[sym] = {
                    "ltp": float(q.get("ltp") or 0), "open": float(q.get("open") or 0),
                    "high": float(q.get("high") or 0), "low": float(q.get("low") or 0),
                    "pct": float(q.get("percentChange") or 0),
                    "vwap": float(q.get("avgPrice") or 0),
                    "vol": float(q.get("tradeVolume") or 0),
                    "hi52": float(q.get("52WeekHigh") or 0),
                }
        time.sleep(1.1)
    return out


# ----------------------------------------------------------------- PREP (raat ka kaam)
def analyse_daily(candles, today=None, now=None):
    """candles: [[ts, o, h, l, c, v], ...]. Return dict ya None."""
    now = now or now_ist()
    today = today or now.date()
    rows = list(candles)
    # market chalte waqt aaj ki adhuri candle hata do
    if rows and str(rows[-1][0])[:10] == today.isoformat() and now.time() < dt.time(15, 30):
        rows = rows[:-1]
    if len(rows) < 21:
        return None
    last, prev = rows[-1], rows[-2]
    o, h, l, c, v = [float(x) for x in last[1:6]]
    vols = [float(r[5]) for r in rows[-20:]]
    avg_vol = sum(vols) / len(vols)
    ranges7 = [float(r[2]) - float(r[3]) for r in rows[-7:]]
    nr7 = (h - l) <= min(ranges7)
    inside = h <= float(prev[2]) and l >= float(prev[3])
    hi10 = max(float(r[2]) for r in rows[-10:])
    lo10 = min(float(r[3]) for r in rows[-10:])
    tight = (hi10 - lo10) / c * 100 <= 8.0
    strong_close = (h > l) and ((c - l) / (h - l) >= 0.75) and v > avg_vol
    return {"avg_vol": avg_vol, "prev_high": h, "prev_close": c,
            "hi20": max(float(r[2]) for r in rows[-20:]),
            "nr7": bool(nr7), "inside": bool(inside), "tight": bool(tight),
            "strong_close": bool(strong_close),
            "watch": bool(nr7 or inside or tight or strong_close)}


def volume_curve(candles, today=None, min_days=3):
    """15-min candles se: din ke har waqt tak AUSATAN kitna volume ho chuka hota hai.

    Volume din bhar barabar nahi aata - subah ke pehle 15 minute mein hi din ka
    kareeb 8-10% ho jaata hai. Isliye "aaj ka volume average se 3 guna hai ya nahi"
    naapne ke liye usi waqt tak ke average se mukabla karna zaroori hai.
    Return: {"09:30": avg cumulative volume, "09:45": ..., ...} ya None.
    """
    today = today or now_ist().date()
    days = {}
    for row in candles or []:
        ts = str(row[0])
        day, start = ts[:10], ts[11:16]
        if day == today.isoformat():
            continue  # aaj ka adhura din average mein nahi
        days.setdefault(day, []).append((start, float(row[5])))
    sums, counts = {}, {}
    for bars in days.values():
        bars.sort()
        cum = 0.0
        for start, vol in bars:
            cum += vol
            h, m = int(start[:2]), int(start[3:5])
            end = "%02d:%02d" % divmod(h * 60 + m + 15, 60)  # bar khatam hone ka time
            sums[end] = sums.get(end, 0.0) + cum
            counts[end] = counts.get(end, 0) + 1
    if len(days) < min_days:
        return None
    return {slot: sums[slot] / counts[slot] for slot in sorted(sums) if counts[slot] >= min_days}


def expected_volume(d, now):
    """Is waqt tak normally kitna volume ho jaana chahiye (prep ke data se)."""
    mins = now.hour * 60 + now.minute + now.second / 60.0
    curve = d.get("curve")
    if curve:
        pts = sorted((int(k[:2]) * 60 + int(k[3:5]), v) for k, v in curve.items())
        if mins <= pts[0][0]:
            start = 9 * 60 + 15
            return pts[0][1] * max(mins - start, 1) / max(pts[0][0] - start, 1)
        for (m1, v1), (m2, v2) in zip(pts, pts[1:]):
            if m1 <= mins <= m2:
                return v1 + (v2 - v1) * (mins - m1) / (m2 - m1)
        return pts[-1][1]
    # curve nahi hai to seedhi line (yeh subah ka volume kam aankta hai, isliye kam bharosemand)
    return d["avg_vol"] * max(1, mins - (9 * 60 + 15)) / 375.0


def cmd_prep(cfg):
    api = login(cfg)
    tokens, _ = load_tokens(cfg["symbols"])
    prep, no_curve = {}, []
    for i, (sym, tok) in enumerate(tokens.items(), 1):
        print("[%d/%d] %s" % (i, len(tokens), sym))
        candles = get_candles(api, tok, "ONE_DAY", 60)
        info = analyse_daily(candles) if candles else None
        if not info:
            print("   data nahi mila, skip")
            continue
        time.sleep(0.5)
        curve = volume_curve(get_candles(api, tok, "FIFTEEN_MINUTE", 20))
        if curve:
            info["curve"] = curve
        else:
            no_curve.append(sym)
        prep[sym] = info
        time.sleep(0.5)
    with open(os.path.join(BASE, "prep.json"), "w", encoding="utf-8") as f:
        json.dump({"date": now_ist().date().isoformat(), "data": prep}, f, indent=1)
    watch = [s for s, d in prep.items() if d["watch"]]
    print("\nPrep complete. %d stocks, watchlist mein %d:" % (len(prep), len(watch)))
    print(", ".join(watch))
    if no_curve:
        print("In stocks ka intraday volume data nahi mila (volume check kam bharosemand):",
              ", ".join(no_curve))
    print("\nAgar kal kisi stock mein news/result hai to news.txt mein uska symbol likh do (har line mein ek).")


# ----------------------------------------------------------------- order layer
class PaperExec:
    """Paper mode: koi order nahi jaata. Fill ko thoda kharab maana jaata hai (slippage)."""
    live = False

    def __init__(self, cfg):
        self.slip = cfg["slippage_pct"] / 100.0

    def refresh(self):
        pass

    def cash(self):
        return None

    def enter(self, sym, qty, ltp, sl):
        return round(ltp * (1 + self.slip), 2), qty

    def protect(self, sym, qty, sl):
        return True

    def sl_alive(self, sym):
        return True

    def reduce(self, sym, qty, ltp, remaining, sl):
        return round(ltp * (1 - self.slip), 2), True

    def exit(self, sym, qty, ltp):
        return round(ltp * (1 - self.slip), 2)

    def broker_exit(self, sym):
        return None

    def export(self):
        return {}

    def restore(self, data):
        pass


class LiveExec:
    """Live mode: asli orders. Har order INTRADAY hai - delivery holdings kabhi nahi chhuta.

    Entry ke turant baad Angel ke server par SL-M order lag jaata hai. Bot band ho jaye,
    net chala jaye, tab bhi woh SL zinda rehta hai.

    Sabse zaroori niyam: bechne se pehle hamesha dekho ki broker ka SL pehle hi to nahi
    lag gaya. Warna do baar bik kar ulti (short) position ban sakti hai.
    """
    live = True

    def __init__(self, api, tokens, ticks, notify=None):
        self.api = api
        self.tokens = tokens
        self.ticks = ticks
        self.notify = notify or (lambda text: None)
        self.sl_orders = {}    # sym -> SL order id
        self.sl_trigger = {}   # sym -> trigger price jo abhi laga hai
        self.filled = {}       # sym -> price (broker SL lag chuka, bot ko batana baaki)
        self.book = {}         # order id -> order row

    # ---- chhote helpers
    def _order(self, sym, side, qty, trigger=None):
        params = {
            "variety": "STOPLOSS" if trigger else "NORMAL",
            "tradingsymbol": sym + "-EQ", "symboltoken": self.tokens[sym],
            "transactiontype": side, "exchange": "NSE",
            "ordertype": "STOPLOSS_MARKET" if trigger else "MARKET",
            "producttype": "INTRADAY", "duration": "DAY",
            "price": "0", "squareoff": "0", "stoploss": "0", "quantity": str(int(qty)),
        }
        if trigger:
            params["triggerprice"] = "%.2f" % trigger
        try:
            oid = self.api.placeOrder(params)
        except Exception as e:
            print("  ORDER ERROR %s %s: %s" % (side, sym, str(e)[:100]))
            return None
        if isinstance(oid, dict):
            oid = (oid.get("data") or {}).get("orderid") or oid.get("orderid")
        return str(oid) if oid else None

    def _load_book(self):
        try:
            resp = self.api.orderBook()
            if resp and resp.get("status"):
                self.book = {str(o.get("orderid")): o for o in (resp.get("data") or [])}
                return True
        except Exception as e:
            print("  Order book error:", str(e)[:80])
        return False

    def _status(self, oid):
        row = self.book.get(str(oid)) or {}
        try:
            avg = float(row.get("averageprice") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        return str(row.get("status") or "").lower(), avg, str(row.get("text") or "")

    def _wait_fill(self, oid, timeout=15):
        """Market order ka fill price. None = fill confirm nahi hua."""
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1.2)
            if not self._load_book():
                continue
            status, avg, text = self._status(oid)
            if status == "complete":
                return avg or None
            if status in ("rejected", "cancelled"):
                print("  Order %s: %s" % (status, text[:100]))
                return None
        return None

    def _cancel(self, oid, variety):
        try:
            self.api.cancelOrder(oid, variety)
        except Exception as e:
            print("  Cancel error:", str(e)[:80])

    def _net_qty(self, sym):
        """Broker ke hisaab se is stock ki INTRADAY position. None = pata nahi chala."""
        try:
            resp = self.api.position()
            if not resp or not resp.get("status"):
                return None
            for p in resp.get("data") or []:
                if (p.get("tradingsymbol") == sym + "-EQ"
                        and str(p.get("producttype", "")).upper() == "INTRADAY"):
                    return int(float(p.get("netqty") or 0))
            return 0
        except Exception:
            return None

    def _cancel_sl(self, sym):
        """SL order hatao. Return ("filled", price) agar woh pehle hi lag chuka, warna ("gone", None)."""
        oid = self.sl_orders.get(sym)
        if not oid:
            return "gone", None
        self._load_book()
        status, avg, _ = self._status(oid)
        if status != "complete":
            self._cancel(oid, "STOPLOSS")
            time.sleep(0.8)
            self._load_book()
            status, avg, _ = self._status(oid)  # cancel karte-karte lag gaya ho to
        self.sl_orders.pop(sym, None)
        if status == "complete":
            return "filled", avg or self.sl_trigger.get(sym)
        return "gone", None

    def _sell(self, sym, qty, ltp):
        """Guarded market sell. Return fill price, ya None agar bik nahi paya."""
        net = self._net_qty(sym)
        if net is not None and net <= 0:
            print("  %s broker par pehle hi band hai - dobara nahi bech raha." % sym)
            return ltp
        qty = qty if net is None else min(qty, net)
        oid = self._order(sym, "SELL", qty)
        avg = self._wait_fill(oid) if oid else None
        if avg is None and oid:
            # Fill confirm nahi hua. Order ko latka mat chhodo - warna agli koshish
            # ke saath milkar do baar bik sakta hai.
            self._cancel(oid, "NORMAL")
            time.sleep(0.8)
            net = self._net_qty(sym)
            if net is not None and net <= 0:
                return ltp  # bik gaya, bas price confirm nahi hua
        return avg

    # ---- bot inhe call karta hai
    def refresh(self):
        if self.sl_orders:
            self._load_book()

    def cash(self):
        try:
            resp = self.api.rmsLimit()
            data = (resp or {}).get("data") or {}
            for key in ("availablecash", "net"):
                if data.get(key) not in (None, ""):
                    return float(data[key])
        except Exception as e:
            print("  Fund check error:", str(e)[:80])
        return None

    def enter(self, sym, qty, ltp, sl):
        oid = self._order(sym, "BUY", qty)
        if not oid:
            return None
        avg = self._wait_fill(oid)
        if avg is None:
            self._cancel(oid, "NORMAL")
            time.sleep(0.8)
            net = self._net_qty(sym)
            if net is None:
                time.sleep(2)
                net = self._net_qty(sym)
            if net is None:
                print("  !!! %s: BUY order ka pata nahi chala. Angel app mein position check karo. !!!" % sym)
                self.notify("🚨 %s: BUY order ka status pata nahi chala. Angel app check karo!" % sym)
                return None
            if net <= 0:
                return None
            qty, avg = min(qty, net), ltp  # order lag gaya, bas price confirm nahi hua
        if not self.protect(sym, qty, sl):
            print("  SL order nahi laga - bina SL ke position nahi rakhenge, turant nikal rahe hain.")
            self.notify("⚠️ %s: SL order reject hua, position turant band ki ja rahi hai" % sym)
            if self._sell(sym, qty, ltp) is None:
                print("  !!! %s BIK NAHI PAYA. Angel app kholo aur haath se band karo. !!!" % sym)
                self.notify("🚨 %s x%d BIK NAHI PAYA. Angel app se haath se band karo!" % (sym, qty))
            return None
        return avg, qty

    def protect(self, sym, qty, sl):
        """Broker par SL-M lagao (purana ho to hata kar). False = SL nahi lag paya."""
        if sym in self.filled:
            return True  # position band ho chuki - naya SELL order lagana short bana dega
        state, _ = self._cancel_sl(sym)
        if state == "filled":
            self.filled[sym] = self.sl_trigger.get(sym, sl)
            return True  # position pehle hi band; agle tick par bot ko pata chal jayega
        tick = self.ticks.get(sym, 0.05)
        trigger = round(math.floor(sl / tick + 1e-9) * tick, 2)
        oid = self._order(sym, "SELL", qty, trigger=trigger)
        if not oid:
            return False
        time.sleep(1.0)
        self._load_book()
        status, _, text = self._status(oid)
        if status in ("rejected", "cancelled"):
            print("  SL order %s: %s" % (status, text[:100]))
            return False
        self.sl_orders[sym] = oid
        self.sl_trigger[sym] = trigger
        return True

    def sl_alive(self, sym):
        oid = self.sl_orders.get(sym)
        if not oid:
            return sym in self.filled
        status, _, _ = self._status(oid)
        return status not in ("rejected", "cancelled")

    def reduce(self, sym, qty, ltp, remaining, sl):
        """Aadhi position becho, baaki par SL dobara lagao. Return (price, protected) ya None."""
        state, price = self._cancel_sl(sym)
        if state == "filled":
            self.filled[sym] = price
            return None
        fill = self._sell(sym, qty, ltp)
        if fill is None:
            self.protect(sym, qty + remaining, sl)  # kuch nahi bika, poori qty par SL wapas
            return None
        return fill, self.protect(sym, remaining, sl)

    def exit(self, sym, qty, ltp):
        """Poori position band. Return fill price, ya None agar bik nahi paya."""
        if sym in self.filled:
            return self.filled.pop(sym) or ltp
        state, price = self._cancel_sl(sym)
        if state == "filled":
            return price or ltp
        fill = self._sell(sym, qty, ltp)
        if fill is None:
            trigger = self.sl_trigger.get(sym)
            if trigger:
                self.protect(sym, qty, trigger)  # kam se kam SL to wapas lage
        return fill

    def broker_exit(self, sym):
        if sym in self.filled:
            self.sl_orders.pop(sym, None)
            return self.filled.pop(sym)
        oid = self.sl_orders.get(sym)
        if oid:
            status, avg, _ = self._status(oid)
            if status == "complete":
                self.sl_orders.pop(sym, None)
                return avg or self.sl_trigger.get(sym)
        return None

    def export(self):
        return {"sl_orders": self.sl_orders, "sl_trigger": self.sl_trigger}

    def restore(self, data):
        self.sl_orders = dict(data.get("sl_orders") or {})
        self.sl_trigger = dict(data.get("sl_trigger") or {})


# ----------------------------------------------------------------- BOT (engine)
class Bot:
    def __init__(self, cfg, prep, news=None, ex=None, notify=None):
        self.cfg = cfg
        self.prep = prep
        self.news = set(news or [])
        self.ex = ex or PaperExec(cfg)
        self.notify = notify or (lambda text: None)
        self.tag = "LIVE" if self.ex.live else "PAPER"
        self.sector_of = {s: name for name, members in (cfg.get("sectors") or {}).items()
                          for s in members}
        self.bars = {}        # sym -> list of completed 5-min bars {o,h,l,c}
        self.cur = {}         # sym -> (bucket, bar)
        self.orng = {}        # sym -> (or_high, or_low)
        self.pos = {}         # sym -> position dict
        self.done = set()     # aaj jin symbols mein trade/signal ho chuka
        self.last_ltp = {}
        self.losses = 0
        self.realised = 0.0
        self.nifty_sum = 0.0
        self.nifty_n = 0
        self.warned_nifty = False

    # ---- restart ke baad yaad rakhna
    def save_state(self):
        data = {"date": now_ist().date().isoformat(), "mode": self.tag,
                "pos": {s: dict(p, time=p["time"].isoformat()) for s, p in self.pos.items()},
                "done": sorted(self.done), "losses": self.losses, "realised": self.realised,
                "orng": self.orng, "ex": self.ex.export()}
        path = os.path.join(BASE, "state.json")
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
        except OSError as e:
            print("  state save nahi hua:", e)

    def load_state(self):
        path = os.path.join(BASE, "state.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if data.get("date") != now_ist().date().isoformat() or data.get("mode") != self.tag:
            return False
        for s, p in (data.get("pos") or {}).items():
            p["time"] = dt.datetime.fromisoformat(p["time"])
            self.pos[s] = p
        self.done = set(data.get("done") or [])
        self.losses = int(data.get("losses") or 0)
        self.realised = float(data.get("realised") or 0.0)
        self.orng = {s: tuple(v) for s, v in (data.get("orng") or {}).items()}
        self.ex.restore(data.get("ex") or {})
        if self.pos:
            print("Aaj ki %d open position wapas mili: %s" % (len(self.pos), ", ".join(self.pos)))
        return True

    # ---- 5-min bars from LTP snapshots
    def _update_bars(self, now, quotes):
        new_bar = set()
        bucket = (now.hour * 60 + now.minute) // 5
        for sym, q in quotes.items():
            p = q["ltp"]
            if p <= 0:
                continue
            b = self.cur.get(sym)
            if b is None or b[0] != bucket:
                if b is not None:
                    self.bars.setdefault(sym, []).append(b[1])
                    new_bar.add(sym)
                self.cur[sym] = (bucket, {"o": p, "h": p, "l": p, "c": p})
            else:
                bar = b[1]
                bar["h"] = max(bar["h"], p)
                bar["l"] = min(bar["l"], p)
                bar["c"] = p
        return new_bar

    def _costs(self, price, qty):
        return self.cfg["brokerage_per_order"] + price * qty * self.cfg["charges_pct_per_side"] / 100

    # ---- Sudhar 2: market aur sector
    def market_ok(self, quotes):
        nifty = quotes.get("NIFTY")
        if not nifty or nifty["ltp"] <= 0:
            if not self.warned_nifty:
                print("  (Nifty quote nahi mila - market check FAIL maana ja raha hai)")
                self.warned_nifty = True
            return False
        ok = nifty["ltp"] > nifty["open"] and nifty["pct"] > 0
        # Index ka VWAP nahi hota; din ke ab tak ke average price ko uski jagah maana hai
        if ok and self.nifty_n >= 5:
            ok = nifty["ltp"] >= self.nifty_sum / self.nifty_n
        return ok

    def sector_ok(self, sym, quotes):
        name = self.sector_of.get(sym)
        if not name:
            return True
        peers = [quotes[s]["pct"] for s, n in self.sector_of.items()
                 if n == name and s != sym and s in quotes and quotes[s]["ltp"] > 0]
        if len(peers) < 2:
            return True
        return sum(peers) / len(peers) > 0

    # ---- check 6: SL aur upar ki jagah
    def pick_sl(self, sym, q):
        """SL: VWAP ke thoda neeche ya pichhli 5-min bar ke low ke neeche - jo rule mein fit ho."""
        cfg, ltp = self.cfg, q["ltp"]
        bars = self.bars.get(sym, [])
        last_low = bars[-1]["l"] if bars else q["low"]
        options = [last_low * 0.999]
        if q["vwap"] > 0:
            options.append(q["vwap"] * 0.998)
        fits = [s for s in options
                if cfg["min_sl_pct"] <= (ltp - s) / ltp * 100 <= cfg["max_sl_pct"]]
        if fits:
            return max(fits), True   # jo sabse paas hai aur rule mein fit hai
        return max(options), False

    def room_to_2r(self, d, q, entry, sl):
        """Entry aur 2R target ke beech koi purana high (resistance) to nahi?"""
        target = entry + 2 * (entry - sl)
        levels = [d.get("prev_high", 0), d.get("hi20", 0)]
        if q["hi52"] > q["high"] * 1.0005:  # aaj ka apna high resistance nahi gina jaata
            levels.append(q["hi52"])
        return not any(entry * 1.001 < lvl < target for lvl in levels if lvl)

    # ---- scoring (Sudhar 3)
    def score(self, sym, q, now, quotes):
        cfg, d = self.cfg, self.prep[sym]
        checks = {}
        near52 = q["hi52"] > 0 and q["ltp"] >= 0.95 * q["hi52"]
        checks["watchlist"] = bool(d.get("watch") or near52)
        checks["news"] = sym in self.news
        expected = expected_volume(d, now)
        rvol = q["vol"] / expected if expected > 0 else 0
        checks["volume"] = rvol >= cfg["rvol_min"]
        checks["market"] = self.market_ok(quotes) and self.sector_ok(sym, quotes)
        ext = (q["ltp"] - q["vwap"]) / q["vwap"] * 100 if q["vwap"] > 0 else 99
        checks["vwap"] = q["vwap"] > 0 and 0 < ext <= cfg["max_ext_from_vwap_pct"]
        sl, sl_fits = self.pick_sl(sym, q)
        checks["sl_ok"] = sl_fits and self.room_to_2r(d, q, q["ltp"], sl)
        return sum(checks.values()), checks, sl, sl_fits, rvol

    # ---- one tick
    def on_tick(self, now, quotes):
        cfg = self.cfg
        t = now.time()
        for sym, q in quotes.items():
            if q["ltp"] > 0:
                self.last_ltp[sym] = q["ltp"]
        nifty = quotes.get("NIFTY")
        if nifty and nifty["ltp"] > 0:
            self.nifty_sum += nifty["ltp"]
            self.nifty_n += 1
        new_bar = self._update_bars(now, quotes)

        # opening range: 9:30 par day ka high/low
        if t >= hhmm(cfg["entry_start"]):
            for sym, q in quotes.items():
                if sym != "NIFTY" and sym not in self.orng and q["high"] > 0:
                    self.orng[sym] = (q["high"], q["low"])

        self.ex.refresh()
        self._manage(now, quotes, new_bar)
        self._entries(now, quotes, new_bar)

    def _manage(self, now, quotes, new_bar):
        cfg, t = self.cfg, now.time()
        for sym in list(self.pos):
            p = self.pos[sym]
            price = self.ex.broker_exit(sym)
            if price is not None:  # broker ka SL lag chuka
                self._finish(sym, price, now, "Trail SL (broker)" if p["be"] else "SL (broker)")
                continue
            q = quotes.get(sym)
            if not q or q["ltp"] <= 0:
                if t >= hhmm(cfg["square_off"]):
                    self._exit(sym, self.last_ltp.get(sym, p["entry"]), now, "3:10 square-off")
                continue
            ltp, r = q["ltp"], p["r"]
            if t >= hhmm(cfg["square_off"]):
                self._exit(sym, ltp, now, "3:10 square-off")
                continue
            if not self.ex.sl_alive(sym) and not self.ex.protect(sym, p["qty"], p["sl"]):
                self._exit(sym, ltp, now, "SL order nahi laga - safety exit")
                continue
            moved = False
            if not p["be"] and ltp >= p["entry"] + r:
                p["sl"], p["be"], moved = max(p["sl"], p["entry"]), True, True
                print("  %s +1R: SL breakeven par (%.2f)" % (sym, p["sl"]))
            if not p["half"] and ltp >= p["entry"] + 2 * r:
                half = p["qty"] // 2
                if half < 1:
                    self._exit(sym, ltp, now, "2R target")
                    continue
                if self._book(sym, half, ltp, "2R par 50% book"):
                    p["half"] = True
                if sym not in self.pos:
                    continue
            if p["half"]:
                closes = [b["c"] for b in self.bars.get(sym, [])]
                trail = ema(closes, 20) or q["vwap"]
                if trail:
                    p["sl"] = max(p["sl"], trail * 0.999)
            # Extension rule: VWAP se bahut door ho gaya to SL pichhli bar ke low par kas do
            bars = self.bars.get(sym, [])
            if q["vwap"] > 0 and bars and (ltp - q["vwap"]) / q["vwap"] * 100 > cfg["max_ext_from_vwap_pct"]:
                tight = bars[-1]["l"] * 0.999
                if tight < ltp:
                    p["sl"] = max(p["sl"], tight)
            if ltp <= p["sl"]:
                self._exit(sym, ltp, now, "Trail SL" if p["be"] else "SL")
                continue
            if sym in new_bar and q["vwap"] > 0 and self.bars[sym][-1]["c"] < q["vwap"]:
                self._exit(sym, ltp, now, "5-min close VWAP ke neeche")
                continue
            held = (now - p["time"]).total_seconds() / 60
            if not p["be"] and held >= cfg["time_stop_min"]:
                self._exit(sym, ltp, now, "Time stop")
                continue
            # broker ka SL tabhi badlo jab SL kaafi upar aa gaya ho (har tick par order mat badlo)
            if p["sl"] > p["sl_placed"] and (moved or p["sl"] >= p["sl_placed"] * 1.0015):
                if self.ex.protect(sym, p["qty"], p["sl"]):
                    p["sl_placed"] = p["sl"]
                    self.save_state()
                else:
                    self._exit(sym, ltp, now, "SL order nahi laga - safety exit")

    def _entries(self, now, quotes, new_bar):
        cfg, t = self.cfg, now.time()
        if not (hhmm(cfg["entry_start"]) < t <= hhmm(cfg["entry_end"])):
            return
        for sym, q in quotes.items():
            if sym == "NIFTY" or sym in self.done or sym in self.pos:
                continue
            if sym not in self.prep or sym not in self.orng or sym not in new_bar:
                continue
            or_high = self.orng[sym][0]
            last = self.bars[sym][-1]
            if not (last["c"] > or_high and q["ltp"] > or_high):
                continue
            if not (cfg["gap_min_pct"] <= q["pct"] <= cfg["gap_max_pct"]):
                continue
            score, checks, sl, sl_fits, rvol = self.score(sym, q, now, quotes)
            take = (score >= cfg["min_score"] and sl_fits and checks["vwap"]
                    and len(self.pos) < cfg["max_positions"]
                    and self.losses < cfg["max_losses_per_day"])
            sector = self.sector_of.get(sym, "-")
            self.done.add(sym)
            entry_ref = q["ltp"]
            qty = 0
            if take and entry_ref - sl <= 0:
                take = False
            if take:
                risk_rs = cfg["capital"] * cfg["risk_pct"] / 100
                qty = int(risk_rs // (entry_ref - sl))
                # bina leverage: ek position mein capital ka barabar hissa se zyada nahi
                qty = min(qty, int(cfg["capital"] / cfg["max_positions"] // entry_ref))
                if self.ex.live:
                    cash = self.ex.cash()
                    if cash is None:
                        print("  %s: account ka balance pata nahi chala, trade skip" % sym)
                        qty = 0
                    else:
                        qty = min(qty, int(cash * 0.98 // entry_ref))
                if qty < 1:
                    print("  %s: quantity 0 ban rahi hai, skip" % sym)
                    take = False
            filled = self.ex.enter(sym, qty, entry_ref, sl) if take else None
            if take and not filled:
                print("  %s: entry order nahi laga, skip" % sym)
                take = False
            append_csv("signals.csv", SIGNAL_HEADER,
                       [now.date(), now.strftime("%H:%M"), sym, sector, q["ltp"], q["pct"],
                        round(rvol, 2), score] + [checks[c] for c in CHECKS] + [take])
            print("%s SIGNAL %s  ltp %.2f  score %d/6  rvol %.1fx  %s" % (
                now.strftime("%H:%M"), sym, q["ltp"], score, rvol,
                "-> %s BUY" % self.tag if take else "(skip)"))
            if not take:
                self.save_state()
                continue
            entry, qty = filled
            r = max(entry - sl, entry * 0.001)
            self.pos[sym] = {"entry": entry, "sl": sl, "sl0": sl, "sl_placed": sl, "r": r,
                             "qty": qty, "qty0": qty, "time": now, "be": False, "half": False,
                             "score": score, "sector": sector, "checks": checks,
                             "pnl": -self._costs(entry, qty)}
            self.save_state()
            beep()
            msg = "BUY %d %s @ %.2f  SL %.2f  target(2R) %.2f" % (qty, sym, entry, sl, entry + 2 * r)
            print("  " + msg)
            self.notify("🟩 [%s] %s  (score %d/6)" % (self.tag, msg, score))

    # ---- booking
    def _book(self, sym, qty, ltp, why):
        """Aadhi position book karo. False = bik nahi paya (agle tick par dobara koshish)."""
        p = self.pos[sym]
        res = self.ex.reduce(sym, qty, ltp, p["qty"] - qty, p["sl"])
        if res is None:
            return False
        price, protected = res
        p["pnl"] += (price - p["entry"]) * qty - self._costs(price, qty)
        p["qty"] -= qty
        p["sl_placed"] = p["sl"]
        self.save_state()
        print("  %s %s: %d qty @ %.2f" % (sym, why, qty, price))
        self.notify("✂️ [%s] %s %s: %d @ %.2f" % (self.tag, sym, why, qty, price))
        if not protected:
            self._exit(sym, ltp, now_ist(), "SL order nahi laga - safety exit")
        return True

    def _exit(self, sym, ltp, now, why):
        p = self.pos[sym]
        price = self.ex.exit(sym, p["qty"], ltp)
        if price is None:
            p["exit_fails"] = p.get("exit_fails", 0) + 1
            print("  !!! %s EXIT ORDER NAHI LAGA (%s). Agle tick par dobara koshish. !!!" % (sym, why))
            if p["exit_fails"] in (1, 5):
                self.notify("🚨 %s exit order nahi laga (%s). Angel app check karo!" % (sym, why))
            return False
        self._finish(sym, price, now, why)
        return True

    def _finish(self, sym, price, now, why):
        p = self.pos.pop(sym)
        p["pnl"] += (price - p["entry"]) * p["qty"] - self._costs(price, p["qty"])
        r_mult = p["pnl"] / (p["r"] * p["qty0"])
        self.realised += p["pnl"]
        # breakeven par nikle trade ko (sirf charges ka chhota loss) "loss" nahi ginte
        if r_mult <= -0.3:
            self.losses += 1
        checks = p.get("checks") or {}
        append_csv("trades.csv", TRADE_HEADER,
                   [now.date(), self.tag, sym, p.get("sector", "-"), p["score"],
                    p["time"].strftime("%H:%M"), now.strftime("%H:%M"), p["qty0"], p["entry"],
                    round(p["sl0"], 2), price, why, round(p["pnl"], 2), round(r_mult, 2)]
                   + [checks.get(c, "") for c in CHECKS])
        self.save_state()
        msg = "EXIT %s (%s)  P&L Rs %.0f  (%.2fR)   Aaj ka total: Rs %.0f" % (
            sym, why, p["pnl"], r_mult, self.realised)
        print("  " + msg)
        self.notify("%s [%s] %s" % ("✅" if p["pnl"] > 0 else "❌", self.tag, msg))

    def force_exit_all(self, now, why):
        """Din ke aakhir mein / bot band hote waqt jo bacha hai use band karo."""
        for sym in list(self.pos):
            self._exit(sym, self.last_ltp.get(sym, self.pos[sym]["entry"]), now, why)


# ----------------------------------------------------------------- commands
def is_live(cfg, want_live):
    """Live sirf tab: config mode=live + confirm_live=true + command mein --live."""
    return cfg["mode"] == "live" and cfg.get("confirm_live") is True and bool(want_live)


def cmd_check(cfg):
    api = login(cfg)
    tokens, _ = load_tokens(cfg["symbols"])
    print("Tokens mile: %d / %d symbols" % (len(tokens), len(cfg["symbols"])))
    quotes = fetch_quotes(api, dict(list(tokens.items())[:5]))
    for sym, q in quotes.items():
        print("  %-12s ltp %10.2f   %+.2f%%" % (sym, q["ltp"], q["pct"]))
    if "NIFTY" not in quotes:
        print("DHYAN: Nifty ka quote nahi mila. Iske bina market check fail rahega aur trade nahi honge.")
    cash = LiveExec(api, tokens, {}).cash()
    print("Account mein available cash: %s" % ("Rs %.0f" % cash if cash is not None else "pata nahi chala"))
    print("Config mode: %s  |  capital Rs %s  |  risk %.1f%%  |  min score %s/6" % (
        cfg["mode"], cfg["capital"], cfg["risk_pct"], cfg["min_score"]))
    if os.path.exists(os.path.join(BASE, "prep.json")):
        print("prep.json maujood hai.")
    else:
        print("Ab chalao: python angel_momentum_bot.py prep")
    print("\nCHECK PASS - connection sahi hai." if quotes else "\nQuotes nahi mile - market band ho sakta hai, ya API mein dikkat.")


def cmd_run(cfg, want_live=False):
    live = is_live(cfg, want_live)
    if (cfg["mode"] == "live" or want_live) and not live:
        print("LIVE ke liye teeno chahiye: mode=live, confirm_live=true, aur --live. Abhi PAPER mein chal raha hai.")
    today = now_ist().date()
    if today.weekday() >= 5 or today.isoformat() in (cfg.get("holidays") or []):
        print("Aaj market band hai (%s). Kuch nahi karna." % today)
        return
    ppath = os.path.join(BASE, "prep.json")
    if not os.path.exists(ppath):
        sys.exit("prep.json nahi mila. Pehle chalao: python angel_momentum_bot.py prep")
    with open(ppath, encoding="utf-8") as f:
        pj = json.load(f)
    age = (today - dt.date.fromisoformat(pj["date"])).days
    if age > 4:
        print("DHYAN: prep.json %d din purana hai. 'prep' dobara chalao." % age)
    prep = pj["data"]
    news = []
    npath = os.path.join(BASE, "news.txt")   # optional: har line mein ek symbol jisme aaj news hai
    if os.path.exists(npath):
        with open(npath, encoding="utf-8") as f:
            news = [x.strip().upper() for x in f if x.strip()]
    api = login(cfg)
    all_tokens, ticks = load_tokens(cfg["symbols"])
    tokens = {s: t for s, t in all_tokens.items() if s in prep}
    notify = make_notifier(cfg)
    ex = LiveExec(api, tokens, ticks, notify) if live else PaperExec(cfg)
    bot = Bot(cfg, prep, news, ex, notify)
    resumed = bot.load_state()
    if live:
        print("\n" + "!" * 66)
        print("LIVE MODE - ASLI PAISE se orders jayenge. Rokna ho to 10 sec mein Ctrl-C dabao.")
        print("!" * 66)
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            print("Roka gaya. Koi order nahi gaya.")
            return
    else:
        print("PAPER MODE - koi real order nahi jayega.")
    print("%d stocks par nazar. News list: %s" % (len(tokens), ", ".join(news) or "(khaali)"))
    if now_ist().time() > dt.time(9, 35) and not resumed:
        print("DHYAN: 9:35 ke baad start hua. Opening range sahi nahi banegi; aaj ke signals par bharosa mat karo.")
    notify("🟢 Momentum bot start (%s). %d stocks." % (bot.tag, len(tokens)))
    fails = 0
    try:
        while True:
            now = now_ist()
            if now.time() < dt.time(9, 15):
                time.sleep(20)
                continue
            if now.time() > dt.time(15, 12):
                break
            try:
                quotes = fetch_quotes(api, tokens)
                if quotes:
                    fails = 0
                    bot.on_tick(now_ist(), quotes)
                else:
                    fails += 1
                    if now.time() >= hhmm(cfg["square_off"]) and bot.pos:
                        bot.force_exit_all(now, "3:10 square-off (quote nahi mila)")
                    elif fails % 5 == 0:
                        print("Quotes nahi aa rahe - dobara login...")
                        api = login(cfg)
                        ex.api = api
            except Exception as e:  # ek kharab tick se bot band nahi hona chahiye
                print("Tick error (bot chalta rahega):", str(e)[:150])
                if bot.pos:
                    notify("⚠️ Bot error: %s" % str(e)[:150])
            time.sleep(max(1, cfg["poll_seconds"] - (time.time() % cfg["poll_seconds"])))
    except KeyboardInterrupt:
        print("\nCtrl-C dabaya. Khuli positions band ki ja rahi hain...")
    if bot.pos:
        bot.force_exit_all(now_ist(), "Bot band hua")
    if bot.pos:
        left = ", ".join(bot.pos)
        print("!!! Yeh positions band nahi ho payi: %s. Angel app se check karo. !!!" % left)
        notify("🚨 Positions band nahi ho payi: %s. Angel app check karo!" % left)
    summary = "Din khatam. Aaj ka %s P&L: Rs %.0f." % (bot.tag, bot.realised)
    print("\n%s Details trades.csv aur signals.csv mein." % summary)
    notify("🔵 " + summary)


def cmd_report():
    """Sudhar 5: data se dekho kya kaam kar raha hai."""
    path = os.path.join(BASE, "trades.csv")
    if not os.path.exists(path):
        sys.exit("trades.csv abhi nahi bana. Pehle kuch din bot chalao.")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("trades.csv khaali hai.")

    def line(label, group):
        rs = [float(x["r_multiple"]) for x in group]
        pnl = sum(float(x["pnl_rs"]) for x in group)
        wins = sum(1 for v in rs if v > 0)
        print("  %-28s %4d trades   jeet %3.0f%%   avg %+.2fR   Rs %+.0f" % (
            label, len(group), 100.0 * wins / len(group), sum(rs) / len(rs), pnl))

    def by(title, key):
        print("\n" + title)
        groups = {}
        for x in rows:
            groups.setdefault(key(x), []).append(x)
        for k in sorted(groups):
            line(k, groups[k])

    def slot(x):
        h, m = x["entry_time"].split(":")
        return "%s:%s se" % (h, "00" if int(m) < 30 else "30")

    print("=" * 74)
    line("KUL (charges ke baad)", rows)
    by("Score ke hisaab se:", lambda x: "score " + x["score"])
    by("Entry time ke hisaab se:", slot)
    by("Exit reason ke hisaab se:", lambda x: x["reason"])
    by("Sector ke hisaab se:", lambda x: x.get("sector") or "-")
    print("\nJis check ke BINA trade liya gaya (kaunsa check sach mein kaam ka hai):")
    for c in CHECKS:
        group = [x for x in rows if str(x.get(c)) == "False"]
        if group:
            line(c + " = nahi tha", group)
    print("=" * 74)
    if len(rows) < 50:
        print("Abhi sirf %d trades hain. 50 se pehle koi rule mat badlo - itne kam data mein"
              " kismat aur hunar alag nahi dikhte." % len(rows))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        cmd_check(load_config())
    elif cmd == "prep":
        cmd_prep(load_config())
    elif cmd == "run":
        cmd_run(load_config(), want_live="--live" in sys.argv[2:])
    elif cmd == "report":
        cmd_report()
    else:
        print(__doc__)
