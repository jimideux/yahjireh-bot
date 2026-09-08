#!/usr/bin/env python3
"""test_g2.py — Gen-2 verification.
Part A: DIFFERENTIAL — the ported g2_signals must agree with the legacy
        signals.py on identical candles (the 'verbatim' proof).
Part B: chassis — cluster rule + shadow rows, same-bar dedupe, scratch-aware
        breaker, daily budget. Signals injected; no network; /tmp paths only.
"""
import asyncio, glob, json, os, random, sys, time, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))   # legacy import
import g2_signals as G2
import signals as LEG

res = []
check = lambda n, c, d="": res.append((n, c, str(d)))
NOW = int(time.time() // 3600 * 3600) * 1000

def mk(closes, tf_ms=3600000, all_confirmed=True):
    ts = NOW - len(closes) * tf_ms
    return [[str(ts + i * tf_ms), str(c * 0.999), str(c * 1.003), str(c * 0.997),
             str(c), "1000", "0", "0", "1" if (all_confirmed or i < len(closes) - 1) else "0"]
            for i, c in enumerate(closes)]

ARR = {}
async def fake_binance(pair, interval="1h", limit=60):
    return ARR.get((pair, interval.lower()), [])
LEG.get_candles_binance = fake_binance

class FakeClient:
    def __init__(self): self._marks = {}
    async def get_candles(self, pair, bar="1H", limit=80):
        return ARR.get((pair, bar.lower()), [])
    async def get_mark_price(self, p): return self._marks.get(p, 100.0)
    async def get_equity(self): return 12.0
    async def get_positions(self): return []
    async def get_pending_orders(self, inst_id=None): return []
    async def get_instrument(self, i):
        return {"contractValue": "1", "lotSize": "0.1", "minSize": "0.1",
                "tickSize": "0.0001", "assetClass": "Crypto"}
    async def calc_contracts(self, i, usd):
        return round(usd / await self.get_mark_price(i), 1)
    async def place_order(self, *a, **k): return "DRYRUN"
    async def place_tpsl(self, *a, **k): return "DRYRUN"
    async def cancel_all_tpsl(self, i): return 0
    async def cancel_all_orders(self, i): return 0
    async def close(self): pass

def series_battery():
    random.seed(7)
    out = []
    for k in range(220):                      # random walks, varied character
        drift = random.uniform(-0.002, 0.003)
        vol = random.uniform(0.001, 0.006)
        s, px = [], 100.0
        for _ in range(75):
            px *= 1 + random.gauss(drift, vol); s.append(px)
        out.append(s)
    for r in (0.0011, 0.0015, 0.002):         # near-window shapes + RSI-cooling wobble
        for m in (6, 10, 14, 18, 24):
            base = [100.0] * 55
            for _ in range(16): base.append(base[-1] * (1 + r))
            for i in range(m): base.append(base[-1] * (1 + (0.0004 if i % 2 else -0.0004)))
            out.append(base[-75:])
    return out

async def part_a():
    ARR[("BTC-USDT", "1d")] = mk([100 + i for i in range(12)], 86400000)
    ARR[("BTC-USDT", "1h")] = mk([100.0] * 25)
    btc_leg = await LEG.get_btc_daily_trend()
    fc = FakeClient()
    btc_mine = await G2.btc_daily_trend(fc)
    check("BTC daily trend agrees", btc_leg == btc_mine, (btc_leg, btc_mine))
    agree = accepts = 0
    mism = None
    for s in series_battery():
        ARR[("SOL-USDT", "1h")] = mk(s)
        leg = await LEG.get_signal("SOL-USDT", ema_short=20, ema_long=50,
                                   price_zone=G2.PRICE_ZONE, interval="1h")
        mine = await G2.scan_pair(fc, "SOL-USDT", btc_mine)
        if (leg is None) != (mine is None):
            mism = mism or (leg, mine and mine.__dict__); continue
        if leg is not None:
            accepts += 1
            ok = (leg["direction"] == mine.direction
                  and abs(leg["price"] - mine.entry) < 1e-9
                  and abs(mine.stop - (leg["price"] - leg["atr"] * 1.0
                          if leg["direction"] == "long" else leg["price"] + leg["atr"])) < 1e-6
                  and abs(abs(mine.target - mine.entry)
                          - max(leg["atr"] * 2.0, leg["price"] * 0.003)) < 1e-6)
            if not ok: mism = mism or (leg, mine.__dict__); continue
        agree += 1
    n_series = len(series_battery())
    check(f"differential parity ({agree}/{n_series})", mism is None and agree == n_series, mism or agree)
    # subfunction numeric parity: ema/atr/rsi must match to 1e-9
    import random as _r; _r.seed(11); sub_ok = True
    for _ in range(60):
        arr = [100.0]
        for _ in range(70): arr.append(arr[-1] * (1 + _r.gauss(0.0005, 0.004)))
        rows = mk(arr)
        prow = G2.parse(rows)
        legc = [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]
        if abs(G2.ema(arr, 20) - LEG.calc_ema(arr, 20)) > 1e-9: sub_ok = False
        if abs(G2.ema(arr, 50) - LEG.calc_ema(arr, 50)) > 1e-9: sub_ok = False
        if abs(G2.atr14(prow) - LEG.calc_atr(rows)) > 1e-9: sub_ok = False
        if abs(G2.rsi14(arr) - LEG.calc_rsi(arr)) > 1e-6: sub_ok = False
    check("ema/atr/rsi numerically identical", sub_ok)
    # accept-path parity: widen zone EQUALLY on both engines
    saved_zone = G2.PRICE_ZONE; G2.PRICE_ZONE = 0.05
    saved_rsi_g, saved_rsi_l = G2.rsi14, LEG.calc_rsi
    G2.rsi14 = lambda c, period=14: 50.0          # pinned EQUALLY on both engines
    LEG.calc_rsi = lambda c, period=14: 50.0      # to exercise the accept path
    acc = par = 0; amism = None
    for s2 in series_battery():
        ARR[("SOL-USDT", "1h")] = mk(s2)
        leg = await LEG.get_signal("SOL-USDT", ema_short=20, ema_long=50,
                                   price_zone=0.05, interval="1h")
        mine = await G2.scan_pair(fc, "SOL-USDT", btc_mine)
        if (leg is None) != (mine is None):
            amism = amism or ("presence", leg, mine and mine.pair); continue
        if leg is None: par += 1; continue
        acc += 1
        exp_sl = leg["atr"] * 1.0
        exp_tp = max(leg["atr"] * 2.0, leg["price"] * 0.003)
        ok = (leg["direction"] == mine.direction
              and abs(leg["price"] - mine.entry) < 1e-9
              and abs(abs(mine.entry - mine.stop) - exp_sl) < 1e-6
              and abs(abs(mine.target - mine.entry) - exp_tp) < 1e-6)
        if ok: par += 1
        else: amism = amism or ("fields", leg, mine.__dict__)
    G2.PRICE_ZONE = saved_zone
    G2.rsi14, LEG.calc_rsi = saved_rsi_g, saved_rsi_l
    check(f"wide-zone parity incl. accepts ({acc} accepts)", amism is None and acc >= 5,
          amism or acc)
    # closed-bar deviation: a fat FORMING candle must not change my decision
    base = series_battery()[100]
    ARR[("SOL-USDT", "1h")] = mk(base)
    before = await G2.scan_pair(fc, "SOL-USDT", btc_mine)
    spoofed = mk(base + [base[-1] * 1.06], all_confirmed=False)
    ARR[("SOL-USDT", "1h")] = spoofed
    after = await G2.scan_pair(fc, "SOL-USDT", btc_mine)
    same = (before is None and after is None) or (
        before and after and abs(before.entry - after.entry) < 1e-9 and before.ts == after.ts)
    check("forming candle ignored (my deviation)", same)

async def part_b():
    import ltf_executor as X
    for p in glob.glob("/tmp/g2x_*"): os.remove(p)
    X.TRADES_PATH = "/tmp/g2x_trades.jsonl"
    X.STATE_PATH = "/tmp/g2x_state.json"
    X.PAPER_PATH = "/tmp/g2x_paper.json"
    X.EXEC_SCAN_STATE = "/tmp/g2x_scan.json"
    async def silent(t): pass
    X.safe_send = silent
    fc = FakeClient()
    ex = X.Executor(fc)
    ex.risk = X.RiskManager(X.RiskConfig(max_risk_pct_per_trade=0.01,
                                         max_concurrent_positions=2),
                            state_path="/tmp/g2x_risk.json")
    def sig(pair, e=100.0):
        return G2.Signal(pair=pair, direction="long", timeframe="1H", ts=NOW,
                         entry=e, stop=e * 0.992, target=e * 1.016,
                         stop_pct=0.008, target_pct=0.016, r_multiple=2.0,
                         ev_pct=0.003, fee_mult=13.3, htf_bias="bull",
                         atr_pct=0.008, rsi=50.0, grade="A")
    injected = {"SOL-USDT": sig("SOL-USDT"), "SUI-USDT": sig("SUI-USDT")}
    async def fake_scan(client, pair, btc): return injected.get(pair)
    X.G2.scan_pair = fake_scan
    async def fake_btc(client): return "bull"
    X.G2.btc_daily_trend = fake_btc
    fc._marks.update({"SOL-USDT": 100.0, "SUI-USDT": 100.0})
    await ex.entry_cycle(["SOL-USDT", "SUI-USDT"])
    rows = [json.loads(l) for l in open("/tmp/g2x_trades.jsonl")]
    skips = [r for r in rows if r["event"] == "cluster_skip"]
    check("first of l1 cluster entered", len(ex.state) == 1, list(ex.state))
    check("second journaled cluster_skip", len(skips) == 1 and skips[0]["group"] == "l1",
          skips and (skips[0]["pair"], skips[0]["group"]))
    await ex.entry_cycle(["SOL-USDT", "SUI-USDT"])          # same bar again
    opens = [r for r in json.loads_all] if False else [json.loads(l) for l in open("/tmp/g2x_trades.jsonl") if '"open"' in l]
    check("same-bar dedupe: still one open", len(opens) == 1 and len(ex.state) == 1)
    pair = list(ex.state)[0]; pos = ex.state[pair]
    fc._marks[pair] = pos["entry"] + 1.05 * (pos["entry"] - pos["stop"])
    await ex.watch_tick()
    fc._marks[pair] = pos["entry"] + 0.05 * (pos["entry"] - pos["stop"])
    await ex.watch_tick()
    closed = [json.loads(l) for l in open("/tmp/g2x_trades.jsonl") if '"close"' in l]
    st = json.load(open("/tmp/g2x_risk.json"))
    check("lock scratched the retrace", len(closed) == 1 and closed[0]["reason"].startswith("lock"),
          closed and (closed[0]["reason"], closed[0]["net"]))
    check("scratch not counted in streak", st["consecutive_losses"] == 0,
          (closed and closed[0]["net"], st["consecutive_losses"]))
    # full-size loss DOES count
    ex.risk.state["last_close_ts"] -= 400          # serve the post-close cooldown
    ex.risk.save()
    injected["ETH-USDT"] = sig("ETH-USDT")
    fc._marks["ETH-USDT"] = 100.0
    await ex.entry_cycle(["ETH-USDT"])
    p2 = ex.state["ETH-USDT"]
    fc._marks["ETH-USDT"] = p2["stop"] * 0.999
    await ex.watch_tick()
    st = json.load(open("/tmp/g2x_risk.json"))
    check("real loss feeds streak", st["consecutive_losses"] == 1, st["consecutive_losses"])

asyncio.run(part_a())
asyncio.run(part_b())
print("-" * 64); f = 0
for n, c, d in res:
    f += (not c); print(f"  {n:<40}{'PASS' if c else 'FAIL'}  {d[:22]}")
print(f"{len(res)-f}/{len(res)} passed")
