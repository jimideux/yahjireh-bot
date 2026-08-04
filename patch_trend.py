#!/usr/bin/env python3
"""
patch_trend.py -- wires the ownership registry into trend.py.

Same safety model as patch_peace.py: nothing is written unless every anchor
matches exactly once and the result parses. Idempotent via markers.

Usage:
    python3 patch_trend.py --check
    python3 patch_trend.py
"""

import ast
import os
import shutil
import sys
import time

TARGET = "/root/trading/trend.py"

PATCHES = [
    {
        "name":   "1. import ownership",
        "marker": "import ownership",
        "why":    "trend.py records which positions it opened",
        "old":  'from declog import log_decision\n',
        "new":  'from declog import log_decision\nimport ownership\n',
    },
    {
        "name":   "2. claim the position on confirmed entry",
        "marker": "ownership.claim",
        "why":    "the claim is what tells peace.py this position is the bot's; "
                  "it is written only after BloFin returns an orderId, never before",
        "old":  '            if oid:\n'
                '                print(f"  ✅ Trend entry: {pair} {direction} @ {entry_px:.4f} size={size_str}")\n'
                '                return {"pair": pair, "direction": direction,\n',
        "new":  '            if oid:\n'
                '                print(f"  ✅ Trend entry: {pair} {direction} @ {entry_px:.4f} size={size_str}")\n'
                '                # Claim ONLY after the exchange confirms. A claim on a\n'
                '                # rejected order would make peace.py adopt whatever\n'
                '                # happens to be open on this pair.\n'
                '                if not ownership.claim(pair, direction, "live",\n'
                '                                       entry_px, oid):\n'
                '                    print(f"  \\u26A0\\uFE0F  {pair} claim did NOT persist - "\n'
                '                          f"peace.py will not manage this position")\n'
                '                return {"pair": pair, "direction": direction,\n',
    },
    {
        "name":   "3. slots count bot positions only",
        "marker": "ownership.reconcile",
        "why":    "manual trades were consuming trend slots because the filter "
                  "was marginMode=='isolated' rather than actual ownership",
        "old":  '    positions  = await client.get_positions()\n'
                '    # Only count isolated margin positions as trend slots\n'
                '    isolated   = [p for p in positions if p.get("marginMode","cross")=="isolated"]\n'
                '    live_pairs = {p.get("instId") for p in isolated}\n'
                '    open_pairs = [t["pair"] for t in state["open_trades"]]\n'
                '    slots      = config.trend_max_slots - len(live_pairs)\n'
                '    if slots <= 0:\n'
                '        print(f"  [TREND] {len(live_pairs)} slots full")\n'
                '        return\n'
                '    equity = await client.get_equity()\n'
                '    print(f"[TREND] Scan | equity=${equity:.2f} | slots={config.trend_max_slots-len(live_pairs)}")\n',
        "new":  '    positions  = await client.get_positions()\n'
                '    released   = ownership.reconcile([p.get("instId","") for p in positions])\n'
                '    if released:\n'
                '        print(f"  [TREND] released closed claims: {\', \'.join(released)}")\n'
                '    # Only BOT-OWNED positions consume trend slots. Manual trades on\n'
                '    # the same account are not the bot\'s business and must not reduce\n'
                '    # its capacity. Union with state["open_trades"] so a claim that\n'
                '    # failed to persist cannot cause double-entry.\n'
                '    live_pairs = {p.get("instId") for p in positions\n'
                '                  if ownership.is_owned(p.get("instId",""))}\n'
                '    open_pairs = [t["pair"] for t in state["open_trades"]]\n'
                '    occupied   = live_pairs | set(open_pairs)\n'
                '    slots      = config.trend_max_slots - len(occupied)\n'
                '    if slots <= 0:\n'
                '        print(f"  [TREND] {len(occupied)} slots full")\n'
                '        return\n'
                '    equity = await client.get_equity()\n'
                '    print(f"[TREND] Scan | equity=${equity:.2f} | slots={slots}")\n',
    },
]


def main() -> int:
    check_only = "--check" in sys.argv
    target = TARGET
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            target = a

    if not os.path.exists(target):
        print(f"ABORT: {target} does not exist")
        return 1

    with open(target, "r", encoding="utf-8") as fh:
        original = fh.read()

    print("\n=== patch_trend.py ===")
    print(f"target : {target}")
    print(f"size   : {len(original)} bytes, {original.count(chr(10))} lines\n")

    text = original
    plan = []
    fatal = False

    for p in PATCHES:
        if p["marker"] in text:
            plan.append((p, "SKIP", "already applied (marker present)"))
            continue
        n_old = text.count(p["old"])
        if n_old == 1:
            plan.append((p, "APPLY", "anchor found"))
            text = text.replace(p["old"], p["new"], 1)
        elif n_old == 0:
            plan.append((p, "FAIL", "anchor NOT FOUND - file differs from expected"))
            fatal = True
        else:
            plan.append((p, "FAIL", f"anchor found {n_old}x - ambiguous, refusing"))
            fatal = True

    for p, status, note in plan:
        mark = {"APPLY": "[+]", "SKIP": "[=]", "FAIL": "[!]"}[status]
        print(f"{mark} {p['name']}")
        print(f"    {status}: {note}")
        if status == "APPLY":
            print(f"    why: {p['why']}")
        print()

    if fatal:
        print("ABORT: one or more anchors did not match. Nothing was written.")
        return 1

    if text == original:
        print("Nothing to do - all patches already applied.")
        return 0

    try:
        ast.parse(text)
        print("syntax check: OK")
    except SyntaxError as exc:
        print(f"ABORT: patched result is not valid Python: {exc}")
        return 1

    if check_only:
        print("\n--check mode: nothing written. Re-run without --check to apply.")
        return 0

    backup = f"{target}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(target, backup)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)

    print(f"\nbackup : {backup}")
    print(f"written: {target} ({len(text)} bytes)")
    print("\nRestore with:")
    print(f"    cp {backup} {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
