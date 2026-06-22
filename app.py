# -*- coding: utf-8 -*-
"""
MEXC single-pair webhook bot (BTC/USDT) met Telegram alerts.
- Paar: BTC/USDT
- Budget via ENV: BUDGET_BTC_USDT (default 500 USDT)
- SIMULATE modus (default aan, dus GEEN echte orders)
- Optionele REHYDRATE_ENABLED (default uit, raakt je bestaande holdings niet)
- Dedup, per-symbol state, inflight guard
- Per-candle lock: max 1 BUY/SELL per 5m-bar
- Virtuele wallet: trade_usd + savings_usd + realized_pnl_usd met auditregels
- Optionele bot-filters op TV payload: EMA200/EMA50/VWAP/RSI/RSI_MA
- Optionele bot TP/SL/trailing monitor via MEXC ticker
- Endpoints: /, /health, /config, /envcheck, /test/send, /webhook
"""

import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from threading import Lock, Thread
from typing import Dict, Any, Tuple, List

import requests
from flask import Flask, request, jsonify
import ccxt

# ------------- helpers -------------

def normalize_tf(tf: str) -> str:
    """
    Normaliseer TV interval naar: 1m/3m/5m/15m/30m/45m, 1h/2h/4h/6h/8h/12h,
    1d, 1w, 1M. Accepteert ook '1','3','5','60','240','D','W','M', etc.
    """
    if tf is None:
        return ""
    s = str(tf).strip()
    if not s:
        return ""
    u = s.upper()

    # pure getal = minuten of uren
    if u.isdigit():
        n = int(u)
        if n < 60:
            return f"{n}m"
        else:
            h = n // 60
            return f"{h}h"

    # veelvoorkomende korte codes
    if u in ("D", "1D"):
        return "1d"
    if u in ("W", "1W"):
        return "1w"
    if u in ("M", "1M", "1MO"):
        return "1M"

    # reeds in notatie als '1m','5m','1h','1d','1w','1M'
    ss = s.strip()
    if ss.lower().endswith(("m", "h", "d", "w")):
        return ss.lower()
    if ss.lower().endswith("mo"):
        return "1M"
    return ss


def parse_symbol(tv_symbol: str) -> str:
    """
    Zet TV symbol (XCDUSDT of BTC/USDT) om naar ccxt-notatie 'BTC/USDT'.
    """
    s = (tv_symbol or "").upper().strip()
    s = s.replace(" ", "")
    if not s:
        return ""
    s = s.replace("/", "")
    # Verwacht BTCUSDT -> BTC/USDT
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT"
    return s


def sym_label(symbol: str) -> str:
    """
    Label voor TG (bv. BTC).
    """
    return symbol.split("/")[0].upper()


def fmt_usd(val: float, decimals: int = 2) -> str:
    """
    Format USD met $ sign.
    """
    return f"${val:,.{decimals}f}"


def fmt_eur(val: float, decimals: int = 2) -> str:
    """
    Format EUR met € sign.
    """
    return f"€{val:,.{decimals}f}"


def fmt_dt(dt: datetime) -> str:
    """
    Format datetime als 'DD-MM HH:MM'.
    """
    return dt.strftime("%d-%m %H:%M")


def local_now() -> datetime:
    """
    Local timezone now.
    """
    tz = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
    return datetime.now(tz)


def eur_rate() -> float:
    """
    EUR/USD rate from ENV (default 0.92).
    """
    return float(os.getenv("USD_TO_EUR", "0.92"))


def env_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(key: str, default: str) -> float:
    try:
        return float(os.getenv(key, default))
    except Exception:
        return float(default)


def payload_float(payload: Dict[str, Any], *keys: str) -> float | None:
    """
    Haal een indicatorwaarde uit TradingView JSON. Accepteert meerdere key-namen.
    Voorbeelden: rsi, rsi_ma, ema50, ema200, vwap.
    """
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            try:
                return float(payload.get(key))
            except Exception:
                return None
    return None


def ema_series(values: List[float], length: int) -> List[float]:
    if not values or length <= 0:
        return []
    k = 2.0 / (length + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1.0 - k))
    return out


def rsi_last(closes: List[float], length: int = 14) -> float | None:
    if len(closes) <= length + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_last(highs: List[float], lows: List[float], closes: List[float], length: int = 14) -> float | None:
    if len(closes) <= length + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < length:
        return None
    atr = sum(trs[:length]) / length
    for tr in trs[length:]:
        atr = (atr * (length - 1) + tr) / length
    return atr


def adx_last(highs: List[float], lows: List[float], closes: List[float], length: int = 14) -> float | None:
    if len(closes) <= length * 2 + 2:
        return None
    trs = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr = sum(trs[:length])
    pdm = sum(plus_dm[:length])
    mdm = sum(minus_dm[:length])
    dxs = []
    for i in range(length, len(trs)):
        atr = atr - (atr / length) + trs[i]
        pdm = pdm - (pdm / length) + plus_dm[i]
        mdm = mdm - (mdm / length) + minus_dm[i]
        if atr <= 0:
            continue
        pdi = 100.0 * (pdm / atr)
        mdi = 100.0 * (mdm / atr)
        denom = pdi + mdi
        if denom > 0:
            dxs.append(100.0 * abs(pdi - mdi) / denom)
    if len(dxs) < length:
        return None
    adx = sum(dxs[:length]) / length
    for dx in dxs[length:]:
        adx = (adx * (length - 1) + dx) / length
    return adx


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# ------------- wallet helpers -------------

def trade_and_savings_usd(symbol: str) -> tuple[float, float]:
    """
    Haal actuele handels- en spaar-saldo uit STATE.
    Als er nog niks staat, bereken uit BUDGET_USDT + SPAREN_*.
    """
    st = STATE.get(symbol, {})

    trade = float(st.get("trade_usd", 0.0))
    savings = float(st.get("savings_usd", 0.0))

    if trade == 0 and savings == 0:
        total = float(BUDGET_USDT.get(symbol, 0.0))
        if SPAREN_ENABLED:
            target_trade = total * (1 - SPAREN_SPLIT_PCT / 100.0)
            savings = total - target_trade
        else:
            target_trade = total
            savings = 0.0
        trade = target_trade
        st["target_trade_usd"] = target_trade
        st["trade_usd"] = trade
        st["savings_usd"] = savings
        STATE[symbol] = st
        _save_state_file()

    return trade, savings


