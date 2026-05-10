# -*- coding: utf-8 -*-
"""
MEXC single-pair webhook bot (BTC/USDT) met Telegram alerts.
- Paar: BTC/USDT
- Budget via ENV: BUDGET_BTC_USDT (default 500 USDT)
- SIMULATE modus (default aan, dus GEEN echte orders)
- Optionele REHYDRATE_ENABLED (default uit, raakt je bestaande holdings niet)
- Dedup, per-symbol state, inflight guard
- Per-candle lock: max 1 BUY/SELL per 5m-bar
- Virtuele wallet: trade_usd + savings_usd, bij verlies aanvullen vanuit spaarpot
- Endpoints: /, /health, /config, /envcheck, /test/send, /webhook
"""

import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from threading import Lock, Thread
from typing import Dict, Any, Tuple, List, Optional

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


def savings_for(symbol: str) -> float:
    """
    Savings balance voor symbol in EUR (placeholder als je ooit echte spaarrekening gaat tracken).
    Hier gebruiken we de virtuele wallet in STATE, niet deze functie.
    """
    return 0.0


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
    STATE[symbol] = st


def tg_buy_msg(symbol: str, price_usd: float, qty: float, invested_usd: float) -> str:
    """
    TG message voor BUY.
    """
    trade_usd, savings_usd = trade_and_savings_usd(symbol)
    trade_eur = eur_rate() * trade_usd
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


def tg_sell_msg(symbol: str, price_usd: float, qty: float, net_out_usd: float, pnl_usd: float) -> str:
    """
    TG message voor SELL.
    Toont actuele virtuele handels- en spaar-saldo.
    """
    trade_usd, savings_usd = trade_and_savings_usd(symbol)
    trade_eur = eur_rate() * trade_usd
    savings_eur = eur_rate() * savings_usd
    total_eur = trade_eur + savings_eur

    now_str = fmt_dt(local_now())
    sym_ccxt = sym_label(symbol)
    pnl_eur = eur_rate() * pnl_usd
    winlose = "Winst" if pnl_eur >= 0 else "Verlies"
    lines = [
        f"{BOT_TITLE}",
        f"📄 [{sym_ccxt}] VERKOOP",
        f"📹 Verkoopprijs: {fmt_usd(price_usd, 4)}",
        f"📈 {winlose}: {fmt_eur(pnl_eur)}",
        f"💰 Handelssaldo: {fmt_eur(trade_eur)}",
        f"💼 Spaarrekening: {fmt_eur(savings_eur)}",
        f"📈 Totale waarde: {fmt_eur(total_eur)}",
        f"🔐 Tradebedrag: {fmt_eur(trade_eur)}",
        f"🔗 Tijd: {now_str}",
        f"🧪 Modus: {'PAPER' if SIMULATE else 'LIVE'}",
    ]
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

# ------------- ADVISOR / QUALITY GATE -------------

ADVISOR_ENABLED = os.getenv("ADVISOR_ENABLED", "true").lower() == "true"
ADVISOR_MIN_SCORE = float(os.getenv("ADVISOR_MIN_SCORE", "0.65"))
ADVISOR_NOTIFY_SKIPS = os.getenv("ADVISOR_NOTIFY_SKIPS", "true").lower() == "true"
ADVISOR_USE_LIVE_PRICE = os.getenv("ADVISOR_USE_LIVE_PRICE", "true").lower() == "true"

# Extra tijdelijke webhook-debug naar Telegram/logs
WEBHOOK_NOTIFY_ALL = os.getenv("WEBHOOK_NOTIFY_ALL", "false").lower() == "true"
WEBHOOK_NOTIFY_SKIPS = os.getenv("WEBHOOK_NOTIFY_SKIPS", "true").lower() == "true"
WEBHOOK_NOTIFY_RAW_MAX = int(os.getenv("WEBHOOK_NOTIFY_RAW_MAX", "350"))
LAST_WEBHOOKS: List[Dict[str, Any]] = []


