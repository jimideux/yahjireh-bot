#!/usr/bin/env python3
"""
patch_trend2.py -- fixes a regression introduced by patch_trend.py.

WHAT WENT WRONG
---------------
patch_trend.py changed `live_pairs` from "every isolated position" to "every
BOT-OWNED position" so that manual trades stop consuming trend slots. That part
is correct.

But the entry loop reused the same set for a different purpose:

    if pair in live_pairs or pair in open_pairs: continue

Narrowing live_pairs therefore also narrowed the entry guard, and pairs held
manually became eligible for new bot entries.

WHY THAT IS DANGEROUS
---------------------
execute_entry() sends positionSide="net". Under net mode BloFin does not open a
second position on a pair you already hold -- it MERGES the new order into the
existing one. So a bot entry on a manually-held pair would:

  1. increase the size of the user's own position,
  2. write an ownership claim for that pair,
  3. hand the combined position to peace.py, which would manage and eventually
     close it -- manual size included.

Ownership filtering cannot repair a merge after the fact. The guard has to
prevent the entry.

THE FIX
-------
Separate the two concepts:
  - slot accounting  -> bot-owned positions only  (patch_trend.py, correct)
  - entry eligibility -> ANY open position on the pair (this patch)
"""

import ast
import os
import shutil
import sys
import time

TARGET = "/root/trading/trend.py"

PATCHES = [
    {
        "name":   "1. no new entry on any pair that already has a position",
        "marker": "all_open_pairs",
        "why":    "BloFin positionSide='net' merges an order into an existing "
                  "position on the same pair; a bot entry on a manually-held "
                  "pair would absorb the user's trade and hand it to peace.py",
        "old":  '    new_count = 0\n'
                '    for pair in config.active_pairs:\n'
                '        if slots <= 0 or new_count >= config.max_new_entries_per_scan: break\n'
                '        if pair in live_pairs or pair in open_pairs: continue\n',
        "new":  '    new_count = 0\n'
                '    # Entry eligibility is NOT the same question as slot accounting.\n'
                '    # Slots count only bot-owned positions, but a pair is off-limits\n'
                '    # for a NEW entry if ANY position exists on it, whoever opened it:\n'
                '    # BloFin runs positionSide="net", so an order on a held pair merges\n'
                '    # into that position rather than creating a separate one. The merged\n'
                '    # position would then be claimed and closed by peace.py with the\n'
                '    # manual size included. A merge cannot be undone after the fact.\n'
                '    all_open_pairs = {p.get("instId") for p in positions}\n'
                '    for pair in config.active_pairs:\n'
                '        if slots <= 0 or new_count >= config.max_new_entries_per_scan: break\n'
                '        if pair in all_open_pairs or pair in open_pairs:\n'
                '            if pair in all_open_pairs and not ownership.is_owned(pair):\n'
                '                print(f"  [TREND] {pair}: position held externally - skipping")\n'
                '            continue\n',
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

    print("\n=== patch_trend2.py ===")
    print(f"target : {target}")
    print(f"size   : {len(original)} bytes, {original.count(chr(10))} lines\n")

    if "ownership.reconcile" not in original:
        print("ABORT: patch_trend.py has not been applied to this file.")
        print("This patch depends on it. Run patch_trend.py first.")
        return 1

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
        print("ABORT: anchor did not match. Nothing was written.")
        return 1

    if text == original:
        print("Nothing to do - already applied.")
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
