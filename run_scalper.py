#!/usr/bin/env python3
"""Baseline scalper on LIVE prices, PAPER fills. Real fees, no real orders."""
import asyncio, sys, time
sys.path.insert(0, '/root/trading')
import paper
from scanner import evaluate, BLACKLIST
from exchange.blofin import BloFinClient

# Force LIVE market data (public reads only — no keys, no orders sent)
import exchange.blofin as bf
bf.BASE_URL = "https://openapi.blofin.com"

COMPOUND_PCT = 0.50
MAX_POSITIONS = 2
SL_PCT = 0.0033
LEVERAGE = 10
MID_LOCKS = [(12, 5), (8, 2)]   # peak$ -> lock$  (baseline values)
TIME_STOP_MIN = 15
cooldowns = {}

async def live_price(c, pair):
    t = await c.get_ticker(pair)
    return float(t.get('last', 0) or 0)

async def manage_positions(c):
    import sqlite3
    for p in paper.get_open_positions():
        px = await live_price(c, p['pair'])
        if px <= 0: continue
        direction = 1 if p['side']=='long' else -1
        upnl = (px - p['entry_price']) * p['size'] * direction
        age_min = (time.time() - p['opened'])/60
        # graduated floors: track peak, ratchet stop to lock profit
        peak = max(p.get('peak_pnl') or 0, upnl)
        if peak > (p.get('peak_pnl') or 0):
            cc=sqlite3.connect(paper.DB)
            cc.execute("UPDATE positions SET peak_pnl=? WHERE id=?",(peak,p['id']))
            cc.commit(); cc.close()
        for trig, lock in MID_LOCKS:
            if peak >= trig:
                # convert lock$ profit into a stop price
                lock_move = lock / (p['size'] * 1)  # price move that yields lock$ pnl
                floor_price = (p['entry_price'] + lock_move) if p['side']=='long' else (p['entry_price'] - lock_move)
                cur_stop = p['stop_price'] or 0
                better = (p['side']=='long' and floor_price>cur_stop) or (p['side']=='short' and (cur_stop==0 or floor_price<cur_stop))
                if better:
                    cc=sqlite3.connect(paper.DB)
                    cc.execute("UPDATE positions SET stop_price=? WHERE id=?",(floor_price,p['id']))
                    cc.commit(); cc.close()
                    print(f"  🔒 {p['pair']} floor ratcheted to +${lock} (peak ${peak:.2f})")
                break
        # refresh stop after possible ratchet
        fresh = [x for x in paper.get_open_positions() if x['id']==p['id']]
        stop_price = fresh[0]['stop_price'] if fresh else p['stop_price']
        # stop check
        if stop_price and ((p['side']=='long' and px<=stop_price) or
                           (p['side']=='short' and px>=stop_price)):
            paper.close_position(p['id'], px, 'stop/floor'); continue
        # time stop
        if age_min >= TIME_STOP_MIN:
            paper.close_position(p['id'], px, 'time-exit'); continue

async def main():
    c = BloFinClient()
    print(f"[{time.strftime('%H:%M:%S')}] scalper paper-runner | balance ${paper.get_balance():.2f}")
    cycle = 0
    while True:
        cycle += 1
        try:
            await manage_positions(c)
            open_pos = paper.get_open_positions()
            slots = MAX_POSITIONS - len(open_pos)
            held = {p['pair'] for p in open_pos}
            if slots > 0:
                ticks = await c._req('GET','/api/v1/market/tickers')
                tmap={}
                for t in (ticks or []):
                    try:
                        last=float(t.get('last',0) or 0); op=float(t.get('open24h',0) or 0)
                        v=float(t.get('volCurrency24h',0) or 0)*last
                        if last>0 and op>0 and t['instId'].endswith('-USDT'):
                            tmap[t['instId']]={'last':last,'macro':(last-op)/op*100,'vol':v}
                    except: pass
                cands=sorted([(p,d) for p,d in tmap.items()
                              if d['vol']>=20e6 and p not in BLACKLIST and abs(d['macro'])>=1.0
                              and p not in held],
                             key=lambda x:-abs(x[1]['macro']))[:15]
                for pair, td in cands:
                    if slots<=0: break
                    if time.time()-cooldowns.get(pair,0) < 1800: continue
                    d, reason = await evaluate(c, pair, td)
                    if d:
                        bal = paper.get_balance()
                        notional = bal * COMPOUND_PCT * LEVERAGE
                        price = td['last']
                        stop = price*(1-SL_PCT) if d=='long' else price*(1+SL_PCT)
                        paper.open_position(pair, d, price, notional, LEVERAGE, 'scalper', stop=stop)
                        cooldowns[pair]=time.time()
                        slots-=1
            bal=paper.get_balance()
            print(f"[{time.strftime('%H:%M:%S')}] cycle {cycle} | bal ${bal:.2f} | open {len(paper.get_open_positions())}/{MAX_POSITIONS}")
        except Exception as e:
            print(f"cycle error: {e}")
        await asyncio.sleep(30)

asyncio.run(main())
