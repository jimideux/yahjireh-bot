#!/usr/bin/env python3
"""
runner_ltf.py — sniper-ltf v2. Full-market LONG/SHORT alerts to Telegram.

v2: scans ALL qualifying BloFin USDT perpetuals instead of a fixed list.
"Qualifying" = live instrument, USDT-margined swap, >= MIN_VOL_USD_24H traded
in 24h. The liquidity floor is the one pre-filter that stays: a signal on a
dead book fills nowhere near the alerted level. Everything else — structure,
closed-bar, fee grade — qualifies each setup individually in ltf_signals.py.

READ-ONLY BY CONSTRUCTION, unchanged: this file touches get_candles(), two
public market endpoints via the client's _req(), and joy.send(). No order
paths exist here.

Design constraints at full-market scale (why v2 differs from v1):

  RATE LIMIT — BloFin allows 500 req/min per IP (5-min suspension if hit) and
  1500 per 5 min (1-HOUR suspension). This budget is shared with sniper-trend/
  peace/resolver on the same droplet. So: 0.4s throttle (same as backtest.py),
  sweeps aligned to 15m bar closes instead of every 60s, HTF cached ~1h with
  jitter so refreshes don't stampede, instrument discovery once a day.

  PHONE FLOOD — a market-wide move can print the same setup on 40 pairs at
  once. Each sweep sends at most MAX_ALERTS_PER_SWEEP, ranked grade-A first,
  then by fee multiple. Suppressed count is logged.

Run modes:
    python3 runner_ltf.py --pairs    discovery dry-run: prints the universe,
                                     raw sample rows, volume math, sweep ETA.
                                     Read-only, 2 API calls. RUN THIS FIRST.
    python3 runner_ltf.py --probe    v1 probe: candle shape + telegram test
    python3 runner_ltf.py            the service loop
"""

import asyncio
import random
import sys
import time

import exchange.blofin as _bf
from joy import send
from ltf_signals import LTFScanner, LTFConfig, normalize_candles

# ---------------------------------------------------------------------------
# Knobs — this block is yours
# ---------------------------------------------------------------------------

MIN_VOL_USD_24H = 2_000_000     # liquidity floor; raise if fills feel thin
MAX_ALERTS_PER_SWEEP = 5        # best-ranked signals per bar close
BAR_S = 900                     # 15m bar cadence the loop aligns to
GRACE_S = 5                     # wake this long after the boundary
LTF_LIMIT = 200
HTF_LIMIT = 120
HTF_TTL_S = 3600                # ~hourly HTF refresh (jittered ±25%)
PAIRS_TTL_S = 86400             # rediscover the universe daily
THROTTLE_S = 0.4                # backtest.py's number; keeps well under 500/min

CFG = LTFConfig(
    ltf="15m",
    htf="4H",
    alert_style="minimal",
    mode="advisory",
    max_alerts_per_day=40,      # was 12 — tuned for 6 pairs, not the market
)

# Fallback universe if discovery fails — alerts degrade, never die.
CORE_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT", "SUI-USDT"]

# ---------------------------------------------------------------------------
# Client discovery (unchanged from v1)
# ---------------------------------------------------------------------------

def _client_cls():
    for obj in vars(_bf).values():
        if isinstance(obj, type) and hasattr(obj, "get_candles"):
            return obj
    raise RuntimeError("no class with get_candles() found in exchange/blofin.py")


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Universe discovery
# ---------------------------------------------------------------------------

def _data(resp):
    """BloFin REST wraps payloads as {"code":"0","data":[...]}. His _req() may
    return that envelope or the unwrapped data — handle both."""
    if isinstance(resp, dict):
        if str(resp.get("code", "0")) not in ("0", "00000"):
            raise RuntimeError(f"BloFin API error: {resp.get('code')} {resp.get('msg')}")
        return resp.get("data", [])
    return resp or []


def _usd_vol(t: dict) -> float:
    """24h USD notional from a ticker row, defensively across key spellings.
    Verify with --pairs: it prints a raw row and BTC's computed notional."""
    for k in ("volUsd24h", "volCurrencyQuote24h", "quoteVolume24h"):
        if t.get(k) not in (None, ""):
            return float(t[k])
    last = float(t.get("last") or 0.0)
    for k in ("volCurrency24h", "volCcy24h", "baseVolume24h"):
        if t.get(k) not in (None, ""):
            return float(t[k]) * last            # base units x price
    return float(t.get("vol24h") or 0.0) * last  # contracts x price — roughest