ADVISOR_TF = os.getenv("ADVISOR_TF", "10m")
ADVISOR_OHLCV_LIMIT = int(os.getenv("ADVISOR_OHLCV_LIMIT", "260"))
ADVISOR_HTF_MINUTES = int(os.getenv("ADVISOR_HTF_MINUTES", "240"))
MAX_TRADES_PER_HTF_REGIME = int(os.getenv("MAX_TRADES_PER_HTF_REGIME", "1"))

LOSS_COOLDOWN_MINUTES = float(os.getenv("LOSS_COOLDOWN_MINUTES", "720"))
MAX_ALERT_PRICE_DEVIATION_PCT = float(os.getenv("MAX_ALERT_PRICE_DEVIATION_PCT", "0.15"))

EMA_FAST_LEN = int(os.getenv("ADVISOR_EMA_FAST", "34"))
EMA_MID_LEN = int(os.getenv("ADVISOR_EMA_MID", "89"))
EMA_SLOW_LEN = int(os.getenv("ADVISOR_EMA_SLOW", "144"))
EMA_TREND_LEN = int(os.getenv("EMA_TREND_LEN", "200"))
REQUIRE_EMA200_UP = os.getenv("REQUIRE_EMA200_UP", "true").lower() == "true"
EMA_SLOPE_LOOKBACK = int(os.getenv("EMA_SLOPE_LOOKBACK", "5"))
MIN_EMA_SPREAD_PCT = float(os.getenv("MIN_EMA_SPREAD_PCT", "0.05"))

