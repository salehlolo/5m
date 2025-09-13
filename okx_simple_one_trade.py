import os, json, time, hmac, base64, hashlib, re
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime, timezone, UTC
import requests
import pandas as pd
import numpy as np

try:
    import pandas_ta as ta
    HAS_TA = True
except Exception:
    HAS_TA = False

getcontext().prec = 34

# ====== CONFIG ======
BASE_URL = os.getenv("OKX_API_BASE", "https://www.okx.com")
API_KEY = os.getenv("OKX_API_KEY") or "29809262-8962-4460-b7a0-280131629aea"
API_SECRET = os.getenv("OKX_API_SECRET") or os.getenv("OKX_SECRET_KEY") or "1EBB409F0B37C9CB936FD6BD510A6C00"
API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE") or os.getenv("OKX_PASSPHRASE") or "Q@BWaG2bf5ybmGZ"
DEMO = os.getenv("DEMO", "1")
HEDGE_MODE = os.getenv("HEDGE_MODE", "0") == "1"

BAR_SECONDS = 900
BAR_STR = "15m"
PREP_MS = 1200
BUSY_WAIT_MS = 50
CLOSE_EARLY_MS = 5000
TOP_N = 10
CONSENSUS_THRESHOLD = int(os.getenv("CONSENSUS", "65"))

# إعدادات التداول والهوامش
TRADE_PCT = Decimal(os.getenv("TRADE_PCT", "0.90"))      # 90% من الإكويتي
LEVERAGE = Decimal(os.getenv("LEVERAGE", "20"))
SAFETY = Decimal(os.getenv("SAFETY", "0.90"))            # هامش أمان إضافي
RETRIES_51008 = int(os.getenv("RETRIES_51008", "4"))
SHRINK_FACTOR = Decimal(os.getenv("SHRINK_FACTOR", "0.85"))

# cache for applied leverage per instrument
_LEVER_CACHE = {}

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8367220857:AAHgvPb1pmAqHSwgixb9jBYCT2TTRrDnNL0"
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "1266351161"

# ====== HELPERS ======
def _now_ts():
    return datetime.now(UTC)

def _ts_str(ms=False):
    if ms:
        return _now_ts().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return _now_ts().isoformat().replace("+00:00", "Z")

def _send_tg(text: str):
    print(text)
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
        except Exception as e:
            print(f"[WARN] Telegram send failed: {e}")