def _ensure_wallet(symbol: str):
    """
    Zorg dat target_trade_usd / trade_usd / savings_usd in STATE staan.
    """
    st = STATE.setdefault(symbol, {})
    if "target_trade_usd" in st and "trade_usd" in st and "savings_usd" in st:
        return

    total = float(BUDGET_USDT.get(symbol, 0.0))
    if SPAREN_ENABLED:
        target_trade = total * (1 - SPAREN_SPLIT_PCT / 100.0)
        savings = total - target_trade
    else:
        target_trade = total
        savings = 0.0

    st.setdefault("target_trade_usd", target_trade)
    st.setdefault("trade_usd", target_trade)
    st.setdefault("savings_usd", savings)
    st.setdefault("realized_pnl_usd", 0.0)
    STATE[symbol] = st


def tg_buy_msg(symbol: str, price_usd: float, qty: float, invested_usd: float) -> str:
    """
    TG message voor BUY.
    """
    trade_usd, savings_usd = trade_and_savings_usd(symbol)
    trade_eur = eur_rate() * invested_usd
    now_str = fmt_dt(local_now())
    sym_ccxt = sym_label(symbol)
    lines = [
        f"{BOT_TITLE}",
        f"🟢 [{sym_ccxt}] AANKOOP",
        f"💰 Investering: {fmt_eur(trade_eur)}",
        f"📈 Aankoopprijs: {fmt_usd(price_usd, 4)}",
        f"📊 Hoeveelheid: {qty:,.4f}",
        f"🔗 Tijd: {now_str}",
        f"🧪 Modus: {'PAPER' if SIMULATE else 'LIVE'}",
    ]
    return "\n".join(lines)


def tg_sell_msg(symbol: str, price_usd: float, qty: float, net_out_usd: float, pnl_usd: float,
                prev_trade_usd: float | None = None, prev_savings_usd: float | None = None) -> str:
    """
    TG message voor SELL.
    Toont actuele virtuele handels- en spaar-saldo plus auditregel.
    """
    trade_usd, savings_usd = trade_and_savings_usd(symbol)
    trade_eur = eur_rate() * trade_usd
    savings_eur = eur_rate() * savings_usd
    total_eur = trade_eur + savings_eur

    now_str = fmt_dt(local_now())
    sym_ccxt = sym_label(symbol)
    pnl_eur = eur_rate() * pnl_usd
    realized_eur = eur_rate() * float(STATE.get(symbol, {}).get("realized_pnl_usd", 0.0))
    winlose = "Winst" if pnl_eur >= 0 else "Verlies"
    lines = [
        f"{BOT_TITLE}",
        f"📄 [{sym_ccxt}] VERKOOP",
        f"📹 Verkoopprijs: {fmt_usd(price_usd, 4)}",
        f"📈 {winlose}: {fmt_eur(pnl_eur)}",
        f"💰 Handelssaldo: {fmt_eur(trade_eur)}",
        f"💼 Spaarrekening: {fmt_eur(savings_eur)}",
        f"📈 Totale waarde: {fmt_eur(total_eur)}",
        f"📊 Cumulatieve PnL: {fmt_eur(realized_eur)}",
        f"🔐 Tradebedrag: {fmt_eur(trade_eur)}",
    ]
    if prev_trade_usd is not None and prev_savings_usd is not None:
        lines.append(
            f"🧾 Wallet: {fmt_eur(eur_rate() * prev_trade_usd)} + {fmt_eur(eur_rate() * prev_savings_usd)} "
            f"{fmt_eur(pnl_eur)} → {fmt_eur(total_eur)}"
        )
    lines.extend([
        f"🔗 Tijd: {now_str}",
        f"🧪 Modus: {'PAPER' if SIMULATE else 'LIVE'}",
    ])
    return "\n".join(lines)


def send_tg(msg: str):
    """
    Stuur Telegram bericht.
    """
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
            requests.post(url, json=data, timeout=5)
            _dbg("[TG] Message sent")
        else:
            _dbg("[TG] No token/chat_id - skipped")
    except Exception as e:
        _dbg(f"[TG] Send error: {e}")


# ------------- ENV / CONSTS -------------

app = Flask(__name__)

PORT = int(os.getenv("PORT", "10000"))
BOT_TITLE = os.getenv("BOT_TITLE", "Scalp Bot")

# ✅ Symbol uit ENV (fallback naar BTC/USDT)
SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
SYMBOLS = [SYMBOL]

# ✅ Budget-key afleiden van het symbool, bv:
# SYMBOL=BTC/USDT  ->  BUDGET_BTC_USDT
_symbol_key = SYMBOL.replace("/", "_").upper()         # BTC/USDT -> BTC_USDT
_budget_env_key = f"BUDGET_{_symbol_key}"              # -> BUDGET_BTC_USDT

BUDGET_USDT = {
    SYMBOL: float(os.getenv(_budget_env_key, os.getenv("BUDGET_BTC_USDT", "500")))
}


MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
MEXC_RECVWINDOW_MS = int(os.getenv("MEXC_RECVWINDOW_MS", "10000"))
CCXT_TIMEOUT_MS = int(os.getenv("CCXT_TIMEOUT_MS", "7000"))

STRICT_DEDUP_S = float(os.getenv("STRICT_DEDUP_S", "3"))
DEDUP_WINDOW_S = float(os.getenv("DEDUP_WINDOW_S", "20"))
ENTRY_LOCK_S = float(os.getenv("ENTRY_LOCK_S", "2"))
MIN_TRADE_COOLDOWN_S = float(os.getenv("MIN_TRADE_COOLDOWN_S", "0"))

PER_BAR_LOCK = os.getenv("PER_BAR_LOCK", "false").lower() == "true"
PER_BAR_LOCK_BUY = os.getenv("PER_BAR_LOCK_BUY", "true").lower() == "true"
PER_BAR_LOCK_SELL = os.getenv("PER_BAR_LOCK_SELL", "false").lower() == "true"

SPAREN_ENABLED = os.getenv("SPAREN_ENABLED", "true").lower() == "true"
SPAREN_SPLIT_PCT = float(os.getenv("SPAREN_SPLIT_PCT", "100"))

