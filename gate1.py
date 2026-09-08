#!/usr/bin/env python3
"""gate1.py -- the Gate-1 review kit. Read-only: journal + candles in, columns out.

Cures the censored fixed counterfactuals (ghost resolver, with consultant 2's
15m first-touch ambiguity handled by pessimistic stop-first + explicit count),
then prints the decision columns the Sep review needs. Changes nothing.

Run:  ENV_FILE=/root/trading/.env.live .venv/bin/python3 gate1.py
"""
import asyncio, json, sys, datetime as dt

sys.path.insert(0, "/root/trading")
from exchange.blofin import BloFinClient

JOURNAL = "/root/trading/ltf_exec_trades.jsonl"
FEE = 0.66
PAPER_ERA_TS = 1788283500.0

def u(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%b %d %H:%M")

def load():
    opens, trades = {}, []
    for line in open(JOURNAL):
        if not line.strip(): continue
        r = json.loads(line)
        if r["event"] == "open":
            opens.setdefault(r["pair"], []).append(r)
        elif r["event"] == "telemetry":
            q = opens.get(r["pair"], [])
            if q: q[-1]["_tele"] = r
        elif r["event"] == "close":
            o = opens[r["pair"]].pop(0) if opens.get(r["pair"]) else {}
            t = dict(pair=r["pair"], open_ts=o.get("ts", 0), close_ts=r["ts"],
                     reason=r["reason"], net=r["net"], high=r.get("high_usd", 0),
                     entry=o.get("entry"), stop=o.get("stop"), target=o.get("target"),
                     side=o.get("side", "long"), virtual=r.get("virtual", {}),
                     tele=o.get("_tele", {}))
            trades.append(t)
    return trades

_CACHE = {}

async def candles(c, pair, bar, limit=1440):
    key = (pair, bar)
    if key not in _CACHE:
        d = await c.get_candles(pair, bar=bar, limit=limit)
        _CACHE[key] = sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in d)
    return _CACHE[key]

def first_touch(rows, start_ms, stop, target, side):
    """Walk bars from start; return ('stop'|'target'|'ambiguous'|'open', ts)."""
    for ts, o, h, l, cl in rows:
        if ts < start_ms: continue
        hit_s = (l <= stop) if side == "long" else (h >= stop)
        hit_t = (h >= target) if side == "long" else (l <= target)
        if hit_s and hit_t: return "ambiguous", ts
        if hit_s: return "stop", ts
        if hit_t: return "target", ts
    return "open", None