MIN_ADX = float(os.getenv("MIN_ADX", "18"))
ADX_LEN = int(os.getenv("ADX_LEN", "14"))
ADX_SMOOTH = int(os.getenv("ADX_SMOOTH", "14"))
ADVISOR_RSI_LEN = int(os.getenv("ADVISOR_RSI_LEN", "14"))
ADVISOR_RSI_MIN_LONG = float(os.getenv("ADVISOR_RSI_MIN_LONG", "48"))
ADVISOR_RSI_MAX_LONG = float(os.getenv("ADVISOR_RSI_MAX_LONG", "78"))
ATR_LEN = int(os.getenv("ATR_LEN", "14"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.10"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "1.20"))

USE_VOLUME_FILTER = os.getenv("USE_VOLUME_FILTER", "true").lower() == "true"
VOLUME_MA_LEN = int(os.getenv("VOLUME_MA_LEN", "20"))
MIN_VOLUME_FACTOR = float(os.getenv("MIN_VOLUME_FACTOR", "0.80"))

BLOCK_AFTER_BIG_CANDLE = os.getenv("BLOCK_AFTER_BIG_CANDLE", "true").lower() == "true"
MAX_CANDLE_ATR_MULT = float(os.getenv("MAX_CANDLE_ATR_MULT", "2.50"))

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
    print(f"[{ts}] {msg}", flush=True)


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
    Init MEXC client.
    In SIMULATE wordt een publieke MEXC-client gestart, zodat de advisor live candles
    en tickerdata kan ophalen zonder echte orders te plaatsen.
    """
    global ex

    cfg = {
        "sandbox": False,
        "enableRateLimit": True,
        "options": {"recvWindow": MEXC_RECVWINDOW_MS},
        "timeout": CCXT_TIMEOUT_MS,
    }

    if not SIMULATE:
        cfg["apiKey"] = MEXC_API_KEY
        cfg["secret"] = MEXC_API_SECRET
        _dbg("[WARMUP] LIVE mode: exchange init with API keys")
    else:
        _dbg("[WARMUP] SIMULATE mode: public exchange init for advisor only")

    ex = ccxt.mexc(cfg)
    ex.load_markets()
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



# ------------- ADVISOR HELPERS -------------

def _ema(values: List[float], length: int) -> List[Optional[float]]:
    """Exponential moving average zonder externe libraries."""
    if length <= 0 or not values:
        return [None] * len(values)
    out: List[Optional[float]] = [None] * len(values)
    k = 2.0 / (length + 1.0)
    ema_val: Optional[float] = None
    for i, v in enumerate(values):
        if ema_val is None:
            ema_val = v
        else:
            ema_val = v * k + ema_val * (1.0 - k)
        if i >= length - 1:
            out[i] = ema_val
    return out


def _rma(values: List[float], length: int) -> List[Optional[float]]:
    """Wilder RMA, gebruikt voor ATR/ADX/RSI."""
    out: List[Optional[float]] = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    acc = sum(values[:length]) / length
    out[length - 1] = acc
    for i in range(length, len(values)):
        acc = (acc * (length - 1) + values[i]) / length
        out[i] = acc
    return out


def _last_valid(vals: List[Optional[float]], default: float = 0.0) -> float:
    for v in reversed(vals):
        if v is not None:
            return float(v)
    return default


def _calc_indicators(ohlcv: List[List[float]]) -> Dict[str, float]:
    """
    Verwacht ccxt OHLCV: [ts, open, high, low, close, volume].
    Retourneert alleen de laatste bevestigde candle-waarden.
    """
    if len(ohlcv) < max(EMA_TREND_LEN, ADX_LEN + ADX_SMOOTH, ATR_LEN, VOLUME_MA_LEN, ADVISOR_RSI_LEN) + 10:
        raise ValueError(f"not_enough_candles:{len(ohlcv)}")

    opens = [float(x[1]) for x in ohlcv]
    highs = [float(x[2]) for x in ohlcv]
    lows = [float(x[3]) for x in ohlcv]
    closes = [float(x[4]) for x in ohlcv]
    vols = [float(x[5]) for x in ohlcv]

    # Gebruik de laatste volledig teruggegeven candle. Bij ccxt kan de allerlaatste candle nog lopen.
    # Daarom gebruiken we index -2 als confirmed candle waar mogelijk.
    idx = -2 if len(closes) >= 2 else -1

    ema_fast = _ema(closes, EMA_FAST_LEN)
    ema_mid = _ema(closes, EMA_MID_LEN)
    ema_slow = _ema(closes, EMA_SLOW_LEN)
    ema_trend = _ema(closes, EMA_TREND_LEN)

    # ATR
    tr: List[float] = [0.0]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr_arr = _rma(tr, ATR_LEN)

    # ADX / DMI
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    tr_rma = _rma(tr, ADX_LEN)
    plus_rma = _rma(plus_dm, ADX_LEN)
    minus_rma = _rma(minus_dm, ADX_LEN)
    dx: List[float] = []
    for i in range(len(closes)):
        if tr_rma[i] is None or tr_rma[i] == 0:
            dx.append(0.0)
            continue
        pdi = 100.0 * (plus_rma[i] or 0.0) / tr_rma[i]
        mdi = 100.0 * (minus_rma[i] or 0.0) / tr_rma[i]
        denom = pdi + mdi
        dx.append(0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom)
    adx_arr = _rma(dx, ADX_SMOOTH)

    # RSI
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = _rma(gains, ADVISOR_RSI_LEN)
    avg_loss = _rma(losses, ADVISOR_RSI_LEN)
    rsi_arr: List[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        if avg_gain[i] is None or avg_loss[i] is None:
            continue
        if avg_loss[i] == 0:
            rsi_arr[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            rsi_arr[i] = 100.0 - (100.0 / (1.0 + rs))

    vol_ma = _ema(vols, VOLUME_MA_LEN)

    c = closes[idx]
    atr = float(atr_arr[idx] or 0.0)
    atr_pct = 100.0 * atr / c if c > 0 else 0.0
    candle_range = highs[idx] - lows[idx]
    candle_atr_mult = candle_range / atr if atr > 0 else 0.0

    et = float(ema_trend[idx] or 0.0)
    et_prev_i = idx - EMA_SLOPE_LOOKBACK
    et_prev = float(ema_trend[et_prev_i] or et) if abs(et_prev_i) <= len(ema_trend) else et
    ema_slope_up = et > et_prev

    ef = float(ema_fast[idx] or 0.0)
    em = float(ema_mid[idx] or 0.0)
    es = float(ema_slow[idx] or 0.0)
    spread_pct = 100.0 * abs(ef - es) / c if c > 0 else 0.0

    vm = float(vol_ma[idx] or 0.0)
    vf = vols[idx] / vm if vm > 0 else 0.0

    return {
        "close": c,
        "ema_fast": ef,
        "ema_mid": em,
        "ema_slow": es,
        "ema_trend": et,
        "ema_slope_up": 1.0 if ema_slope_up else 0.0,
        "ema_spread_pct": spread_pct,
        "atr": atr,
        "atr_pct": atr_pct,
        "adx": float(adx_arr[idx] or 0.0),
        "rsi": float(rsi_arr[idx] or 0.0),
        "vol_factor": vf,
        "candle_atr_mult": candle_atr_mult,
    }


def _advisor_htf_key(now_s: float) -> int:
    """Bucket-key voor max aantal entries per HTF-blok."""
    bucket_s = max(1, ADVISOR_HTF_MINUTES) * 60
    return int(now_s // bucket_s)


def advisor_check_buy(symbol: str, alert_price: float, source: str = "", tf: str = "") -> Dict[str, Any]:
    """
    Trade Quality Gate voor BUY-signalen.
    SELL-signalen blijven exits en worden niet door deze advisor geblokkeerd.
    """
    if not ADVISOR_ENABLED:
        return {"allow": True, "score": 1.0, "reason": "advisor_disabled", "live_price": alert_price}

    st = STATE.get(symbol, {})
    now_s = time.time()
    hard_blocks: List[str] = []
    notes: List[str] = []
    score = 0.0
    max_score = 0.0

    # Cooldown na verlies
    last_loss_ts = float(st.get("last_loss_ts", 0.0) or 0.0)
    if LOSS_COOLDOWN_MINUTES > 0 and last_loss_ts > 0:
        age_min = (now_s - last_loss_ts) / 60.0
        if age_min < LOSS_COOLDOWN_MINUTES:
            hard_blocks.append(f"loss_cooldown {age_min:.0f}/{LOSS_COOLDOWN_MINUTES:.0f}m")

    # Max 1 trade per HTF regime/bucket
    if MAX_TRADES_PER_HTF_REGIME > 0:
        key = _advisor_htf_key(now_s)
        if int(st.get("last_buy_htf_key", -999999)) == key:
            hard_blocks.append(f"htf_trade_limit bucket={key}")

    live_price = alert_price
    try:
        if ex is not None:
            ticker = ex.fetch_ticker(symbol)
            live_price = float(ticker.get("last") or alert_price)
    except Exception as e:
        notes.append(f"ticker_warn:{e}")

    # TV-alertprijs vs actuele MEXC-prijs
    max_score += 0.10
    if alert_price > 0 and live_price > 0:
        dev_pct = 100.0 * abs(live_price - alert_price) / alert_price
        if dev_pct > MAX_ALERT_PRICE_DEVIATION_PCT:
            hard_blocks.append(f"price_deviation {dev_pct:.3f}%>{MAX_ALERT_PRICE_DEVIATION_PCT:.3f}%")
        else:
            score += 0.10
    else:
        notes.append("no_price_deviation_check")

    try:
        if ex is None:
            raise ValueError("exchange_not_ready")
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=ADVISOR_TF, limit=ADVISOR_OHLCV_LIMIT)
        ind = _calc_indicators(ohlcv)
    except Exception as e:
        if os.getenv("ADVISOR_FAIL_OPEN", "false").lower() == "true":
            return {"allow": True, "score": 0.0, "reason": f"advisor_fail_open:{e}", "live_price": live_price}
        return {"allow": False, "score": 0.0, "reason": f"advisor_data_error:{e}", "live_price": live_price}

    # Trend: long alleen boven EMA200 / EMA-structuur
    max_score += 0.25
    trend_ok = live_price > ind["ema_trend"] and ind["ema_fast"] > ind["ema_mid"] > ind["ema_slow"]
    if REQUIRE_EMA200_UP:
        trend_ok = trend_ok and bool(ind["ema_slope_up"])
    if trend_ok:
        score += 0.25
    else:
        hard_blocks.append("trend_not_ok")

    # EMA spread tegen chop
    max_score += 0.10
    if ind["ema_spread_pct"] >= MIN_EMA_SPREAD_PCT:
        score += 0.10
    else:
        hard_blocks.append(f"ema_spread_low {ind['ema_spread_pct']:.3f}%<{MIN_EMA_SPREAD_PCT:.3f}%")

    # ADX
    max_score += 0.15
    if ind["adx"] >= MIN_ADX:
        score += 0.15
    else:
        notes.append(f"adx_low {ind['adx']:.1f}<{MIN_ADX:.1f}")

    # ATR% bereik
    max_score += 0.15
    if MIN_ATR_PCT <= ind["atr_pct"] <= MAX_ATR_PCT:
        score += 0.15
    else:
        hard_blocks.append(f"atr_pct_bad {ind['atr_pct']:.3f}% not {MIN_ATR_PCT:.3f}-{MAX_ATR_PCT:.3f}%")

    # RSI: niet te zwak, niet extreem overbought
    max_score += 0.10
    if ADVISOR_RSI_MIN_LONG <= ind["rsi"] <= ADVISOR_RSI_MAX_LONG:
        score += 0.10
    else:
        notes.append(f"rsi_out {ind['rsi']:.1f}")

    # Volume
    max_score += 0.05
    if not USE_VOLUME_FILTER or ind["vol_factor"] >= MIN_VOLUME_FACTOR:
        score += 0.05
    else:
        notes.append(f"vol_low x{ind['vol_factor']:.2f}<x{MIN_VOLUME_FACTOR:.2f}")

    # Grote spike-candle vermijden
    max_score += 0.10
    if not BLOCK_AFTER_BIG_CANDLE or ind["candle_atr_mult"] <= MAX_CANDLE_ATR_MULT:
        score += 0.10
    else:
        hard_blocks.append(f"big_candle {ind['candle_atr_mult']:.2f}ATR>{MAX_CANDLE_ATR_MULT:.2f}ATR")

    norm_score = score / max_score if max_score > 0 else 0.0
    allow = not hard_blocks and norm_score >= ADVISOR_MIN_SCORE

    reason_bits = []
    if hard_blocks:
        reason_bits.append("blocks=" + "; ".join(hard_blocks))
    if notes:
        reason_bits.append("notes=" + "; ".join(notes))
    reason_bits.append(
        f"score={norm_score:.2f} adx={ind['adx']:.1f} rsi={ind['rsi']:.1f} "
        f"atr%={ind['atr_pct']:.3f} spread%={ind['ema_spread_pct']:.3f} volx={ind['vol_factor']:.2f}"
    )

    return {
        "allow": allow,
        "score": round(norm_score, 3),
        "reason": " | ".join(reason_bits),
        "live_price": live_price,
        "indicators": ind,
        "source": source,
        "tf": tf,
    }


def advisor_tg_skip(symbol: str, action: str, advisor: Dict[str, Any]):
    """Telegram-melding voor geweigerde advisor-trades."""
    if not ADVISOR_NOTIFY_SKIPS:
        return
    sym_ccxt = sym_label(symbol)
    msg = (
        f"{BOT_TITLE}\n"
        f"🧠❌ [{sym_ccxt}] {action.upper()} geweigerd door advisor\n"
        f"Score: {advisor.get('score', 0)} / min {ADVISOR_MIN_SCORE}\n"
        f"Reden: {advisor.get('reason', '')}\n"
        f"Tijd: {fmt_dt(local_now())}\n"
        f"Modus: {'PAPER' if SIMULATE else 'LIVE'}"
    )
    send_tg(msg)


def _record_webhook_event(event: Dict[str, Any]):
    """Bewaar laatste webhook-events voor /debug/last_webhooks."""
    try:
        event["ts_utc"] = datetime.now(timezone.utc).isoformat()
        LAST_WEBHOOKS.append(event)
        del LAST_WEBHOOKS[:-25]
    except Exception as e:
        _dbg(f"[WEBHOOKDBG] record error: {e}")


def tg_webhook_notice(title: str, detail: str):
    """Tijdelijke Telegram-debugmelding voor webhook/skip-events."""
    try:
        if WEBHOOK_NOTIFY_SKIPS:
            send_tg(f"{BOT_TITLE}\n🧩 {title}\n{detail[:900]}")
    except Exception as e:
        _dbg(f"[WEBHOOKDBG] tg notice error: {e}")


# ------------- ENDPOINTS -------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "symbols": SYMBOLS,
        "simulate": SIMULATE,
        "rehydrate_enabled": REHYDRATE_ENABLED,
        "advisor_enabled": ADVISOR_ENABLED,
        "advisor_min_score": ADVISOR_MIN_SCORE,
        "advisor_tf": ADVISOR_TF,
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
        "advisor_enabled": ADVISOR_ENABLED,
        "advisor_min_score": ADVISOR_MIN_SCORE,
        "advisor_tf": ADVISOR_TF,
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



@app.route("/advisor/status", methods=["GET"])
def advisor_status():
    return jsonify({
        "enabled": ADVISOR_ENABLED,
        "min_score": ADVISOR_MIN_SCORE,
        "tf": ADVISOR_TF,
        "ohlcv_limit": ADVISOR_OHLCV_LIMIT,
        "htf_minutes": ADVISOR_HTF_MINUTES,
        "max_trades_per_htf_regime": MAX_TRADES_PER_HTF_REGIME,
        "loss_cooldown_minutes": LOSS_COOLDOWN_MINUTES,
        "max_alert_price_deviation_pct": MAX_ALERT_PRICE_DEVIATION_PCT,
        "min_adx": MIN_ADX,
        "min_atr_pct": MIN_ATR_PCT,
        "max_atr_pct": MAX_ATR_PCT,
        "ema_trend_len": EMA_TREND_LEN,
        "min_ema_spread_pct": MIN_EMA_SPREAD_PCT,
        "volume_filter": USE_VOLUME_FILTER,
        "min_volume_factor": MIN_VOLUME_FACTOR,
        "fail_open": os.getenv("ADVISOR_FAIL_OPEN", "false").lower() == "true",
    }), 200

@app.route("/debug/last_webhooks", methods=["GET"])
def debug_last_webhooks():
    return jsonify({"last_webhooks": LAST_WEBHOOKS[-25:]}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data(as_text=True)
    _dbg(f"[SIGDBG] /webhook hit ct={request.content_type} raw='{raw[:200]}...'")
    if WEBHOOK_NOTIFY_ALL:
        send_tg(f"{BOT_TITLE}\n🧩 WEBHOOK ONTVANGEN\nct={request.content_type}\nraw={raw[:WEBHOOK_NOTIFY_RAW_MAX]}")
    _record_webhook_event({"stage": "received", "content_type": str(request.content_type), "raw": raw[:WEBHOOK_NOTIFY_RAW_MAX]})

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
        _record_webhook_event({"stage": "bad_json", "raw": raw[:WEBHOOK_NOTIFY_RAW_MAX]})
        tg_webhook_notice("WEBHOOK geweigerd: bad_json", f"TradingView stuurde geen geldige JSON. Raw={raw[:WEBHOOK_NOTIFY_RAW_MAX]}")
        return jsonify({"ok": True, "skip": "bad_json"}), 200

    action = (payload.get("action") or "").lower().strip()
    if action not in ["buy", "sell"]:
        _dbg(f"[SKIP] Invalid action '{action}'")
        _record_webhook_event({"stage": "invalid_action", "payload": payload})
        tg_webhook_notice("WEBHOOK geskipt: invalid_action", f"action='{action}' payload={str(payload)[:WEBHOOK_NOTIFY_RAW_MAX]}")
        return jsonify({"ok": True, "skipped": "invalid_action"}), 200

    tv_symbol_raw = payload.get("symbol") or ""
    symbol = parse_symbol(tv_symbol_raw)

    if symbol != SYMBOL:
        _dbg(f"[SKIP] Unknown symbol '{symbol}' (raw='{tv_symbol_raw}')")
        _record_webhook_event({"stage": "unknown_symbol", "symbol": symbol, "raw_symbol": tv_symbol_raw, "payload": payload})
        tg_webhook_notice("WEBHOOK geskipt: unknown_symbol", f"raw='{tv_symbol_raw}' parsed='{symbol}' expected='{SYMBOL}'")
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
    _record_webhook_event({"stage": "parsed", "action": action, "symbol": symbol, "tf": tf, "price": price, "source": source, "payload": payload})

    # Timeframe allowlist
    try:
        atfs = allowed_tfs_for(symbol)
        if tf not in atfs:
            _dbg(f"[TF FILTER] skip {symbol} tf={tf} not allowed ({', '.join(sorted(atfs))})")
            _record_webhook_event({"stage": "tf_not_allowed", "symbol": symbol, "tf": tf, "allowed": sorted(atfs), "payload": payload})
            tg_webhook_notice("WEBHOOK geskipt: tf_not_allowed", f"{symbol} tf={tf} allowed={', '.join(sorted(atfs))}")
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
        age_s = now - st.get("last_action_ts", 0)
        _dbg(f"[DEDUP] skip {symbol} too soon ({age_s:.2f}s)")
        _record_webhook_event({"stage": "dedup", "symbol": symbol, "action": action, "age_s": age_s, "payload": payload})
        tg_webhook_notice("WEBHOOK geskipt: dedup", f"{symbol} {action} te snel na vorige actie: {age_s:.2f}s")
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
        _record_webhook_event({"stage": "in_position", "symbol": symbol, "action": action, "entry": st.get("entry_price", 0), "payload": payload})
        tg_webhook_notice("BUY geskipt: al in positie", f"{symbol} entry={st.get('entry_price', 0)}")
        return jsonify({"ok": True, "skip": "in_position"}), 200

    # Min cooldown
    if now - st.get("last_action_ts", 0) < MIN_TRADE_COOLDOWN_S:
        _dbg(f"[COOLDOWN] skip {symbol} cooldown ({now - st['last_action_ts']:.2f}s)")
        return jsonify({"ok": True, "skip": "cooldown"}), 200

    # Advisor / Quality Gate: alleen BUY wordt gefilterd. SELL blijft exit.
    if action == "buy":
        advisor = advisor_check_buy(symbol, price, source=source, tf=tf)
        _dbg(f"[ADVISOR] allow={advisor.get('allow')} score={advisor.get('score')} reason={advisor.get('reason')}")
        if not advisor.get("allow", False):
            advisor_tg_skip(symbol, action, advisor)
            return jsonify({"ok": True, "skip": "advisor_block", "advisor": advisor}), 200
        if ADVISOR_USE_LIVE_PRICE and advisor.get("live_price", 0) > 0:
            price = float(advisor["live_price"])
        st["last_buy_htf_key"] = _advisor_htf_key(now)

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
        amount_usd = trade_usd

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
        _record_webhook_event({"stage": "sell_no_position", "symbol": symbol, "source": source, "tf": tf})
        tg_webhook_notice("SELL geskipt: geen positie", f"{symbol} source={source} tf={tf}")
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

        # Advisor leert van verlies: start loss-cooldown na verliestrade.
        if pnl < 0:
            STATE[symbol]["last_loss_ts"] = time.time()
            STATE[symbol]["last_loss_pnl_usd"] = float(pnl)
        else:
            STATE[symbol]["last_win_ts"] = time.time()
            STATE[symbol]["last_win_pnl_usd"] = float(pnl)

        # Wallet herverdelen: trade + savings + pnl
        trade_usd, savings_usd = trade_and_savings_usd(symbol)
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

        msg = tg_sell_msg(symbol, avg if not SIMULATE else price, qty, net_out, pnl)
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
    _dbg(f"[CONF] ADVISOR_ENABLED={ADVISOR_ENABLED} MIN_SCORE={ADVISOR_MIN_SCORE} TF={ADVISOR_TF}")
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
    app.run(host="0.0.0.0", port=PORT, debug=False)
