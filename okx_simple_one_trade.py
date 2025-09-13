
import os, json, time, math, hmac, base64, hashlib
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd

try:
    import pandas_ta as ta
    HAS_TA = True
except Exception:
    HAS_TA = False

getcontext().prec = 34

# ====== CONFIG (env first, then fallback to your provided keys) ======
BASE_URL = os.getenv("OKX_API_BASE", "https://www.okx.com")
API_KEY = os.getenv("OKX_API_KEY") or "29809262-8962-4460-b7a0-280131629aea"
API_SECRET = os.getenv("OKX_API_SECRET") or os.getenv("OKX_SECRET_KEY") or "1EBB409F0B37C9CB936FD6BD510A6C00"
API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE") or os.getenv("OKX_PASSPHRASE") or "Q@BWaG2bf5ybmGZ"
DEMO = os.getenv("DEMO", "1")  # "1" for demo
HEDGE_MODE = os.getenv("HEDGE_MODE", "0") == "1"

BAR = "5m"
TOP_N = 10
NOTIONAL_USDT = Decimal(os.getenv("NOTIONAL_USDT", "90"))  # $90 fixed per trade

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8367220857:AAHgvPb1pmAqHSwgixb9jBYCT2TTRrDnNL0"
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "1266351161"

# ====== HELPERS ======
def _now_ts():
    return datetime.utcnow().replace(tzinfo=timezone.utc)

def _ts_str(ms=False):
    if ms:
        return _now_ts().isoformat(timespec="milliseconds").replace("+00:00","Z")
    return _now_ts().isoformat().replace("+00:00","Z")

def _send_tg(text: str):
    print(text)
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
        except Exception as e:
            print(f"[WARN] Telegram send failed: {e}")

def _headers(method: str, request_path: str, body_str: str = "", query_str: str = None):
    # Use ONE timestamp for both header and signing
    ts = _ts_str(ms=True)
    path_for_sign = request_path
    if method == "GET" and query_str:
        path_for_sign += "?" + query_str
    prehash = f"{ts}{method}{path_for_sign}{body_str}"
    sign = base64.b64encode(hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
    h = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json",
    }
    if str(DEMO) == "1":
        h["x-simulated-trading"] = "1"
    return h

def _req(method: str, path: str, params: dict = None):
    url = BASE_URL + path
    query = None
    body = ""
    if method == "GET":
        if params:
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            url += "?" + query
        hdrs = _headers(method, path, "", query)
        r = requests.get(url, headers=hdrs, timeout=15)
    else:
        body = json.dumps(params or {})
        hdrs = _headers(method, path, body)
        r = requests.post(url, headers=hdrs, data=body, timeout=15)
    try:
        return r.json()
    except Exception:
        return {"code": "HTTP", "msg": r.text, "status": r.status_code}

