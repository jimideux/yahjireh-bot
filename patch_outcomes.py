#!/usr/bin/env python3
"""
patch_outcomes.py -- wires trade outcomes into the risk engine and makes the
dry-run resolver simulate the strategy that is actually deployed.

1. SINGLE-WRITER OUTCOME FEED (trend.py)
   The loss-streak breaker counted entries but never outcomes. peace.py and
   resolver.py are separate processes, so giving either its own RiskEngine
   would race the state file. Instead: journal.db is already the ledger every
   component writes closes to, and trend.py already owns the engine. trend.py
   now tails the journal each loop (closed rows with id > last seen) and
   feeds each net_pnl to record_close(). One process, one writer. Dry
   outcomes count toward the streak deliberately: in shadow mode they are
   the only outcome data the breaker can learn from.

2. RESOLVER SIMULATES THE DEPLOYED STRATEGY (resolver.py)
   The resolver closed dry trades at fixed +2.5% / -1% -- the constants the
   live path just moved away from. Every dry result would have been evidence
   about a strategy no longer running. journal.py stores sig_atr at entry
   (verified populated on rows 113/114), so the resolver now reconstructs
   the same ATR-based TP/SL the live exit path uses. Fallback to the old
   percentage constants only when sig_atr is missing, and says so.

3. MODE-FILTERED JOURNAL CLOSES (peace.py)
   close_market() closed the latest open journal row for the pair with no
   mode filter -- a live close could consume a dry row. Now mode="live".

Usage:
    python3 patch_outcomes.py --check
    python3 patch_outcomes.py
"""

import ast
import os
import shutil
import sys
import time

TREND    = "/root/trading/trend.py"
PEACE    = "/root/trading/peace.py"
RESOLVER = "/root/trading/resolver.py"

TREND_PATCHES = [
    {
        "name":   "T1. journal outcome feed",
        "marker": "async def feed_risk_outcomes",
        "old":  'async def main():\n',
        "new":  'async def feed_risk_outcomes(client, state):\n'
                '    """Tail journal.db for newly closed trades and feed each outcome to\n'
                '    the risk engine. trend.py is the SOLE writer of risk state -- other\n'
                '    processes write closes to the journal and this loop picks them up.\n'
                '    Never raises: an sqlite hiccup must not stop scanning."""\n'
                '    try:\n'
                '        import sqlite3\n'
                '        last = int(state.get("last_close_id", 0))\n'
                '        c = sqlite3.connect("/root/trading/journal.db")\n'
                '        c.row_factory = sqlite3.Row\n'
                '        rows = c.execute(\n'
                '            "SELECT id, pair, mode, net_pnl FROM trades "\n'
                '            "WHERE status=\'closed\' AND id > ? ORDER BY id", (last,)\n'
                '        ).fetchall()\n'
                '        c.close()\n'
                '        if not rows:\n'
                '            return\n'
                '        equity = await client.get_equity()\n'
                '        for r in rows:\n'
                '            pnl = float(r["net_pnl"] or 0.0)\n'
                '            _risk.record_close(pnl, equity)\n'
                '            state["last_close_id"] = int(r["id"])\n'
                '            streak = _risk.snapshot()["consecutive_losses"]\n'
                '            print(f"  [RISK] outcome {r[\'pair\']} [{r[\'mode\']}] "\n'
                '                  f"net ${pnl:+.2f} | loss streak: {streak}")\n'
                '    except Exception as e:\n'
                '        print(f"  [RISK] outcome feed error: {e}")\n'
                '\n'
                'async def main():\n',
    },
    {
        "name":   "T2. call the feed each loop",
        "marker": "await feed_risk_outcomes(client, state)",
        "old":  '            await monitor(client, state)\n'
                '            await scan(client, state)\n',
        "new":  '            await monitor(client, state)\n'
                '            await feed_risk_outcomes(client, state)\n'
                '            await scan(client, state)\n',
    },
]

PEACE_PATCHES = [
    {
        "name":   "P1. journal closes are mode-filtered",
        "marker": 'mode="live"',
        "old":  '                journal.close_trade(inst_id, _mk, reason, _u,\n'
                '                    fees=abs(float(_p.get("notional",0) or 0))*0.0012)\n',
        "new":  '                journal.close_trade(inst_id, _mk, reason, _u,\n'
                '                    fees=abs(float(_p.get("notional",0) or 0))*0.0012,\n'
                '                    mode="live")\n',
    },
]

