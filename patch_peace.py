#!/usr/bin/env python3
"""
patch_peace.py -- applies the ownership + return-value fixes to peace.py.

SAFETY MODEL
------------
Nothing is written unless EVERY anchor is found exactly once and the patched
result parses as valid Python. A partial application is worse than no
application, so failure at any stage aborts the whole run with the original
file untouched.

Idempotent: re-running after a successful patch reports "already applied"
rather than corrupting the file.

Usage:
    python3 patch_peace.py --check     # report only, write nothing
    python3 patch_peace.py             # apply
"""

import ast
import os
import shutil
import sys
import time

TARGET = "/root/trading/peace.py"

PATCHES = [
    {
        "name": "1. import ownership",
        "marker": 'import ownership',
        "why":  "peace.py needs the registry to know which positions are its own",
        "old":  'from joy import send\n',
        "new":  'from joy import send\nimport ownership\n',
    },
    {
        "name": "2. honest TP return value",
        "marker": 'row_code',
        "why":  "top-level code '0' means the REQUEST was well-formed, not that "
                "the order filled; per-order status lives in data[0]",
        "old":  '                d    = await r.json()\n'
                '                data = d.get("data", [{}])\n'
                '                oid  = data[0].get("orderId","") if data else ""\n'
                '                code = str(d.get("code","1"))\n'
                '                return bool(oid) or code == "0"\n',
        "new":  '                d    = await r.json()\n'
                '                rows = d.get("data") or [{}]\n'
                '                row  = rows[0] if rows else {}\n'
                '                oid  = row.get("orderId", "")\n'
                '                # Per-order code lives in data[0], NOT at the top\n'
                '                # level. Top-level "0" only means the request was\n'
                '                # well-formed -- the order itself can still fail.\n'
                '                row_code = str(row.get("code", "0"))\n'
                '                if oid and row_code == "0":\n'
                '                    return True\n'
                '                print(f"  TP rejected {inst_id}: code={row_code} "\n'
                '                      f"msg={row.get(\'msg\') or d.get(\'msg\')}")\n'
                '                return False\n',
    },
    {
        "name": "3. stop reporting DRYRUN as success",
        "marker": 'ok == "DRYRUN"',
        "why":  'place_tp_order returns the string "DRYRUN" when blocked, which '
                'is truthy -- so "if ok:" printed a success line for an order '
                'that was never sent',
        "old":  '        ok = await place_tp_order(inst_id, tp_side, tp_price, abs_size, margin_mode, tick)\n'
                '        if ok:\n',
        "new":  '        ok = await place_tp_order(inst_id, tp_side, tp_price, abs_size, margin_mode, tick)\n'
                '        if ok == "DRYRUN":\n'
                '            print(f"  \\U0001F6AB DRY-RUN: no TP exists for {inst_id} "\n'
                '                  f"(would be ${tp_price:.4f})")\n'
                '        elif ok is True:\n',
    },
    {
        "name": "4. only manage bot-owned positions",
        "marker": 'ignoring unowned',
        "why":  "BloFin returns every position on the account with no indication "
                "of what opened it; without this filter peace.py adopts manual trades",
        "old":  '            positions  = await client.get_positions()\n'
                '            total_upnl = sum(float(p.get("unrealizedPnl",0)) for p in positions)\n',
        "new":  '            all_positions = await client.get_positions()\n'
                '            positions = [p for p in all_positions\n'
                '                         if ownership.is_owned(p.get("instId", ""))]\n'
                '            skipped = [p.get("instId", "?") for p in all_positions\n'
                '                       if not ownership.is_owned(p.get("instId", ""))]\n'
                '            if skipped:\n'
                '                print(f"[PEACE] ignoring unowned: {\', \'.join(skipped)}")\n'
                '            total_upnl = sum(float(p.get("unrealizedPnl",0)) for p in positions)\n',
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

    print(f"\n=== patch_peace.py ===")
    print(f"target : {target}")
    print(f"size   : {len(original)} bytes, {original.count(chr(10))} lines\n")

    text = original
    plan = []
    fatal = False

    for p in PATCHES:
        # Marker check MUST come first. Several patches embed their own anchor
        # inside the replacement (patch 1 appends a line after the anchor), so
        # a found anchor does NOT mean the patch is unapplied -- testing the
        # anchor first would apply those twice.
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
        print("The file on disk is unchanged. Send me the current peace.py.")
        return 1

    if text == original:
        print("Nothing to do - all patches already applied.")
        return 0

    try:
        ast.parse(text)
        print("syntax check: OK")
    except SyntaxError as exc:
        print(f"ABORT: patched result is not valid Python: {exc}")
        print("Nothing was written.")
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
