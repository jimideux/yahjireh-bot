#!/usr/bin/env python3
"""
patch_risk_wiring.py -- wires risk.py into the entry path and makes the stop
actually ATR-based. Implements the 1.5% risk / ATR-stop decision.

WHAT THIS CHANGES
-----------------
peace.py: the operative stop was a hardcoded `entry * (1 -/+ atr_sl_mult *
0.005)` -- a fixed 0.5% that ignored the ATR computed four lines above it.
Consequences at current fee structure: fees were ~24% of risked money, and a
0.5% stop sits inside normal 1H bar range, so correct calls got shaken out.
The "original" stop branch now returns the ATR-derived sl_price that
check_position already computes -- the same level the server-side stop is
armed at, so the two protection layers finally agree.

trend.py: sizing was margin-based (15% of equity x 3x leverage = notional,
regardless of stop distance), which makes risk per trade an accident of
volatility. It is now risk-based: the strategy proposes entry/stop/target,
risk.evaluate_entry() decides whether the trade is allowed and how much
notional 1.5% of equity buys at that stop distance. Denials are logged with
the engine's reason. This also activates, at the entry gate: the daily
drawdown anchor, loss-streak cooldown, volatility no-trade band,
fee-adjusted RR floor, correlation-group caps, and portfolio exposure
limits from risk.py.

NOT INCLUDED (deliberately): record_close() wiring. peace.py runs in a
separate process; calling record_close there with a second engine instance
would race the state file. The loss-streak breaker therefore does not
accumulate yet -- it needs a single-writer design, next session.

REQUIRES: risk.py present in /root/trading (uploaded but never deployed
until now), patch_peace.py, patch_peace_stops.py, patch_trend.py,
patch_trend2.py all applied.

Usage:
    python3 patch_risk_wiring.py --check
    python3 patch_risk_wiring.py
"""

import ast
import os
import shutil
import sys
import time

PEACE = "/root/trading/peace.py"
TREND = "/root/trading/trend.py"
RISK  = "/root/trading/risk.py"

PEACE_PATCHES = [
    {
        "name":   "P1. get_trailing_sl receives the ATR stop",
        "marker": "def get_trailing_sl(entry, mark, tp_price, is_long, sl_price)",
        "old":  'def get_trailing_sl(entry, mark, tp_price, is_long):\n',
        "new":  'def get_trailing_sl(entry, mark, tp_price, is_long, sl_price):\n',
    },
    {
        "name":   "P2. long 'original' branch returns the ATR stop",
        "marker": "return sl_price, \"original\"  # ATR stop (long)",
        "old":  '        return entry * (1 - config.atr_sl_mult * 0.005), "original"\n',
        "new":  '        # Was entry * (1 - atr_sl_mult * 0.005): a fixed 0.5% that\n'
                '        # ignored ATR entirely. Now the ATR stop check_position\n'
                '        # computed -- the same level the exchange stop is armed at.\n'
                '        return sl_price, "original"  # ATR stop (long)\n',
    },
    {
        "name":   "P3. short 'original' branch returns the ATR stop",
        "marker": "return sl_price, \"original\"  # ATR stop (short)",
        "old":  '        return entry * (1 + config.atr_sl_mult * 0.005), "original"\n',
        "new":  '        return sl_price, "original"  # ATR stop (short)\n',
    },
    {
        "name":   "P4. call site passes sl_price",
        "marker": "dynamic_sl, sl_type = get_trailing_sl(entry, mark, tp_price, is_long, sl_price)",
        "old":  '    dynamic_sl, sl_type = get_trailing_sl(entry, mark, tp_price, is_long)\n',
        "new":  '    dynamic_sl, sl_type = get_trailing_sl(entry, mark, tp_price, is_long, sl_price)\n',
    },
]