RESOLVER_PATCHES = [
    {
        "name":   "R1. ATR-based exits matching the live strategy",
        "marker": "sig_atr",
        "old":  "        peak = max(t['peak_pnl'] or 0, upnl)\n"
                "        lock = get_trail_lock(peak)\n"
                "        reason = None\n"
                "        if move >= config.trend_tp_pct:      reason = 'take-profit'\n"
                "        elif move <= -config.trend_sl_pct:   reason = 'stop-loss'\n"
                "        elif lock is not None and upnl <= lock and peak > 0: reason = f'trail-lock ${lock:.0f}'\n"
                "        if reason:\n"
                "            fees = t['notional'] * 0.0012\n"
                "            journal.close_trade(t['pair'], mark, reason, upnl, fees=fees,\n"
                "                                risk=t['notional'] * config.trend_sl_pct, mode='dry')\n",
        "new":  "        peak = max(t['peak_pnl'] or 0, upnl)\n"
                "        lock = get_trail_lock(peak)\n"
                "        # Exits must mirror the DEPLOYED strategy: ATR-based, same\n"
                "        # formulas as peace.py/trend.py. sig_atr was recorded at entry;\n"
                "        # fall back to the old fixed percentages only if it is missing.\n"
                "        atr = float(t['sig_atr'] or 0)\n"
                "        if atr > 0:\n"
                "            tp_dist = max(atr * config.atr_tp_mult, entry * config.min_tp_pct)\n"
                "            sl_dist = atr * config.atr_sl_mult\n"
                "        else:\n"
                "            tp_dist = entry * config.trend_tp_pct\n"
                "            sl_dist = entry * config.trend_sl_pct\n"
                "            print(f\"[RESOLVER] {t['pair']}: no sig_atr, using pct fallback\")\n"
                "        favor = (mark - entry) * d   # signed $ move per unit in our favor\n"
                "        reason = None\n"
                "        if favor >= tp_dist:    reason = 'take-profit'\n"
                "        elif favor <= -sl_dist: reason = 'stop-loss'\n"
                "        elif lock is not None and upnl <= lock and peak > 0: reason = f'trail-lock ${lock:.0f}'\n"
                "        if reason:\n"
                "            fees = t['notional'] * 0.0012\n"
                "            risk_usd = t['notional'] * (sl_dist / entry) if entry else 0\n"
                "            journal.close_trade(t['pair'], mark, reason, upnl, fees=fees,\n"
                "                                risk=risk_usd, mode='dry')\n",
    },
]


def apply_set(target, patches, label):
    with open(target, "r", encoding="utf-8") as fh:
        original = fh.read()
    print(f"--- {label}: {target} ({len(original.encode('utf-8'))} bytes) ---\n")
    text = original
    fatal = False
    for p in patches:
        if p["marker"] in text:
            print(f"[=] {p['name']}\n    SKIP: already applied\n")
            continue
        n = text.count(p["old"])
        if n == 1:
            text = text.replace(p["old"], p["new"], 1)
            print(f"[+] {p['name']}\n    APPLY: anchor found\n")
        else:
            print(f"[!] {p['name']}\n    FAIL: anchor found {n}x (need exactly 1)\n")
            fatal = True
    return original, text, fatal


def main() -> int:
    check_only = "--check" in sys.argv

    with open(TREND, encoding="utf-8") as fh:
        if "_risk = risk.RiskEngine" not in fh.read():
            print("ABORT: patch_risk_wiring.py has not been applied. Run it first.")
            return 1

    print("\n=== patch_outcomes.py ===\n")
    results = []
    fatal = False
    for target, patches, label in [(TREND, TREND_PATCHES, "trend.py"),
                                   (PEACE, PEACE_PATCHES, "peace.py"),
                                   (RESOLVER, RESOLVER_PATCHES, "resolver.py")]:
        orig, new, f = apply_set(target, patches, label)
        results.append((target, orig, new))
        fatal = fatal or f

    if fatal:
        print("ABORT: anchors did not match. NOTHING was written to any file.")
        return 1

    for target, _, new in results:
        try:
            ast.parse(new)
        except SyntaxError as exc:
            print(f"ABORT: patched {target} is not valid Python: {exc}")
            print("NOTHING was written.")
            return 1
    print("syntax check: OK (all files)")

    if all(o == n for _, o, n in results):
        print("Nothing to do - all patches already applied.")
        return 0

    if check_only:
        print("\n--check mode: nothing written. Re-run without --check to apply.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for target, orig, new in results:
        if orig != new:
            shutil.copy2(target, f"{target}.bak-{stamp}")
    for target, orig, new in results:
        if orig != new:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(new)
            print(f"written: {target} ({len(new.encode('utf-8'))} bytes)")

    print(f"\nRestore any file with: cp <file>.bak-{stamp} <file>")
    print("Restart: systemctl restart sniper-trend sniper-peace sniper-resolver")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
