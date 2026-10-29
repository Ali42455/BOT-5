#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PIONEX SPOT BOT — EMA 20 SCALPING (BUY ONLY) — 5M
=================================================

Isticmaalka:
    pip install requests
    Samee fayl la yidhaahdo .env (isla galka bot-ka) oo ku qor:
        PIONEX_API_KEY=...               # API key (Enable reading + Enable trading)
        PIONEX_API_SECRET=...
    python3 pionex_bot.py

Ikhtiyaari:
    LIVE_TRADING=false      -> scan oo keliya (signal-ka ayuu muujinayaa, amar ma dirayo)
    WATCHLIST="BTC_USDT,ETH_USDT,..."   -> beddel watchlist-ka (19 pair)
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID -> ogeysiis Telegram ah

MUHIIM — sida SL iyo TP u shaqeeyaan (Pionex API):
    Pionex API wuxuu taageeraa oo keliya amarrada LIMIT iyo MARKET (stop-order ma jiro).
      * TP  = amar LIMIT SELL ah oo si toos ah ugu dhaca Pionex (exchange-ka gudihiisa).
      * SL  = bot-ku ayaa kormeera qiimaha; marka uu taabto SL, TP-ga ayuu joojiyaa
              kaddibna MARKET SELL buu sameeyaa.
    Haddii bot-ku damo / internet go'o, SL-ku ma shaqaynayo ilaa bot-ku soo noqdo.
"""

import os
import sys
import json
import time
import hmac
import hashlib
import logging
from logging.handlers import RotatingFileHandler
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation

import requests


def load_env_file():
    """Akhri faylka .env (KEY=VALUE). Wuxuu raadiyaa isla galka bot-ka, kaddibna galka aad ka socodsiiso.
    Doorsoomayaasha hore ugu jiray system-ka (export) waa mudnaan leeyihiin."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                elif " #" in val:
                    val = val.split(" #", 1)[0].strip()
                if key:
                    os.environ.setdefault(key, val)


load_env_file()

# =====================================================================
# 1) DEJINTA (CONFIG)
# =====================================================================
API_KEY = os.getenv("PIONEX_API_KEY", "")
API_SECRET = os.getenv("PIONEX_API_SECRET", "")
LIVE_TRADING = os.getenv("LIVE_TRADING", "true").strip().lower() in ("1", "true", "yes", "on")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

BASE_URL = "https://api.pionex.com"
REQUEST_TIMEOUT = 10
MIN_REQUEST_GAP = 0.15            # ~6-7 codsi/ilbiriqsi (xadka Pionex waa 10/s)

STATE_FILE = os.getenv("STATE_FILE", "pionex_state.json")
LOG_FILE = os.getenv("LOG_FILE", "pionex_bot.log")

# --- Suuqa ---
INTERVAL = "5M"
INTERVAL_MS = 5 * 60 * 1000
SCAN_DELAY_SEC = 5                # scan-ka ku billow 5s kadib marka shumacu xirmo
MONITOR_INTERVAL_SEC = 5          # sidee u badan loo hubiyo SL/TP
KLINE_LIMIT = 150

# --- Maaraynta trades-ka ---
TRADE_USDT = Decimal("50")        # lacagta trade kasta
MAX_OPEN_TRADES = 3               # ugu badnaan 3 trade oo furan
TP_RETRY_COOLDOWN_SEC = 30

# --- Strategy: EMA 20 Scalping (BUY ONLY) ---
EMA_PERIOD = 20
SLOPE_LOOKBACK = 5                # EMA20 hadda vs 5 shumac ka hor
MIN_SLOPE_PCT = 0.10              # ugu yaraan +0.10%
TREND_CANDLES = 4                 # 4 shumac oo aan EMA20 taaban
MIN_BODY_RATIO = 0.60             # jirka shumaca >= 60% baaxaddiisa
RR = 2.0                          # Risk:Reward = 1:2
SL_BUFFER_PCT = 0.05              # "wax yar" hoosta Swing Low (0.05%)  <- BEDDEL HADDII LOO BAAHDO
MAX_PULLBACK_CANDLES = 3          # pullback-ku ugu badnaan 3 shumac  <- BEDDEL HADDII LOO BAAHDO
MAX_RISK_PCT = 1.5                # xadka khatarta trade-ka (%)       <- BEDDEL HADDII LOO BAAHDO

# --- Watchlist: 19 USDT Spot pairs ---
# ⚠️ Dokumentigaaga ma lahayn liiska 19-ka pair. Kuwani waa kuwo ku meel gaar ah (liquid).
#    BEDDEL halkan, ama isticmaal WATCHLIST="A_USDT,B_USDT,..." (env).
DEFAULT_WATCHLIST = [
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "BNB_USDT", "XRP_USDT",
    "DOGE_USDT", "ADA_USDT", "AVAX_USDT", "LINK_USDT", "DOT_USDT",
    "LTC_USDT", "TRX_USDT", "NEAR_USDT", "SUI_USDT", "APT_USDT",
    "ARB_USDT", "OP_USDT", "INJ_USDT", "TON_USDT",
]