# ------------- optionele bot-filters op TV payload -------------
# Let op: deze filters werken alleen als TradingView de waarden meestuurt in de alert JSON.
BOT_FILTER_ENABLED = env_bool("BOT_FILTER_ENABLED", "false")
BOT_FILTER_MISSING = os.getenv("BOT_FILTER_MISSING", "open").strip().lower()  # open of closed
BUY_REQUIRE_CLOSE_ABOVE_EMA200 = env_bool("BUY_REQUIRE_CLOSE_ABOVE_EMA200", "true")
BUY_REQUIRE_EMA50_ABOVE_EMA200 = env_bool("BUY_REQUIRE_EMA50_ABOVE_EMA200", "true")
BUY_REQUIRE_CLOSE_ABOVE_VWAP = env_bool("BUY_REQUIRE_CLOSE_ABOVE_VWAP", "false")
BUY_REQUIRE_RSI_ABOVE_RSI_MA = env_bool("BUY_REQUIRE_RSI_ABOVE_RSI_MA", "true")
BUY_RSI_MAX = env_float("BUY_RSI_MAX", "72")

# ------------- optionele bot TP/SL/trailing monitor -------------
BOT_TPSL_ENABLED = env_bool("BOT_TPSL_ENABLED", "false")
TPSL_POLL_S = env_float("TPSL_POLL_S", "15")
HARD_SL_PCT = env_float("HARD_SL_PCT", "0.90")          # verkoop bij -0.90%
BE_TRIGGER_PCT = env_float("BE_TRIGGER_PCT", "0.60")    # break-even actief vanaf +0.60%
BE_OFFSET_PCT = env_float("BE_OFFSET_PCT", "0.05")      # BE stop op entry +0.05%
TRAIL_TRIGGER_PCT = env_float("TRAIL_TRIGGER_PCT", "1.00")
TRAIL_DISTANCE_PCT = env_float("TRAIL_DISTANCE_PCT", "0.45")

# ------------- Supervisor v1: guard bovenop TradingView BUY-signalen -------------
SUPERVISOR_ENABLED = env_bool("SUPERVISOR_ENABLED", "false")
SUPERVISOR_FAIL_OPEN = env_bool("SUPERVISOR_FAIL_OPEN", "true")
SUPERVISOR_NOTIFY = env_bool("SUPERVISOR_NOTIFY", "true")
SUPERVISOR_TF = os.getenv("SUPERVISOR_TF", os.getenv("ADVISOR_TF", "10m"))
SUPERVISOR_OHLCV_LIMIT = int(os.getenv("SUPERVISOR_OHLCV_LIMIT", os.getenv("ADVISOR_OHLCV_LIMIT", "260")))
SUPERVISOR_MIN_SCORE = env_float("SUPERVISOR_MIN_SCORE", "0.55")
SUPERVISOR_DYNAMIC_SIZE = env_bool("SUPERVISOR_DYNAMIC_SIZE", "true")
SUPERVISOR_MIN_SIZE_FACTOR = env_float("SUPERVISOR_MIN_SIZE_FACTOR", "0.35")
SUPERVISOR_REQUIRE_PRICE_ABOVE_EMA200 = env_bool("SUPERVISOR_REQUIRE_PRICE_ABOVE_EMA200", "false")
SUPERVISOR_REQUIRE_EMA50_ABOVE_EMA200 = env_bool("SUPERVISOR_REQUIRE_EMA50_ABOVE_EMA200", "false")
SUPERVISOR_MIN_ADX = env_float("SUPERVISOR_MIN_ADX", os.getenv("MIN_ADX", "18"))
SUPERVISOR_RSI_MIN_LONG = env_float("SUPERVISOR_RSI_MIN_LONG", "50")
SUPERVISOR_RSI_MAX_LONG = env_float("SUPERVISOR_RSI_MAX_LONG", "74")
SUPERVISOR_MIN_ATR_PCT = env_float("SUPERVISOR_MIN_ATR_PCT", "0.05")
SUPERVISOR_MAX_ATR_PCT = env_float("SUPERVISOR_MAX_ATR_PCT", "1.20")
SUPERVISOR_MIN_VOLUME_FACTOR = env_float("SUPERVISOR_MIN_VOLUME_FACTOR", "0.75")

# ✅ Symbol uit ENV (fallback BTC/USDT)
SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
SYMBOLS = [SYMBOL]

# ✅ Automatisch state-bestand op basis van SYMBOL
sym_key = SYMBOL.replace("/", "_").lower()        # "BTC/USDT" -> "btc_usdt"
default_state_name = f"bot_state_{sym_key}.json" # -> "bot_state_btc_usdt.json"

STATE_FILE = Path(os.getenv("STATE_FILE", default_state_name))

# ✅ flags
SIMULATE = os.getenv("SIMULATE", "true").lower() == "true"          # default PAPER
REHYDRATE_ENABLED = os.getenv("REHYDRATE_ENABLED", "false").lower() == "true"

TRADE_LOG: List[Dict[str, Any]] = []
STATE: Dict[str, Dict[str, Any]] = {}

# ccxt exchange client
ex = None

lock = Lock()


def _dbg(msg: str):
    """
    Debug log met timestamp.
    """
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}")


def _load_state_file():
    """
    State laden uit JSON (en zorgen dat BTC/USDT key + wallet bestaat).
    """
    global STATE
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                STATE = json.load(f)
            _dbg(f"[STATE] Loaded {len(STATE)} symbols from file")
        except Exception as e:
            _dbg(f"[STATE] Load error: {e}")
            STATE = {}
    else:
        STATE = {}

    # Zorg dat onze enige symbol altijd aanwezig is
    if SYMBOL not in STATE:
        STATE[SYMBOL] = {
            "in_position": False,
            "inflight": False,
            "last_action_ts": 0,
            "last_bar_time": 0,
            "entry_price": 0,
            "qty": 0,
            "invested_usd": 0,
        }

    _ensure_wallet(SYMBOL)
    _save_state_file()


def _save_state_file():
    """
    State bewaren naar JSON.
    """
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        _dbg(f"[STATE] Save error: {e}")


def allowed_tfs_for(symbol: str) -> set:
    """
    Haal toegestane TFs voor symbol uit ENV.
    Env key:  ALLOW_TF_BTC_USDT
    """
    key = f"ALLOW_TF_{symbol.replace('/', '_').upper()}"
    tfs_str = os.getenv(key, "1m")
    return set(tfs_str.split(","))