TREND_PATCHES = [
    {
        "name":   "T1. import risk, build the engine",
        "marker": "_risk = risk.RiskEngine",
        "old":  'from declog import log_decision\nimport ownership\n',
        "new":  'from declog import log_decision\nimport ownership\n'
                'import risk\n'
                '# 1.5%% of equity at risk per trade (spec maximum, chosen 2026-08-04).\n'
                '# The engine is the isolated hard-constraint layer: it sizes from risk\n'
                '# and can only ever deny or shrink what the strategy proposes.\n'
                '_risk = risk.RiskEngine(limits=risk.RiskLimits(\n'
                '    risk_pct_per_trade=0.015, risk_pct_max=0.015))\n',
    },
    {
        "name":   "T2. anchor the daily drawdown on each scan",
        "marker": "_risk.roll_day_if_needed",
        "old":  '    equity = await client.get_equity()\n'
                '    print(f"[TREND] Scan | equity=${equity:.2f} | slots={slots}")\n',
        "new":  '    equity = await client.get_equity()\n'
                '    _risk.roll_day_if_needed(equity)\n'
                '    print(f"[TREND] Scan | equity=${equity:.2f} | slots={slots}")\n',
    },
    {
        "name":   "T3. risk gate at the entry decision",
        "marker": "_risk.evaluate_entry",
        "old":  '        margin    = round(equity * config.trend_margin_pct, 2)\n'
                '        await send(\n',
        "new":  '        # ---- risk gate: the strategy proposes, the engine disposes ----\n'
                '        atr = await client.get_atr(pair, period=config.atr_period,\n'
                '                                   bar=config.atr_bar)\n'
                '        px  = float(signal.get("price") or 0) or await client.get_mark_price(pair)\n'
                '        if atr <= 0 or px <= 0:\n'
                '            print(f"  [TREND] {pair}: no ATR/price - skipping")\n'
                '            continue\n'
                '        sl_dist = atr * config.atr_sl_mult\n'
                '        tp_dist = max(atr * config.atr_tp_mult, px * config.min_tp_pct)\n'
                '        if signal["direction"] == "long":\n'
                '            stop_px, tgt_px = px - sl_dist, px + tp_dist\n'
                '        else:\n'
                '            stop_px, tgt_px = px + sl_dist, px - tp_dist\n'
                '        open_pos = [{"pair": p.get("instId",""),\n'
                '                     "side": "long" if float(p.get("positions",0) or 0) > 0 else "short",\n'
                '                     "notional": abs(float(p.get("notional",0) or 0))}\n'
                '                    for p in positions if ownership.is_owned(p.get("instId",""))]\n'
                '        dec = _risk.evaluate_entry(\n'
                '            pair=pair, side=signal["direction"], entry_price=px,\n'
                '            stop_price=stop_px, equity=equity, target_price=tgt_px,\n'
                '            atr_pct=atr / px, open_positions=open_pos)\n'
                '        if not dec.allowed:\n'
                '            print(f"  [TREND] {pair}: risk denied - {dec.reason} {dec.detail}")\n'
                '            log_decision(pair, "denied:" + dec.reason, px,\n'
                '                         {"equity": round(equity, 2)})\n'
                '            continue\n'
                '        for w in dec.warnings:\n'
                '            print(f"  [TREND] {pair}: risk warning - {w}")\n'
                '        margin    = round(dec.notional / config.trend_leverage, 2)\n'
                '        await send(\n',
    },
    {
        "name":   "T4. execute_entry accepts the sized margin",
        "marker": "margin_override",
        "old":  'async def execute_entry(client, signal, equity):\n'
                '    pair      = signal["pair"]\n'
                '    direction = signal["direction"]\n'
                '    price     = await client.get_mark_price(pair)\n'
                '    if price <= 0: return None\n'
                '    margin    = round(equity * config.trend_margin_pct, 2)\n',
        "new":  'async def execute_entry(client, signal, equity, margin_override=None):\n'
                '    pair      = signal["pair"]\n'
                '    direction = signal["direction"]\n'
                '    price     = await client.get_mark_price(pair)\n'
                '    if price <= 0: return None\n'
                '    # Risk-sized margin from the gate; the old pct-of-equity path\n'
                '    # remains only as a fallback for direct callers.\n'
                '    margin    = margin_override if margin_override is not None \\\n'
                '                else round(equity * config.trend_margin_pct, 2)\n',
    },
    {
        "name":   "T5. pass the sized margin through",
        "marker": "margin_override=margin)",
        "old":  '        trade = await execute_entry(client, signal, equity)\n',
        "new":  '        trade = await execute_entry(client, signal, equity,\n'
                '                                    margin_override=margin)\n',
    },
    {
        "name":   "T6. count dry entries against rate limits",
        "marker": "_risk.record_entry()  # dry",
        "old":  '            state["cooldowns"][pair] = time.time() + 3600\n'
                '            slots -= 1; new_count += 1\n'
                '            continue\n',
        "new":  '            state["cooldowns"][pair] = time.time() + 3600\n'
                '            _risk.record_entry()  # dry entries hit the same rate caps\n'
                '            slots -= 1; new_count += 1\n'
                '            continue\n',
    },
    {
        "name":   "T7. count live entries against rate limits",
        "marker": "_risk.record_entry()  # live",
        "old":  '            state["open_trades"].append(trade)\n'
                '            slots -= 1\n'
                '            new_count += 1\n',
        "new":  '            state["open_trades"].append(trade)\n'
                '            _risk.record_entry()  # live\n'
                '            slots -= 1\n'
                '            new_count += 1\n',
    },
]


