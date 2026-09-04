"""Verifies the LTF scanner fires on a valid pullback and rejects near-misses."""
import os, tempfile, math
from ltf_signals import LTFScanner, LTFConfig, normalize_candles, viability

STATE = os.path.join(tempfile.mkdtemp(), "ltf.json")


def bar(ts, o, h, l, c, v=1000.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def htf_uptrend(n=120, start=100.0, step=0.9):
    """4H candles in a clean uptrend."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append(bar(i * 14400, c - step * 0.4, c + step * 0.5, c - step * 0.6, c))
    return out


def htf_flat(n=120, start=100.0):
    out = []
    for i in range(n):
        c = start + (0.4 if i % 2 else -0.4)
        out.append(bar(i * 14400, c, c + 0.5, c - 0.5, c))
    return out


def ltf_pullback_setup(n=200, base=100.0, drift=0.06, atr_scale=0.55,
                       reclaim=True, deep=False, weak_close=False):
    """Uptrending LTF that pulls back to EMA20 then reclaims on the last bar.

    atr_scale controls bar size -> controls ATR% -> controls fee viability.
    """
    out = []
    price = base
    for i in range(n - 12):
        price += drift
        rng = atr_scale
        out.append(bar(i * 900, price - rng * 0.3, price + rng * 0.5,
                       price - rng * 0.5, price))

    peak = price
    # 10-bar pullback, drifting down toward/below the EMA20
    depth = atr_scale * (4.0 if deep else 1.6)
    for j in range(10):
        price = peak - depth * (j + 1) / 10.0
        rng = atr_scale
        out.append(bar((n - 12 + j) * 900, price + rng * 0.3, price + rng * 0.4,
                       price - rng * 0.6, price))

    low = price
    if reclaim:
        # strong bullish reclaim bar closing near its high
        c = low + depth * 1.15
        o = low + depth * 0.1
        h = c + atr_scale * 0.08
        l = low - atr_scale * 0.15
        if weak_close:
            c = low + depth * 0.35          # closes weak, mid-range
            h = low + depth * 1.2
        out.append(bar((n - 2) * 900, o, h, l, c))
    else:
        out.append(bar((n - 2) * 900, low, low + atr_scale * 0.2,
                       low - atr_scale * 0.5, low - atr_scale * 0.3))
    return out


results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))


def fresh(**kw):
    if os.path.exists(STATE):
        os.remove(STATE)
    return LTFScanner(LTFConfig(**kw), state_path=STATE)


# --- 1. valid setup on a volatile pair fires ---------------------------------
sc = fresh()
ltf = ltf_pullback_setup(atr_scale=0.60)      # ~0.5%+ ATR, clears fees
sig = sc.scan_pair("SOL-USDT", ltf, htf_uptrend())
check("valid pullback fires", sig is not None, str(sig and sig.direction))
if sig:
    check("direction matches HTF", sig.direction == "long")
    check("stop below entry", sig.stop < sig.entry, f"{sig.stop:.2f} < {sig.entry:.2f}")
    check("target above entry", sig.target > sig.entry)
    check("clears fee gate", sig.fee_mult >= 8.0, f"{sig.fee_mult:.1f}x")
    check("EV positive", sig.ev_pct > 0, f"{sig.ev_pct:+.3%}")
    check("R:R honoured", abs(sig.target_pct / sig.stop_pct - 2.2) < 0.05,
          f"{sig.target_pct/sig.stop_pct:.2f}R")
    check("clean setup is grade A", sig.grade == "A" and not sig.warnings)

# --- 2. fee grading: advisory fires B, strict suppresses, <2x always blocked --
ltf_thin = ltf_pullback_setup(atr_scale=0.15, drift=0.015)    # BTC-5m-ish ATR
sc = fresh()                                                   # default: advisory
sig2 = sc.scan_pair("BTC-USDT", ltf_thin, htf_uptrend(step=0.3))
check("advisory: thin setup fires as B",
      sig2 is not None and sig2.grade == "B" and sig2.warnings,
      (sig2.warnings[0][:26] if sig2 and sig2.warnings else "None"))
check("B warning shows maker EV",
      sig2 is not None and "maker" in sig2.warnings[0])

sc = fresh(mode="strict")
sig2s = sc.scan_pair("BTC-USDT", ltf_thin, htf_uptrend(step=0.3))
check("strict: same setup suppressed", sig2s is None)

sc = fresh(min_atr_pct=0.0)          # isolate the hard floor from the ATR floor
ltf_dead = ltf_pullback_setup(atr_scale=0.03, drift=0.003)   # ~1.5x fees
sig2d = sc.scan_pair("BTC-USDT", ltf_dead, htf_uptrend(step=0.3))
check("hard floor: <2x fees blocked even advisory", sig2d is None)

# --- 3. no HTF trend -> no signal --------------------------------------------
sc = fresh()
sig3 = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60), htf_flat())
check("flat HTF blocks signal", sig3 is None)

# --- 4. HTF down + LTF long setup -> no signal -------------------------------
sc = fresh()
htf_down = [bar(i * 14400, 210 - i * 0.9, 211 - i * 0.9, 209 - i * 0.9, 210 - i * 0.9)
            for i in range(120)]
sig4 = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60), htf_down)
check("counter-trend long blocked", sig4 is None)

# --- 5. no reclaim bar -> no signal ------------------------------------------
sc = fresh()
sig5 = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60, reclaim=False),
                    htf_uptrend())
check("no reclaim bar blocks", sig5 is None)

# --- 6. weak close on reclaim bar -> no signal -------------------------------
sc = fresh()
sig6 = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60, weak_close=True),
                    htf_uptrend())
check("weak-close bar blocked", sig6 is None)

# --- 7. pullback too deep (structure broke) -> no signal ---------------------
sc = fresh()
sig7 = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60, deep=True),
                    htf_uptrend())
check("over-deep pullback blocked", sig7 is None)

# --- 7b. forming bar (confirm=0) must not be trusted -------------------------
sc = fresh()
ltf_form = ltf_pullback_setup(atr_scale=0.60)
ltf_form[-1]["confirm"] = 0                 # the reclaim bar is still painting
sigf = sc.scan_pair("SOL-USDT", ltf_form, htf_uptrend())
check("forming reclaim bar dropped (no repaint)", sigf is None)

sc = fresh(require_closed_bar=False)        # explicit mid-bar mode
sigf2 = sc.scan_pair("SOL-USDT", ltf_form, htf_uptrend())
check("mid-bar mode fires when opted in", sigf2 is not None)

raw9 = [["2000", "2", "3", "1", "2.5", "10", "0", "0", "0"],    # newest, forming
        ["1000", "1", "2", "0.5", "1.5", "10", "0", "0", "1"]]
n9 = normalize_candles(raw9)
check("BloFin confirm col parsed", n9[0]["confirm"] == 1 and n9[-1]["confirm"] == 0)

# --- 8. cooldown suppresses the second alert ---------------------------------
sc = fresh(cooldown_per_pair_s=3600)
sent = []
s = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60), htf_uptrend())
a1 = sc.maybe_alert(s, sent.append, now=1000.0)
a2 = sc.maybe_alert(s, sent.append, now=1600.0)      # 10 min later
a3 = sc.maybe_alert(s, sent.append, now=1000.0 + 3700)
check("first alert sends", a1)
check("cooldown suppresses 2nd", not a2)
check("post-cooldown sends", a3)
check("exactly 2 messages", len(sent) == 2, f"{len(sent)}")

# --- 9. daily quota ----------------------------------------------------------
sc = fresh(max_alerts_per_day=2, cooldown_per_pair_s=0)
s = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60), htf_uptrend())
fired = sum(1 for i in range(6) if sc.maybe_alert(s, lambda m: None, now=2000.0 + i))
check("daily quota enforced", fired == 2, f"{fired} fired")

# --- 10. candle normalization ------------------------------------------------
raw_newest_first = [[3000, 3, 4, 2, 3.5], [2000, 2, 3, 1, 2.5], [1000, 1, 2, 0.5, 1.5]]
norm = normalize_candles(raw_newest_first)
check("newest-first sorted to oldest-first",
      [c["ts"] for c in norm] == [1000, 2000, 3000])

# --- 11. state survives restart ----------------------------------------------
sc = fresh()
s = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60), htf_uptrend())
sc.maybe_alert(s, lambda m: None, now=5000.0)
reloaded = LTFScanner(LTFConfig(), state_path=STATE)
check("alert state persists", reloaded.state["last_alert"].get("SOL-USDT") == 5000.0)

# --- report ------------------------------------------------------------------
print(f"  {'test':<36}result")
print("-" * 70)
fails = 0
for name, ok, detail in results:
    if not ok:
        fails += 1
    print(f"  {name:<36}{'PASS' if ok else 'FAIL'}   {detail[:26]}")
print("-" * 70)
print(f"{len(results)-fails}/{len(results)} passed\n")

# --- sample alert ------------------------------------------------------------
sc = fresh()
demo = sc.scan_pair("SOL-USDT", ltf_pullback_setup(atr_scale=0.60), htf_uptrend())
if demo:
    print("=" * 44)
    print(demo.to_telegram())
    print("=" * 44)