def init_exchange():
    """
    Init MEXC client. In SIMULATE gebruiken we alleen publieke ticker-data
    zodat de TP/SL/trailing monitor ook in PAPER kan werken.
    """
    global ex
    params = {
        "sandbox": False,
        "enableRateLimit": True,
        "options": {"recvWindow": MEXC_RECVWINDOW_MS},
        "timeout": CCXT_TIMEOUT_MS,
    }
    if not SIMULATE:
        params["apiKey"] = MEXC_API_KEY
        params["secret"] = MEXC_API_SECRET

    ex = ccxt.mexc(params)
    ex.load_markets()
    if SIMULATE:
        _dbg("[WARMUP] SIMULATE mode: public MEXC client ready for ticker/TP-SL only")
    else:
        _dbg("[WARMUP] MEXC client ready")


def rehydrate_positions():
    """
    Rehydrate positions uit balances op startup (alleen als REHYDRATE_ENABLED true).
    """
    global STATE
    if not REHYDRATE_ENABLED:
        _dbg("[REHYDRATE] disabled via REHYDRATE_ENABLED=false")
        return

    if SIMULATE:
        _dbg("[REHYDRATE] skip in SIMULATE mode")
        return

    if not MEXC_API_KEY or not MEXC_API_SECRET:
        _dbg("[REHYDRATE] Skip: No API keys set")
        return

    if ex is None:
        _dbg("[REHYDRATE] Skip: exchange not initialised")
        return

    try:
        _dbg(f"[REHYDRATE] Fetching balances for {SYMBOLS}")
        balances = ex.fetch_balance()
        _dbg(f"[REHYDRATE] Balances fetched: {len(balances['free'])} assets")

        for symbol in SYMBOLS:
            if symbol not in STATE:
                STATE[symbol] = {
                    "in_position": False,
                    "inflight": False,
                    "last_action_ts": 0,
                    "last_bar_time": 0,
                    "entry_price": 0,
                    "qty": 0,
                    "invested_usd": 0,
                }
                _ensure_wallet(symbol)

            base = symbol.replace("/USDT", "")
            free_base = balances["free"].get(base, 0)
            _dbg(f"[REHYDRATE] {symbol} free base: {free_base}")

            if free_base > 0.001:  # Threshold
                STATE[symbol]["in_position"] = True
                STATE[symbol]["entry_price"] = 0  # Onbekend
                STATE[symbol]["qty"] = free_base
                STATE[symbol]["invested_usd"] = 0
                _dbg(f"[REHYDRATE] {symbol} in position (free {free_base}) [entry unknown]")
            else:
                STATE[symbol]["in_position"] = False
                STATE[symbol]["qty"] = 0
                STATE[symbol]["invested_usd"] = 0
    except Exception as e:
        _dbg(f"[REHYDRATE] Fetch error: {e}")


def supervisor_size_factor(score: float) -> float:
    if not SUPERVISOR_DYNAMIC_SIZE:
        return 1.0
    if score >= 0.75:
        return 1.0
    if score >= 0.65:
        return 0.70
    return SUPERVISOR_MIN_SIZE_FACTOR


def supervisor_decision(action: str, symbol: str, price: float, payload: Dict[str, Any]) -> tuple[bool, str, float, float]:
    """
    Supervisor v1: alleen BUY bewaken. SELL wordt altijd doorgelaten.
    Return: allow, reason, score, size_factor.
    """
    if not SUPERVISOR_ENABLED or action != "buy":
        return True, "supervisor_disabled_or_not_buy", 1.0, 1.0

    try:
        if ex is None:
            raise RuntimeError("exchange_client_not_ready")

        tf = normalize_tf(SUPERVISOR_TF) or "10m"
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=tf, limit=max(80, SUPERVISOR_OHLCV_LIMIT))
        if not ohlcv or len(ohlcv) < 80:
            raise RuntimeError(f"not_enough_ohlcv:{len(ohlcv) if ohlcv else 0}")

        highs = [float(x[2]) for x in ohlcv]
        lows = [float(x[3]) for x in ohlcv]
        closes = [float(x[4]) for x in ohlcv]
        volumes = [float(x[5]) for x in ohlcv]
        live_close = float(closes[-1])
        check_price = price if price > 0 else live_close

        ema50_s = ema_series(closes, 50)
        ema200_s = ema_series(closes, 200)
        ema50 = ema50_s[-1] if len(ema50_s) else live_close
        ema200 = ema200_s[-1] if len(ema200_s) else live_close
        ema200_old = ema200_s[-6] if len(ema200_s) > 6 else ema200
        ema200_up = ema200 > ema200_old
        rsi = rsi_last(closes, 14) or 50.0
        atr = atr_last(highs, lows, closes, 14) or 0.0
        atr_pct = (atr / live_close * 100.0) if live_close > 0 else 0.0
        adx = adx_last(highs, lows, closes, 14) or 0.0
        vol_ma = sum(volumes[-21:-1]) / 20.0 if len(volumes) >= 21 else max(volumes[-1], 1.0)
        vol_factor = volumes[-1] / vol_ma if vol_ma > 0 else 1.0

        checks = []
        hard_blocks = []
        score = 0.0

        if check_price > ema200:
            score += 0.18
            checks.append("price>ema200")
        elif SUPERVISOR_REQUIRE_PRICE_ABOVE_EMA200:
            hard_blocks.append("price_below_ema200")

        if ema50 > ema200:
            score += 0.16
            checks.append("ema50>ema200")
        elif SUPERVISOR_REQUIRE_EMA50_ABOVE_EMA200:
            hard_blocks.append("ema50_below_ema200")

        if ema200_up:
            score += 0.12
            checks.append("ema200_up")

        if SUPERVISOR_RSI_MIN_LONG <= rsi <= SUPERVISOR_RSI_MAX_LONG:
            score += 0.16
            checks.append("rsi_ok")
        elif rsi > SUPERVISOR_RSI_MAX_LONG:
            hard_blocks.append("rsi_too_high")
        else:
            hard_blocks.append("rsi_too_low")

        if adx >= SUPERVISOR_MIN_ADX:
            score += 0.12
            checks.append("adx_ok")

        if SUPERVISOR_MIN_ATR_PCT <= atr_pct <= SUPERVISOR_MAX_ATR_PCT:
            score += 0.10
            checks.append("atr_ok")
        elif atr_pct > SUPERVISOR_MAX_ATR_PCT:
            hard_blocks.append("atr_too_high")

        if vol_factor >= SUPERVISOR_MIN_VOLUME_FACTOR:
            score += 0.08
            checks.append("volume_ok")

        # Bonus voor signaal dichtbij actuele prijs, voorkomt oude/verkeerde alerts.
        dev_pct = abs(check_price / live_close - 1.0) * 100.0 if live_close > 0 else 0.0
        if dev_pct <= float(os.getenv("MAX_ALERT_PRICE_DEVIATION_PCT", "0.15")):
            score += 0.08
            checks.append("price_fresh")

        score = clamp(score, 0.0, 1.0)
        size = supervisor_size_factor(score)
        allow = score >= SUPERVISOR_MIN_SCORE and not hard_blocks
        reason = f"score={score:.2f} size={size:.2f} checks={','.join(checks) or '-'} blocks={','.join(hard_blocks) or '-'}"

        if SUPERVISOR_NOTIFY:
            decision = "ALLOW" if allow else "BLOCK"
            send_tg(
                "🤖 Supervisor " + decision + "\n" +
                f"Signaal: BUY {sym_label(symbol)}\n" +
                f"Score: {score:.2f} | Size: {size:.0%}\n" +
                f"Prijs: {fmt_usd(check_price, 2)}\n" +
                f"RSI: {rsi:.1f} | ADX: {adx:.1f} | ATR%: {atr_pct:.2f} | Vol: {vol_factor:.2f}x\n" +
                f"Reden: {reason}"
            )

        return allow, reason, score, size

    except Exception as e:
        reason = f"supervisor_error:{e}"
        _dbg(f"[SUPERVISOR] {reason}")
        if SUPERVISOR_NOTIFY:
            send_tg(f"🤖 Supervisor {'ALLOW' if SUPERVISOR_FAIL_OPEN else 'BLOCK'}\nReden: {reason}")
        return SUPERVISOR_FAIL_OPEN, reason, 0.0, 1.0