def _to_df(candles):
    if not candles:
        return None
    cols = ["ts","o","h","l","c","vol","volCcy","volCcyQuote","confirm"][:len(candles[0])]
    df = pd.DataFrame(candles, columns=cols)
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for c in ["o","h","l","c","vol"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.rename(columns={"o":"Open","h":"High","l":"Low","c":"Close","vol":"Volume"}, inplace=True)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def seconds_to_next_5m():
    now = _now_ts()
    minute_block = (now.minute // 5 + 1) * 5
    target = now.replace(second=0, microsecond=0)
    if minute_block >= 60:
        target = target.replace(minute=0) + timedelta(hours=1)
    else:
        target = target.replace(minute=minute_block)
    return max(0, int((target - now).total_seconds()))

# ====== OKX API WRAPPERS ======
def get_top_swaps(n=TOP_N):
    res = _req("GET", "/api/v5/market/tickers", {"instType":"SWAP"})
    arr = res.get("data", []) if isinstance(res, dict) else []
    arr.sort(key=lambda x: float(x.get("volCcy24h","0") or 0), reverse=True)
    return [x["instId"] for x in arr if x.get("instId","").endswith("-SWAP")][:n]

def get_instruments_map():
    res = _req("GET", "/api/v5/public/instruments", {"instType":"SWAP"})
    mp = {}
    for it in res.get("data", []):
        mp[it["instId"]] = {
            "lotSz": Decimal(str(it.get("lotSz") or it.get("minSz") or "1")),
            "ctVal": Decimal(str(it.get("ctVal") or "1")),
            "ctValCcy": it.get("ctValCcy","")
        }
    return mp

def get_candles(instId, limit=300):
    res = _req("GET", "/api/v5/market/candles", {"instId":instId, "bar":BAR, "limit":limit})
    return res.get("data", [])

def place_order(instId, side, sz, reduceOnly=False, tdMode="cross"):
    body = {
        "instId": instId,
        "tdMode": tdMode,
        "side": side,
        "ordType": "market",
        "sz": str(sz)
    }
    if reduceOnly:
        body["reduceOnly"] = "true"
    if HEDGE_MODE:
        body["posSide"] = "long" if side=="buy" else "short"
    res = _req("POST", "/api/v5/trade/order", body)
    code = res.get("code")
    data = res.get("data", [{}])[0]
    ok = (code=="0" and str(data.get("sCode","0")) in ("0",""))
    ordId = data.get("ordId")
    return ok, ordId, res

def get_fills(ordId=None, instId=None, limit=100):
    params = {}
    if ordId: params["ordId"] = ordId
    if instId: params["instId"] = instId
    params["limit"] = limit
    res = _req("GET", "/api/v5/trade/fills", params)
    return res.get("data", [])

# ====== INDICATORS ======
def indicators(df):
    if df is None or len(df) < 100:
        return 50.0, 50.0
    i = -2 if len(df) >= 2 else -1
    o,h,l,c = df["Open"], df["High"], df["Low"], df["Close"]
    v = df.get("Volume")

    votes = []

    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    votes.append(1 if ema9.iloc[i] > ema21.iloc[i] else -1)

    sma20 = c.rolling(20).mean(); sma50 = c.rolling(50).mean()
    votes.append(0 if (pd.isna(sma20.iloc[i]) or pd.isna(sma50.iloc[i])) else (1 if sma20.iloc[i] > sma50.iloc[i] else -1))

    try:
        rsi = ta.rsi(c, length=14) if HAS_TA else None
    except Exception:
        rsi = None
    if rsi is None:
        d = c.diff(); up = d.clip(lower=0).rolling(14).mean(); dn = -d.clip(upper=0).rolling(14).mean()
        rs = up / (dn + 1e-9); rsi = 100 - (100/(1+rs))
    rv = rsi.iloc[i]
    votes.append(1 if rv>55 else (-1 if rv<45 else 0))

    # Stoch
    if HAS_TA:
        st = ta.stoch(h,l,c,k=14,d=3)
        k = st.iloc[:,0]; d_ = st.iloc[:,1]
        votes.append(1 if (k.iloc[i] > d_.iloc[i] and k.iloc[i] < 80) else (-1 if (k.iloc[i] < d_.iloc[i] and k.iloc[i] > 20) else 0))
    else:
        ll = l.rolling(14).min(); hh = h.rolling(14).max()
        k = 100*(c-ll)/(hh-ll+1e-9); d_ = k.rolling(3).mean()
        votes.append(1 if (k.iloc[i] > d_.iloc[i] and k.iloc[i] < 80) else (-1 if (k.iloc[i] < d_.iloc[i] and k.iloc[i] > 20) else 0))

    # MACD
    if HAS_TA:
        mac = ta.macd(c); ml = mac.iloc[:,0]; sg = mac.iloc[:,2]
    else:
        ema12 = c.ewm(span=12, adjust=False).mean(); ema26 = c.ewm(span=26, adjust=False).mean()
        ml = ema12 - ema26; sg = ml.ewm(span=9, adjust=False).mean()
    votes.append(1 if ml.iloc[i] > sg.iloc[i] else -1)

    # BB basis
    basis = c.rolling(20).mean()
    votes.append(1 if c.iloc[i] > basis.iloc[i] else -1)

    # DMI/ADX
    if HAS_TA:
        adx = ta.adx(h,l,c,length=14)
        plusd, minusd, ax = adx["DMP_14"], adx["DMN_14"], adx["ADX_14"]
        votes.append(0 if ax.iloc[i] < 20 else (1 if plusd.iloc[i] > minusd.iloc[i] else -1))
    else:
        votes.append(0)

    # CCI
    if HAS_TA:
        cci = ta.cci(h,l,c,length=20).iloc[i]
        votes.append(1 if cci>0 else (-1 if cci<0 else 0))
    else:
        votes.append(0)

    # Heikin Ashi
    ha_c = (o+h+l+c)/4.0
    ha_o = ha_c.copy()
    for idx in range(1, len(df)):
        ha_o.iloc[idx] = (ha_o.iloc[idx-1] + ha_c.iloc[idx-1]) / 2.0
    votes.append(1 if ha_c.iloc[i] > ha_o.iloc[i] else -1)

    # Ichimoku
    conv = (h.rolling(9).max()+l.rolling(9).min())/2.0
    base = (h.rolling(26).max()+l.rolling(26).min())/2.0
    span_a = ((conv+base)/2.0).shift(26)
    span_b = ((h.rolling(52).max()+l.rolling(52).min())/2.0).shift(26)
    top = pd.concat([span_a, span_b], axis=1).max(axis=1)
    bot = pd.concat([span_a, span_b], axis=1).min(axis=1)
    if pd.isna(top.iloc[i]) or pd.isna(bot.iloc[i]):
        votes.append(0)
    else:
        votes.append(1 if c.iloc[i] > top.iloc[i] else (-1 if c.iloc[i] < bot.iloc[i] else 0))

    # Supertrend
    if HAS_TA:
        st = ta.supertrend(h,l,c,length=10,multiplier=3.0)
        dcol = [x for x in st.columns if x.startswith("SUPERTd_")]
        votes.append(1 if st[dcol[0]].iloc[i] > 0 else -1)
    else: votes.append(0)

    # Keltner
    if HAS_TA:
        kc = ta.kc(h,l,c,length=20); mid = kc.iloc[:,1]
        votes.append(1 if c.iloc[i] > mid.iloc[i] else -1)
    else: votes.append(0)

    # Williams %R
    if HAS_TA:
        wr = ta.willr(h,l,c,length=14).iloc[i]
        votes.append(1 if wr > -50 else (-1 if wr < -50 else 0))
    else: votes.append(0)

    # MFI
    if HAS_TA and "Volume" in df.columns:
        mfi = ta.mfi(h,l,c,df["Volume"], length=14).iloc[i]
        votes.append(1 if mfi > 50 else (-1 if mfi < 50 else 0))
    else: votes.append(0)

    # ROC
    if HAS_TA:
        rv = ta.roc(c,length=9).iloc[i]
        votes.append(1 if rv > 0 else (-1 if rv < 0 else 0))
    else: votes.append(0)

    # Aroon
    if HAS_TA:
        ar = ta.aroon(h,l,length=14); up = ar.iloc[:,0]; dn = ar.iloc[:,1]
        votes.append(1 if up.iloc[i] > dn.iloc[i] else -1)
    else: votes.append(0)

    # TSI
    if HAS_TA:
        tv = ta.tsi(c).iloc[i]
        votes.append(1 if tv > 0 else (-1 if tv < 0 else 0))
    else: votes.append(0)

    # OBV delta
    if HAS_TA and "Volume" in df.columns:
        dv = ta.obv(c, df["Volume"]).diff().iloc[i]
        votes.append(1 if dv > 0 else -1)
    else: votes.append(0)

    # HMA slope
    if HAS_TA:
        hma = ta.hma(c,length=55)
        votes.append(1 if hma.iloc[i] > hma.shift(1).iloc[i] else -1)
    else: votes.append(0)

    # PSAR
    if HAS_TA:
        ps = ta.psar(h,l,c)
        pcol = [x for x in ps.columns if x.startswith("PSAR")]
        pv = ps[pcol[0]].iloc[i]
        votes.append(1 if c.iloc[i] > pv else -1)
    else: votes.append(0)

    bulls = sum(1 for v in votes if v==1)
    bears = sum(1 for v in votes if v==-1)
    eff = bulls + bears
    bull_pct = 100.0 * bulls/eff if eff>0 else 50.0
    bear_pct = 100.0 - bull_pct
    return bull_pct, bear_pct

def decide(instId):
    df = _to_df(get_candles(instId, 300))
    if df is None or len(df) < 100:
        return None
    bull, bear = indicators(df)
    side = "buy" if bull >= bear else "sell"
    score = bull if side=="buy" else bear
    last_close = float(df["Close"].iloc[-2])
    return {"instId": instId, "bull": bull, "bear": bear, "side": side, "score": score, "px": last_close}

def quantize_to_step(value: Decimal, step: Decimal) -> Decimal:
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    q = (steps * step).normalize()
    # ensure non-zero
    if q <= Decimal("0"):
        q = step
    return q

def compute_size(px_float: float, lotSz: Decimal, ctVal: Decimal, notional: Decimal) -> str:
    # contracts = notional / (price * ctVal)
    px = Decimal(str(px_float))
    raw = notional / (px * (ctVal if ctVal > 0 else Decimal("1e-9")))
    q = quantize_to_step(raw, lotSz)
    # format to the same number of decimals as lotSz
    lot_decimals = max(0, -lotSz.as_tuple().exponent)
    return f"{q:.{lot_decimals}f}"

def seconds_until_bar_end():
    # After opening trade at bar start, wait till next 5m boundary to close
    return seconds_to_next_5m()

def avg_fill_price_and_qty(ordId):
    fills = get_fills(ordId=ordId)
    if not fills:
        return None, None
    total_q = Decimal("0")
    total_notional = Decimal("0")
    for f in fills:
        try:
            px = Decimal(str(f.get("px","0")))
            sz = Decimal(str(f.get("sz","0")))
            total_q += sz
            total_notional += (px * sz)
        except Exception:
            continue
    if total_q == 0:
        return None, None
    avg_px = (total_notional / total_q)
    return float(avg_px), float(total_q)

def main():
    if not (API_KEY and API_SECRET and API_PASSPHRASE):
        print("[ERR] Set API credentials"); return

    ids = get_top_swaps(TOP_N)
    info = get_instruments_map()
    print(f"[INIT] Top {len(ids)}: {', '.join(ids)}")

    while True:
        # wait to new bar start
        time.sleep(seconds_to_next_5m())

        # evaluate all and send TG summary
        decisions = []
        lines = []
        for instId in ids:
            d = decide(instId)
            if d:
                decisions.append(d)
                lines.append(f"{instId}: Bull {d['bull']:.1f}% | Bear {d['bear']:.1f}% ⇒ {d['side'].upper()} (score {d['score']:.1f}%)")
        if decisions:
            _send_tg("[5m SUMMARY]\\n" + "\\n".join(lines))
        else:
            _send_tg("[5m SUMMARY] No valid data."); 
            continue

        # pick the highest consensus
        best = max(decisions, key=lambda x: x["score"])

        # compute order size (contracts) using lotSz & ctVal
        lotSz = info.get(best["instId"],{}).get("lotSz", Decimal("1"))
        ctVal = info.get(best["instId"],{}).get("ctVal", Decimal("1"))
        sz_str = compute_size(best["px"], lotSz, ctVal, NOTIONAL_USDT)

        ok, ordId, resp = place_order(best["instId"], best["side"], sz_str, reduceOnly=False)
        if not ok:
            _send_tg(f"[OPEN-FAILED] {best['instId']} {best['side'].upper()} sz={sz_str} resp={resp}")
            continue

        # get entry fill price & qty
        entry_px, entry_q = avg_fill_price_and_qty(ordId)
        if entry_px is None:
            entry_px = best["px"]  # fallback to last close
        if entry_q is None:
            try: entry_q = Decimal(sz_str)
            except: entry_q = Decimal("0")

        _send_tg(f"[OPEN] {best['instId']} {best['side'].upper()} ordId={ordId} sz={sz_str} px≈{entry_px}")

        # wait till end of bar and close reduceOnly
        time.sleep(seconds_until_bar_end() or 298)  # safety fallback

        close_side = "sell" if best["side"]=="buy" else "buy"
        ok2, ordId2, resp2 = place_order(best["instId"], close_side, sz_str, reduceOnly=True)
        if not ok2:
            _send_tg(f"[CLOSE-FAILED] {best['instId']} resp={resp2}")
            continue

        exit_px, exit_q = avg_fill_price_and_qty(ordId2)
        if exit_px is None:
            # fetch latest candle close as fallback
            d2 = decide(best["instId"])
            exit_px = d2["px"] if d2 else entry_px
        if exit_q is None:
            try: exit_q = Decimal(sz_str)
            except: exit_q = Decimal("0")

        # compute P&L in USDT (approx; fees excluded)
        # Linear USDT-margined: PnL = (exit - entry) * sign * ctVal * qty
        sign = Decimal("1") if best["side"]=="buy" else Decimal("-1")
        qty_used = Decimal(str(min(float(entry_q), float(exit_q))))
        pnl = (Decimal(str(exit_px)) - Decimal(str(entry_px))) * sign * ctVal * qty_used

        result = "ربح ✅" if pnl > 0 else ("خسارة ❌" if pnl < 0 else "متعادل •")
        _send_tg(f"[CLOSE] {best['instId']} {close_side.upper()} ordId={ordId2} px≈{exit_px}\\nالنتيجة: {result} | PnL≈ {pnl:.4f} USDT")

if __name__ == "__main__":
    main()