def apply_set(target, patches, label):
    with open(target, "r", encoding="utf-8") as fh:
        original = fh.read()
    print(f"--- {label}: {target} "
          f"({len(original.encode('utf-8'))} bytes) ---\n")
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
            print(f"[!] {p['name']}\n    FAIL: anchor found {n}x "
                  f"(need exactly 1)\n")
            fatal = True
    return original, text, fatal


def main() -> int:
    check_only = "--check" in sys.argv

    if not os.path.exists(RISK):
        print(f"ABORT: {RISK} does not exist.")
        print("risk.py was built earlier but never uploaded to the droplet.")
        print("Upload it first: scp ~/Downloads/risk.py prod-hai:/root/trading/risk.py")
        return 1

    for f, needle, dep in [(PEACE, "await ensure_stop(", "patch_peace_stops.py"),
                           (TREND, "all_open_pairs", "patch_trend2.py")]:
        with open(f, encoding="utf-8") as fh:
            if needle not in fh.read():
                print(f"ABORT: {dep} has not been applied to {f}. Run it first.")
                return 1

    print("\n=== patch_risk_wiring.py ===\n")
    p_orig, p_new, p_fatal = apply_set(PEACE, PEACE_PATCHES, "peace.py")
    t_orig, t_new, t_fatal = apply_set(TREND, TREND_PATCHES, "trend.py")

    if p_fatal or t_fatal:
        print("ABORT: anchors did not match. NOTHING was written to either file.")
        return 1

    for label, txt in [("peace.py", p_new), ("trend.py", t_new)]:
        try:
            ast.parse(txt)
        except SyntaxError as exc:
            print(f"ABORT: patched {label} is not valid Python: {exc}")
            print("NOTHING was written.")
            return 1
    print("syntax check: OK (both files)")

    if (p_new == p_orig) and (t_new == t_orig):
        print("Nothing to do - all patches already applied.")
        return 0

    if check_only:
        print("\n--check mode: nothing written. Re-run without --check to apply.")
        return 0

    # Write both or neither: back up both first, then write both.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for target in (PEACE, TREND):
        shutil.copy2(target, f"{target}.bak-{stamp}")
    for target, txt in [(PEACE, p_new), (TREND, t_new)]:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(txt)
        print(f"written: {target} ({len(txt.encode('utf-8'))} bytes)")

    print(f"\nRestore with:")
    print(f"    cp {PEACE}.bak-{stamp} {PEACE}")
    print(f"    cp {TREND}.bak-{stamp} {TREND}")
    print("\nRestart: systemctl restart sniper-trend sniper-peace")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
