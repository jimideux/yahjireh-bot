"""Exercises every gate in risk.py using love.py's real parameter values."""
import os, time, tempfile
from risk_ltf import RiskManager, RiskConfig, OpenPosition

STATE = os.path.join(tempfile.mkdtemp(), "risk_state.json")
EQUITY = 1000.0

def fresh():
    if os.path.exists(STATE):
        os.remove(STATE)
    rm = RiskManager(state_path=STATE)
    rm.roll_day_if_needed(EQUITY)
    return rm

# love.py: leverage 3, margin_pct 0.15, tp 2.5%, sl 1.0%
NOTIONAL = EQUITY * 0.15 * 3      # 450
ENTRY, SL, TP = 100.0, 99.0, 102.5

def base(rm, **kw):
    args = dict(pair="BTC-USDT", side="long", equity=EQUITY,
                proposed_notional=NOTIONAL, proposed_leverage=3,
                entry_price=ENTRY, stop_price=SL, tp_price=TP,
                open_positions=[])
    args.update(kw)
    return rm.evaluate_entry(**args)

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))

# --- 1. baseline should pass -------------------------------------------------
rm = fresh()
v = base(rm)
check("baseline trade allowed", v.allow, str(v))
# risk = 450 * 1% = $4.50 = 0.45% of equity, under the 1.5% cap
check("max_notional computed", v.max_notional > 0, f"{v.max_notional:.0f}")

# --- 2. per-trade risk cap ---------------------------------------------------
rm = fresh()
v = base(rm, proposed_notional=EQUITY * 10)   # 10x equity notional
check("oversized notional blocked", not v.allow, str(v))

# --- 3. fee gate: scalper-shaped TP ------------------------------------------
rm = fresh()
v = base(rm, tp_price=100.3)   # 0.3% TP vs 0.12% round-trip fees
check("fee-dominated TP blocked", not v.allow, str(v))

# --- 4. leverage cap ---------------------------------------------------------
rm = fresh()
v = base(rm, pair="SUI-USDT", proposed_leverage=10)
check("leverage over asset cap blocked", not v.allow, str(v))

# --- 5. cluster limit --------------------------------------------------------
rm = fresh()
held = [OpenPosition(pair="ETH-USDT", side="long", notional=200, entry_price=3000)]
v = base(rm, pair="BTC-USDT", open_positions=held)
check("majors cluster limit blocked", not v.allow, str(v))
rm = fresh()
v = base(rm, pair="XRP-USDT", open_positions=held)   # different cluster
check("cross-cluster allowed", v.allow, str(v))

# --- 6. slot limit -----------------------------------------------------------
rm = fresh()
two = [OpenPosition(pair="ETH-USDT", side="long", notional=100, entry_price=3000),
       OpenPosition(pair="XRP-USDT", side="long", notional=100, entry_price=0.5)]
v = base(rm, pair="LINK-USDT", open_positions=two)
check("slot limit blocked", not v.allow, str(v))

# --- 7. inverted stop --------------------------------------------------------
rm = fresh()
v = base(rm, stop_price=101.0)   # stop above entry on a long
check("inverted stop blocked", not v.allow, str(v))

# --- 8. consecutive-loss breaker ---------------------------------------------
rm = fresh()
for i in range(3):
    rm.record_close(pair="BTC-USDT", net_pnl=-5.0, equity_after=EQUITY - 5 * (i + 1))
v = base(rm, equity=EQUITY - 15)
check("3-loss breaker blocked", not v.allow, str(v))
check("cooldown armed", rm.state["cooldown_until"] > time.time())

# --- 9. daily drawdown -------------------------------------------------------
rm = fresh()
v = base(rm, equity=EQUITY * 0.95)   # -5%, past the 4% cap
check("daily DD blocked", not v.allow, str(v))
check("halt latched", rm.state["halt_reason"] == "daily_drawdown")

# --- 10. halt clearing requires the phrase -----------------------------------
check("wrong ack rejected", rm.clear_halt("yes") is False)
check("correct ack accepted", rm.clear_halt("I have reviewed the cause") is True)

# --- 11. volatility spike ----------------------------------------------------
rm = fresh()
v = base(rm, atr=8.0)   # ATR 8% of a 100 price
check("vol spike blocked", not v.allow, str(v))