def _headers(method: str, request_path: str, body_str: str = "", query_str: str = None):
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
    df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"], errors="coerce"), unit="ms", utc=True)
    for c in ["o","h","l","c","vol"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df.rename(columns={"o":"Open","h":"High","l":"Low","c":"Close","vol":"Volume"}, inplace=True)
    df.sort_values("ts", inplace=True)
    df.set_index("ts", drop=True, inplace=True)
    return df

def server_time_ms():
    try:
        r = _req("GET", "/api/v5/public/time")
        return int(r.get("data", [{}])[0].get("ts", 0))
    except Exception:
        return int(time.time() * 1000)

def next_bar_boundary_ms(ms=None):
    ms = server_time_ms() if ms is None else ms
    period = BAR_SECONDS * 1000
    return ((ms // period) + 1) * period

def next_hour_boundary_ms(ms=None):
    ms = server_time_ms() if ms is None else ms
    H = 3600 * 1000
    return ((ms // H) + 1) * H

def wait_until(target_ms, busy_ms=BUSY_WAIT_MS):
    while True:
        now = server_time_ms()
        remain = target_ms - now
        if remain <= 0:
            break
        if remain > busy_ms:
            time.sleep((remain - busy_ms) / 1000)
        else:
            time.sleep(remain / 1000)

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
            "ctValCcy": it.get("ctValCcy", ""),
            "maxMktSz": Decimal(str(it.get("maxMktSz") or it.get("maxLmtSz") or "0"))
        }
    return mp

def get_candles(instId, limit=300):
    res = _req("GET", "/api/v5/market/candles", {"instId":instId, "bar":BAR_STR, "limit":limit})
    return res.get("data", [])

def get_ticker(instId):
    res = _req("GET", "/api/v5/market/ticker", {"instId": instId})
    try:
        return float(res.get("data", [{}])[0].get("last"))
    except Exception:
        return None

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

def get_usdt_equity():
    """يرجع إجمالي الإكويتي/الرصيد المتاح بـ USDT من حساب OKX (الديمو/الحقيقي)."""
    try:
        r = _req("GET", "/api/v5/account/balance", {"ccy": "USDT"})
        d = r.get("data", [{}])[0]
        eq = Decimal(str(d.get("totalEq") or "0"))
        if eq == 0:
            for it in d.get("details", []) or []:
                if it.get("ccy") == "USDT":
                    val = it.get("availEq") or it.get("eq") or it.get("cashBal")
                    eq = Decimal(str(val or "0"))
                    break
        return max(eq, Decimal("0"))
    except Exception:
        return Decimal("0")


def set_leverage_safely(instId, lever, mgnMode="cross"):
    if _LEVER_CACHE.get(instId) == lever:
        return {"code": "0"}
    body = {"instId": instId, "lever": str(lever), "mgnMode": mgnMode}
    for wait in (0.1, 0.2, 0.4, 0.8, 1.6):
        res = _req("POST", "/api/v5/account/set-leverage", body)
        code = str(res.get("code"))
        if code == "0":
            _LEVER_CACHE[instId] = lever
            return res
        if code == "50011":
            time.sleep(wait)
            continue
        return res
    return res


def money_flow_index_series(h, l, c, v, length=14):
    h = h.astype("float64"); l = l.astype("float64"); c = c.astype("float64"); v = v.astype("float64")
    tp = (h + l + c) / 3.0
    rmf = tp * v
    up = tp > tp.shift(1)
    dn = tp < tp.shift(1)
    pos = rmf.where(up, 0.0).rolling(length).sum()
    neg = rmf.where(dn, 0.0).rolling(length).sum()
    mfr = pos / (neg + 1e-12)
    return 100.0 - (100.0 / (1.0 + mfr))


def session_vwap_series(df):
    vol = df["Volume"].astype("float64")
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    tpv = tp * vol
    dates = df.index.tz_convert("UTC").date
    cum_tpv = tpv.groupby(dates).cumsum()
    cum_v = vol.groupby(dates).cumsum()
    return cum_tpv / (cum_v + 1e-12)


def _parse_max_contracts_51004(msg: str) -> int | None:
    m = re.search(r"more than\s+([\d,]+)\(contracts\)", msg or "", flags=re.I)
    return int(m.group(1).replace(",", "")) if m else None

# ====== INDICATORS ======
def indicators(df, idx):
    o = df["Open"].astype("float64")
    h = df["High"].astype("float64")
    l = df["Low"].astype("float64")
    c = df["Close"].astype("float64")
    v = df.get("Volume")
    if v is not None:
        v = v.astype("float64")
    votes = []

    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    votes.append(1 if ema9.iloc[idx] > ema21.iloc[idx] else -1)

    sma20 = c.rolling(20).mean(); sma50 = c.rolling(50).mean()
    votes.append(0 if (pd.isna(sma20.iloc[idx]) or pd.isna(sma50.iloc[idx])) else (1 if sma20.iloc[idx] > sma50.iloc[idx] else -1))

    try:
        rsi = ta.rsi(c, length=14) if HAS_TA else None
    except Exception:
        rsi = None
    if rsi is None:
        d = c.diff(); up = d.clip(lower=0).rolling(14).mean(); dn = -d.clip(upper=0).rolling(14).mean()
        rs = up / (dn + 1e-9); rsi = 100 - (100/(1+rs))
    rv = rsi.iloc[idx]
    votes.append(1 if rv>55 else (-1 if rv<45 else 0))

    if HAS_TA:
        try:
            st = ta.stoch(h,l,c,k=14,d=3)
            k = st.iloc[:,0]; d_ = st.iloc[:,1]
            votes.append(1 if (k.iloc[idx] > d_.iloc[idx] and k.iloc[idx] < 80) else (-1 if (k.iloc[idx] < d_.iloc[idx] and k.iloc[idx] > 20) else 0))
        except Exception:
            votes.append(0)
    else:
        ll = l.rolling(14).min(); hh = h.rolling(14).max()
        k = 100*(c-ll)/(hh-ll+1e-9); d_ = k.rolling(3).mean()
        votes.append(1 if (k.iloc[idx] > d_.iloc[idx] and k.iloc[idx] < 80) else (-1 if (k.iloc[idx] < d_.iloc[idx] and k.iloc[idx] > 20) else 0))

    if HAS_TA:
        try:
            mac = ta.macd(c); ml = mac.iloc[:,0]; sg = mac.iloc[:,2]
            votes.append(1 if ml.iloc[idx] > sg.iloc[idx] else -1)
        except Exception:
            votes.append(0)
    else:
        ema12 = c.ewm(span=12, adjust=False).mean(); ema26 = c.ewm(span=26, adjust=False).mean()
        ml = ema12 - ema26; sg = ml.ewm(span=9, adjust=False).mean()
        votes.append(1 if ml.iloc[idx] > sg.iloc[idx] else -1)

    basis = c.rolling(20).mean()
    votes.append(1 if c.iloc[idx] > basis.iloc[idx] else -1)

    if HAS_TA:
        try:
            adx = ta.adx(h,l,c,length=14)
            plusd, minusd, ax = adx["DMP_14"], adx["DMN_14"], adx["ADX_14"]
            votes.append(0 if ax.iloc[idx] < 20 else (1 if plusd.iloc[idx] > minusd.iloc[idx] else -1))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            cci = ta.cci(h,l,c,length=20).iloc[idx]
            votes.append(1 if cci>0 else (-1 if cci<0 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    ha_c = (o+h+l+c)/4.0
    ha_o = ha_c.copy()
    for j in range(1, len(df)):
        ha_o.iloc[j] = (ha_o.iloc[j-1] + ha_c.iloc[j-1]) / 2.0
    votes.append(1 if ha_c.iloc[idx] > ha_o.iloc[idx] else -1)

    conv = (h.rolling(9).max()+l.rolling(9).min())/2.0
    base = (h.rolling(26).max()+l.rolling(26).min())/2.0
    span_a = ((conv+base)/2.0).shift(26)
    span_b = ((h.rolling(52).max()+l.rolling(52).min())/2.0).shift(26)
    top = pd.concat([span_a, span_b], axis=1).max(axis=1)
    bot = pd.concat([span_a, span_b], axis=1).min(axis=1)
    if pd.isna(top.iloc[idx]) or pd.isna(bot.iloc[idx]):
        votes.append(0)
    else:
        votes.append(1 if c.iloc[idx] > top.iloc[idx] else (-1 if c.iloc[idx] < bot.iloc[idx] else 0))

    if HAS_TA:
        try:
            st = ta.supertrend(h,l,c,length=10,multiplier=3.0)
            dcol = [x for x in st.columns if x.startswith("SUPERTd_")]
            votes.append(1 if st[dcol[0]].iloc[idx] > 0 else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            kc = ta.kc(h,l,c,length=20); mid = kc.iloc[:,1]
            votes.append(1 if c.iloc[idx] > mid.iloc[idx] else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            wr = ta.willr(h,l,c,length=14).iloc[idx]
            votes.append(1 if wr > -50 else (-1 if wr < -50 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if v is not None:
        try:
            mfi_val = float(money_flow_index_series(h, l, c, v, 14).iloc[idx])
            votes.append(1 if mfi_val > 50 else (-1 if mfi_val < 50 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            rv = ta.roc(c,length=9).iloc[idx]
            votes.append(1 if rv > 0 else (-1 if rv < 0 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            ar = ta.aroon(h,l,length=14); up = ar.iloc[:,0]; dn = ar.iloc[:,1]
            votes.append(1 if up.iloc[idx] > dn.iloc[idx] else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            tv = ta.tsi(c).iloc[idx]
            votes.append(1 if tv > 0 else (-1 if tv < 0 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA and v is not None:
        try:
            dv = ta.obv(c, v).diff().iloc[idx]
            votes.append(1 if dv > 0 else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            hma = ta.hma(c,length=55)
            votes.append(1 if hma.iloc[idx] > hma.shift(1).iloc[idx] else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    if HAS_TA:
        try:
            ps = ta.psar(h,l,c)
            pcol = [x for x in ps.columns if x.startswith("PSAR")]
            pv = ps[pcol[0]].iloc[idx]
            votes.append(1 if c.iloc[idx] > pv else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    # 21) EMA 50/200 cross
    ema50 = c.ewm(span=50, adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()
    votes.append(1 if ema50.iloc[idx] > ema200.iloc[idx] else -1)

    # 22) SMA100 slope
    sma100 = c.rolling(100).mean()
    if pd.isna(sma100.iloc[idx]) or pd.isna(sma100.shift(1).iloc[idx]):
        votes.append(0)
    else:
        votes.append(1 if sma100.iloc[idx] > sma100.shift(1).iloc[idx] else -1)

    # 23) VWAP session
    if v is not None:
        try:
            vwap = float(session_vwap_series(df).iloc[idx])
            votes.append(1 if c.iloc[idx] > vwap else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    # 24) Donchian(20) mid
    try:
        upper = h.rolling(20).max(); lower = l.rolling(20).min(); mid = (upper+lower)/2
        if pd.isna(mid.iloc[idx]):
            votes.append(0)
        else:
            votes.append(1 if c.iloc[idx] > mid.iloc[idx] else -1)
    except Exception:
        votes.append(0)

    # 25) KAMA(10) slope
    if HAS_TA:
        try:
            kama = ta.kama(c,length=10)
            if pd.isna(kama.iloc[idx]) or pd.isna(kama.shift(1).iloc[idx]):
                votes.append(0)
            else:
                votes.append(1 if kama.iloc[idx] > kama.shift(1).iloc[idx] else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    # 26) PPO(12,26,9)
    if HAS_TA:
        try:
            ppo = ta.ppo(c)
            line = ppo.iloc[:,0]; sig = ppo.iloc[:,1]
            votes.append(1 if line.iloc[idx] > sig.iloc[idx] else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    # 27) Ultimate Oscillator
    if HAS_TA:
        try:
            uo = ta.uo(h,l,c).iloc[idx]
            votes.append(1 if uo > 50 else (-1 if uo < 50 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    # 28) Chaikin Money Flow
    if HAS_TA and v is not None:
        try:
            cmf = ta.cmf(h,l,c,v,length=20).iloc[idx]
            votes.append(1 if cmf > 0 else (-1 if cmf < 0 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    # 29) Vortex(14)
    if HAS_TA:
        try:
            vx = ta.vortex(h,l,c,length=14)
            vplus = vx.iloc[:,0]; vminus = vx.iloc[:,1]
            if pd.isna(vplus.iloc[idx]) or pd.isna(vminus.iloc[idx]):
                votes.append(0)
            else:
                votes.append(1 if vplus.iloc[idx] > vminus.iloc[idx] else -1)
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    # 30) TRIX(30)
    if HAS_TA:
        try:
            tr = ta.trix(c,length=30).iloc[idx]
            votes.append(1 if tr > 0 else (-1 if tr < 0 else 0))
        except Exception:
            votes.append(0)
    else:
        votes.append(0)

    return votes

def decide(instId):
    df = _to_df(get_candles(instId, 300))
    if df is None or len(df) < 100:
        return None
    idx = -2 if len(df) >= 2 else -1
    votes = indicators(df, idx)
    bulls = votes.count(1)
    bears = votes.count(-1)
    eff = bulls + bears
    bull_pct = 100.0 * bulls/eff if eff>0 else 50.0
    bear_pct = 100.0 - bull_pct
    side = "buy" if bull_pct >= bear_pct else "sell"
    score = bull_pct if side=="buy" else bear_pct
    last_close = float(df["Close"].iloc[idx])
    return {
        "instId": instId,
        "bull": bull_pct,
        "bear": bear_pct,
        "side": side,
        "score": score,
        "px": last_close
    }

def quantize_to_step(value: Decimal, step: Decimal) -> Decimal:
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    q = (steps * step).normalize()
    return q

def clamp_order_size(inst_info: dict, sz: Decimal) -> Decimal:
    lot = inst_info.get("lotSz", Decimal("1"))
    max_mkt = inst_info.get("maxMktSz", Decimal("0"))
    if max_mkt > 0 and sz > max_mkt:
        sz = max_mkt
    sz = quantize_to_step(sz, lot)
    return max(sz, Decimal("0"))

def compute_size(px_float: float, lotSz: Decimal, ctVal: Decimal, notional: Decimal) -> str:
    px = Decimal(str(px_float))
    raw = notional / (px * (ctVal if ctVal > 0 else Decimal("1e-9")))
    q = quantize_to_step(raw, lotSz)
    lot_decimals = max(0, -lotSz.as_tuple().exponent)
    return f"{q:.{lot_decimals}f}"

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

# ====== MAIN LOOP ======
def main():
    if not (API_KEY and API_SECRET and API_PASSPHRASE):
        print("[ERR] Set API credentials"); return
    info = get_instruments_map()
    hour_pnl_pos = Decimal("0")   # مجموع الأرباح الموجبة داخل الساعة
    hour_pnl_neg = Decimal("0")   # مجموع الخسائر (قيم سالبة) داخل الساعة
    next_hour_ms = next_hour_boundary_ms()
    while True:
        now_ms = server_time_ms()
        if now_ms >= next_hour_ms:
            net = (hour_pnl_pos + hour_pnl_neg)
            _send_tg(
                f"[ملخص الساعة] ربح إجمالي: {hour_pnl_pos:.2f} USDT | "
                f"خسارة إجمالية: {abs(hour_pnl_neg):.2f} USDT | "
                f"الصافي: {net:.2f} USDT"
            )
            hour_pnl_pos = Decimal("0")
            hour_pnl_neg = Decimal("0")
            next_hour_ms = next_hour_boundary_ms(now_ms)
        next_ms = next_bar_boundary_ms(now_ms)
        prep_wait = next_ms - PREP_MS - now_ms
        if prep_wait > 0:
            time.sleep(prep_wait/1000)
        ids = get_top_swaps(TOP_N)
        lever_int = int(LEVERAGE)
        for instId in ids:
            try:
                set_leverage_safely(instId, lever_int, "cross")
            except Exception:
                pass
        decisions = []
        for instId in ids:
            try:
                d = decide(instId)
                if d:
                    decisions.append(d)
            except Exception:
                continue
        decisions.sort(key=lambda x: x["score"], reverse=True)
        boundary_time = datetime.fromtimestamp(next_ms/1000, tz=timezone.utc).isoformat()
        if decisions:
            lines = []
            for idx, d in enumerate(decisions,1):
                dir_ar = "شراء" if d['side']=="buy" else "بيع"
                lines.append(f"{idx}) {d['instId']}: Bull {d['bull']:.1f}% | Bear {d['bear']:.1f}% ⇒ {dir_ar} (score {d['score']:.1f}%)")
            _send_tg(f"ملخّص 15m — {boundary_time} (يدخل بعد أقل من ثانية)\n" + "\n".join(lines))
        else:
            _send_tg(f"ملخّص 15m — {boundary_time} (يدخل بعد أقل من ثانية)\nلا بيانات")
        wait_until(next_ms, BUSY_WAIT_MS)
        if not decisions or decisions[0]['score'] < CONSENSUS_THRESHOLD:
            continue
        best = decisions[0]
        inst_info = info.get(best["instId"], {})
        lotSz = inst_info.get("lotSz", Decimal("1"))
        ctVal = inst_info.get("ctVal", Decimal("1"))
        max_mkt = inst_info.get("maxMktSz", Decimal("0"))
        print(f"[INFO] {best['instId']} lotSz={lotSz} ctVal={ctVal} maxMktSz={max_mkt}")
        lot_decimals = max(0, -lotSz.as_tuple().exponent)
        equity = get_usdt_equity()
        notional_90 = (equity * TRADE_PCT).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        max_notional = (equity * LEVERAGE * SAFETY).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        final_notional = notional_90 if notional_90 < max_notional else max_notional
        if final_notional <= Decimal("0.00"):
            _send_tg(f"[SKIP] eq={equity} notional90={notional_90} final={final_notional}")
            continue
        last_px = get_ticker(best["instId"]) or best["px"]
        sz_str = compute_size(last_px, lotSz, ctVal, final_notional)
        sz_dec = clamp_order_size(inst_info, Decimal(sz_str))
        sz_str = f"{sz_dec:.{lot_decimals}f}"
        if sz_dec <= 0:
            _send_tg(f"[SKIP] eq={equity} notional90={notional_90} final={final_notional}")
            continue
        set_leverage_safely(best["instId"], lever_int, "cross")
        ok, ordId, resp = place_order(best["instId"], best["side"], sz_str, reduceOnly=False)
        data = resp.get("data", [{}])[0]
        if not ok:
            s_code = str(data.get("sCode"))
            s_msg = data.get("sMsg", "")
            hard_cap = _parse_max_contracts_51004(s_msg) if s_code == "51004" else None
            if s_code in ("51008", "51004"):
                cur_size = Decimal(sz_str)
                if hard_cap is not None:
                    cur_size = min(cur_size, Decimal(str(hard_cap)))
                for _ in range(RETRIES_51008):
                    new_size = quantize_to_step(cur_size * SHRINK_FACTOR, lotSz)
                    new_size = clamp_order_size(inst_info, new_size)
                    if hard_cap is not None:
                        new_size = min(new_size, Decimal(str(hard_cap)))
                    if new_size <= 0:
                        break
                    old_str = f"{cur_size:.{lot_decimals}f}"
                    new_str = f"{new_size:.{lot_decimals}f}"
                    _send_tg(f"[RETRY-{s_code}] {best['instId']} size={old_str} -> {new_str}")
                    set_leverage_safely(best["instId"], lever_int, "cross")
                    ok, ordId, resp = place_order(best["instId"], best["side"], new_str, reduceOnly=False)
                    cur_size = new_size
                    sz_str = new_str
                    if ok:
                        break
                    time.sleep(0.15)
                data = resp.get("data", [{}])[0]
        if not ok:
            _send_tg(f"[OPEN-FAILED] {best['instId']} {best['side'].upper()} sz={sz_str} resp={resp}")
            continue
        entry_px, entry_q = avg_fill_price_and_qty(ordId)
        if entry_px is None:
            entry_px = best['px']
        if entry_q is None:
            try: entry_q = Decimal(sz_str)
            except: entry_q = Decimal("0")
        _send_tg(f"[OPEN] {best['instId']} {best['side'].upper()} ordId={ordId} sz={sz_str} px≈{entry_px} score={best['score']:.1f}%")
        open_ms = next_ms
        close_ms = open_ms + BAR_SECONDS*1000 - CLOSE_EARLY_MS
        wait_until(close_ms, BUSY_WAIT_MS)
        close_side = "sell" if best["side"]=="buy" else "buy"
        ok2, ordId2, resp2 = place_order(best["instId"], close_side, sz_str, reduceOnly=True)
        data2 = resp2.get("data", [{}])[0]
        if not ok2:
            s_code2 = str(data2.get("sCode"))
            s_msg2 = data2.get("sMsg", "")
            hard_cap = _parse_max_contracts_51004(s_msg2) if s_code2 == "51004" else None
            if s_code2 in ("51008", "51004"):
                cur_size = Decimal(sz_str)
                if hard_cap is not None:
                    cur_size = min(cur_size, Decimal(str(hard_cap)))
                for _ in range(RETRIES_51008):
                    new_size = quantize_to_step(cur_size * SHRINK_FACTOR, lotSz)
                    new_size = clamp_order_size(inst_info, new_size)
                    if hard_cap is not None:
                        new_size = min(new_size, Decimal(str(hard_cap)))
                    if new_size <= 0:
                        break
                    old_str = f"{cur_size:.{lot_decimals}f}"
                    new_str = f"{new_size:.{lot_decimals}f}"
                    _send_tg(f"[RETRY-{s_code2}] {best['instId']} size={old_str} -> {new_str}")
                    set_leverage_safely(best["instId"], lever_int, "cross")
                    ok2, ordId2, resp2 = place_order(best["instId"], close_side, new_str, reduceOnly=True)
                    cur_size = new_size
                    sz_str = new_str
                    if ok2:
                        break
                    time.sleep(0.15)
                data2 = resp2.get("data", [{}])[0]
        if not ok2:
            _send_tg(f"[CLOSE-FAILED] {best['instId']} resp={resp2}")
            continue
        exit_px, exit_q = avg_fill_price_and_qty(ordId2)
        if exit_px is None:
            d2 = decide(best['instId'])
            exit_px = d2['px'] if d2 else entry_px
        if exit_q is None:
            try: exit_q = Decimal(sz_str)
            except: exit_q = Decimal("0")
        sign = Decimal("1") if best['side']=="buy" else Decimal("-1")
        qty_used = Decimal(str(min(float(entry_q or 0), float(exit_q or 0))))
        pnl = (Decimal(str(exit_px)) - Decimal(str(entry_px))) * sign * ctVal * qty_used
        _send_tg(f"[CLOSE] {best['instId']} {close_side.upper()} ordId={ordId2} px≈{exit_px}\nPnL≈ {pnl:.4f} USDT")
        if pnl > 0:
            hour_pnl_pos += pnl
        elif pnl < 0:
            hour_pnl_neg += pnl

if __name__ == "__main__":
    main()
