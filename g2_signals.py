#!/usr/bin/env python3
"""g2_signals.py -- Generation 2 signal layer: the owner's legacy 1H trend
ruleset (signals.py + trend.py TP/SL math), ported verbatim onto the Gen-1
chassis. Deviations from as-coded are pre-registered in GEN2.md; the load-
bearing one is CLOSED BARS ONLY (legacy read the forming candle).

Rules (all from the owner's repaired system, parameters from love.py):
  direction: EMA20 vs EMA50 on 1H
  entry zone: |price - EMA50| / EMA50 <= ema_threshold (0.008)
  regime:     EMA separation > 0.4% AND widening vs 2 bars ago
  momentum:   last 3 closed bars must not contradict direction (+/-0.1%)
  RSI:        long blocked > 55; short blocked < 45 (35 in strong BTC bear)
  macro:      BTC 1D vs EMA5 +/-0.5% -> bull blocks shorts, bear blocks longs
  stop:       1.0 x ATR(14, 1H);  target: max(2.0 x ATR, 0.3% of price)
"""
from dataclasses import dataclass
from love import config as love_cfg

EMA_SHORT = love_cfg.ema_short          # 20
EMA_LONG = love_cfg.ema_long            # 50
PRICE_ZONE = love_cfg.ema_threshold     # 0.008
ATR_PERIOD = love_cfg.atr_period        # 14
SL_MULT = love_cfg.atr_sl_mult          # 1.0
TP_MULT = love_cfg.atr_tp_mult          # 2.0
MIN_TP_PCT = love_cfg.min_tp_pct        # 0.003
FEE_RT = 0.0012
ASSUMED_WR = 0.42


@dataclass
class Signal:
    pair: str; direction: str; timeframe: str; ts: int
    entry: float; stop: float; target: float
    stop_pct: float; target_pct: float; r_multiple: float
    ev_pct: float; fee_mult: float; htf_bias: str
    atr_pct: float; rsi: float; grade: str


def ema(closes, period):
    if len(closes) < period: return 0.0
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def atr14(rows, period=14):
    if len(rows) < period + 1: return 0.0
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i][2], rows[i][3], rows[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def rsi14(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def closed(rows):
    """Keep confirmed bars only — the pre-registered repaint fix."""
    out = [r for r in rows if len(r) < 9 or str(r[8]) == "1"]
    return out


def parse(raw):
    return sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                   float(x[4]), *([float(x[5])] if len(x) > 5 else [0.0]),
                   *([0.0, 0.0]), (str(x[8]) if len(x) > 8 else "1"))
                  for x in raw)


async def btc_daily_trend(client):
    raw = await client.get_candles("BTC-USDT", bar="1D", limit=12)
    rows = closed(parse(raw))
    if len(rows) < 6: return "neutral"
    closes = [r[4] for r in rows]
    e5, px = ema(closes, 5), closes[-1]
    if px > e5 * 1.005: return "bull"
    if px < e5 * 0.995: return "bear"
    return "neutral"


async def scan_pair(client, pair, btc_trend):
    raw = await client.get_candles(pair, bar="1H", limit=80)
    rows = closed(parse(raw))
    if len(rows) < EMA_LONG + 5: return None
    closes = [r[4] for r in rows]
    price = closes[-1]
    e20, e50 = ema(closes, EMA_SHORT), ema(closes, EMA_LONG)
    a = atr14(rows, ATR_PERIOD)
    if a <= 0 or e50 <= 0: return None
    if abs(price - e50) / e50 > PRICE_ZONE: return None
    trend = "long" if e20 > e50 else "short"
    sep = abs(e20 - e50) / e50
    if sep < 0.004: return None
    e20p, e50p = ema(closes[:-2], EMA_SHORT), ema(closes[:-2], EMA_LONG)
    sep_prev = abs(e20p - e50p) / e50p if e50p else 0.0
    if sep < sep_prev: return None
    if len(closes) >= 4:
        recent = (closes[-1] - closes[-4]) / closes[-4]
        if trend == "short" and recent > 0.001: return None
        if trend == "long" and recent < -0.001: return None
    r = rsi14(closes)
    rsi_short_min = 35 if btc_trend == "bear" else 45
    if trend == "short" and r < rsi_short_min: return None
    if trend == "long" and r > 55: return None
    if btc_trend == "bull" and trend == "short": return None
    if btc_trend == "bear" and trend == "long": return None

    sl_dist = a * SL_MULT
    tp_dist = max(a * TP_MULT, price * MIN_TP_PCT)
    if trend == "long":
        stop, target = price - sl_dist, price + tp_dist
    else:
        stop, target = price + sl_dist, price - tp_dist
    stop_pct, tp_pct = sl_dist / price, tp_dist / price
    fee_mult = tp_pct / FEE_RT
    ev = ASSUMED_WR * (tp_pct - FEE_RT) - (1 - ASSUMED_WR) * (stop_pct + FEE_RT)
    grade = "A" if (fee_mult >= 8.0 and ev > 0) else "B"
    return Signal(pair=pair, direction=trend, timeframe="1H", ts=rows[-1][0],
                  entry=price, stop=stop, target=target, stop_pct=stop_pct,
                  target_pct=tp_pct, r_multiple=tp_dist / sl_dist, ev_pct=ev,
                  fee_mult=fee_mult, htf_bias=btc_trend, atr_pct=a / price,
                  rsi=r, grade=grade)


CLUSTERS = {
    "BTC-USDT": "btc", "ETH-USDT": "eth",
    "SOL-USDT": "l1", "SUI-USDT": "l1",
    "XRP-USDT": "payments", "LINK-USDT": "defi",
}


def group_of(pair):
    return CLUSTERS.get(pair, pair)
