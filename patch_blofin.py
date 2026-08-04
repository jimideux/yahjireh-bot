#!/usr/bin/env python3
"""
patch_blofin.py -- prerequisites for wiring native exchange stops (§9.1).

Three defects block server-side stop-losses:

1. place_tpsl() hardcodes marginMode="cross", but trend.py opens positions
   with marginMode="isolated". A stop sent in the wrong margin mode cannot
   protect the position it is meant to protect.

2. The dry-run guard in _req() tests for "/trade/" in the path, so POSTs to
   /api/v1/account/set-leverage were never blocked. §7 describes the guard as
   covering live transmission; its actual boundary was narrower. This is why
   trend.py's set_leverage() reaches BloFin and returns 152404 even with
   LIVE_TRADING_ENABLED=false.

3. cancel_all_tpsl() counts the "DRYRUN" sentinel string as a successful
   cancellation -- the same truthy-string defect that made peace.py report
   phantom take-profits.

Also: place_tpsl accepted a `size` argument and ignored it, since the body
hardcoded "-1". Now honoured, with "-1" (entire position) as the default.

Usage:
    python3 patch_blofin.py --check
    python3 patch_blofin.py
"""

import ast
import os
import shutil
import sys
import time

TARGET = "/root/trading/exchange/blofin.py"

PATCHES = [
    {
        "name":   "1. dry-run guard covers account mutations, not just /trade/",
        "marker": "_mutating",
        "why":    "POSTs to /api/v1/account/set-leverage bypassed the guard "
                  "entirely and fired for real while trading was disabled",
        "old":  '        if method == "POST" and "/trade/" in path and not LIVE_TRADING_ENABLED:\n',
        "new":  '        # The guard must cover order transmission AND account mutation.\n'
                '        # "/account/" sat outside it, so set-leverage POSTs reached\n'
                '        # BloFin even with LIVE_TRADING_ENABLED=false.\n'
                '        _mutating = "/trade/" in path or "/account/" in path\n'
                '        if method == "POST" and _mutating and not LIVE_TRADING_ENABLED:\n',
    },
    {
        "name":   "2. place_tpsl uses the position's real margin mode",
        "marker": "margin_mode=None",
        "why":    "trend.py opens isolated positions; a cross-mode stop cannot "
                  "protect them",
        "old":  '    async def place_tpsl(self, inst_id, side, sl_trigger, size=None):\n',
        "new":  '    async def place_tpsl(self, inst_id, side, sl_trigger, size=None,\n'
                '                         margin_mode=None):\n',
    },
    {
        "name":   "3. resolve margin mode from the open position",
        "marker": "no open position to read marginMode",
        "why":    "callers should not have to know the mode; read it off the "
                  "position being protected",
        "old":  '        inst = await self.get_instrument(inst_id)\n'
                '        tick = float(inst.get("tickSize","0.01"))\n'
                '        # close side is opposite of position side\n'
                '        close_side = "sell" if side == "long" else "buy"\n'
                '        body = {\n'
                '            "instId": inst_id, "marginMode": "cross", "positionSide": "net",\n'
                '            "side": close_side,\n'
                '            "slTriggerPrice": round_price(sl_trigger, tick),\n'
                '            "slOrderPrice": "-1",     # -1 = market order on trigger\n'
                '            "size": "-1",             # -1 = entire position\n',
        "new":  '        if margin_mode is None:\n'
                '            for _p in await self.get_positions():\n'
                '                if _p.get("instId") == inst_id:\n'
                '                    margin_mode = _p.get("marginMode")\n'
                '                    break\n'
                '        if margin_mode is None:\n'
                '            print(f"  Cannot arm stop for {inst_id}: no open position "\n'
                '                  f"to read marginMode from")\n'
                '            return None\n'
                '        inst = await self.get_instrument(inst_id)\n'
                '        tick = float(inst.get("tickSize","0.01"))\n'
                '        # close side is opposite of position side\n'
                '        close_side = "sell" if side == "long" else "buy"\n'
                '        body = {\n'
                '            "instId": inst_id, "marginMode": margin_mode,\n'
                '            "positionSide": "net",\n'
                '            "side": close_side,\n'
                '            "slTriggerPrice": round_price(sl_trigger, tick),\n'
                '            "slOrderPrice": "-1",     # -1 = market order on trigger\n'
                '            "size": "-1" if size is None else str(size),\n',
    },
    {
        "name":   "4. cancel_all_tpsl stops counting DRYRUN as a cancel",
        "marker": "res is True",
        "why":    'cancel_tpsl returns the string "DRYRUN" when blocked; a bare '
                  "truthiness test reported it as a successful cancellation",
        "old":  '        for o in orders:\n'
                '            tid = o.get("tpslId") or o.get("algoId","")\n'
                '            if tid and await self.cancel_tpsl(inst_id, tid):\n'
                '                count += 1\n'
                '            await asyncio.sleep(0.1)\n',
        "new":  '        for o in orders:\n'
                '            tid = o.get("tpslId") or o.get("algoId","")\n'
                '            if not tid:\n'
                '                continue\n'
                '            res = await self.cancel_tpsl(inst_id, tid)\n'
                '            # cancel_tpsl returns the STRING "DRYRUN" when blocked.\n'
                '            # Truthiness alone would count that as a real cancel.\n'
                '            if res is True:\n'
                '                count += 1\n'
                '            await asyncio.sleep(0.1)\n',
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

    print("\n=== patch_blofin.py ===")
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
    print("\nNOTE: restart services after this - the module is imported at startup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