def bot_filter_decision(action: str, price: float, payload: Dict[str, Any]) -> tuple[bool, str]:
    """
    Extra bot-side filter. Werkt alleen als TV indicatorwaarden meestuurt.
    Bij BOT_FILTER_MISSING=open worden ontbrekende waarden niet geblokkeerd.
    """
    if not BOT_FILTER_ENABLED or action != "buy":
        return True, "filter_disabled_or_not_buy"

    missing_policy_closed = BOT_FILTER_MISSING == "closed"
    checks: list[tuple[bool | None, str]] = []

    close = payload_float(payload, "close", "price") or price
    ema200 = payload_float(payload, "ema200", "ema_200", "ma200")
    ema50 = payload_float(payload, "ema50", "ema_50", "ma50")
    vwap = payload_float(payload, "vwap")
    rsi = payload_float(payload, "rsi")
    rsi_ma = payload_float(payload, "rsi_ma", "rsima", "rsiMA")

    def check_available(condition: bool | None, name: str):
        checks.append((condition, name))

    if BUY_REQUIRE_CLOSE_ABOVE_EMA200:
        check_available(None if ema200 is None else close > ema200, "close>ema200")
    if BUY_REQUIRE_EMA50_ABOVE_EMA200:
        check_available(None if ema50 is None or ema200 is None else ema50 > ema200, "ema50>ema200")
    if BUY_REQUIRE_CLOSE_ABOVE_VWAP:
        check_available(None if vwap is None else close > vwap, "close>vwap")
    if BUY_REQUIRE_RSI_ABOVE_RSI_MA:
        check_available(None if rsi is None or rsi_ma is None else rsi > rsi_ma, "rsi>rsi_ma")
    if BUY_RSI_MAX > 0:
        check_available(None if rsi is None else rsi <= BUY_RSI_MAX, f"rsi<={BUY_RSI_MAX:g}")

    failed = [name for ok, name in checks if ok is False]
    missing = [name for ok, name in checks if ok is None]

    if failed:
        return False, "failed:" + ",".join(failed)
    if missing and missing_policy_closed:
        return False, "missing:" + ",".join(missing)
    if missing:
        return True, "pass_missing_open:" + ",".join(missing)
    return True, "pass"


def update_trade_protection_state(symbol: str, price: float):
    """
    Houd highest_price, break-even en trailing stop in STATE bij.
    Dit verandert niets aan de TradingView strategie; dit is bot-risk-management.
    """
    st = STATE.get(symbol, {})
    if not st.get("in_position", False):
        return
    entry = float(st.get("entry_price", 0.0))
    if entry <= 0 or price <= 0:
        return

    highest = max(float(st.get("highest_price", entry)), price)
    st["highest_price"] = highest
    profit_pct = pct_change(entry, price)

    stop_candidates = []
    hard_sl_price = entry * (1.0 - HARD_SL_PCT / 100.0)
    stop_candidates.append((hard_sl_price, "hard_sl"))

    if profit_pct >= BE_TRIGGER_PCT or st.get("be_armed", False):
        st["be_armed"] = True
        be_price = entry * (1.0 + BE_OFFSET_PCT / 100.0)
        stop_candidates.append((be_price, "break_even"))

    if pct_change(entry, highest) >= TRAIL_TRIGGER_PCT or st.get("trail_armed", False):
        st["trail_armed"] = True
        trail_price = highest * (1.0 - TRAIL_DISTANCE_PCT / 100.0)
        stop_candidates.append((trail_price, "trailing"))

    active_stop, reason = max(stop_candidates, key=lambda x: x[0])
    st["active_stop_price"] = active_stop
    st["active_stop_reason"] = reason


