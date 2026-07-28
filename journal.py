#!/usr/bin/env python3
"""TRADE JOURNAL — every trade, full context, entry to exit."""
import sqlite3, time, json
DB = '/root/trading/journal.db'

def _c():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    c = _c()
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT, pair TEXT, direction TEXT,
        entry_time REAL, entry_price REAL, size REAL, notional REAL,
        leverage REAL, margin REAL,
        sig_ema20 REAL, sig_ema50 REAL, sig_rsi REAL, sig_atr REAL,
        sig_dist_pct REAL, sig_vol_ratio REAL, sig_btc_trend TEXT,
        exit_time REAL, exit_price REAL, exit_reason TEXT, duration_min REAL,
        gross_pnl REAL, fees REAL, net_pnl REAL, r_multiple REAL,
        peak_pnl REAL DEFAULT 0, min_pnl REAL DEFAULT 0,
        equity_before REAL, equity_after REAL,
        status TEXT DEFAULT 'open')''')
    c.commit(); c.close()

def open_trade(mode, pair, direction, price, size, notional, leverage, margin,
               sig=None, equity=None):
    s = sig or {}
    c = _c()
    cur = c.execute('''INSERT INTO trades
        (mode,pair,direction,entry_time,entry_price,size,notional,leverage,margin,
         sig_ema20,sig_ema50,sig_rsi,sig_atr,sig_dist_pct,sig_vol_ratio,sig_btc_trend,
         equity_before,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')''',
        (mode,pair,direction,time.time(),price,size,notional,leverage,margin,
         s.get('ema20'),s.get('ema50'),s.get('rsi'),s.get('atr'),
         s.get('dist_pct'),s.get('vol_ratio'),s.get('btc_trend'),equity))
    c.commit(); tid = cur.lastrowid; c.close()
    return tid

def update_path(pair, upnl):
    c = _c()
    r = c.execute("SELECT id,peak_pnl,min_pnl FROM trades WHERE pair=? AND status='open' ORDER BY id DESC LIMIT 1",(pair,)).fetchone()
    if r:
        peak = max(r['peak_pnl'] or 0, upnl)
        mn = min(r['min_pnl'] or 0, upnl)
        c.execute("UPDATE trades SET peak_pnl=?,min_pnl=? WHERE id=?",(peak,mn,r['id']))
        c.commit()
    c.close()

def close_trade(pair, exit_price, reason, gross_pnl, fees=0.0, equity_after=None, risk=20.0, mode=None):
    c = _c()
    if mode:
        r = c.execute("SELECT * FROM trades WHERE pair=? AND status='open' AND mode=? ORDER BY id DESC LIMIT 1",(pair,mode)).fetchone()
    else:
        r = c.execute("SELECT * FROM trades WHERE pair=? AND status='open' ORDER BY id DESC LIMIT 1",(pair,)).fetchone()
    if not r: c.close(); return None
    net = gross_pnl - fees
    dur = (time.time() - r['entry_time'])/60
    rmult = net / risk if risk else 0
    c.execute('''UPDATE trades SET exit_time=?,exit_price=?,exit_reason=?,duration_min=?,
                 gross_pnl=?,fees=?,net_pnl=?,r_multiple=?,equity_after=?,status='closed'
                 WHERE id=?''',
              (time.time(),exit_price,reason,dur,gross_pnl,fees,net,rmult,equity_after,r['id']))
    c.commit(); c.close()
    return net

def report():
    c = _c()
    rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE status='closed'").fetchall()]
    openn = c.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    c.close()
    if not rows:
        print(f"No closed trades yet ({openn} open)"); return
    n=len(rows); pnls=[r['net_pnl'] or 0 for r in rows]
    w=[p for p in pnls if p>0]; l=[p for p in pnls if p<0]
    gw=sum(w); gl=abs(sum(l)); fees=sum(r['fees'] or 0 for r in rows)
    print("="*56)
    print(f"TRADE JOURNAL — {n} closed, {openn} open")
    print("="*56)
    print(f"Net P&L:     ${sum(pnls):+,.2f}   (fees ${fees:,.2f})")
    print(f"Win rate:    {len(w)}/{n} = {len(w)/n*100:.0f}%")
    print(f"Avg win:     ${gw/len(w):+.2f}" if w else "Avg win: n/a")
    print(f"Avg loss:    ${-gl/len(l):+.2f}" if l else "Avg loss: n/a")
    print(f"Profit factor: {gw/gl:.2f}" if gl else "Profit factor: inf")
    print(f"Expectancy:  ${sum(pnls)/n:+.2f}/trade")
    print(f"Avg R:       {sum(r['r_multiple'] or 0 for r in rows)/n:+.2f}R")
    def group(key, label):
        d={}
        for r in rows:
            k=r.get(key) or '?'
            d.setdefault(k,[]).append(r['net_pnl'] or 0)
        print(f"\n{label}:")
        for k,v in sorted(d.items(), key=lambda x:-sum(x[1])):
            wr=len([p for p in v if p>0])/len(v)*100
            print(f"  {str(k):<16}{len(v):>3}t {wr:>3.0f}%WR  ${sum(v):+8.2f}")
    group('pair','BY PAIR')
    group('direction','BY DIRECTION')
    group('exit_reason','BY EXIT REASON')
    # MFE analysis - money left on table
    mfe=[(r['peak_pnl'] or 0)-(r['net_pnl'] or 0) for r in rows if (r['peak_pnl'] or 0)>0]
    if mfe:
        print(f"\nLEFT ON TABLE: avg ${sum(mfe)/len(mfe):.2f}/trade (peak vs actual exit)")
    # hour analysis
    import datetime
    hrs={}
    for r in rows:
        h=datetime.datetime.utcfromtimestamp(r['entry_time']).hour
        hrs.setdefault(h,[]).append(r['net_pnl'] or 0)
    print("\nBY HOUR (UTC):")
    for h in sorted(hrs):
        v=hrs[h]; print(f"  {h:02d}:00  {len(v):>2}t  ${sum(v):+7.2f}")

if __name__ == '__main__':
    import sys
    init()
    if len(sys.argv)>1 and sys.argv[1]=='report': report()
    else: print("journal ready — use: journal.py report")
