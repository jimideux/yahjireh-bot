#!/usr/bin/env python3
"""
patch_peace_stops.py -- arms native exchange stop-losses. Closes §9.1.

THE GAP
-------
place_tpsl() exists in the client but is wired into nothing. Protection for an
open position depends entirely on peace.py polling every 5 seconds and issuing
a market close. If the process dies, the server reboots, or the network drops,
the position sits unprotected until a human notices.

THE FIX
-------
Every cycle, any bot-owned position without a resting stop on the exchange gets
one, at the ATR-derived disaster level peace.py already computes.

DIVISION OF LABOUR
------------------
  server-side stop : wide, static, "the process died" insurance. Lives on
                     BloFin's matching engine and survives anything happening
                     on this droplet.
  peace.py trailing: tight, dynamic, ratchets with profit. Requires the
                     process to be alive.

The exchange stop is deliberately NOT ratcheted alongside the trail. Doing so
means cancel-then-replace on every tightening, and each of those opens a window
with no stop at all -- trading a rare catastrophic failure for a frequent small
one. The trail already handles tightening while the process lives.

ORPHAN CLEANUP
--------------
close_market() cancels resting orders but not stops, so a bot-closed position
would leave its stop behind. A stale reduce-only stop is mostly inert, but it
can fire against a later position on the same pair. Now cancelled.

Usage:
    python3 patch_peace_stops.py --check
    python3 patch_peace_stops.py
"""

import ast
import os
import shutil
import sys
import time

TARGET = "/root/trading/peace.py"

PATCHES = [
    {
        "name":   "1. stop-state cache",
        "marker": "_armed_stops",
        "why":    "avoids a get_tpsl_orders call every 5s per position; BloFin "
                  "rate-limits (§10) and the answer rarely changes",
        "old":  '_profit_highs = {}  # tracks highest profit seen per position\n',
        "new":  '_profit_highs = {}  # tracks highest profit seen per position\n'
                '\n'
                '# inst_id -> {"id": tpsl_id, "trigger": float, "checked": ts}\n'
                '# In-memory only. A restart re-verifies against the exchange on the\n'
                '# next cycle, which is the correct source of truth anyway.\n'
                '_armed_stops = {}\n'
                'STOP_RECHECK_SECONDS = 60\n',
    },
    {
        "name":   "2. ensure_stop()",
        "marker": "async def ensure_stop",
        "why":    "arms a resting stop on the exchange for any owned position "
                  "that lacks one",
        "old":  'async def place_tp_order(inst_id, side, price, size, margin_mode, tick):\n',
        "new":  'async def ensure_stop(client, inst_id, side_is_long, sl_price, margin_mode):\n'
                '    """Guarantee a resting stop exists on the exchange for this position.\n'
                '\n'
                '    Returns True if a stop is known to be resting, False otherwise.\n'
                '    Never raises -- a failure here must not take down the exit engine,\n'
                '    because peace.py\'s own trailing logic is still protecting the\n'
                '    position while the process lives.\n'
                '    """\n'
                '    now = time.time()\n'
                '    cached = _armed_stops.get(inst_id)\n'
                '    if cached and (now - cached["checked"]) < STOP_RECHECK_SECONDS:\n'
                '        return True\n'
                '    try:\n'
                '        resting = await client.get_tpsl_orders(inst_id)\n'
                '    except Exception as e:\n'
                '        print(f"  stop check failed {inst_id}: {e}")\n'
                '        return False\n'
                '    if resting:\n'
                '        tid = resting[0].get("tpslId") or resting[0].get("algoId", "")\n'
                '        _armed_stops[inst_id] = {"id": tid, "trigger": sl_price,\n'
                '                                 "checked": now}\n'
                '        return True\n'
                '    side = "long" if side_is_long else "short"\n'
                '    try:\n'
                '        res = await client.place_tpsl(inst_id, side, sl_price,\n'
                '                                      margin_mode=margin_mode)\n'
                '    except Exception as e:\n'
                '        print(f"  stop arm error {inst_id}: {e}")\n'
                '        return False\n'
                '    if res == "DRYRUN":\n'
                '        print(f"  \\U0001F6AB DRY-RUN: {inst_id} has NO exchange stop "\n'
                '              f"(would arm @ ${sl_price:.4f})")\n'
                '        return False\n'
                '    if res:\n'
                '        _armed_stops[inst_id] = {"id": res, "trigger": sl_price,\n'
                '                                 "checked": now}\n'
                '        await send(f"\\U0001F6E1 <b>Stop Armed</b>\\n"\n'
                '                   f"\\U0001F4CC {inst_id}\\n"\n'
                '                   f"\\U0001F6D1 Trigger: ${sl_price:.4f}")\n'
                '        return True\n'
                '    print(f"  \\u26A0\\uFE0F  {inst_id} UNPROTECTED - stop could not be armed")\n'
                '    return False\n'
                '\n'
                'async def place_tp_order(inst_id, side, price, size, margin_mode, tick):\n',
    },
    {
        "name":   "3. arm the stop each cycle",
        "marker": "await ensure_stop(",
        "why":    "runs before the TP logic so a position is protected even if "
                  "take-profit placement fails",
        "old":  '    # Check and place TP order\n'
                '    orders    = await client.get_pending_orders(inst_id)\n',
        "new":  '    # Arm a resting stop on the exchange. This is the backstop for\n'
                '    # process death; the trailing logic above handles the live case.\n'
                '    await ensure_stop(client, inst_id, is_long, sl_price, margin_mode)\n'
                '\n'
                '    # Check and place TP order\n'
                '    orders    = await client.get_pending_orders(inst_id)\n',
    },
    {
        "name":   "4. cancel the stop when closing",
        "marker": "cancel_all_tpsl",
        "why":    "an orphaned reduce-only stop can fire against a later "
                  "position on the same pair",
        "old":  '    try:\n'
                '        await client.cancel_all_orders(inst_id)\n'
                '        await asyncio.sleep(0.3)\n',
        "new":  '    try:\n'
                '        await client.cancel_all_tpsl(inst_id)\n'
                '        await client.cancel_all_orders(inst_id)\n'
                '        _armed_stops.pop(inst_id, None)\n'
                '        await asyncio.sleep(0.3)\n',
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

    print("\n=== patch_peace_stops.py ===")
    print(f"target : {target}")
    print(f"size   : {len(original.encode('utf-8'))} bytes, "
          f"{original.count(chr(10))} lines\n")

    if "import ownership" not in original:
        print("ABORT: patch_peace.py has not been applied. Run it first.")
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
    print(f"written: {target} ({len(text.encode('utf-8'))} bytes)")
    print("\nRestore with:")
    print(f"    cp {backup} {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
