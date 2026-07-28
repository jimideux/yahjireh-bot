import aiohttp, asyncio
from datetime import datetime, timezone

# Binance symbol mapping
SYMBOL_MAP = {
    "BTC-USDT":  "BTCUSDT",
    "ETH-USDT":  "ETHUSDT",
    "SOL-USDT":  "SOLUSDT",
    "XRP-USDT":  "XRPUSDT",
    "LINK-USDT": "LINKUSDT",
    "SUI-USDT":  "SUIUSDT",
    "APT-USDT":  "APTUSDT",
    "TON-USDT":  "TONUSDT",
    "NEAR-USDT": "NEARUSDT",
    "ICP-USDT":  "ICPUSDT",
}

INTERVAL_MAP = {
    "1H": "1h", "4H": "4h", "1D": "1d",
    "15m": "15m", "5m": "5m", "1h": "1h",
}

# ── Filter 4: Trading hours (8am-4pm EST = 13:00-21:00 UTC) ──────────────────
def is_trading_hours():
    hour = datetime.now(timezone.utc).hour
    return 13 <= hour <= 21

# ── Binance candle fetcher ────────────────────────────────────────────────────
async def get_candles_binance(pair, interval="1h", limit=60):
    symbol   = SYMBOL_MAP.get(pair, pair.replace("-",""))
    interval = INTERVAL_MAP.get(interval, interval.lower())
    # Binance geoblocks this server (HTTP 451) -> use BloFin candles
    bar = {"1h":"1H","4h":"4H","1d":"1D","15m":"15m","5m":"5m"}.get(interval, "1H")
    url = f"https://openapi.blofin.com/api/v1/market/candles?instId={pair}&bar={bar}&limit={limit}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                j = await r.json()
                data = j.get("data") if isinstance(j, dict) else None
                if isinstance(data, list) and data:
                    # BloFin returns newest-first; reverse to oldest-first
                    return sorted(data, key=lambda x: int(x[0]))
    except Exception as e:
        print(f"Candle error {pair}: {e}")
    return []

# ── EMA calculation ───────────────────────────────────────────────────────────
def calc_ema(closes, period):
    if len(closes) < period: return 0.0
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema

# ── ATR calculation ───────────────────────────────────────────────────────────
def calc_atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    trs = []
    for i in range(1, len(candles)):
        high       = float(candles[i][2])
        low        = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        tr = max(high-low, abs(high-prev_close), abs(low-prev_close))
        trs.append(tr)
    if len(trs) < period: return sum(trs)/len(trs)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr*(period-1) + tr) / period
    return atr

def calc_rsi(closes, period=14):
    """Calculate RSI from closing prices"""
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ── Filter 1: BTC daily trend ─────────────────────────────────────────────────
async def get_btc_daily_trend():
    candles = await get_candles_binance("BTC-USDT", interval="1d", limit=10)
    if len(candles) < 5: return "neutral"
    closes = [float(c[4]) for c in candles]
    ema5   = calc_ema(closes, 5)
    price  = closes[-1]
    if price > ema5 * 1.005:
        return "bull"
    elif price < ema5 * 0.995:
        return "bear"
    return "neutral"

# ── Main signal function ──────────────────────────────────────────────────────
async def get_signal(pair, ema_short=20, ema_long=50,
                     vol_min=0.20, price_zone=0.015, interval="1h"):

    # Filter 1: BTC macro trend
    btc_trend = await get_btc_daily_trend()
    candles   = await get_candles_binance(pair, interval=interval, limit=70)
    if len(candles) < ema_long + 5: return None

    closes  = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    price   = closes[-1]
    ema20   = calc_ema(closes, ema_short)
    ema50   = calc_ema(closes, ema_long)
    atr     = calc_atr(candles)

    # Volume data (kept for signal info only)
    cur_vol   = volumes[-1]
    avg_vol   = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0
    vol_ratio = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 0

    # Filter 3: Price near EMA50
    dist = abs(price - ema50) / ema50
    if dist > price_zone:
        return None

    # Direction from EMA crossover
    trend = "long" if ema20 > ema50 else "short"

    # Dynamic RSI filter — adjusts based on BTC trend strength
    rsi = calc_rsi(closes)
    # In strong bear trend, RSI stays low — lower the threshold
    btc_candles = await get_candles_binance("BTC-USDT", interval="1h", limit=20)
    btc_closes  = [float(c[4]) for c in btc_candles]
    btc_rsi     = calc_rsi(btc_closes)
    if btc_trend == "bear" and btc_rsi < 40:
        rsi_short_min = 35  # Strong bear — allow oversold shorts
    else:
        rsi_short_min = 45  # Normal — require momentum confirmation
    if trend == "short" and rsi < rsi_short_min:
        print(f"  {pair}: skipping SHORT — RSI={rsi} below {rsi_short_min} (BTC RSI={btc_rsi})")
        return None
    if trend == "long" and rsi > 55:
        print(f"  {pair}: skipping LONG — RSI={rsi} overbought (pullback likely)")
        return None

    # Filter 1 applied: only trade WITH BTC macro trend
    if btc_trend == "bull" and trend == "short":
        print(f"  {pair}: skipping SHORT — BTC daily is BULLISH")
        return None
    if btc_trend == "bear" and trend == "long":
        print(f"  {pair}: skipping LONG — BTC daily is BEARISH")
        return None

    return {
        "pair":      pair,
        "direction": trend,
        "price":     price,
        "ema20":     round(ema20, 4),
        "ema50":     round(ema50, 4),
        "atr":       round(atr, 6),
        "vol_ratio": round(vol_ratio, 2),
        "dist_pct":  round(dist * 100, 3),
        "rsi":       rsi,
        "btc_trend": btc_trend,
        "source":    "binance"
    }

async def get_atr_binance(pair, period=14, interval="1h"):
    candles = await get_candles_binance(pair, interval=interval, limit=period+5)
    if not candles: return 0.0
    return calc_atr(candles, period)

async def get_ema_binance(pair, period=50, interval="1h"):
    candles = await get_candles_binance(pair, interval=interval, limit=period+10)
    if not candles: return 0.0
    closes = [float(c[4]) for c in candles]
    return calc_ema(closes, period)
