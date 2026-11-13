# Momentum Bot – Version 2 setup (seedha chalane ke liye)

Ek hi file hai: `angel_momentum_bot.py`. Aapke "Intraday Momentum Setup – Version 2"
document ke rules isi mein hain. **Default PAPER mode hai** – koi asli order nahi jaata.

---

## 5 step mein shuru karo

**1. Install (ek baar)**

```
pip install smartapi-python pyotp logzero websocket-client requests
```

**2. Config banao**

`config.example.json` ko copy karke usi folder mein `config.json` naam do, aur 4 cheezein bharo:

| Field | Kahan se milega |
|---|---|
| `api_key` | smartapi.angelone.in → My Apps → 👁 icon |
| `client_code` | Aapka Angel login ID (jaise A123456) |
| `pin` | Angel app ka 4-digit PIN |
| `totp_secret` | Enable TOTP page ki **lambi string** (6-digit code nahi) |

`capital` mein utna hi likho jitna **free cash** is setup ke liye rakhna hai.

**3. Connection test (koi order nahi)**

```
python angel_momentum_bot.py check
```

`CHECK PASS` aana chahiye. Ye login, tokens, live price aur account ka cash dikhata hai.

**4. Raat ko (ya subah 9:00 se pehle)**

```
python angel_momentum_bot.py prep
```

50 stocks ka data laata hai aur watchlist banata hai (2–3 minute lagte hain).
Agar kal kisi stock mein news/result hai to `news.txt` file mein uska symbol likh do – har line mein ek.

**5. Subah 9:10 tak**

```
python angel_momentum_bot.py run
```

3:12 PM par khud band ho jaata hai. Beech mein band karna ho to `Ctrl-C`.

---

## Bot din bhar kya karta hai

```
9:15   market khula – sirf data dekh raha hai
9:30   pehle 15 minute ka high note kiya (opening range)
9:30 – 11:30   jo stock us high ke upar 5-min close de, uska SCORE nikalta hai
       score 5/6 ya zyada  →  BUY
       score kam           →  sirf signals.csv mein likh deta hai
3:10   jo bacha hai sab band
```

### 6 point ka score (aapke document se)

| # | Check | Bot kaise dekhta hai |
|---|---|---|
| 1 | Raat ki watchlist | NR7 / inside day / tight base / strong close / 52-week high ke 5% ke andar |
| 2 | News | `news.txt` mein symbol hai ya nahi (ye aap haath se likhte ho) |
| 3 | Volume 3 guna | Aaj ka volume ÷ **usi time tak ka** pichhle dino ka average |
| 4 | Nifty + sector | Nifty hara aur apne din ke average ke upar; sector ke baaki stocks bhi hare |
| 5 | VWAP ke upar | Price VWAP ke upar, par 3% se zyada door nahi |
| 6 | SL aur jagah | SL 0.3%–1% door, aur 2R target tak koi purana high beech mein nahi |

### Exit ke rules

- **SL** – VWAP ke thoda neeche ya pichhli 5-min bar ke low ke neeche
- **+1R** par SL breakeven
- **+2R** par aadhi quantity book, baaki 20 EMA par trail
- **45 minute** mein +1R nahi aaya → nikal jao (time stop)
- 5-min bar VWAP ke neeche band hui → nikal jao
- VWAP se 3% se zyada door → SL pichhli bar ke low par kas do
- Din mein **2 loss** ke baad naya trade nahi
- **3:10** par sab band

---

## Files jo banti hain

| File | Kya hai |
|---|---|
| `prep.json` | Raat ka watchlist data |
| `signals.csv` | Har signal + uska score + kaunsa check pass/fail (trade hua ya nahi) |
| `trades.csv` | Har trade ka result – Rs aur R mein |
| `state.json` | Aaj ki khuli positions (bot beech mein band ho jaye to dobara chalane par yaad rahe) |

50 trades ke baad:

```
python angel_momentum_bot.py report
```

Ye batata hai kaunsa score, kaunsa time, kaunsa sector paisa de raha hai, aur **kis check
ke bina liye gaye trades fail hue**. Document ka Sudhar 5 yahi hai.

---

## Live (asli paisa) – sirf jab paper ke 50 trades dekh lo

Teen cheezein **ek saath** chahiye. Ek bhi kam hui to paper hi chalega:

1. `config.json` mein `"mode": "live"`
2. `config.json` mein `"confirm_live": true`
3. Command: `python angel_momentum_bot.py run --live`

Live mein kya alag hota hai:

- Har order **INTRADAY** hai. Aapki delivery holdings ko ye kabhi nahi chhuta.
- BUY ke turant baad **Angel ke server par SL order** lag jaata hai. Laptop band ho jaye,
  net chala jaye – SL tab bhi laga rehta hai.
- **Leverage nahi.** Quantity do cheezon se bandhi hai: `capital ÷ 2` aur account mein
  asal mein pada cash. Jo kam ho.
- Bechne se pehle bot hamesha dekhta hai ki broker ka SL pehle hi to nahi lag gaya, aur
  broker ke paas asal mein kitni quantity hai – taaki kabhi do baar na bike.
- SL order reject ho jaye to bot **bina SL ke position nahi rakhta** – turant nikal jaata hai.

Live ke liye aapka computer/VPS ka IP SmartAPI app mein registered hona chahiye.

**Pehli baar live karo to `capital` bahut chhota rakho** (jaise 10000). Live wala hissa
nakli broker par test hua hai, asli Angel account par nahi – pehle din khud Angel app mein
dekho ki BUY ke saath SL order dikh raha hai ya nahi.

---

## Telegram alert (optional)

`config.json` mein `telegram_token` aur `telegram_chat_id` bharo. BUY, EXIT aur din ka
total phone par aa jayega. Khaali chhodo to kuch nahi hota.

---

## Settings jo badal sakte ho (`config.json` mein)

| Setting | Default | Matlab |
|---|---|---|
| `capital` | 100000 | Is setup ka paisa |
| `risk_pct` | 1.0 | Ek trade mein capital ka kitna % risk |
| `min_score` | 5 | 6 mein se kitne par trade |
| `max_positions` | 2 | Ek saath kitni positions |
| `max_losses_per_day` | 2 | Itne loss ke baad din band |
| `rvol_min` | 3.0 | Volume kitna guna hona chahiye |
| `gap_min_pct` / `gap_max_pct` | 1.5 / 6.0 | Stock kitna % upar ho |
| `time_stop_min` | 45 | Time stop |
| `entry_end` | 11:30 | Iske baad nayi entry nahi |
| `symbols` | 50 large caps | Apni list daal sakte ho |
| `holidays` | [] | `["2026-11-25"]` – in dino bot nahi chalega |

Ek baar mein **ek hi cheez** badlo, aur 50 trades se pehle mat badlo.

---

## Sach baat

- Ye testing framework hai, **profit ki guarantee nahi**. Aapke document ki aakhri line yahi kehti hai.
- Har trade par kareeb Rs 40–60 charges lagte hain. `trades.csv` ka P&L charges kaat kar hota hai (andaazan).
- Nifty aur sector ka VWAP API se nahi milta, isliye unke liye **kareebi naap** use hua hai (upar table dekho).
- Bot har 60 second mein price dekhta hai. Paper mode mein SL isi wajah se thoda der se lag sakta hai; live mein broker ka SL turant lagta hai.
- `config.json` mein aapka PIN aur TOTP secret hai. **Kisi ko mat bhejo, screenshot mat lo.** Ye file `.gitignore` mein hai.
