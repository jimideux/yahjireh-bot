#!/usr/bin/env python3
"""DRY-TRADE RESOLVER — makes dry trades live like real ones.
Tracks each open dry journal trade against live price; closes it
when TP (+2.5%), SL (-1%), or the trail lock would have fired."""
import os
os.environ.setdefault("ENV_FILE", "/root/trading/.env.live")
import asyncio, sys, sqlite3, time
sys.path.insert(0, "/root/trading")
from exchange.blofin import BloFinClient
from love import config
import journal
from peace import get_trail_lock

DB = '/root/trading/journal.db'

async def resolve_once(client):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM trades WHERE mode='dry' AND status='open'").fetchall()]
    c.close()
    for t in rows:
        tick = await client.get_ticker(t['pair'])
        mark = float(tick.get('last', 0) or 0)
        if mark <= 0: continue
        entry = t['entry_price']; sz = t['size']
        d = 1 if t['direction'] == 'long' else -1
        upnl = (mark - entry) * sz * d
        move = (mark - entry) / entry * d   # signed % move in our favor
        journal.update_path(t['pair'], upnl)
        peak = max(t['peak_pnl'] or 0, upnl)
        lock = get_trail_lock(peak)
        # Exits must mirror the DEPLOYED strategy: ATR-based, same
        # formulas as peace.py/trend.py. sig_atr was recorded at entry;
        # fall back to the old fixed percentages only if it is missing.
        atr = float(t['sig_atr'] or 0)
        if atr > 0:
            tp_dist = max(atr * config.atr_tp_mult, entry * config.min_tp_pct)
            sl_dist = atr * config.atr_sl_mult
        else:
            tp_dist = entry * config.trend_tp_pct
            sl_dist = entry * config.trend_sl_pct
            print(f"[RESOLVER] {t['pair']}: no sig_atr, using pct fallback")
        favor = (mark - entry) * d   # signed $ move per unit in our favor
        reason = None
        if favor >= tp_dist:    reason = 'take-profit'
        elif favor <= -sl_dist: reason = 'stop-loss'
        elif lock is not None and upnl <= lock and peak > 0: reason = f'trail-lock ${lock:.0f}'
        if reason:
            fees = t['notional'] * 0.0012
            risk_usd = t['notional'] * (sl_dist / entry) if entry else 0
            journal.close_trade(t['pair'], mark, reason, upnl, fees=fees,
                                risk=risk_usd, mode='dry')
            print(f"[RESOLVER] {t['pair']} {t['direction']} CLOSED {reason} net ${upnl - fees:+.2f}")

async def main():
    client = BloFinClient()
    print("Resolver running — dry trades now live like real ones")
    while True:
        try:
            await resolve_once(client)
        except Exception as e:
            print(f"resolver err: {e}")
        await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