async def discover_pairs(client):
    """Returns (pairs_sorted_by_vol_desc, diagnostics)."""
    inst = _data(await client._req("GET", "/api/v1/market/instruments",
                                   params={"instType": "SWAP"}))
    tick = _data(await client._req("GET", "/api/v1/market/tickers",
                                   params={"instType": "SWAP"}))
    vols = {t.get("instId"): _usd_vol(t) for t in tick if t.get("instId")}

    live, floored = [], 0
    for i in inst:
        iid = i.get("instId", "")
        if not iid.endswith("-USDT"):
            continue
        if "state" in i and str(i["state"]).lower() not in ("live", "normal", ""):
            continue
        v = vols.get(iid, 0.0)
        if v < MIN_VOL_USD_24H:
            floored += 1
            continue
        live.append((iid, v))

    live.sort(key=lambda x: -x[1])
    diag = {
        "instruments_total": len(inst),
        "usdt_swaps": sum(1 for i in inst if i.get("instId", "").endswith("-USDT")),
        "below_floor": floored,
        "qualified": len(live),
        "sample_instrument": inst[0] if inst else None,
        "sample_ticker": tick[0] if tick else None,
        "top10": live[:10],
        "tail5": live[-5:],
    }
    return [iid for iid, _ in live], diag


# ---------------------------------------------------------------------------
# Bar alignment
# ---------------------------------------------------------------------------