# =====================================================================
# 2) LOGGING + OGEYSIIS
# =====================================================================
log = logging.getLogger("pionex_bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)
try:
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except OSError:
    pass


def notify(msg, level="info"):
    """Qor log-ka, oo haddii Telegram la dejiyey, u dir ogeysiis."""
    getattr(log, level, log.info)(msg)
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg[:3500]},
                timeout=8,
            )
        except Exception:
            pass


# =====================================================================
# 3) HELPERS (Decimal / precision)
# =====================================================================
def D(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _q(prec):
    return Decimal(1).scaleb(-int(prec))


def floor_dec(x, prec):
    return x.quantize(_q(prec), rounding=ROUND_DOWN)


def ceil_dec(x, prec):
    return x.quantize(_q(prec), rounding=ROUND_UP)


def fmt(x):
    return format(x, "f")


# =====================================================================
# 4) PIONEX API CLIENT
# =====================================================================
class PionexError(Exception):
    """Pionex wuxuu jawaab ku celiyay result=false (amarku si cad ayaa loo diiday)."""

    def __init__(self, code, message, status=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class NetworkError(Exception):
    """Internet/timeout/5xx — natiijada codsiga lama hubo (amarka laga yaabo inuu gaadhay)."""


def _is_not_found(e):
    """Amarka Pionex kuma jiro? Koodhka rasmiga ah + qaababka la mid ah (Pionex wuxuu soo celiyaa koodhado aan dukumentiga ku jirin)."""
    code = str(getattr(e, "code", "")).upper()
    return code == "TRADE_ORDER_NOT_FOUND" or "NOT_FOUND" in code or "NOT_EXIST" in code


class PionexAPI:
    def __init__(self):
        self.session = requests.Session()
        self.time_offset_ms = 0
        self.last_server_ts = None
        self._last_call = 0.0

    def _throttle(self):
        gap = time.time() - self._last_call
        if gap < MIN_REQUEST_GAP:
            time.sleep(MIN_REQUEST_GAP - gap)
        self._last_call = time.time()

    def _request(self, method, path, params=None, body=None, private=False, retries=2):
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            p = dict(params or {})
            if private:
                p["timestamp"] = str(int(time.time() * 1000) + self.time_offset_ms)
            query = "&".join(f"{k}={p[k]}" for k in sorted(p))
            path_url = f"{path}?{query}" if query else path
            body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

            headers = {}
            if private:
                msg = method + path_url + body_str          # METHOD + PATH_URL(sorted query) + body
                sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
                headers["PIONEX-KEY"] = API_KEY
                headers["PIONEX-SIGNATURE"] = sig
            if body is not None:
                headers["Content-Type"] = "application/json"

            try:
                resp = self.session.request(
                    method, BASE_URL + path_url,
                    data=body_str.encode() if body is not None else None,
                    headers=headers, timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                if method == "GET" and attempt <= retries:
                    time.sleep(1.5 * attempt)
                    continue
                raise NetworkError(str(e))

            if resp.status_code == 429:
                log.warning("Rate limit (429) — sugayaa 61s")
                time.sleep(61)
                if method == "GET" and attempt <= retries:
                    continue
                raise PionexError("RATE_LIMIT", "HTTP 429", 429)
            if resp.status_code >= 500:
                if method == "GET" and attempt <= retries:
                    time.sleep(1.5 * attempt)
                    continue
                raise NetworkError(f"HTTP {resp.status_code}")

            try:
                data = resp.json()
            except ValueError:
                raise PionexError("BAD_RESPONSE", resp.text[:200], resp.status_code)

            if isinstance(data, dict) and data.get("result") is True:
                self.last_server_ts = data.get("timestamp")
                return data.get("data") or {}
            code = (data or {}).get("code", "UNKNOWN")
            message = (data or {}).get("message", "")
            if code == "INVALID_TIMESTAMP":
                message += " (saacadda kombuyuutarka ayaa laga yaabaa inay khaldan tahay)"
            raise PionexError(code, message, resp.status_code)

    # ---------- Public ----------
    def sync_time(self):
        local = int(time.time() * 1000)
        self._request("GET", "/api/v1/market/tickers", {"symbol": "BTC_USDT"})
        if self.last_server_ts:
            self.time_offset_ms = int(self.last_server_ts) - local

    def get_symbols(self):
        return self._request("GET", "/api/v1/common/symbols", {"type": "SPOT"}).get("symbols", [])

    def get_klines(self, symbol):
        d = self._request("GET", "/api/v1/market/klines",
                          {"symbol": symbol, "interval": INTERVAL, "limit": KLINE_LIMIT})
        out = {}
        for k in d.get("klines", []):
            t = int(k["time"])
            out[t] = {"time": t, "open": float(k["open"]), "high": float(k["high"]),
                      "low": float(k["low"]), "close": float(k["close"]),
                      "volume": float(k.get("volume", 0) or 0)}
        return [out[t] for t in sorted(out)]

    def get_tickers(self):
        d = self._request("GET", "/api/v1/market/tickers", {"type": "SPOT"})
        return {t["symbol"]: D(t["close"]) for t in d.get("tickers", []) if t.get("close")}

    # ---------- Private ----------
    def get_balances(self):
        d = self._request("GET", "/api/v1/account/balances", private=True)
        return {b["coin"]: (D(b.get("free")), D(b.get("frozen"))) for b in d.get("balances", [])}

    def _new_order(self, body):
        return self._request("POST", "/api/v1/trade/order", body=body, private=True)

    def market_buy(self, symbol, amount, cid):
        return self._new_order({"symbol": symbol, "side": "BUY", "type": "MARKET",
                                "amount": amount, "clientOrderId": cid})

    def market_sell(self, symbol, size, cid):
        return self._new_order({"symbol": symbol, "side": "SELL", "type": "MARKET",
                                "size": size, "clientOrderId": cid})

    def limit_sell(self, symbol, size, price, cid):
        return self._new_order({"symbol": symbol, "side": "SELL", "type": "LIMIT",
                                "size": size, "price": price, "clientOrderId": cid})

    def cancel_order(self, symbol, order_id):
        return self._request("DELETE", "/api/v1/trade/order",
                             body={"symbol": symbol, "orderId": int(order_id)}, private=True)

    def get_order(self, order_id):
        try:
            return self._request("GET", "/api/v1/trade/order",
                                 {"orderId": int(order_id)}, private=True)
        except PionexError as e:
            if _is_not_found(e):
                return None
            raise

    def get_order_by_cid(self, cid):
        try:
            return self._request("GET", "/api/v1/trade/orderByClientOrderId",
                                 {"clientOrderId": cid}, private=True)
        except PionexError as e:
            if _is_not_found(e):
                return None
            raise


# =====================================================================
# 5) STRATEGY — EMA 20 SCALPING (BUY ONLY)
# =====================================================================
def ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def evaluate_signal(candles):
    """
    candles = shumacyo DHAMMAADAY oo kor loo habeeyey (kii ugu dambeeyay = shumaca xaqiijinta).
    Soo celiya (signal|None, sabab).
    """
    n = len(candles)
    if n < EMA_PERIOD + SLOPE_LOOKBACK + TREND_CANDLES + MAX_PULLBACK_CANDLES + 2:
        return None, "xog aan ku filnayn"

    closes = [c["close"] for c in candles]
    ema = ema_series(closes, EMA_PERIOD)
    i = n - 1
    c = candles[i]
    if ema[i] is None or ema[i - SLOPE_LOOKBACK] is None:
        return None, "EMA20 ma diyaarsana"

    # --- Confirmation candle ---
    rng = c["high"] - c["low"]
    if rng <= 0:
        return None, "shumaca baaxad ma leh"
    if c["close"] <= c["open"]:
        return None, "shumaca xaqiijintu ma aha bullish"
    if c["close"] <= ema[i]:
        return None, "shumaca wuxuu ku xirmay dhinaca khaldan ee EMA20"
    body_ratio = (c["close"] - c["open"]) / rng
    if body_ratio < MIN_BODY_RATIO:
        return None, f"jirka shumaca {body_ratio * 100:.0f}% (<60%)"

    # --- Trend (EMA slope) ---
    slope_pct = (ema[i] / ema[i - SLOPE_LOOKBACK] - 1.0) * 100.0
    if slope_pct < MIN_SLOPE_PCT:
        return None, f"EMA20 jiirad ku filan ma laha ({slope_pct:.3f}%)"

    # --- Pullback: shumac taabtay EMA20 (low <= EMA20) ---
    def touch(k):
        return ema[k] is not None and candles[k]["low"] <= ema[k]

    if touch(i):
        end = i
    elif touch(i - 1):
        end = i - 1
    else:
        return None, "qiimuhu ma samayn pullback"
    start = end
    while start - 1 >= 0 and touch(start - 1):
        start -= 1
    if end - start + 1 > MAX_PULLBACK_CANDLES:
        return None, "pullback aad u dheer"

    # --- 4 shumac oo aan EMA20 taaban (Low > EMA20) ka hor pullback-ka ---
    if start - TREND_CANDLES < 0:
        return None, "xog aan ku filnayn (trend candles)"
    for k in range(start - TREND_CANDLES, start):
        if ema[k] is None or candles[k]["low"] <= ema[k]:
            return None, "midkood afarta shumac wuu taabtay EMA20"

    # --- SL / khatar ---
    swing_low = min(candles[k]["low"] for k in range(start, end + 1))
    sl = swing_low * (1.0 - SL_BUFFER_PCT / 100.0)
    entry_ref = c["close"]
    if sl >= entry_ref:
        return None, "SL ma xisaabmi karo"
    risk_pct = (entry_ref - sl) / entry_ref * 100.0
    if risk_pct > MAX_RISK_PCT:
        return None, f"khatarta {risk_pct:.2f}% waa ka badan tahay xadka"

    return {
        "time": c["time"], "entry_ref": entry_ref, "sl": sl, "swing_low": swing_low,
        "ema": ema[i], "slope_pct": slope_pct, "body_ratio": body_ratio,
        "risk_pct": risk_pct, "pullback_len": end - start + 1,
    }, "OK"


# =====================================================================
# 6) STATE (xusuusta bot-ka — faylka JSON)
# =====================================================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("trades", {})
            s.setdefault("memory", {})
            s.setdefault("history", [])
            return s
        except (OSError, ValueError):
            log.error("State file ma akhrin karo — waan joogsanayaa si aan u ilaaliyo trades-ka jira.")
            raise
    return {"trades": {}, "memory": {}, "history": []}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# =====================================================================
# 7) BOT
# =====================================================================
class Bot:
    def __init__(self, api=None):
        self.api = api or PionexAPI()
        self.symbols = {}
        self.watchlist = []
        self.state = load_state()

    # ---------- Bilaabid ----------
    def load_symbols(self):
        for s in self.api.get_symbols():
            self.symbols[s["symbol"]] = {
                "base": s.get("baseCurrency"),
                "quote": s.get("quoteCurrency"),
                "base_prec": int(s.get("basePrecision", 8)),
                "quote_prec": int(s.get("quotePrecision", 8)),
                "amount_prec": int(s.get("amountPrecision", 2)),
                "min_amount": D(s.get("minAmount") or 0),
                "min_trade_size": D(s.get("minTradeSize") or 0),
                "min_dump": D(s.get("minTradeDumping") or 0),
                "enable": s.get("enable", True),
            }

    def startup(self):
        env_list = os.getenv("WATCHLIST", "").strip()
        wl = [x.strip().upper() for x in env_list.split(",") if x.strip()] if env_list else DEFAULT_WATCHLIST
        if LIVE_TRADING and not (API_KEY and API_SECRET):
            log.error("PIONEX_API_KEY / PIONEX_API_SECRET lama helin — ku qor faylka .env (isla galka bot-ka).")
            sys.exit(1)

        self.api.sync_time()
        self.load_symbols()

        self.watchlist = []
        for s in wl:
            info = self.symbols.get(s)
            if not info or not info["enable"] or info["quote"] != "USDT":
                log.warning(f"{s}: lagama helin Pionex Spot (ama waa la joojiyey) — waa la ka tegay")
                continue
            self.watchlist.append(s)

        log.info("=" * 60)
        log.info(f"PIONEX EMA20 BOT | {'LIVE' if LIVE_TRADING else 'SCAN-ONLY'} | {INTERVAL} | "
                 f"{TRADE_USDT} USDT/trade | max {MAX_OPEN_TRADES} trades | RR 1:{RR:g}")
        log.info(f"Watchlist ({len(self.watchlist)}): {', '.join(self.watchlist)}")
        log.info("=" * 60)

        if LIVE_TRADING:
            bal = self.api.get_balances()                 # hubi API key-ga
            log.info(f"USDT free: {bal.get('USDT', (Decimal(0), Decimal(0)))[0]}")
            log.info("Dib u akhrinta trades-ka jira ee Pionex (reconcile)...")
            self.monitor()
            log.info(f"Trades furan: {self.open_count()}")

    # ---------- Xaaladda ----------
    def save(self):
        save_state(self.state)

    def open_count(self):
        return sum(1 for t in self.state["trades"].values() if t["status"] in ("OPEN", "PENDING_ENTRY"))

    def wait_order(self, order_id, tries=12, delay=0.5):
        o = None
        for _ in range(tries):
            time.sleep(delay)
            o = self.api.get_order(order_id)
            if o and o.get("status") == "CLOSED":
                return o
        return o

    def drop_trade(self, t, why):
        self.state["trades"].pop(t["id"], None)
        self.save()
        log.warning(f"{t['symbol']}: trade waa laga saaray xusuusta — {why}")

    def close_trade(self, t, reason, exit_price=None, proceeds=None):
        cost = D(t.get("entry_cost") or 0)
        pnl = (proceeds - cost) if (proceeds is not None and cost > 0) else None
        t["status"] = "CLOSED"
        t["exit_reason"] = reason
        t["closed_ms"] = int(time.time() * 1000)
        mem = self.state["memory"].setdefault(t["symbol"], {})
        mem["last_closed_ms"] = t["closed_ms"]
        self.state["history"].append({
            "id": t["id"], "symbol": t["symbol"], "reason": reason,
            "entry": t.get("entry_price"), "exit": fmt(exit_price) if exit_price is not None else None,
            "sl": t.get("sl"), "tp": t.get("tp"),
            "pnl_usdt_approx": fmt(pnl.quantize(Decimal("0.0001"))) if pnl is not None else None,
            "opened_ms": t.get("opened_ms"), "closed_ms": t["closed_ms"],
        })
        self.state["history"] = self.state["history"][-300:]
        self.state["trades"].pop(t["id"], None)
        self.save()
        extra = f" | PnL ≈ {pnl.quantize(Decimal('0.01'))} USDT (khidmadda la'aanteed)" if pnl is not None else ""
        icon = {"TP": "🎯", "TP_SOFT": "🎯", "SL": "🛑"}.get(reason, "ℹ️")
        notify(f"{icon} {t['symbol']} XIDHMAY ({reason}) | entry {t.get('entry_price')} → exit "
               f"{fmt(exit_price) if exit_price is not None else '?'}{extra}")

    # ---------- Furitaanka trade ----------
    def open_trade(self, sym, sig):
        info = self.symbols[sym]
        tid = f"e20-{info['base']}-{int(time.time())}"
        t = {
            "id": tid, "symbol": sym, "base": info["base"], "status": "PENDING_ENTRY",
            "created_ms": int(time.time() * 1000), "signal_time": sig["time"],
            "sl": fmt(D(repr(sig["sl"]))), "tp": None, "qty": None,
            "entry_price": None, "entry_cost": None, "entry_order_id": None,
            "tp_order_id": None, "tp_seq": 0, "tp_last_try": 0, "tp_fail": 0,
            "pending_exit": None, "exit_order_id": None, "exit_seq": 0,
        }
        self.state["trades"][tid] = t
        self.state["memory"].setdefault(sym, {})["last_signal_time"] = sig["time"]
        self.save()                                         # ka hor inta amarka la dirin

        amount = fmt(floor_dec(TRADE_USDT, info["amount_prec"]))
        notify(f"📈 SIGNAL {sym} | entry≈{sig['entry_ref']:.8g} SL≈{sig['sl']:.8g} "
               f"risk {sig['risk_pct']:.2f}% | slope {sig['slope_pct']:.2f}% | BUY {amount} USDT")
        try:
            r = self.api.market_buy(sym, amount, tid)
            t["entry_order_id"] = r.get("orderId")
            self.save()
        except PionexError as e:
            self.drop_trade(t, f"Pionex wuu diiday amarka: {e}")
            notify(f"⚠️ {sym}: amarka BUY waa la diiday — {e}", "warning")
            return
        except NetworkError as e:
            log.warning(f"{sym}: amarka natiijadiisa lama hubo ({e}) — waan hubinayaa clientOrderId")

        for _ in range(12):
            time.sleep(0.5)
            self.finalize_entry(t)
            if t["id"] not in self.state["trades"] or t["status"] == "OPEN":
                break

    def finalize_entry(self, t):
        """Hubi in amarka BUY si dhab ah u fulay; kaddib u beddel OPEN oo dhig TP."""
        if t["status"] != "PENDING_ENTRY":
            return
        sym = t["symbol"]
        info = self.symbols[sym]
        o = self.api.get_order(t["entry_order_id"]) if t.get("entry_order_id") else self.api.get_order_by_cid(t["id"])
        if o is None:
            if int(time.time() * 1000) - t["created_ms"] > 60_000:
                self.drop_trade(t, "amarka BUY Pionex kuma jiro")
            return
        t["entry_order_id"] = o.get("orderId")
        if o.get("status") != "CLOSED":
            return                                            # wali ma dhammaan
        filled = D(o.get("filledSize"))
        if filled <= 0:
            self.drop_trade(t, "amarka BUY waxba ma fulin")
            return

        cost = D(o.get("filledAmount"))
        entry = cost / filled
        qty = filled
        if o.get("feeCoin") == info["base"]:
            qty -= D(o.get("fee"))
        try:                                                   # ha iibin wax ka badan inta aad hayso
            free_base = self.api.get_balances().get(info["base"], (Decimal(0), Decimal(0)))[0]
            if free_base > 0:
                qty = min(qty, free_base)
        except (PionexError, NetworkError):
            pass
        qty = floor_dec(qty, info["base_prec"])

        sl = D(t["sl"])
        t["entry_price"] = fmt(entry)
        t["entry_cost"] = fmt(cost)
        t["qty"] = fmt(qty)
        t["opened_ms"] = int(time.time() * 1000)
        t["status"] = "OPEN"

        if entry <= sl:
            t["tp"] = fmt(entry)                               # SL-ga hore ayaa la gaadhay
            t["pending_exit"] = "SL"
            self.save()
            notify(f"⚠️ {sym}: entry ({fmt(entry)}) wuxuu ka hooseeyaa SL ({t['sl']}) — si degdeg ah ayaa loo xidhayaa", "warning")
            return

        tp = entry + Decimal(str(RR)) * (entry - sl)
        t["tp"] = fmt(tp)
        self.save()
        notify(f"✅ {sym} FURAN | entry {fmt(entry)} | qty {fmt(qty)} | SL {t['sl']} | TP {t['tp']}")
        self.place_tp(t)

    # ---------- TP (amar LIMIT SELL oo Pionex ku jira) ----------
    def place_tp(self, t):
        now = time.time()
        if now - t.get("tp_last_try", 0) < TP_RETRY_COOLDOWN_SEC:
            return
        t["tp_last_try"] = now
        sym = t["symbol"]
        info = self.symbols[sym]
        qty = floor_dec(D(t["qty"]), info["base_prec"])
        price = ceil_dec(D(t["tp"]), info["quote_prec"])
        if qty <= 0 or qty < info["min_trade_size"]:
            t["tp_fail"] += 1
            if t["tp_fail"] in (1, 10):
                notify(f"⚠️ {sym}: qty ({fmt(qty)}) waa ka yar yahay minTradeSize — TP exchange lama dhigi karo; "
                       f"bot-ku ayaa TP/SL ku kormeerayaa", "warning")
            self.save()
            return
        cid = f"{t['id']}-tp{t['tp_seq']}"
        try:
            existing = self.api.get_order_by_cid(cid)          # ka hortag amar labo jeer la dhigo
            order_id = existing["orderId"] if existing else self.api.limit_sell(sym, fmt(qty), fmt(price), cid)["orderId"]
            o = self.api.get_order(order_id)
            if o and (o.get("status") == "OPEN" or D(o.get("filledSize")) > 0):
                t["tp_order_id"] = order_id
                t["tp_fail"] = 0
                self.save()
                log.info(f"{sym}: TP LIMIT SELL @ {fmt(price)} waa la aqbalay (orderId {order_id})")
                return
            raise PionexError("TP_NOT_ACCEPTED", f"status={o.get('status') if o else None}")
        except (PionexError, NetworkError) as e:
            t["tp_fail"] += 1
            if t["tp_fail"] in (1, 10) or t["tp_fail"] % 50 == 0:
                notify(f"⚠️ {sym}: amarka TP Pionex ma aqbalin ({e}). Bot-ku wuu isku dayayaa; "
                       f"ilaa markaas TP/SL waa lagu kormeerayaa software-ka.", "warning")
            self.save()

    # ---------- Kormeerka trades-ka ----------
    def monitor(self):
        active = [t for t in list(self.state["trades"].values()) if t["status"] in ("OPEN", "PENDING_ENTRY")]
        if not active:
            return
        prices = {}
        if any(t["status"] == "OPEN" for t in active):
            try:
                prices = self.api.get_tickers()
            except (PionexError, NetworkError) as e:
                log.warning(f"Qiimaha lama helin ({e}) — SL cusbooneysiin lama sameynin")
        for t in active:
            try:
                self.manage_trade(t, prices)
            except (PionexError, NetworkError) as e:
                log.warning(f"{t['symbol']}: khalad API ah markii la kormeerayay — {e}")
            except Exception:
                log.exception(f"{t['symbol']}: khalad lama filaan ah")

    def manage_trade(self, t, prices):
        if t["status"] == "PENDING_ENTRY":
            self.finalize_entry(t)
            return
        if t["status"] != "OPEN":
            return
        sym = t["symbol"]
        qty = D(t["qty"])

        # 1) Amar-ka TP ee Pionex — ma fulay mise waa la joojiyey?
        if t.get("tp_order_id"):
            o = self.api.get_order(t["tp_order_id"])
            if o is not None and o.get("status") == "CLOSED":
                filled = D(o.get("filledSize"))
                if filled >= qty * Decimal("0.99"):
                    proceeds = D(o.get("filledAmount"))
                    self.close_trade(t, "TP", proceeds / filled, proceeds)
                    return
            if o is None or o.get("status") == "CLOSED":
                notify(f"⚠️ {sym}: amarka TP Pionex kuma jiro (la joojiyey?) — waxaa la dhigayaa mid kale oo qiimo isku mid ah", "warning")
                t["tp_order_id"] = None
                t["tp_seq"] += 1
                self.save()

        # 2) Wax-ka-baxa dhexda ah (SL ama TP software) — sii wad ilaa uu dhammaado
        if t.get("pending_exit"):
            self.execute_market_exit(t)
            return

        price = prices.get(sym)
        if price is None:
            return

        # 3) SL — mudnaanta koowaad
        if price <= D(t["sl"]):
            notify(f"🛑 {sym}: qiimuhu ({fmt(price)}) wuxuu taabtay SL ({t['sl']})", "warning")
            t["pending_exit"] = "SL"
            self.save()
            self.execute_market_exit(t)
            return

        # 4) Haddii TP aanu Pionex ku jirin
        if not t.get("tp_order_id"):
            if self.sync_qty_with_balance(t):
                return
            if price >= D(t["tp"]):
                t["pending_exit"] = "TP_SOFT"
                self.save()
                self.execute_market_exit(t)
                return
            self.place_tp(t)

    def sync_qty_with_balance(self, t):
        """Hubi balance-ka dhabta ah. Haddii coin-ka la iibiyey (gacan), trade-ka xidh. Soo celiya True haddii la xidhay."""
        info = self.symbols[t["symbol"]]
        free, frozen = self.api.get_balances().get(info["base"], (Decimal(0), Decimal(0)))
        held = free + frozen
        if held < max(info["min_dump"], Decimal("1E-8")):
            self.close_trade(t, "EXTERNAL")
            return True
        if held < D(t["qty"]):
            t["qty"] = fmt(floor_dec(held, info["base_prec"]))
            self.save()
        return False

    def execute_market_exit(self, t):
        sym = t["symbol"]
        info = self.symbols[sym]
        reason = t["pending_exit"]
        qty = D(t["qty"])

        # Amarka bixitaanka ee hore (haddii uu jiro)
        if t.get("exit_order_id"):
            o = self.api.get_order(t["exit_order_id"])
            if o and o.get("status") == "OPEN":
                return
            if o and o.get("status") == "CLOSED" and D(o.get("filledSize")) > 0:
                proceeds = D(o.get("filledAmount"))
                self.close_trade(t, reason, proceeds / D(o.get("filledSize")), proceeds)
                return
            t["exit_order_id"] = None

        # TP-ga Pionex ka joojin (coin-ka waa la qaboojiyey)
        if t.get("tp_order_id"):
            try:
                self.api.cancel_order(sym, t["tp_order_id"])
            except PionexError as e:
                log.warning(f"{sym}: joojinta TP — {e}")
            o = self.api.get_order(t["tp_order_id"])
            if o and o.get("status") == "OPEN":
                notify(f"⚠️ {sym}: TP lama joojin karo — mar kale ayaa la isku dayayaa", "warning")
                return
            if o and D(o.get("filledSize")) >= qty * Decimal("0.99"):
                proceeds = D(o.get("filledAmount"))
                self.close_trade(t, "TP", proceeds / D(o.get("filledSize")), proceeds)
                return
            t["tp_order_id"] = None
            t["tp_seq"] += 1
            self.save()

        free, frozen = self.api.get_balances().get(info["base"], (Decimal(0), Decimal(0)))
        sell_qty = floor_dec(min(qty, free), info["base_prec"])
        if sell_qty <= 0 or sell_qty < info["min_dump"]:
            if free + frozen < max(info["min_dump"], Decimal("1E-8")):
                self.close_trade(t, "EXTERNAL")
            else:
                notify(f"⚠️ {sym}: qty-ga la iibin karo ({fmt(sell_qty)}) waa ka yar yahay minTradeDumping — gacan ku xidh", "warning")
            return

        cid = f"{t['id']}-x{t['exit_seq']}"
        t["exit_seq"] += 1
        self.save()
        try:
            r = self.api.market_sell(sym, fmt(sell_qty), cid)
        except PionexError as e:
            notify(f"🚨 {sym}: MARKET SELL waa la diiday ({e}) — mar kale ayaa la isku dayayaa", "error")
            return
        except NetworkError as e:
            log.warning(f"{sym}: MARKET SELL natiijadiisa lama hubo ({e})")
            o = self.api.get_order_by_cid(cid)
            if not o:
                return
            r = {"orderId": o.get("orderId")}
        t["exit_order_id"] = r.get("orderId")
        self.save()
        o = self.wait_order(t["exit_order_id"])
        if o and o.get("status") == "CLOSED" and D(o.get("filledSize")) > 0:
            proceeds = D(o.get("filledAmount"))
            self.close_trade(t, reason, proceeds / D(o.get("filledSize")), proceeds)

    # ---------- Scan-ka ----------
    def scan_and_trade(self):
        now_ms = int(time.time() * 1000)
        bucket = now_ms // INTERVAL_MS * INTERVAL_MS
        expected_last = bucket - INTERVAL_MS

        if LIVE_TRADING:
            self.monitor()                                     # xaaladda dhabta ah ka hor scan-ka
            if self.open_count() >= MAX_OPEN_TRADES:
                log.info(f"Scan: {self.open_count()}/{MAX_OPEN_TRADES} trades furan — trade cusub lama furi karo")
                return

        busy = {t["symbol"] for t in self.state["trades"].values()}
        candidates, stats = [], {"no_signal": 0, "stale": 0, "error": 0}
        for sym in list(self.watchlist):
            if sym in busy:
                continue
            try:
                closed = [k for k in self.api.get_klines(sym) if k["time"] < bucket]
                if not closed or closed[-1]["time"] != expected_last:
                    stats["stale"] += 1
                    continue
                sig, reason = evaluate_signal(closed)
                if sig:
                    candidates.append((sym, sig))
                else:
                    stats["no_signal"] += 1
                    log.debug(f"{sym}: {reason}")
            except (PionexError, NetworkError) as e:
                if isinstance(e, PionexError) and "INVALID_SYMBOL" in str(e.code):
                    self.watchlist.remove(sym)                 # klines ma bixiyo — mar uun ogeysii, kaddib ka saar
                    log.warning(f"{sym}: Pionex wuxuu leeyahay symbol khaldan ({e.code}) — waa laga saaray watchlist-ka")
                    continue
                stats["error"] += 1
                log.warning(f"{sym}: scan khalad — {e}")

        log.info(f"Scan {time.strftime('%H:%M:%S')} | signals: {len(candidates)} | "
                 f"ma jiro: {stats['no_signal']} | xog duug: {stats['stale']} | khalad: {stats['error']}")
        if not candidates:
            return

        # Doorashada fursadda ugu habboon: trend-ka ugu xooggan, kaddib jirka shumaca
        candidates.sort(key=lambda x: (x[1]["slope_pct"], x[1]["body_ratio"]), reverse=True)

        if not LIVE_TRADING:
            for sym, sig in candidates:
                log.info(f"[SCAN-ONLY] SIGNAL {sym} | entry≈{sig['entry_ref']:.8g} SL≈{sig['sl']:.8g} "
                         f"TP≈{sig['entry_ref'] + RR * (sig['entry_ref'] - sig['sl']):.8g} | risk {sig['risk_pct']:.2f}%")
            return

        try:
            usdt_free = self.api.get_balances().get("USDT", (Decimal(0), Decimal(0)))[0]
            prices = self.api.get_tickers()
        except (PionexError, NetworkError) as e:
            log.warning(f"Balance/qiimo lama helin ({e}) — trade lama furin")
            return
        if usdt_free < TRADE_USDT:
            log.info(f"USDT ma ku filna ({usdt_free} < {TRADE_USDT}) — trade lama furin")
            return

        for sym, sig in candidates:                            # hal trade oo keliya
            mem = self.state["memory"].get(sym, {})
            if sig["time"] <= mem.get("last_signal_time", 0):
                continue                                       # signal-kan hore ayaa loo isticmaalay
            if sig["time"] + INTERVAL_MS <= mem.get("last_closed_ms", 0):
                continue                                       # waa signal hore (ka hor xidhitaankii)
            info = self.symbols[sym]
            if TRADE_USDT < info["min_amount"]:
                log.info(f"{sym}: {TRADE_USDT} USDT waa ka yar yahay minAmount ({info['min_amount']})")
                continue
            price, sl = prices.get(sym), D(repr(sig["sl"]))
            if price is None or price <= sl:
                log.info(f"{sym}: qiimaha hadda SL ayuu hoos marayaa — waa la ka tegay")
                continue
            if float((price - sl) / price * 100) > MAX_RISK_PCT:
                log.info(f"{sym}: khatarta qiimaha hadda waa ka badan tahay {MAX_RISK_PCT}% — waa la ka tegay")
                continue
            self.open_trade(sym, sig)
            break


# =====================================================================
# 8) LOOP-KA UGU WEYN
# =====================================================================
def next_scan_time():
    now = time.time()
    return (int(now) // 300 + 1) * 300 + SCAN_DELAY_SEC


def main():
    bot = Bot()
    bot.startup()
    next_scan = next_scan_time()                               # scan-ka ugu horreeya = shumaca xiga
    log.info(f"Scan-ka xiga: {time.strftime('%H:%M:%S', time.localtime(next_scan))}")
    try:
        while True:
            try:
                if time.time() >= next_scan:
                    bot.scan_and_trade()
                    next_scan = next_scan_time()
                elif LIVE_TRADING:
                    bot.monitor()
            except Exception:
                log.exception("Khalad loop-ka ah (bot-ku wuu sii socdaa)")
            time.sleep(MONITOR_INTERVAL_SEC)
    except KeyboardInterrupt:
        notify(f"⛔ Bot-ku waa joogsaday. Trades furan: {bot.open_count()}. "
               f"TP-yada waxay ku sugan yihiin Pionex, laakiin SL lama kormeerayo ilaa bot-ku soo noqdo!", "warning")


if __name__ == "__main__":
    main()
