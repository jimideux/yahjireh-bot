#!/usr/bin/env python3
"""
runner_ltf.py — sniper-ltf service entrypoint. LONG/SHORT alerts to Telegram.

READ-ONLY BY CONSTRUCTION. This file calls exactly two things in your stack:
    exchange.blofin  ->  get_candles()        (public market data)
    joy              ->  send()               (telegram)
No place_order, no place_tpsl, no cancel_*, no positions, no equity. It cannot
trade regardless of LIVE_TRADING_ENABLED.

Run modes:
    python3 runner_ltf.py --probe    one-shot: verify candle shape + telegram,
                                     print everything it sees, send test msg
    python3 runner_ltf.py            the service loop (what systemd runs)

Deploy order: probe FIRST. Only install the service after the probe message
lands on your phone and the printed candle shape looks right.
"""

import asyncio
import sys
import time

import exchange.blofin as _bf
from joy import send
from ltf_signals import LTFScanner, LTFConfig, normalize_candles

# ---------------------------------------------------------------------------
# Knobs — this block is yours
# ---------------------------------------------------------------------------

PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT", "SUI-USDT"]

CFG = LTFConfig(
    ltf="15m",              # entry timeframe: "5m" or "15m"
    htf="4H",               # bias timeframe
    alert_style="minimal",  # direction-first, 2-3 lines
    mode="advisory",        # B-grades fire with a fee note; "strict" mutes them
)

SWEEP_S = 60                # seconds between sweeps (15m bars: 60s is plenty)
LTF_LIMIT = 200             # candles per LTF fetch (need ~65 minimum)
HTF_LIMIT = 120             # candles per HTF fetch
HTF_TTL_S = 900             # HTF cache lifetime — 4H bars don't need minutely refetch
THROTTLE_S = 0.3            # pause between pairs; BloFin 429s on bursts (handoff §12)

# ---------------------------------------------------------------------------
# Client discovery — grep showed the methods (line 52 __init__, line 103
# get_candles) but not the class name, so find it by capability instead of
# hardcoding a guessed name.
# ---------------------------------------------------------------------------

def _client_cls():
    for obj in vars(_bf).values():
        if isinstance(obj, type) and hasattr(obj, "get_candles"):
            return obj
    raise RuntimeError(
        "no class with get_candles() found in exchange/blofin.py — "
        "tell Claude what `grep -n 'class ' exchange/blofin.py` says")


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)   # -> journalctl


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

async def sweep_once(client, scanner: LTFScanner, htf_cache: dict,
                     pairs=None, now=None) -> list:
    """One pass over all pairs. Returns list of telegram messages sent."""
    pairs = pairs or PAIRS
    sent = []
    for pair in pairs:
        try:
            raw_ltf = await client.get_candles(pair, bar=CFG.ltf, limit=LTF_LIMIT)
            ltf = normalize_candles(raw_ltf)

            cached = htf_cache.get(pair)
            if cached and (time.time() - cached[0]) < HTF_TTL_S:
                htf = cached[1]
            else:
                raw_htf = await client.get_candles(pair, bar=CFG.htf, limit=HTF_LIMIT)
                htf = normalize_candles(raw_htf)
                htf_cache[pair] = (time.time(), htf)

            sig = scanner.scan_pair(pair, ltf, htf, now)
            if sig is not None:
                msgs = []
                if scanner.maybe_alert(sig, msgs.append, now):
                    for m in msgs:
                        try:
                            await send(m)
                            sent.append(m)
                            _log(f"ALERT {pair} {sig.direction} grade {sig.grade}")
                        except Exception as e:            # noqa: BLE001
                            _log(f"telegram send failed {pair}: {e!r}")
                else:
                    _log(f"signal {pair} {sig.direction} suppressed "
                         f"(cooldown/quota)")
        except Exception as e:                            # noqa: BLE001
            _log(f"sweep error {pair}: {e!r}")            # one pair never kills the sweep
        await asyncio.sleep(THROTTLE_S)
    return sent


# ---------------------------------------------------------------------------
# Probe — run this before installing the service
# ---------------------------------------------------------------------------

async def probe() -> int:
    client = _client_cls()()
    ok = True
    try:
        for bar, limit in ((CFG.ltf, LTF_LIMIT), (CFG.htf, HTF_LIMIT)):
            _log(f"fetching BTC-USDT {bar} x{limit} ...")
            raw = await client.get_candles("BTC-USDT", bar=bar, limit=limit)
            _log(f"  raw type={type(raw).__name__} len={len(raw) if hasattr(raw,'__len__') else '?'}")
            first = raw[0] if raw else None
            _log(f"  first row: {str(first)[:120]}")
            try:
                norm = normalize_candles(raw)
                _log(f"  normalized {len(norm)} candles, "
                     f"ts {norm[0]['ts']:.0f} -> {norm[-1]['ts']:.0f} "
                     f"(oldest-first: {norm[0]['ts'] < norm[-1]['ts']}), "
                     f"last close {norm[-1]['close']}")
                if len(norm) < 65 and bar == CFG.ltf:
                    _log(f"  ⚠️ only {len(norm)} candles — scanner needs ~65. "
                         f"API may cap `limit`; check get_candles body.")
                    ok = False
            except Exception as e:                        # noqa: BLE001
                _log(f"  ❌ normalize failed: {e!r}")
                _log("  -> column order assumption is wrong. Paste this output "
                     "back to Claude along with `sed -n '103,108p' "
                     "/root/trading/exchange/blofin.py`")
                ok = False

        scanner = LTFScanner(CFG, state_path="/tmp/ltf_probe_state.json")
        raw_l = await client.get_candles("BTC-USDT", bar=CFG.ltf, limit=LTF_LIMIT)
        raw_h = await client.get_candles("BTC-USDT", bar=CFG.htf, limit=HTF_LIMIT)
        sig = scanner.scan_pair("BTC-USDT",
                                normalize_candles(raw_l), normalize_candles(raw_h))
        _log(f"scan BTC-USDT right now: "
             f"{'SIGNAL ' + sig.direction if sig else 'no setup (normal — this fires ~a few times/day across 6 pairs, not per sweep)'}")

        _log("sending telegram test message ...")
        await send("✅ sniper-ltf probe OK — candles readable, telegram wired. "
                   f"{CFG.ltf}/{CFG.htf}, {len(PAIRS)} pairs, minimal alerts.")
        _log("telegram send returned — check your phone.")
    except Exception as e:                                # noqa: BLE001
        _log(f"❌ probe failed: {e!r}")
        ok = False
    finally:
        try:
            await client.close()
        except Exception:                                 # noqa: BLE001
            pass
    _log("PROBE " + ("PASS — safe to install the service" if ok
                     else "FAIL — paste this output back before installing"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Service loop
# ---------------------------------------------------------------------------

async def main_loop() -> None:
    client = _client_cls()()
    scanner = LTFScanner(CFG)                 # state: /root/trading/ltf_alerts.json
    htf_cache: dict = {}
    _log(f"sniper-ltf up — {CFG.ltf}/{CFG.htf}, pairs={','.join(PAIRS)}, "
         f"style={CFG.alert_style}, mode={CFG.mode}")
    try:
        while True:
            t0 = time.time()
            await sweep_once(client, scanner, htf_cache)
            await asyncio.sleep(max(1.0, SWEEP_S - (time.time() - t0)))
    finally:
        try:
            await client.close()
        except Exception:                                 # noqa: BLE001
            pass


if __name__ == "__main__":
    if "--probe" in sys.argv:
        sys.exit(asyncio.run(probe()))
    asyncio.run(main_loop())