def _next_boundary(now: float) -> float:
    return (int(now) // BAR_S + 1) * BAR_S + GRACE_S


# ---------------------------------------------------------------------------
# Sweep — gather, rank, send top-K
# ---------------------------------------------------------------------------

async def sweep_once(client, scanner: LTFScanner, htf_cache: dict,
                     pairs, now=None) -> dict:
    t0 = time.time()
    signals, errors = [], 0

    for pair in pairs:
        try:
            raw_ltf = await client.get_candles(pair, bar=CFG.ltf, limit=LTF_LIMIT)
            ltf = normalize_candles(raw_ltf)

            cached = htf_cache.get(pair)
            if cached and time.time() < cached[0]:
                htf = cached[1]
            else:
                raw_htf = await client.get_candles(pair, bar=CFG.htf, limit=HTF_LIMIT)
                htf = normalize_candles(raw_htf)
                ttl = HTF_TTL_S * (0.75 + random.random() * 0.5)   # de-stampede
                htf_cache[pair] = (time.time() + ttl, htf)

            sig = scanner.scan_pair(pair, ltf, htf, now)
            if sig is not None:
                signals.append(sig)
        except Exception as e:                    # noqa: BLE001
            errors += 1
            _log(f"sweep error {pair}: {e!r}")
            await asyncio.sleep(1.0)              # extra backoff on failure/429
        await asyncio.sleep(THROTTLE_S)

    # rank: A before B, then fattest fee multiple
    signals.sort(key=lambda s: (0 if s.grade == "A" else 1, -s.fee_mult))

    sent, suppressed = [], 0
    for sig in signals:
        if len(sent) >= MAX_ALERTS_PER_SWEEP:
            suppressed += 1
            _log(f"rank-suppressed {sig.pair} {sig.direction} grade {sig.grade} "
                 f"({sig.fee_mult:.1f}x)")
            continue
        msgs = []
        if scanner.maybe_alert(sig, msgs.append, now):
            for m in msgs:
                try:
                    await send(m)
                    sent.append(sig)
                    _log(f"ALERT {sig.pair} {sig.direction} grade {sig.grade}")
                except Exception as e:            # noqa: BLE001
                    _log(f"telegram send failed {sig.pair}: {e!r}")
        # cooldown/quota rejections don't count against the sweep cap

    a = sum(1 for s in signals if s.grade == "A")
    _log(f"[sweep] {len(pairs)} pairs, {len(signals)} signals "
         f"({a}A/{len(signals) - a}B), {len(sent)} alerted, "
         f"{suppressed} rank-suppressed, {errors} errors, "
         f"{time.time() - t0:.0f}s")
    return {"signals": signals, "sent": sent, "suppressed": suppressed,
            "errors": errors}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

async def probe_pairs() -> int:
    """Discovery dry-run. Read-only, 2 API calls, no telegram."""
    client = _client_cls()()
    try:
        pairs, d = await discover_pairs(client)
        _log(f"instruments total={d['instruments_total']}  "
             f"USDT swaps={d['usdt_swaps']}  below ${MIN_VOL_USD_24H:,.0f} "
             f"floor={d['below_floor']}  QUALIFIED={d['qualified']}")
        _log(f"raw instrument row: {str(d['sample_instrument'])[:200]}")
        _log(f"raw ticker row:     {str(d['sample_ticker'])[:200]}")
        _log("top 10 by 24h USD volume:")
        for iid, v in d["top10"]:
            _log(f"    {iid:<14} ${v:,.0f}")
        _log("thinnest 5 admitted:")
        for iid, v in d["tail5"]:
            _log(f"    {iid:<14} ${v:,.0f}")
        n = len(pairs)
        cold = (n * 2) * THROTTLE_S
        warm = (n + n // 4) * THROTTLE_S
        _log(f"sweep ETA at {THROTTLE_S}s throttle: first ~{cold/60:.1f} min "
             f"(cold HTF cache), steady ~{warm/60:.1f} min "
             f"(~{(n + n//4) / (warm/60):.0f} req/min vs 500/min IP limit)")
        _log("SANITY CHECK: BTC-USDT should be near the top with 24h volume in "
             "the BILLIONS. If the numbers look absurd, the ticker volume field "
             "guess is wrong — paste this output back.")
        return 0
    except Exception as e:                        # noqa: BLE001
        _log(f"❌ pairs probe failed: {e!r}")
        return 1
    finally:
        try:
            await client.close()
        except Exception:                         # noqa: BLE001
            pass


async def probe() -> int:
    """v1 probe: candle shape + telegram delivery. Unchanged behavior."""
    client = _client_cls()()
    ok = True
    try:
        for bar, limit in ((CFG.ltf, LTF_LIMIT), (CFG.htf, HTF_LIMIT)):
            raw = await client.get_candles("BTC-USDT", bar=bar, limit=limit)
            norm = normalize_candles(raw)
            _log(f"{bar}: {len(norm)} candles, oldest-first="
                 f"{norm[0]['ts'] < norm[-1]['ts']}, last close {norm[-1]['close']}")
        await send("✅ sniper-ltf v2 probe OK — full-market mode ready.")
        _log("telegram sent — check your phone.")
    except Exception as e:                        # noqa: BLE001
        _log(f"❌ probe failed: {e!r}")
        ok = False
    finally:
        try:
            await client.close()
        except Exception:                         # noqa: BLE001
            pass
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Service loop
# ---------------------------------------------------------------------------

async def main_loop() -> None:
    client = _client_cls()()
    scanner = LTFScanner(CFG)
    htf_cache: dict = {}
    pairs, pairs_expiry, warned = list(CORE_PAIRS), 0.0, False

    _log(f"sniper-ltf v2 up — {CFG.ltf}/{CFG.htf}, full-market mode, "
         f"floor ${MIN_VOL_USD_24H:,.0f}, top {MAX_ALERTS_PER_SWEEP}/sweep, "
         f"style={CFG.alert_style}, mode={CFG.mode}")
    try:
        while True:
            if time.time() >= pairs_expiry:
                try:
                    pairs, d = await discover_pairs(client)
                    pairs_expiry = time.time() + PAIRS_TTL_S
                    warned = False
                    _log(f"universe: {d['qualified']} qualified pairs "
                         f"(of {d['usdt_swaps']} USDT swaps, "
                         f"{d['below_floor']} below floor)")
                except Exception as e:            # noqa: BLE001
                    pairs = list(CORE_PAIRS)
                    pairs_expiry = time.time() + 1800     # retry in 30 min
                    _log(f"❌ discovery failed, falling back to core 6: {e!r}")
                    if not warned:
                        warned = True
                        try:
                            await send("⚠️ sniper-ltf: pair discovery failed — "
                                       "running on core 6 pairs until it recovers.")
                        except Exception:         # noqa: BLE001
                            pass

            await sweep_once(client, scanner, htf_cache, pairs)
            await asyncio.sleep(max(1.0, _next_boundary(time.time()) - time.time()))
    finally:
        try:
            await client.close()
        except Exception:                         # noqa: BLE001
            pass


if __name__ == "__main__":
    if "--pairs" in sys.argv:
        sys.exit(asyncio.run(probe_pairs()))
    if "--probe" in sys.argv:
        sys.exit(asyncio.run(probe()))
    asyncio.run(main_loop())
