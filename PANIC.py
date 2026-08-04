#!/usr/bin/env python3
"""
PANIC.py -- emergency flatten for YahJireh. Closes the §9.2 gap.

DEFAULT IS REPORT-ONLY. Running this with no arguments changes nothing; it
prints what it would do and exits. Flattening requires --execute.

    python3 PANIC.py                          # report only, touches nothing
    python3 PANIC.py --execute                # flatten BOT-OWNED positions
    python3 PANIC.py --execute --include-manual   # flatten EVERYTHING

WHY IT ROUTES THROUGH _req()
----------------------------
peace.py posts directly to a hardcoded https://openapi.blofin.com, which means
it targets the live endpoint even when IS_DEMO=true, and it bypasses the
guard inside _req(). This module uses client._req() so it inherits both
BASE_URL selection and the LIVE_TRADING_ENABLED block. When trading is
disabled every send returns the string "DRYRUN" and nothing reaches BloFin.

WHY marginMode COMES FROM THE POSITION
--------------------------------------
BloFinClient.place_order() and place_tpsl() both hardcode marginMode="cross",
but trend.py opens positions with marginMode="isolated". Closing an isolated
position with a cross-mode body is a mismatch. Every body built here reads
marginMode off the position being closed.

ORDERING
--------
Per position: cancel stops, then cancel pending orders, then market close.
Resting reduce-only orders can block or partially fill a close, so they go
first. The PAUSED flag is written before anything else so that a running
trend.py does not open something new mid-flatten.

OWNERSHIP
---------
By default only positions claimed in bot_positions.json are touched. Manual
positions require --include-manual, stated explicitly on the command line.
The registry fails closed, so an unreadable registry means nothing is owned
and --execute alone will flatten nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time

sys.path.insert(0, "/root/trading")

# ---------------------------------------------------------------------------
# ENV FILE SELECTION -- MUST HAPPEN BEFORE exchange.blofin IS IMPORTED
# ---------------------------------------------------------------------------
# blofin.py runs load_dotenv(os.getenv("ENV_FILE", "/root/trading/.env")) at
# IMPORT time. The systemd units set ENV_FILE=/root/trading/.env.live, but a
# shell has no such variable -- so a hand-run script silently falls back to
# .env, which is the DEMO account (IS_DEMO=true).
#
# For an emergency flatten that is a critical failure: it would connect to
# demo, find nothing to close, report success, and leave live positions open.
# So PANIC picks the file itself and states which one out loud.
ENV_LIVE = "/root/trading/.env.live"
ENV_DEMO = "/root/trading/.env"

if "--demo" in sys.argv:
    os.environ["ENV_FILE"] = ENV_DEMO
elif not os.environ.get("ENV_FILE"):
    os.environ["ENV_FILE"] = ENV_LIVE

_ENV_FILE = os.environ["ENV_FILE"]

from exchange.blofin import (  # noqa: E402
    BloFinClient, LIVE_TRADING_ENABLED, BASE_URL, IS_DEMO,
)

try:
    import ownership
except ImportError:
    ownership = None

PAUSED_FLAG = "/root/trading/PAUSED"
SERVICES = ["sniper-trend", "sniper-peace"]


def _fmt_size(size: float) -> str:
    a = abs(size)
    return str(int(a)) if a == int(a) else str(round(a, 8))


def _sent(result) -> bool:
    """
    True only if something actually reached the exchange.

    _req() returns the STRING "DRYRUN" when blocked, and a bare truthiness
    test on that string reports success for an order that was never sent --
    the same defect that made peace.py log phantom take-profits.
    """
    return result is not None and result != "DRYRUN"


def write_paused() -> None:
    try:
        with open(PAUSED_FLAG, "w") as fh:
            fh.write(f"PANIC.py {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        print(f"  [PANIC] wrote {PAUSED_FLAG}")
    except OSError as exc:
        print(f"  [PANIC] WARN could not write PAUSED flag: {exc}")


def stop_services() -> None:
    for svc in SERVICES:
        try:
            subprocess.run(["systemctl", "stop", svc], check=False,
                           capture_output=True, timeout=20)
            print(f"  [PANIC] stopped {svc}")
        except Exception as exc:
            print(f"  [PANIC] WARN could not stop {svc}: {exc}")


async def flatten_one(client, pos: dict, execute: bool) -> dict:
    inst_id = pos.get("instId", "")
    size = float(pos.get("positions") or 0)
    margin_mode = pos.get("marginMode", "cross")
    upnl = float(pos.get("unrealizedPnl") or 0)
    mark = float(pos.get("markPrice") or 0)

    out = {"pair": inst_id, "size": size, "upnl": upnl, "mark": mark,
           "stops_cancelled": 0, "orders_cancelled": 0,
           "closed": False, "note": ""}

    if abs(size) < 1e-9:
        out["note"] = "zero size, skipped"
        return out

    if not execute:
        out["note"] = "report only"
        return out

    try:
        out["stops_cancelled"] = await client.cancel_all_tpsl(inst_id)
    except Exception as exc:
        out["note"] += f"tpsl cancel error: {exc}; "

    try:
        out["orders_cancelled"] = await client.cancel_all_orders(inst_id)
    except Exception as exc:
        out["note"] += f"order cancel error: {exc}; "

    await asyncio.sleep(0.3)

    body = {
        "instId":       inst_id,
        "marginMode":   margin_mode,   # from the position, never hardcoded
        "positionSide": "net",
        "side":         "sell" if size > 0 else "buy",
        "orderType":    "market",
        "size":         _fmt_size(size),
        "reduceOnly":   "true",
    }

    try:
        res = await client._req("POST", "/api/v1/trade/order",
                                body=body, private=True)
    except Exception as exc:
        out["note"] += f"close error: {exc}"
        return out

    if res == "DRYRUN":
        out["note"] += "BLOCKED by LIVE_TRADING_ENABLED=false"
    elif _sent(res):
        oid = ""
        if isinstance(res, list) and res:
            oid = res[0].get("orderId", "")
            row_code = str(res[0].get("code", "0"))
            if oid and row_code == "0":
                out["closed"] = True
                out["note"] += f"closed, orderId={oid}"
            else:
                out["note"] += (f"REJECTED code={row_code} "
                                f"msg={res[0].get('msg')}")
        else:
            out["note"] += f"unexpected response: {res!r}"
    else:
        out["note"] += "no response from exchange"

    if out["closed"] and ownership is not None:
        ownership.release(inst_id, "panic flatten")

    return out


async def main_async(args) -> int:
    print("\n" + "=" * 60)
    print("  PANIC.py -- emergency flatten")
    print("=" * 60)
    print(f"  env file             : {_ENV_FILE}")
    print(f"  endpoint             : {BASE_URL}")
    print(f"  account              : {'DEMO' if IS_DEMO else 'LIVE'}")
    print(f"  LIVE_TRADING_ENABLED : {LIVE_TRADING_ENABLED}")
    print(f"  mode                 : {'EXECUTE' if args.execute else 'REPORT ONLY'}")
    print(f"  scope                : {'ALL POSITIONS' if args.include_manual else 'bot-owned only'}")
    if ownership is None:
        print("  ownership module     : NOT AVAILABLE (cannot identify bot positions)")
    print()

    if not os.path.exists(_ENV_FILE):
        print(f"ABORT: {_ENV_FILE} does not exist.")
        print("Without it the client falls back to unset credentials.")
        return 1

    if IS_DEMO and args.execute and not args.demo:
        print("ABORT: resolved to the DEMO account but --demo was not given.")
        print("Refusing to run an emergency flatten against an unintended account.")
        print(f"Check IS_DEMO in {_ENV_FILE}.")
        return 1

    if args.execute:
        write_paused()
        if not args.no_stop_services:
            stop_services()
        print()

    client = BloFinClient()
    try:
        positions = await client.get_positions()
    except Exception as exc:
        print(f"ABORT: could not fetch positions: {exc}")
        return 1

    if not positions:
        print("  No open positions on the account. Nothing to do.")
        await client.close()
        return 0

    targets, skipped = [], []
    for p in positions:
        pair = p.get("instId", "")
        if args.include_manual:
            targets.append(p)
            continue
        if ownership is not None and ownership.is_owned(pair):
            targets.append(p)
        else:
            skipped.append(pair)

    print(f"  {len(positions)} open position(s) on the account")
    if skipped:
        print(f"  NOT touching (not bot-owned): {', '.join(skipped)}")
        print("  Use --include-manual to flatten these too.")
    print(f"  targeting {len(targets)} position(s)\n")

    if not targets:
        print("  Nothing in scope.")
        await client.close()
        return 0

    results = []
    for p in targets:
        r = await flatten_one(client, p, args.execute)
        results.append(r)
        await asyncio.sleep(0.4)

    print("-" * 60)
    for r in results:
        state = "CLOSED" if r["closed"] else "open"
        print(f"  {r['pair']:<14} size={r['size']:<12} uPnL=${r['upnl']:<9.2f} {state}")
        if r["stops_cancelled"] or r["orders_cancelled"]:
            print(f"    cancelled: {r['stops_cancelled']} stop(s), "
                  f"{r['orders_cancelled']} order(s)")
        if r["note"]:
            print(f"    {r['note']}")
    print("-" * 60)

    closed = sum(1 for r in results if r["closed"])
    print(f"\n  {closed}/{len(results)} position(s) closed")

    if not args.execute:
        print("\n  REPORT ONLY -- nothing was sent.")
        print("  Re-run with --execute to flatten.")
    elif not LIVE_TRADING_ENABLED:
        print("\n  All sends were BLOCKED by LIVE_TRADING_ENABLED=false.")
        print("  The flatten path ran end to end; no orders reached BloFin.")

    if args.execute and closed < len(results):
        print("\n  WARNING: not every position closed. Verify on BloFin directly.")
        await client.close()
        return 2

    await client.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Emergency flatten. Report-only unless --execute is given.")
    ap.add_argument("--execute", action="store_true",
                    help="actually attempt to flatten (default: report only)")
    ap.add_argument("--include-manual", action="store_true",
                    help="also close positions the bot did not open")
    ap.add_argument("--no-stop-services", action="store_true",
                    help="do not stop sniper-trend / sniper-peace first")
    ap.add_argument("--demo", action="store_true",
                    help=f"target the demo account ({ENV_DEMO}) instead of live")
    args = ap.parse_args()

    if args.include_manual and not args.execute:
        print("note: --include-manual has no effect without --execute")

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted -- positions may be partially flattened. "
              "Verify on BloFin.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