async def main():
    c = BloFinClient()
    trades = load()
    coh = [t for t in trades if t["close_ts"] >= PAPER_ERA_TS]
    print(f"loaded {len(trades)} trades ({len(coh)} paper-era)\n")

    # ---- 1. ghost resolver -------------------------------------------------
    print("== 1. GHOST RESOLVER (censored fixed counterfactuals) ==")
    ghosts, amb = [], 0
    for t in trades:
        if t["virtual"].get("fixed") is not None: continue
        risk = t["tele"].get("risk_usd"); tgt_gross = t["tele"].get("max_upnl")
        rows15 = await candles(c, t["pair"], "15m")
        verdict, ts = first_touch(rows15, int(t["close_ts"] * 1000),
                                  t["stop"], t["target"], t["side"])
        note = ""
        if verdict == "ambiguous":
            rows5 = await candles(c, t["pair"], "5m")
            v2, ts2 = first_touch([r for r in rows5 if r[0] >= ts], ts,
                                  t["stop"], t["target"], t["side"])
            if v2 in ("stop", "target"): verdict, ts, note = v2, ts2, " (5m-resolved)"
            else: verdict, note, amb = "stop", " (AMBIGUOUS->pessimistic stop)", amb + 1
        if verdict == "open":
            fx = None; print(f"  {t['pair']:<11} ratchet {t['net']:+7.2f} | fixed: STILL OPEN — unresolved")
        else:
            fx = round((tgt_gross - FEE) if verdict == "target" else -(risk + FEE), 2)
            ghosts.append((t, fx))
            print(f"  {t['pair']:<11} ratchet {t['net']:+7.2f} | fixed would: {verdict:<7}{fx:+7.2f} at {u(ts/1000)}{note}  -> ratchet {'SAVED' if t['net']>fx else 'COST':<5} {abs(t['net']-fx):.2f}")
    if ghosts:
        ra, fa = sum(t["net"] for t, _ in ghosts), sum(f for _, f in ghosts)
        print(f"  resolved {len(ghosts)} | ambiguous->pessimistic {amb} | lock-exit total: ratchet {ra:+.2f} vs fixed {fa:+.2f} -> ladder edge {ra-fa:+.2f}")
        allr = sum(t["net"] for t in coh)
        print(f"  cohort under ratchet {allr:+.2f} | cohort under pure-fixed {allr - ra + fa:+.2f}")

    # ---- 2. time-stop counterfactual --------------------------------------
    print("\n== 2. TIME STOP (exit at mark after H hours, cohort, candle-depth permitting) ==")
    for H in (8, 12):
        delta, nmod, miss = 0.0, 0, 0
        for t in coh:
            if t["close_ts"] - t["open_ts"] <= H * 3600: continue
            rows = await candles(c, t["pair"], "15m")
            cut = int((t["open_ts"] + H * 3600) * 1000)
            px = next((r[4] for r in rows if r[0] >= cut), None)
            if px is None or not t["entry"]: miss += 1; continue
            sgn = 1 if t["side"] == "long" else -1
            cts = t["tele"].get("risk_usd", 0) / abs(t["entry"] - t["stop"]) if t["stop"] else 0
            alt = round((px - t["entry"]) * sgn * cts - FEE, 2)
            delta += alt - t["net"]; nmod += 1
        print(f"  {H}h cap: modifies {nmod} trades, out-of-depth {miss}, cohort delta {delta:+.2f}")

    # ---- 3. HTF regime at entry -------------------------------------------
    print("\n== 3. 4H REGIME AT ENTRY vs OUTCOME (cohort) ==")
    buckets = {}
    for t in coh:
        rows = await candles(c, t["pair"], "4H", limit=300)
        pre = [r[4] for r in rows if r[0] <= t["open_ts"] * 1000][-60:]
        if len(pre) < 55: continue
        e = pre[0]
        for p in pre[1:]: e = e + (2 / 51) * (p - e)
        e_prev = pre[0]
        for p in pre[1:-3]: e_prev = e_prev + (2 / 51) * (p - e_prev)
        slope = (e - e_prev) / e_prev * 100
        key = "strong" if abs(slope) > 0.6 else "weak"
        b = buckets.setdefault(key, [0, 0.0])
        b[0] += 1; b[1] += t["net"]
    for k, (cnt, s) in sorted(buckets.items()):
        print(f"  {k:<7} 4H slope: {cnt:>2} trades, net {s:+.2f}")

    # ---- 4. same-cycle + scratch accounting (journal-only) -----------------
    print("\n== 4. SAME-CYCLE keep-first & BREAKER SCRATCHES ==")
    ts_sorted = sorted(trades, key=lambda t: t["open_ts"])
    i, rule = 0, 0.0
    while i < len(ts_sorted) - 1:
        a, b = ts_sorted[i], ts_sorted[i + 1]
        if b["open_ts"] - a["open_ts"] <= 120:
            rule += -b["net"]
            print(f"  [{u(a['open_ts'])}] kept {a['pair']} {a['net']:+.2f}, skipping {b['pair']} {b['net']:+.2f} -> rule delta {-b['net']:+.2f}")
            i += 2
        else: i += 1
    print(f"  one-per-cycle rule total delta: {rule:+.2f}")
    scr = [t for t in trades if t["net"] < 0 and t["tele"].get("risk_usd") and abs(t["net"]) < 0.1 * t["tele"]["risk_usd"]]
    for t in scr: print(f"  scratch counted as streak loss: {t['pair']} {t['net']:+.2f} (risk {t['tele']['risk_usd']:.2f})")

    # ---- 5. fee drag -------------------------------------------------------
    print("\n== 5. FEE DRAG (fee as R-fraction, cohort) ==")
    tight = [t for t in coh if t["tele"].get("risk_usd") and FEE / t["tele"]["risk_usd"] > 0.15]
    wide = [t for t in coh if t["tele"].get("risk_usd") and FEE / t["tele"]["risk_usd"] <= 0.15]
    for name, g in (("fee>0.15R (tight stops)", tight), ("fee<=0.15R", wide)):
        if g: print(f"  {name:<24} n={len(g):>2} net {sum(t['net'] for t in g):+8.2f}  WR {sum(1 for t in g if t['net']>0)/len(g):.0%}")

    await c.close()
    print("\nColumns complete. Verdict discussion happens against these numbers, not vibes.")

asyncio.run(main())