def _tpsl_monitor_loop():
    """
    Optionele actieve monitor. Als BOT_TPSL_ENABLED=true, kan de bot zelf verkopen
    op hard SL, break-even of trailing stop, ook zonder SELL alert uit TradingView.
    """
    while True:
        try:
            time.sleep(max(5.0, TPSL_POLL_S))
            if not BOT_TPSL_ENABLED:
                continue
            if ex is None:
                continue
            symbol = SYMBOL
            st = STATE.get(symbol, {})
            if not st.get("in_position", False) or st.get("inflight", False):
                continue
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker.get("last") or 0.0)
            if price <= 0:
                continue

            update_trade_protection_state(symbol, price)
            stop_price = float(st.get("active_stop_price", 0.0))
            reason = st.get("active_stop_reason", "")
            if stop_price > 0 and price <= stop_price:
                _dbg(f"[TPSL] trigger {symbol} reason={reason} price={price} stop={stop_price}")
                _market_sell_all(symbol, price, source=f"bot_tpsl_{reason}", tf="bot")
        except Exception as e:
            _dbg(f"[TPSL] loop error: {e}")
            time.sleep(10)


def _daily_report_loop():
    """
    Dagelijkse report-thread (placeholder).
    """
    while True:
        try:
            hhmm = os.getenv("DAILY_REPORT_HHMM", "23:59")
            hh, mm = map(int, hhmm.split(":"))
            now = local_now()
            next_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now > next_run:
                next_run += timedelta(days=1)
            sleep_s = (next_run - now).total_seconds()
            time.sleep(sleep_s)
            # Hier kun je later /report logic toevoegen
            _dbg("[REPORT] Daily report tick")
        except Exception as e:
            _dbg(f"[REPORT] Loop error: {e}")
            time.sleep(3600)


# ------------- ENDPOINTS -------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "symbols": SYMBOLS,
        "simulate": SIMULATE,
        "rehydrate_enabled": REHYDRATE_ENABLED,
        "bot_filter_enabled": BOT_FILTER_ENABLED,
        "bot_filter_missing": BOT_FILTER_MISSING,
        "bot_tpsl_enabled": BOT_TPSL_ENABLED,
        "supervisor_enabled": SUPERVISOR_ENABLED,
        "supervisor_min_score": SUPERVISOR_MIN_SCORE,
        "supervisor_dynamic_size": SUPERVISOR_DYNAMIC_SIZE,
        "supervisor_tf": SUPERVISOR_TF,
        "hard_sl_pct": HARD_SL_PCT,
        "be_trigger_pct": BE_TRIGGER_PCT,
        "trail_trigger_pct": TRAIL_TRIGGER_PCT,
        "trail_distance_pct": TRAIL_DISTANCE_PCT,
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


@app.route("/config", methods=["GET"])
def config():
    return jsonify({
        "symbols": SYMBOLS,
        "budgets": BUDGET_USDT,
        "strict_dedup_s": STRICT_DEDUP_S,
        "dedup_window_s": DEDUP_WINDOW_S,
        "entry_lock_s": ENTRY_LOCK_S,
        "per_bar_lock": PER_BAR_LOCK,
        "simulate": SIMULATE,
        "rehydrate_enabled": REHYDRATE_ENABLED,
        "supervisor_enabled": SUPERVISOR_ENABLED,
        "supervisor_min_score": SUPERVISOR_MIN_SCORE,
        "bot_tpsl_enabled": BOT_TPSL_ENABLED,
    }), 200


@app.route("/envcheck", methods=["GET"])
def envcheck():
    missing = []
    if not SIMULATE:
        if not MEXC_API_KEY:
            missing.append("MEXC_API_KEY")
        if not MEXC_API_SECRET:
            missing.append("MEXC_API_SECRET")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        missing.append("TELEGRAM_BOT_TOKEN")
    if not os.getenv("TELEGRAM_CHAT_ID"):
        missing.append("TELEGRAM_CHAT_ID")
    return jsonify({"missing": missing}), 200


