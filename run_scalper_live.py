#!/usr/bin/env python3
"""LIVE scalper runner - dry-run guarded."""
import os
os.environ["ENV_FILE"] = "/root/trading/.env.live"
import asyncio, sys, time
sys.path.insert(0, '/root/trading')
from exchange.blofin import BloFinClient, LIVE_TRADING_ENABLED
from scanner import evaluate, BLACKLIST
COMPOUND_PCT = 0.50
MAX_POSITIONS = 2
SL_PCT = 0.0033
LEVERAGE = 10
MID_LOCKS = [(12, 5), (8, 2)]
TIME_STOP_MIN = 15
VOL_MIN = 20_000_000
cooldowns = {}

async def live_price(c, pair):
    t = await c.get_ticker(pair)
    return float(t.get('last', 0) or 0)

async def manage_positions(c):
    positions = await c.get_positions()
    for p in positions:
        sz = float(p.get('positions', 0) or 0)
        if abs(sz) == 0: continue
        pair = p['instId']
        entry = float(p.get('averagePrice', 0) or 0)
        upnl = float(p.get('unrealizedPnl', 0) or 0)
        side = 'long' if sz > 0 else 'short'
        print(f"    holding {pair} {side} sz={sz} entry={entry} uPnL=${upnl:+.2f}")

async def scan_and_signal(c):
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
                    if d['vol']>=VOL_MIN and p not in BLACKLIST and abs(d['macro'])>=1.0],
                   key=lambda x:-abs(x[1]['macro']))[:15]
    signals = []
    for pair, td in cands:
        if time.time()-cooldowns.get(pair,0) < 1800: continue
        d, reason = await evaluate(c, pair, td)
        if d:
            signals.append((pair, d, td['last']))
    return signals, len(cands)

async def main():
    c = BloFinClient()
    eq = await c.get_equity()
    print(f"[LIVE SCALPER] start | equity ${eq:.2f} | LIVE_TRADING={LIVE_TRADING_ENABLED}")
    cycle = 0
    while True:
        cycle += 1
        try:
            await manage_positions(c)
            positions = await c.get_positions()
            open_count = sum(1 for p in positions if abs(float(p.get('positions',0) or 0))>0)
            slots = MAX_POSITIONS - open_count
            if slots > 0:
                signals, n_cands = await scan_and_signal(c)
                for pair, d, price in signals:
                    if slots <= 0: break
                    eq = await c.get_equity()
                    notional = eq * COMPOUND_PCT * LEVERAGE
                    contracts = await c.calc_contracts(pair, notional)
                    side = 'buy' if d == 'long' else 'sell'
                    print(f"  >>> SIGNAL {pair} {d} @ {price}")
                    await c.place_order(pair, side, price=price, size=contracts)
                    cooldowns[pair] = time.time()
                    slots -= 1
            else:
                n_cands = 0
            print(f"[{time.strftime('%H:%M:%S')}] cycle {cycle} | eq ${eq:.2f} | open {open_count}/{MAX_POSITIONS} | cands {n_cands}")
        except Exception as e:
            print(f"cycle error: {e}")
        await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
