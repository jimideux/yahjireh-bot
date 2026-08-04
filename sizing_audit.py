#!/usr/bin/env python3
"""
sizing_audit.py -- read-only. Measures BloFin contract minimums against the
sizing love.py actually requests.

THE QUESTION
------------
trend.py sizes a position like this:

    margin    = equity * trend_margin_pct
    notional  = margin * trend_leverage
    raw       = notional / (price * contractValue)
    raw       = (raw // lotSize) * lotSize
    contracts = max(raw, minSize)          <-- silent floor

When the intended notional buys fewer contracts than BloFin's minimum, that
max() raises the size to the minimum and the resulting position is larger than
anything in the config asked for. There is no warning and no log line; the
overshoot only shows up as an unexpectedly large fill.

This script computes, per pair, what the bot WOULD open right now and how far
that is from what it intended.

SAFETY
------
GETs only -- instruments, tickers, balance. No orders, no account mutation.
Loads .env.live explicitly, because a hand-run script inherits no ENV_FILE and
would otherwise fall back to .env (the demo account).

Usage:
    python3 sizing_audit.py
    python3 sizing_audit.py --equity 500     # model a different balance
    python3 sizing_audit.py --demo
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, "/root/trading")

ENV_LIVE = "/root/trading/.env.live"
ENV_DEMO = "/root/trading/.env"

if "--demo" in sys.argv:
    os.environ["ENV_FILE"] = ENV_DEMO
elif not os.environ.get("ENV_FILE"):
    os.environ["ENV_FILE"] = ENV_LIVE

_ENV_FILE = os.environ["ENV_FILE"]

from exchange.blofin import BloFinClient, BASE_URL, IS_DEMO  # noqa: E402
from love import config  # noqa: E402


async def audit_pair(client, pair, equity):
    inst = await client.get_instrument(pair)
    if not inst:
        return {"pair": pair, "error": "no instrument data"}
    price = await client.get_mark_price(pair)
    if price <= 0:
        return {"pair": pair, "error": "no price"}

    cv = float(inst.get("contractValue", "1") or 1)
    lot = float(inst.get("lotSize", "1") or 1)
    min_s = float(inst.get("minSize", "1") or 1)
    tick = float(inst.get("tickSize", "0.01") or 0.01)

    # What the config asks for
    margin = round(equity * config.trend_margin_pct, 2)
    intended_notional = margin * config.trend_leverage

    # What trend.py actually computes
    raw = intended_notional / (price * cv)
    if lot > 0:
        raw = (raw // lot) * lot
    contracts = max(raw, min_s)
    actual_notional = contracts * price * cv

    floored = contracts > raw + 1e-12
    overshoot = (actual_notional / intended_notional) if intended_notional > 0 else 0.0
    eff_lev = actual_notional / equity if equity > 0 else 0.0

    # Risk if the configured stop is hit
    risk_usd = actual_notional * config.trend_sl_pct
    risk_pct_equity = (risk_usd / equity * 100) if equity > 0 else 0.0

    # Equity at which the minimum stops binding
    denom = config.trend_margin_pct * config.trend_leverage
    min_viable_equity = (min_s * price * cv / denom) if denom > 0 else 0.0

    return {
        "pair": pair, "price": price, "cv": cv, "lot": lot,
        "min_s": min_s, "tick": tick,
        "intended": intended_notional, "raw": raw,
        "contracts": contracts, "actual": actual_notional,
        "floored": floored, "overshoot": overshoot, "eff_lev": eff_lev,
        "risk_usd": risk_usd, "risk_pct": risk_pct_equity,
        "min_viable_equity": min_viable_equity,
        "error": None,
    }


async def main_async(args):
    print("\n" + "=" * 74)
    print("  sizing_audit.py -- read-only")
    print("=" * 74)
    print(f"  env file : {_ENV_FILE}")
    print(f"  endpoint : {BASE_URL}")
    print(f"  account  : {'DEMO' if IS_DEMO else 'LIVE'}")

    client = BloFinClient()

    if args.equity is not None:
        equity = args.equity
        src = "supplied via --equity"
    else:
        equity = await client.get_equity()
        src = "live account balance"
    print(f"  equity   : ${equity:.2f} ({src})")
    print(f"  config   : margin {config.trend_margin_pct:.0%} of equity "
          f"x {config.trend_leverage}x leverage, stop {config.trend_sl_pct:.1%}")
    print()

    if equity <= 0:
        print("  ABORT: equity is zero or unreadable.")
        await client.close()
        return 1

    rows = []
    for pair in config.active_pairs:
        try:
            rows.append(await audit_pair(client, pair, equity))
        except Exception as exc:
            rows.append({"pair": pair, "error": str(exc)})
        await asyncio.sleep(0.35)   # §10: BloFin rate-limits bulk requests

    print(f"  {'PAIR':<12}{'PRICE':>11}{'MIN SIZE':>10}{'WANTED':>10}"
          f"{'ACTUAL':>10}{'x':>7}{'RISK':>9}")
    print("  " + "-" * 71)

    flagged = []
    for r in rows:
        if r.get("error"):
            print(f"  {r['pair']:<12}  ERROR: {r['error']}")
            continue
        mark = " !" if r["floored"] else "  "
        print(f"  {r['pair']:<12}{r['price']:>11.4f}{r['min_s']:>10.4g}"
              f"{r['intended']:>10.2f}{r['actual']:>10.2f}"
              f"{r['overshoot']:>6.1f}x{r['risk_pct']:>8.1f}%{mark}")
        if r["floored"]:
            flagged.append(r)

    print("  " + "-" * 71)
    print("  WANTED = notional the config asks for;  ACTUAL = what would open")
    print("  x      = actual/wanted;  RISK = loss at the configured stop, "
          "as % of equity")
    print("  !      = exchange minimum overrode the configured size")

    if flagged:
        print(f"\n  {len(flagged)} pair(s) where the minimum overrides your sizing:\n")
        for r in flagged:
            print(f"    {r['pair']}")
            print(f"      wanted ${r['intended']:.2f} notional, "
                  f"would open ${r['actual']:.2f} ({r['overshoot']:.1f}x)")
            print(f"      effective leverage {r['eff_lev']:.2f}x on the account")
            print(f"      a stop-out costs ${r['risk_usd']:.2f} "
                  f"= {r['risk_pct']:.1f}% of equity")
            print(f"      minimum viable equity for this pair: "
                  f"${r['min_viable_equity']:.2f}")
            print()
        worst = max(flagged, key=lambda r: r["min_viable_equity"])
        print(f"  To trade every configured pair at the intended size, equity")
        print(f"  needs to be at least ${worst['min_viable_equity']:.2f} "
              f"(set by {worst['pair']}).")
    else:
        print("\n  No pair is floored by the exchange minimum at this equity.")

    ok = [r for r in rows if not r.get("error") and not r["floored"]]
    if ok and flagged:
        print(f"\n  Tradeable at intended size right now: "
              f"{', '.join(r['pair'] for r in ok)}")

    await client.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="Read-only sizing audit.")
    ap.add_argument("--equity", type=float, default=None,
                    help="model a hypothetical equity instead of reading it")
    ap.add_argument("--demo", action="store_true",
                    help="use the demo account")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