@app.route("/test/send", methods=["GET", "POST"])
def test_send():
    """
    Test TG send.
    """
    send_tg("🧪 Test message from BTC bot")
    return jsonify({"ok": True}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data(as_text=True)
    _dbg(f"[SIGDBG] /webhook hit ct={request.content_type} raw='{raw[:200]}...'")

    # Parse payload (verwacht JSON van TradingView)
    payload = None
    for _ in range(3):
        try:
            payload = json.loads(raw)
            _dbg("[PARSE] JSON loaded")
            break
        except Exception:
            pass

    if payload is None:
        _dbg("[SIGDBG] bad_json")
        return jsonify({"ok": True, "skip": "bad_json"}), 200

    action = (payload.get("action") or "").lower().strip()
    if action not in ["buy", "sell"]:
        _dbg(f"[SKIP] Invalid action '{action}'")
        return jsonify({"ok": True, "skipped": "invalid_action"}), 200

    tv_symbol_raw = payload.get("symbol") or ""
    symbol = parse_symbol(tv_symbol_raw)

    if symbol != SYMBOL:
        _dbg(f"[SKIP] Unknown symbol '{symbol}' (raw='{tv_symbol_raw}')")
        return jsonify({"ok": True, "skip": "unknown_symbol"}), 200

    tf_raw = payload.get("tf") or ""
    tf = normalize_tf(tf_raw)

    # prijs uit payload, fallback naar live ticker (alleen live)
    try:
        price = float(payload.get("price") or 0.0)
    except ValueError:
        price = 0.0

    if price <= 0 and not SIMULATE:
        try:
            price = float(ex.fetch_ticker(SYMBOL)["last"])
            _dbg(f"[PRICE] Fallback ticker price {price}")
        except Exception as e:
            _dbg(f"[WARN] Fetch ticker error: {e}; price=0")
            price = 0.0

    source = payload.get("source", "unknown")

    # Timeframe allowlist
    try:
        atfs = allowed_tfs_for(symbol)
        if tf not in atfs:
            _dbg(f"[TF FILTER] skip {symbol} tf={tf} not allowed ({', '.join(sorted(atfs))})")
            return jsonify({"ok": True, "skip": "tf_not_allowed"}), 200
    except Exception as e:
        _dbg(f"[TF FILTER] warn: {e}")

    now = time.time()
    st = STATE.get(symbol, {})
    if not st:
        # Safety: init als ontbreekt
        STATE[symbol] = {
            "in_position": False,
            "inflight": False,
            "last_action_ts": 0,
            "last_bar_time": 0,
            "entry_price": 0,
            "qty": 0,
            "invested_usd": 0,
        }
        _ensure_wallet(symbol)
        st = STATE[symbol]

    # Strict dedup
    if now - st.get("last_action_ts", 0) < STRICT_DEDUP_S:
        _dbg(f"[DEDUP] skip {symbol} too soon ({now - st['last_action_ts']:.2f}s)")
        return jsonify({"ok": True, "skip": "dedup"}), 200

    # Per-bar lock (5m bars)
    if PER_BAR_LOCK or (action == "buy" and PER_BAR_LOCK_BUY) or (action == "sell" and PER_BAR_LOCK_SELL):
        bar_time = int(now / 300) * 300  # 5m
        if bar_time == st.get("last_bar_time", 0):
            _dbg(f"[BAR LOCK] skip {symbol} same bar {bar_time}")
            return jsonify({"ok": True, "skip": "bar_lock"}), 200
        st["last_bar_time"] = bar_time

    # Inflight guard
    if st.get("inflight", False):
        _dbg(f"[INFLIGHT] skip {symbol} order pending")
        return jsonify({"ok": True, "skip": "inflight"}), 200

    # Entry lockout / al in positie
    if action == "buy" and st.get("in_position", False):
        _dbg(f"[POS] skip {symbol} already in_position at entry={st.get('entry_price', 0)}")
        return jsonify({"ok": True, "skip": "in_position"}), 200

    # Min cooldown
    if now - st.get("last_action_ts", 0) < MIN_TRADE_COOLDOWN_S:
        _dbg(f"[COOLDOWN] skip {symbol} cooldown ({now - st['last_action_ts']:.2f}s)")
        return jsonify({"ok": True, "skip": "cooldown"}), 200

    # Extra bot-side filter op TV payload-indicatoren
    allow, filter_reason = bot_filter_decision(action, price, payload)
    if not allow:
        _dbg(f"[BOT FILTER] skip {symbol} action={action} reason={filter_reason}")
        return jsonify({"ok": True, "skip": "bot_filter", "reason": filter_reason}), 200
    if filter_reason != "filter_disabled_or_not_buy":
        _dbg(f"[BOT FILTER] pass {symbol} action={action} reason={filter_reason}")

    # Supervisor v1: extra guard op BUY's. SELL altijd doorlaten.
    sup_allow, sup_reason, sup_score, sup_size = supervisor_decision(action, symbol, price, payload)
    STATE[symbol]["last_supervisor"] = {
        "ts": time.time(),
        "allow": bool(sup_allow),
        "reason": sup_reason,
        "score": float(sup_score),
        "size_factor": float(sup_size),
    }
    if action == "buy":
        STATE[symbol]["next_size_factor"] = float(sup_size)
    if not sup_allow:
        _dbg(f"[SUPERVISOR] skip {symbol} action={action} reason={sup_reason}")
        _save_state_file()
        return jsonify({"ok": True, "skip": "supervisor", "reason": sup_reason, "score": sup_score}), 200
    if sup_reason != "supervisor_disabled_or_not_buy":
        _dbg(f"[SUPERVISOR] pass {symbol} action={action} reason={sup_reason}")

    st["last_action_ts"] = now

    if action == "buy":
        return _ensure_spend_buy(symbol, price, source=source, tf=tf)
    else:
        return _market_sell_all(symbol, price, source=source, tf=tf)


def _ensure_spend_buy(symbol: str, price: float, source: str = "", tf: str = "") -> Tuple[Dict, int]:
    """
    BUY met budget voor symbol (live of simulate).
    Gebruikt trade_usd uit de virtuele wallet.
    """
    st = STATE[symbol]
    st["inflight"] = True

    try:
        trade_usd, savings_usd = trade_and_savings_usd(symbol)
        size_factor = clamp(float(st.pop("next_size_factor", 1.0)), 0.05, 1.0)
        amount_usd = trade_usd * size_factor
        st["last_size_factor"] = size_factor

        if amount_usd <= 0 or price <= 0:
            _dbg(f"[BUY] skip {symbol} invalid budget/price budget={amount_usd} price={price}")
            return jsonify({"ok": True, "skip": "invalid_budget_or_price"}), 200

        qty = amount_usd / price

        if qty < 0.001:
            _dbg(f"[BUY] skip {symbol} qty too small {qty}")
            return jsonify({"ok": True, "skip": "qty_too_small"}), 200

        if SIMULATE:
            # PAPER BUY
            st["in_position"] = True
            st["entry_price"] = price
            st["qty"] = qty
            st["invested_usd"] = amount_usd
            st["highest_price"] = price
            st["be_armed"] = False
            st["trail_armed"] = False
            st["active_stop_price"] = price * (1.0 - HARD_SL_PCT / 100.0)
            st["active_stop_reason"] = "hard_sl"
            _dbg(f"[PAPER BUY] {symbol} qty={qty} price={price} invested={amount_usd}")
            msg = tg_buy_msg(symbol, price, qty, amount_usd)
            send_tg(msg)
        else:
            # LIVE BUY
            order = ex.create_market_buy_order(symbol, qty)
            _dbg(f"[LIVE] BUY {symbol} id={order.get('id')} qty={qty} price={price} budget_used={amount_usd}")

            # Poll voor fill
            filled = 0.0
            avg = 0.0
            for _ in range(10):
                time.sleep(0.5)
                filled_order = ex.fetch_order(order["id"], symbol)
                filled = filled_order.get("filled", 0.0)
                avg = filled_order.get("average", 0.0)
                if filled > 0:
                    break

            if filled <= 0:
                _dbg(f"[BUY] warn: no fill for {symbol}")
                filled = qty
                avg = price

            gross_q = filled * (avg if avg > 0 else price)
            fee_q = gross_q * 0.001  # 0.1% taker
            net_in = gross_q - fee_q

            st["in_position"] = True
            st["entry_price"] = avg if avg > 0 else price
            st["qty"] = filled
            st["invested_usd"] = net_in
            st["highest_price"] = st["entry_price"]
            st["be_armed"] = False
            st["trail_armed"] = False
            st["active_stop_price"] = st["entry_price"] * (1.0 - HARD_SL_PCT / 100.0)
            st["active_stop_reason"] = "hard_sl"

            msg = tg_buy_msg(symbol, st["entry_price"], filled, net_in)
            send_tg(msg)

        TRADE_LOG.append({
            "ts": time.time(),
            "mode": "paper" if SIMULATE else "live",
            "action": "buy",
            "symbol": symbol,
            "price_usd": float(st["entry_price"]),
            "qty": float(st["qty"]),
            "invested_usd": float(st.get("invested_usd", amount_usd)),
            "source": source,
            "tf": tf,
        })
        _save_state_file()

        return jsonify({"ok": True, "state": STATE[symbol]}), 200
    except Exception as e:
        _dbg(f"[BUY ERROR] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        st["inflight"] = False


def _market_sell_all(symbol: str, price: float, source: str = "", tf: str = "") -> Tuple[Dict, int]:
    """
    Market SELL van volledige positie (live of simulate).
    Na PnL wordt virtuele wallet geherbalanceerd:
    total = trade + savings + pnl, trade gevuld tot target_trade_usd,
    rest naar savings.
    """
    st = STATE[symbol]
    if not st.get("in_position", False):
        _dbg(f"[SELL] skip {symbol} no position")
        return jsonify({"ok": True, "skip": "no_position"}), 200

    st["inflight"] = True

    try:
        qty = st.get("qty", 0.0)
        if qty < 0.001:
            _dbg(f"[SELL] skip {symbol} qty too small {qty}")
            return jsonify({"ok": True, "skip": "qty_too_small"}), 200

        if SIMULATE:
            # PAPER SELL
            entry = st.get("entry_price", 0.0)
            invested = st.get("invested_usd", qty * entry)
            gross_q = qty * price
            if invested <= 0:
                pnl = 0.0
            else:
                pnl = gross_q - invested
            _dbg(f"[PAPER SELL] {symbol} qty={qty} price={price} gross={gross_q} invested={invested} pnl={pnl}")
            net_out = gross_q
            avg = price
        else:
            # LIVE SELL
            order = ex.create_market_sell_order(symbol, qty)
            _dbg(f"[LIVE] SELL {symbol} id={order.get('id')} qty={qty} price={price}")

            filled = 0.0
            avg = 0.0
            for _ in range(10):
                time.sleep(0.5)
                filled_order = ex.fetch_order(order["id"], symbol)
                filled = filled_order.get("filled", 0.0)
                avg = filled_order.get("average", 0.0)
                if filled > 0:
                    break

            if filled <= 0:
                _dbg(f"[SELL] warn: no fill for {symbol}")
                filled = qty
                avg = price

            gross_q = filled * (avg if avg > 0 else price)
            fee_q = gross_q * 0.001
            net_out = gross_q - fee_q

            entry = st.get("entry_price", 0.0)
            invested = st.get("invested_usd", filled * entry)
            if invested <= 0:
                pnl = 0.0
            else:
                pnl = net_out - invested
            _dbg(f"[LIVE] SELL {symbol} filled={filled} avg={avg} gross={gross_q} fee_q={fee_q} invested={invested} net_out={net_out} pnl={pnl}")

        # gezamenlijke afhandeling TG + virtuele wallet + state reset
        if SIMULATE:
            entry = st.get("entry_price", 0.0)
            invested = st.get("invested_usd", qty * entry)
            if invested <= 0:
                pnl = 0.0
            else:
                pnl = net_out - invested

        # Wallet herverdelen: trade + savings + pnl
        trade_usd, savings_usd = trade_and_savings_usd(symbol)
        prev_trade_usd, prev_savings_usd = trade_usd, savings_usd
        target_trade = float(STATE[symbol].get("target_trade_usd", trade_usd))

        total_capital = trade_usd + savings_usd + pnl
        if total_capital <= 0:
            new_trade = 0.0
            new_savings = 0.0
        else:
            new_trade = min(target_trade, total_capital)
            new_savings = total_capital - new_trade

        STATE[symbol]["trade_usd"] = new_trade
        STATE[symbol]["savings_usd"] = new_savings
        STATE[symbol]["realized_pnl_usd"] = float(STATE[symbol].get("realized_pnl_usd", 0.0)) + float(pnl)

        msg = tg_sell_msg(symbol, avg if not SIMULATE else price, qty, net_out, pnl, prev_trade_usd, prev_savings_usd)
        send_tg(msg)

        TRADE_LOG.append({
            "ts": time.time(),
            "mode": "paper" if SIMULATE else "live",
            "action": "sell",
            "symbol": symbol,
            "price_usd": float(avg if not SIMULATE else price),
            "qty": float(qty),
            "net_out_usd": float(net_out),
            "pnl_usd": float(pnl),
            "source": source,
            "tf": tf,
        })

        st["in_position"] = False
        st["entry_price"] = 0.0
        st["qty"] = 0.0
        st["invested_usd"] = 0.0
        st["highest_price"] = 0.0
        st["be_armed"] = False
        st["trail_armed"] = False
        st["active_stop_price"] = 0.0
        st["active_stop_reason"] = ""
        st["last_action_ts"] = time.time()

        _save_state_file()

    except Exception as e:
        _dbg(f"[SELL ERROR] {e}")
    finally:
        st["inflight"] = False

    return jsonify({"ok": True, "state": STATE[symbol]}), 200


# ------------- main -------------

if __name__ == "__main__":
    _dbg(f"[CONF] MEXC_RECV_WINDOW={MEXC_RECVWINDOW_MS} CCXT_TIMEOUT_MS={CCXT_TIMEOUT_MS}")
    _dbg(f"[CONF] SIMULATE={SIMULATE} REHYDRATE_ENABLED={REHYDRATE_ENABLED}")
    _load_state_file()
    try:
        init_exchange()
    except Exception as e:
        _dbg(f"[WARMUP] MEXC init error: {e}")
    rehydrate_positions()
    _dbg(f"✅ Webhook server op http://0.0.0.0:{PORT}/webhook — symbol: {SYMBOL}")
    _dbg(f"[CONF] budgets={BUDGET_USDT}")
    try:
        t = Thread(target=_daily_report_loop, daemon=True)
        t.start()
        _dbg("[REPORT] daily scheduler started")
    except Exception as e:
        _dbg(f"[REPORT] scheduler warn: {e}")

    try:
        t2 = Thread(target=_tpsl_monitor_loop, daemon=True)
        t2.start()
        _dbg(f"[TPSL] monitor started enabled={BOT_TPSL_ENABLED}")
    except Exception as e:
        _dbg(f"[TPSL] scheduler warn: {e}")

    app.run(host="0.0.0.0", port=PORT, debug=False)
