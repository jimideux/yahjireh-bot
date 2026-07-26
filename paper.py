#!/usr/bin/env python3
"""Paper-trading engine: real live prices, simulated fills, honest fees."""
import sqlite3, time
from pathlib import Path
DB = '/root/trading/paper.db'
TAKER_FEE = 0.0006
MAKER_FEE = 0.0002
START_BALANCE = 1000.0

def _conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db(start=START_BALANCE):
    c = _conn()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK (id=1),
            balance REAL, start_balance REAL, created REAL);
        CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT, side TEXT, entry_price REAL, size REAL, notional REAL,
            leverage REAL, entry_fee REAL, opened REAL, strategy TEXT,
            stop_price REAL, target_price REAL, peak_pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT, side TEXT, entry_price REAL, exit_price REAL, size REAL,
            notional REAL, leverage REAL, gross_pnl REAL, fees REAL, net_pnl REAL,
            opened REAL, closed REAL, hold_min REAL, exit_reason TEXT, strategy TEXT);
    ''')
    row = c.execute("SELECT * FROM account WHERE id=1").fetchone()
    if not row:
        c.execute("INSERT INTO account VALUES (1,?,?,?)", (start, start, time.time()))
        print(f"Paper account created: ${start:,.2f}")
    else:
        print(f"Paper account exists: ${row['balance']:,.2f}")
    c.commit(); c.close()

def get_balance():
    c=_conn(); r=c.execute("SELECT balance FROM account WHERE id=1").fetchone(); c.close()
    return r['balance'] if r else 0

def get_open_positions():
    c=_conn(); rows=c.execute("SELECT * FROM positions WHERE status='open'").fetchall(); c.close()
    return [dict(r) for r in rows]

def open_position(pair, side, price, notional, leverage, strategy, stop=None, target=None, maker=False):
    fee_rate = MAKER_FEE if maker else TAKER_FEE
    size = notional / price
    entry_fee = notional * fee_rate
    c=_conn()
    bal = c.execute("SELECT balance FROM account WHERE id=1").fetchone()['balance']
    c.execute("UPDATE account SET balance=? WHERE id=1", (bal - entry_fee,))
    c.execute('''INSERT INTO positions (pair,side,entry_price,size,notional,leverage,
                 entry_fee,opened,strategy,stop_price,target_price)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
              (pair,side,price,size,notional,leverage,entry_fee,time.time(),strategy,stop,target))
    c.commit(); c.close()
    print(f"  PAPER OPEN {pair} {side} @ {price:.6g} | notional ${notional:.0f} | fee ${entry_fee:.2f}")
    return True

def close_position(pos_id, exit_price, reason, maker=False):
    fee_rate = MAKER_FEE if maker else TAKER_FEE
    c=_conn()
    p = c.execute("SELECT * FROM positions WHERE id=? AND status='open'", (pos_id,)).fetchone()
    if not p: c.close(); return None
    p = dict(p)
    direction = 1 if p['side']=='long' else -1
    gross = (exit_price - p['entry_price']) * p['size'] * direction
    exit_fee = p['notional'] * fee_rate
    hold_min = (time.time() - p['opened'])/60
    bal = c.execute("SELECT balance FROM account WHERE id=1").fetchone()['balance']
    c.execute("UPDATE account SET balance=? WHERE id=1", (bal + gross - exit_fee,))
    c.execute("UPDATE positions SET status='closed' WHERE id=?", (pos_id,))
    c.execute('''INSERT INTO trades (pair,side,entry_price,exit_price,size,notional,leverage,
                 gross_pnl,fees,net_pnl,opened,closed,hold_min,exit_reason,strategy)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (p['pair'],p['side'],p['entry_price'],exit_price,p['size'],p['notional'],
               p['leverage'],gross,p['entry_fee']+exit_fee,gross-exit_fee,p['opened'],
               time.time(),hold_min,reason,p['strategy']))
    c.commit(); c.close()
    print(f"  PAPER CLOSE {p['pair']} @ {exit_price:.6g} | net ${gross-exit_fee:+.2f} | {reason}")
    return gross - exit_fee

def stats():
    c=_conn()
    acct=c.execute("SELECT * FROM account WHERE id=1").fetchone()
    trades=c.execute("SELECT * FROM trades").fetchall()
    c.close()
    if not trades:
        print(f"Balance ${acct['balance']:,.2f} | no closed trades yet"); return
    pnls=[t['net_pnl'] for t in trades]; fees=sum(t['fees'] for t in trades)
    w=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    print(f"\n{'='*50}\nPAPER TRADING STATS\n{'='*50}")
    print(f"Balance:  ${acct['balance']:,.2f} (started ${acct['start_balance']:,.2f})")
    print(f"Net P&L:  ${acct['balance']-acct['start_balance']:+,.2f}")
    print(f"Trades:   {len(trades)} | Wins {len(w)} ({len(w)/len(trades)*100:.0f}%)")
    print(f"Fees:     ${fees:,.2f}")
    if w: print(f"Avg win:  ${sum(w)/len(w):+.2f}")
    if losses: print(f"Avg loss: ${sum(losses)/len(losses):+.2f}")

if __name__ == '__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='init':
        init_db(float(sys.argv[2]) if len(sys.argv)>2 else START_BALANCE)
    elif len(sys.argv)>1 and sys.argv[1]=='stats': stats()
    elif len(sys.argv)>1 and sys.argv[1]=='reset':
        Path(DB).unlink(missing_ok=True); init_db(); print("reset done")
    else: print("usage: paper.py [init [balance] | stats | reset]")
