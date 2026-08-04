#!/usr/bin/env python3
"""BACKTEST — replays v3 logic over historical candles. No lookahead."""
import os
os.environ.setdefault("ENV_FILE","/root/trading/.env.live")
import asyncio, sys, aiohttp
sys.path.insert(0,'/root/trading')
from love import config

PAIRS = ["BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","LINK-USDT","SUI-USDT",
         "DOGE-USDT","AVAX-USDT","ADA-USDT","LTC-USDT"]
FEE = 0.0012   # round trip taker

async def fetch(pair, limit=1000, bar="1H"):
    out=[]; after=None
    async with aiohttp.ClientSession() as s:
        for _ in range(10):
            url=f"https://openapi.blofin.com/api/v1/market/candles?instId={pair}&bar={bar}&limit=100"
            if after: url+=f"&after={after}"
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                j=await r.json()
            d=j.get("data") or []
            if not d: break
            out+=d
            after=min(int(x[0]) for x in d)
            if len(out)>=limit: break
            await asyncio.sleep(0.4)
    return sorted(out, key=lambda x:int(x[0]))

def ema(vals, n):
    if not vals: return 0
    k=2/(n+1); e=vals[0]
    for v in vals[1:]: e=v*k+e*(1-k)
    return e

def atr(c, n=14):
    if len(c)<n+1: return 0
    h=[float(x[2]) for x in c]; l=[float(x[3]) for x in c]; cl=[float(x[4]) for x in c]
    trs=[max(h[i]-l[i],abs(h[i]-cl[i-1]),abs(l[i]-cl[i-1])) for i in range(1,len(c))]
    return sum(trs[-n:])/n

def signal_at(candles, i):
    """v3 logic using ONLY candles[0:i+1]. Returns 'long'/'short'/None."""
    window = candles[max(0,i-69):i+1]
    if len(window) < 55: return None
    closes = [float(x[4]) for x in window]
    price = closes[-1]
    e20 = ema(closes, 20); e50 = ema(closes, 50)
    if e50 <= 0: return None

    # Filter: price near EMA50 (price_zone)
    if abs(price - e50)/e50 > config.ema_threshold: return None

    trend = "long" if e20 > e50 else "short"

    # REGIME: separation meaningful + widening
    sep = abs(e20-e50)/e50
    if sep < 0.004: return None
    e20p = ema(closes[:-2], 20); e50p = ema(closes[:-2], 50)
    sep_p = abs(e20p-e50p)/e50p if e50p else 0
    if sep < sep_p: return None

    # MOMENTUM GATE: last 3 candles confirm
    if len(closes) >= 4:
        recent = (closes[-1]-closes[-4])/closes[-4]
        if trend=="short" and recent > 0.001: return None
        if trend=="long"  and recent < -0.001: return None
    return trend

def run_pair(pair, candles):
    trades=[]; i=60; n=len(candles)
    while i < n-1:
        d = signal_at(candles, i)
        if not d:
            i+=1; continue
        entry = float(candles[i][4])
        a = atr(candles[max(0,i-20):i+1])
        atr_pct = a/entry if entry else 0
        sl = max(2.0*atr_pct, 0.008)
        tp = 2.5*sl
        sgn = 1 if d=="long" else -1
        # walk forward to see which hits first
        out=None
        for j in range(i+1, min(n, i+200)):
            hi=float(candles[j][2]); lo=float(candles[j][3])
            up=(hi-entry)/entry*sgn; dn=(lo-entry)/entry*sgn
            best=max(up,dn); worst=min(up,dn)
            if worst <= -sl: out=("stop", -sl, j); break
            if best >= tp:   out=("target", tp, j); break
        if out is None:
            last=float(candles[min(n-1,i+199)][4])
            out=("timeout",(last-entry)/entry*sgn, min(n-1,i+199))
        reason,ret,j = out
        trades.append({"pair":pair,"dir":d,"entry":entry,"ret":ret-FEE,
                       "reason":reason,"bars":j-i,"sl":sl,"tp":tp})
        i = j+1   # no overlapping trades on same pair
    return trades

async def main():
    all_t=[]
    for p in PAIRS:
        try:
            c = await fetch(p)
            if len(c) < 100: print(f"{p}: only {len(c)} candles, skip"); continue
            t = run_pair(p, c)
            all_t += t
            print(f"{p:<11} {len(c)} candles -> {len(t)} trades")
        except Exception as e:
            print(f"{p}: {e}")
    if not all_t: print("no trades"); return
    rets=[t["ret"] for t in all_t]
    w=[r for r in rets if r>0]; l=[r for r in rets if r<=0]
    print("\n"+"="*56)
    print(f"BACKTEST RESULT — {len(all_t)} trades")
    print("="*56)
    print(f"Win rate:   {len(w)}/{len(all_t)} = {len(w)/len(all_t)*100:.1f}%")
    print(f"Avg return: {sum(rets)/len(rets)*100:+.3f}% per trade (after fees)")
    if w: print(f"Avg win:    {sum(w)/len(w)*100:+.2f}%")
    if l: print(f"Avg loss:   {sum(l)/len(l)*100:+.2f}%")
    gw=sum(w); gl=abs(sum(l))
    print(f"Profit factor: {gw/gl:.2f}" if gl else "PF: inf")
    print(f"TOTAL return (sum of %): {sum(rets)*100:+.2f}%")
    print(f"On $1000 risking 2%/trade: ${sum(rets)*100*10:+.0f} (rough)")
    from collections import Counter
    print("\nExits:", dict(Counter(t["reason"] for t in all_t)))
    byd={}
    for t in all_t: byd.setdefault(t["dir"],[]).append(t["ret"])
    for k,v in byd.items():
        print(f"  {k:<6}{len(v):>4}t  {len([x for x in v if x>0])/len(v)*100:>5.1f}%WR  {sum(v)*100:+7.2f}%")

asyncio.run(main())
