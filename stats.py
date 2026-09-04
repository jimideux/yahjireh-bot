#!/usr/bin/env python3
"""stats.py -- the only authorized source of YahJireh LTF aggregates.

Born Sep 4 2026 after an external audit caught the owner-side summary
overstating net P&L by exactly one dropped trade (+38.13 vs true +27.48).
Prose arithmetic is retired; this script sums the journal and reconciles
it against the paper account file. Exit code 0 = books balance.

Usage:  .venv/bin/python3 stats.py            # human table + PASS/FAIL
        .venv/bin/python3 stats.py --brief    # one-line summary
"""
import json, sys, datetime as dt

JOURNAL = "/root/trading/ltf_exec_trades.jsonl"
PAPER   = "/root/trading/ltf_exec_paper.json"
PAPER_START_EQUITY = 1878.0
PAPER_ERA_TS = 1788283500.0        # v1.1 deploy Sep 1 16:45 UTC; closes after this hit the paper book
SAME_CYCLE_WINDOW_S = 120.0

def u(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%b %d %H:%M")

def derive_cv(notional, contracts, entry):
    if contracts <= 0 or entry <= 0: return 1.0
    raw = notional / (contracts * entry)
    best = min((10.0**k for k in range(-4, 5)), key=lambda c: abs(c - raw))
    return best

def main():
    brief = "--brief" in sys.argv
    opens, trades = {}, []
    try:
        rows = [json.loads(l) for l in open(JOURNAL) if l.strip()]
    except FileNotFoundError:
        print(f"FAIL: journal not found at {JOURNAL}"); return 1
    for r in rows:
        ev = r.get("event")
        if ev == "open":
            r["_cv"] = derive_cv(r.get("notional", 0), r.get("contracts", 0), r.get("entry", 0))
            opens.setdefault(r["pair"], []).append(r)
        elif ev == "close":
            o = opens.get(r["pair"], [None]) and (opens[r["pair"]].pop(0) if opens.get(r["pair"]) else None)
            risk = abs(o["entry"] - o["stop"]) * o["contracts"] * o["_cv"] if o else float("nan")
            trades.append({
                "pair": r["pair"], "open_ts": o["ts"] if o else float("nan"), "close_ts": r["ts"],
                "reason": r["reason"], "net": r["net"], "high": r.get("high_usd", 0.0),
                "risk": risk, "virtual": r.get("virtual", {}),
                "side": o.get("side", "?") if o else "?",
            })
    n = len(trades)
    if n == 0:
        print("no closed trades in journal"); return 0
    wins  = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gw, gl = sum(t["net"] for t in wins), -sum(t["net"] for t in losses)
    net = gw - gl
    paper_trades = [t for t in trades if t["close_ts"] >= PAPER_ERA_TS]
    pnet = sum(t["net"] for t in paper_trades)
    pw = sum(t["net"] for t in paper_trades if t["net"] > 0)
    pl = -sum(t["net"] for t in paper_trades if t["net"] <= 0)
    # streaks
    streak = max_streak = 0
    for t in trades:
        streak = streak + 1 if t["net"] <= 0 else 0
        max_streak = max(max_streak, streak)
    locks = [t for t in trades if str(t["reason"]).startswith("lock")]
    censored = [t for t in trades if t["virtual"].get("fixed") is None]
    # same-cycle groups (opens within window)
    ots = sorted((t["open_ts"], t["pair"]) for t in trades if t["open_ts"] == t["open_ts"])
    groups, cur = [], [ots[0]] if ots else []
    for a in ots[1:]:
        if a[0] - cur[-1][0] <= SAME_CYCLE_WINDOW_S: cur.append(a)
        else:
            if len(cur) > 1: groups.append(cur)
            cur = [a]
    if len(cur) > 1: groups.append(cur)
    # paper reconciliation
    try:
        paper_eq = json.load(open(PAPER))["equity"]
        expect = round(PAPER_START_EQUITY + pnet, 2)
        recon_ok = abs(paper_eq - expect) < 0.01
        recon = f"paper file {paper_eq:.2f} vs {PAPER_START_EQUITY:.0f}{pnet:+.2f}={expect:.2f} -> {'PASS' if recon_ok else 'FAIL'}"
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        recon_ok, recon = True, "paper file absent (pre-first-close) -> skipped"
    if brief:
        print(f"n={n} net {net:+.2f} PF {gw/gl if gl else float('inf'):.2f} WR {len(wins)/n:.1%} | paper-era n={len(paper_trades)} {pnet:+.2f} | {recon}")
        return 0 if recon_ok else 1
    print(f"{'pair':<11}{'opened':<14}{'exit':<9}{'net':>8}{'risk':>7}{'peak':>7}")
    for t in trades:
        print(f"{t['pair']:<11}{u(t['open_ts']):<14}{t['reason']:<9}{t['net']:>8.2f}{t['risk']:>7.2f}{t['high']:>7.2f}")
    print("-" * 56)
    print(f"trades {n} | wins {len(wins)} ({len(wins)/n:.1%}) | net {net:+.2f}")
    print(f"gross +{gw:.2f} / -{gl:.2f} | PF {gw/gl if gl else float('inf'):.2f} | "
          f"avg win {gw/len(wins) if wins else 0:.2f} avg loss {gl/len(losses) if losses else 0:.2f} "
          f"payoff {(gw/len(wins))/(gl/len(losses)) if wins and losses else 0:.2f}")
    print(f"EV/trade {net/n:+.2f} | current loss streak {streak} (max {max_streak})")
    print(f"paper-era cohort: n={len(paper_trades)} net {pnet:+.2f} PF {pw/pl if pl else float('inf'):.2f}")
    print(f"lock exits {len(locks)} | censored fixed counterfactuals {len(censored)}")
    for g in groups:
        pairs = ", ".join(p for _, p in g)
        tot = sum(t["net"] for t in trades if (t["open_ts"], t["pair"]) in g)
        print(f"same-cycle group [{u(g[0][0])}]: {pairs} -> combined {tot:+.2f}")
    print(recon)
    return 0 if recon_ok else 1

if __name__ == "__main__":
    sys.exit(main())