# --- 12. liquidation buffer --------------------------------------------------
rm = fresh()
tight = OpenPosition("BTC-USDT", "long", 450, 100.0, liq_price=95.0, mark_price=100.0)
safe  = OpenPosition("BTC-USDT", "long", 450, 100.0, liq_price=70.0, mark_price=100.0)
check("tight liq buffer flagged", not rm.liquidation_buffer_ok(tight).allow)
check("safe liq buffer ok", rm.liquidation_buffer_ok(safe).allow)

# --- 13. state survives restart ----------------------------------------------
rm = fresh()
rm.record_close(pair="BTC-USDT", net_pnl=-5.0, equity_after=995.0)
reloaded = RiskManager(state_path=STATE)
check("state persists across restart", reloaded.state["consecutive_losses"] == 1)

# --- 14. EV math -------------------------------------------------------------
rm = fresh()
ev_good = rm.expected_value_pct(0.435, 0.025, 0.010)
ev_thin = rm.expected_value_pct(0.333, 0.025, 0.010)
ev_scalp = rm.expected_value_pct(0.320, 0.003, 0.002)
check("4H+daily EV positive", ev_good > 0, f"{ev_good:+.4%}")
check("1H EV marginal", 0 < ev_thin < 0.001, f"{ev_thin:+.4%}")
check("scalper EV negative", ev_scalp < 0, f"{ev_scalp:+.4%}")

# --- report ------------------------------------------------------------------
print(f"{'':2}{'test':<34}{'':4}result")
print("-" * 72)
fails = 0
for name, ok, detail in results:
    mark = "PASS" if ok else "FAIL"
    if not ok:
        fails += 1
    print(f"  {name:<34}    {mark}   {detail[:60]}")
# ---- regressions added Sep 4 2026 (Phase 0) --------------------------------
import risk_ltf as risk
# exposure gate true-positive (the day-one gap found via WLFI forensics)
_e2 = risk.RiskManager(risk.RiskConfig(max_risk_pct_per_trade=0.01,
                                       max_concurrent_positions=2),
                       state_path="/tmp/p0_exp.json")
_op = [risk.OpenPosition(pair="PUMP-USDT", side="long", notional=550.0,
                         entry_price=0.002815563)]
_kw2 = dict(pair="WLFI-USDT", side="long", proposed_notional=550.0,
            proposed_leverage=1, entry_price=0.06041208,
            stop_price=0.059916657862195806, tp_price=0.06146335270316924,
            open_positions=_op, atr=0.0003)
_v = _e2.evaluate_entry(equity=589.43, **_kw2)
check("exposure gate blocks 2nd slot at $589",
      not _v.allow and any("exposure" in r for r in _v.reasons), _v.reasons)
_v = _e2.evaluate_entry(equity=1200.0, **_kw2)
check("exposure gate allows at $1200", _v.allow, _v.reasons)

# breaker release cycle (the Sep 3 deadlock fix)
import os as _os
if _os.path.exists("/tmp/p0_brk.json"): _os.remove("/tmp/p0_brk.json")
_e3 = risk.RiskManager(risk.RiskConfig(max_risk_pct_per_trade=0.01,
                                       max_concurrent_positions=2),
                       state_path="/tmp/p0_brk.json")
_t0 = 1_800_000_000.0
_kw3 = dict(pair="SOL-USDT", side="long", equity=1578.0, proposed_notional=550.0,
            proposed_leverage=1, entry_price=100.0, stop_price=99.0,
            tp_price=102.2, open_positions=[], atr=0.6)
for _i in range(3):
    _e3.record_close(pair="X", net_pnl=-4.0, equity_after=1578-4*(_i+1), now=_t0+_i*60)
_v = _e3.evaluate_entry(now=_t0+300, **_kw3)
check("breaker blocks during 6h cooldown", not _v.allow, _v.reasons)
_v = _e3.evaluate_entry(now=_t0+7*3600, **_kw3)
check("breaker RELEASES after cooldown served", _v.allow, _v.reasons)
_e3.record_close(pair="X", net_pnl=+8.0, equity_after=1580, now=_t0+7*3600+60)
check("win resets streak", _e3.state["consecutive_losses"] == 0)

print("-" * 72)
print(f"{len(results) - fails}/{len(results)} passed")

