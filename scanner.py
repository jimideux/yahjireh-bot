#!/usr/bin/env python3
"""Scalper scanner + 12-filter gauntlet. READ-ONLY: scans and prints, never trades."""
import asyncio, sys, time
sys.path.insert(0, '/root/trading')
from exchange.blofin import BloFinClient

BLACKLIST = {'ASTER-USDT','ZEC-USDT','PUMP-USDT','PENGU-USDT','PEPE-USDT',
             'WIF-USDT','NEAR-USDT','WLD-USDT','BEAT-USDT'}
VOL_MIN = 20_000_000
ATR_MIN, ATR_MAX = 0.005, 0.010

def rsi(closes, n=14):
    if len(closes) < n+1: return 50
    g=l=0
    for i in range(1,n+1):
        d=closes[i]-closes[i-1]; g+=max(d,0); l+=max(-d,0)
    ag,al=g/n,l/n
    for i in range(n+1,len(closes)):
        d=closes[i]-closes[i-1]
        ag=(ag*(n-1)+max(d,0))/n; al=(al*(n-1)+max(-d,0))/n
    return 100 if al==0 else 100-100/(1+ag/al)

def atr_pct(h,l,c,n=14):
    if len(c)<n+1: return 0
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    a=sum(trs[:n])/n
    for t in trs[n:]: a=(a*(n-1)+t)/n
    return a/c[-1] if c[-1] else 0

def adx(h,l,c,n=14):
    if len(c)<2*n+1: return 0
    pdm,ndm,trs=[],[],[]
    for i in range(1,len(c)):
        up,dn=h[i]-h[i-1],l[i-1]-l[i]
        pdm.append(up if up>dn and up>0 else 0)
        ndm.append(dn if dn>up and dn>0 else 0)
        trs.append(max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])))
    atr_=sum(trs[:n]); p=sum(pdm[:n]); m=sum(ndm[:n]); dxs=[]
    for i in range(n,len(trs)):
        atr_=atr_-atr_/n+trs[i]; p=p-p/n+pdm[i]; m=m-m/n+ndm[i]
        pdi=100*p/atr_ if atr_ else 0; ndi=100*m/atr_ if atr_ else 0
        dxs.append(100*abs(pdi-ndi)/(pdi+ndi) if pdi+ndi else 0)
    if len(dxs)<n: return 0
    a=sum(dxs[:n])/n
    for d in dxs[n:]: a=(a*(n-1)+d)/n
    return a

async def evaluate(c, inst, td):
    pair = inst
    if pair in BLACKLIST: return None, 'blacklisted'
    vol = td['vol']
    if vol < VOL_MIN: return None, f'volume ${vol/1e6:.0f}M < $20M'
    candles = await c.get_candles(pair, bar='5m', limit=60)
    if len(candles) < 35: return None, 'insufficient candles'
    rows = sorted(candles, key=lambda x:int(x[0]))
    h=[float(x[2]) for x in rows]; l=[float(x[3]) for x in rows]
    cl=[float(x[4]) for x in rows]; o=[float(x[1]) for x in rows]
    a=atr_pct(h,l,cl); ax=adx(h,l,cl); r=rsi(cl)
    m15=(cl[-1]/cl[-4]-1)*100 if len(cl)>=4 else 0
    macro=td['macro']
    rng=h[-1]-l[-1]; body=abs(cl[-1]-o[-1])/rng if rng>0 else 0
    if a<ATR_MIN: return None, f'ATR {a*100:.2f}% < 0.5%'
    if a>ATR_MAX: return None, f'ATR {a*100:.2f}% > 1.0% (too hot)'
    if ax>25: return None, f'ADX {ax:.0f} > 25 (trending)'
    if abs(m15)>=3: return None, f'15m {m15:+.1f}% too extreme (knife)'
    if macro>=5 or m15>=1: d='short'
    elif macro<=-5 or m15<=-1: d='long'
    else: return None, f'no direction (macro {macro:+.1f}%)'
    if d=='short' and not (55<=r<=75): return None, f'RSI {r:.0f} not 55-75 for short'
    if d=='long' and not (25<=r<=55): return None, f'RSI {r:.0f} not 25-55 for long'
    if body<0.30: return None, f'body {body*100:.0f}% < 30%'
    return d, f'✅ {d.upper()} | ATR {a*100:.2f}% ADX {ax:.0f} RSI {r:.0f} body {body*100:.0f}%'

async def main():
    c = BloFinClient()
    print('Fetching market tickers...')
    ticks = await c._req('GET','/api/v1/market/tickers')
    tmap = {}
    for t in (ticks or []):
        try:
            last=float(t.get('last',0) or 0); op=float(t.get('open24h',0) or 0)
            v=float(t.get('volCurrency24h',0) or 0)*last
            if last>0 and op>0 and t['instId'].endswith('-USDT'):
                tmap[t['instId']]={'last':last,'macro':(last-op)/op*100,'vol':v}
        except: pass
    cands = sorted([(p,d) for p,d in tmap.items()
                    if d['vol']>=10e6 and p not in BLACKLIST and abs(d['macro'])>=1.0],
                   key=lambda x:-abs(x[1]['macro']))[:20]
    print(f'Scanning top {len(cands)} movers...\n')
    passed=0
    for pair, td in cands:
        d, reason = await evaluate(c, pair, td)
        tag = '🎯' if d else '  '
        print(f'{tag} {pair:<14} {reason}')
        if d: passed+=1
    print(f'\n{"="*50}')
    print(f'SIGNALS FOUND: {passed} / {len(cands)} scanned')
    print(f'(read-only scan — no orders placed)')
    await c.close()

if __name__ == '__main__':
    asyncio.run(main())
