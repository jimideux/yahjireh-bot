#!/usr/bin/env python3
"""
ltf_executor.py — sniper-ltf-exec. Takes A-grade LTF signals as real positions.

Built against the ACTUAL /root/trading stack (blofin.py, peace.py, love.py read
in full before writing this). Key architectural facts this file depends on:

  * BloFinClient.place_order(inst_id, side, price, size, reduce_only, order_type)
      -> True | False | "DRYRUN".  No orderId returned, so entries are MARKET
      orders and we reconcile against get_positions(), not order ids.
  * BloFinClient.place_tpsl(inst_id, side, sl_trigger, size, margin_mode)
      -> SL ONLY despite the name. Market-close on trigger, reduce-only,
      mark-price trigger. Needs margin_mode; place_order hardcodes "cross",
      so we pass margin_mode="cross" explicitly and never depend on reading
      it back from a position that might not have settled yet.
  * The five-guard LIVE_TRADING_ENABLED stack lives INSIDE the client. This
    file adds no bypass and no flag of its own: when the stack is dry, every
    mutating call returns the string "DRYRUN" and we simulate; when the stack
    is live, the same code trades. One switch, theirs, unchanged.
  * peace.py only manages positions registered in `ownership`. This executor
    NEVER registers its positions there — so peace ignores them ("unowned"),
    exactly as it ignores Jimi's manual trades. Two exit engines, zero overlap.
  * Manual trades outrank the bot: any pair with an untracked position, any
    pending order we didn't place, or any ownership-registered (trend-bot)
    pair is skipped for entry. The bot yields to the human, never the reverse.

Exit modes (EXIT_MODE below), all three journaled per trade as counterfactuals:
  fixed     — the backtested config: exchange SL at the signal's structure
              stop, exchange TP at 2.2R. No management. (PF 1.61 family.)
  lock_once — at +$LOCK_ARM_USD unrealized, move the stop to entry+fees,
              then hands off. One intervention, then fixed behavior.
  ratchet   — Jimi's spec, parameterized from peace.get_trail_lock's shape:
              +$25 arms breakeven+fees; +$40 locks +$25; above that trails
              LADDER_GAP_USD behind the high-water mark. (Backtest warning:
              the trailing family scored PF 0.60 on 4H — the whole point of
              journaling all three is to settle this with YOUR fills.)

Stages: dry-run (guards closed, simulated fills at live prices) -> DEMO
(IS_DEMO=true creds in /root/trading/.env, real orders, fake money) -> live.
This file is identical across all three; only the env decides.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/root/trading")

import ownership                                    # read-only: is_owned()
from love import config as love_cfg
from joy import send
from exchange.blofin import BloFinClient, LIVE_TRADING_ENABLED
from ltf_signals import LTFScanner, LTFConfig, normalize_candles
import runner_ltf                                   # reuse discover_pairs + knobs
from risk import RiskManager, RiskConfig, OpenPosition

# ---------------------------------------------------------------------------
# Knobs — this block is yours
# ---------------------------------------------------------------------------

EXIT_MODE = "ratchet"            # "fixed" | "lock_once" | "ratchet"

LOCK_ARM_USD = 25.0              # your number: up $25 -> start locking
LOCK_FIRST_USD = 5.0             # first lock: breakeven + ~fees
LOCK_STEP_HIGH = 40.0            # at +$40 high-water, lock the full $25
LADDER_GAP_USD = 15.0            # above that: stop trails high-water by $15

RISK_PCT_PER_TRADE = 0.010       # 1% of usable equity risked per trade
MAX_EXEC_SLOTS = 2               # concurrent executor positions
MAX_NOTIONAL_USD = love_cfg.max_position_notional_usd   # 550 from love.py
RESERVE_USD = love_cfg.reserve_usd                      # 300 from love.py
CRYPTO_ONLY = True               # skip tokenized equities/metals for AUTO entries
SLIPPAGE_BP = 2.0                # dry-run fill realism: 0.02% adverse on market
FEE_RT = 0.0012                  # taker round-trip, matches all prior math

WATCH_INTERVAL_S = 5             # position management cadence (peace uses 5)
BAR_S = 900
GRACE_S = 8                      # enter a touch after the alert runner wakes

STATE_PATH = "/root/trading/ltf_exec_positions.json"
TRADES_PATH = "/root/trading/ltf_exec_trades.jsonl"

SCAN_CFG = LTFConfig(
    ltf="15m", htf="4H",
    alert_style="minimal",
    mode="strict",               # executor takes A-grades ONLY
    max_alerts_per_day=10,       # entry budget, separate from alert budget
    cooldown_per_pair_s=4 * 3600,
)
EXEC_SCAN_STATE = "/root/trading/ltf_exec_scan_state.json"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _dry() -> bool:
    return not LIVE_TRADING_ENABLED


# ---------------------------------------------------------------------------
# Position state (survives Restart=always)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def journal_row(row: dict) -> None:
    with open(TRADES_PATH, "a") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Exit-mode engine (pure; unit-tested)
# ---------------------------------------------------------------------------

def ratchet_lock(high_usd: float) -> float | None:
    """Jimi's ladder, shaped like peace.get_trail_lock with his $25 arm."""
    if high_usd >= 100.0:
        return high_usd - 18.0
    if high_usd >= 70.0:
        return high_usd - 16.0
    if high_usd >= LOCK_STEP_HIGH:
        return max(LOCK_ARM_USD, high_usd - LADDER_GAP_USD)
    if high_usd >= LOCK_ARM_USD:
        return LOCK_FIRST_USD
    return None


def lock_once_lock(high_usd: float) -> float | None:
    return LOCK_FIRST_USD if high_usd >= LOCK_ARM_USD else None


def decide_exit(mode: str, *, mark: float, entry: float, stop: float,
                target: float, side: str, upnl: float,
                high_usd: float) -> tuple[str, float] | None:
    """Returns (reason, exit_price) if this mode exits now, else None.
    Price-level exits use the level itself (that's where the exchange order
    would fill); lock exits use mark (software close)."""
    long = side == "long"
    if (long and mark <= stop) or (not long and mark >= stop):
        return ("stop", stop)
    if (long and mark >= target) or (not long and mark <= target):
        return ("target", target)
    if mode == "fixed":
        return None
    lock = ratchet_lock(high_usd) if mode == "ratchet" else lock_once_lock(high_usd)
    if lock is not None and upnl > 0 and upnl <= lock:
        return (f"lock@{lock:.0f}", mark)
    return None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class Executor:

    def __init__(self, client: BloFinClient):
        self.client = client
        self.scanner = LTFScanner(SCAN_CFG, state_path=EXEC_SCAN_STATE)
        self.risk = RiskManager(RiskConfig(
            max_risk_pct_per_trade=RISK_PCT_PER_TRADE,
            max_concurrent_positions=MAX_EXEC_SLOTS,
        ), state_path="/root/trading/ltf_exec_risk_state.json")
        self.state = load_state()          # inst_id -> position dict
        self._inst_cache: dict = {}

    # -- instrument helpers --------------------------------------------------

    async def inst(self, inst_id: str) -> dict:
        if inst_id not in self._inst_cache:
            self._inst_cache[inst_id] = await self.client.get_instrument(inst_id)
        return self._inst_cache[inst_id]

    async def contract_value(self, inst_id: str) -> float:
        return float((await self.inst(inst_id)).get("contractValue", "1") or 1)

    async def is_crypto(self, inst_id: str) -> bool:
        ac = str((await self.inst(inst_id)).get("assetClass", "")).lower()
        return (not ac) or ac.startswith("crypt")

    # -- entry gate ----------------------------------------------------------

    async def pair_is_free(self, inst_id: str) -> tuple[bool, str]:
        """The bot yields to the human and to the trend bot. Always."""
        if inst_id in self.state:
            return False, "executor already in this pair"
        if ownership.is_owned(inst_id):
            return False, "trend-bot owns this pair"
        for p in await self.client.get_positions():
            if p.get("instId") == inst_id and abs(float(p.get("positions") or 0)) > 0:
                return False, "untracked (manual?) position exists"
        for o in await self.client.get_pending_orders(inst_id):
            return False, "untracked pending order exists"
        return True, ""

    # -- entry ---------------------------------------------------------------

    async def try_enter(self, sig) -> bool:
        pair = sig.pair
        if CRYPTO_ONLY and not await self.is_crypto(pair):
            _log(f"skip {pair}: non-crypto assetClass (CRYPTO_ONLY)")
            return False
        free, why = await self.pair_is_free(pair)
        if not free:
            _log(f"skip {pair}: {why}")
            return False

        equity = await self.client.get_equity()
        usable = max(0.0, equity - RESERVE_USD)
        if usable <= 0:
            _log(f"skip {pair}: no usable equity (eq={equity:.2f})")
            return False

        stop_frac = abs(sig.entry - sig.stop) / sig.entry
        notional = min((usable * RISK_PCT_PER_TRADE) / stop_frac, MAX_NOTIONAL_USD)

        open_pos = [OpenPosition(pair=k, side=v["side"], notional=v["notional"],
                                 entry_price=v["entry"])
                    for k, v in self.state.items()]
        verdict = self.risk.evaluate_entry(
            pair=pair, side=sig.direction, equity=usable,
            proposed_notional=notional, proposed_leverage=1,
            entry_price=sig.entry, stop_price=sig.stop, tp_price=sig.target,
            open_positions=open_pos, atr=sig.atr_pct * sig.entry)
        if not verdict:
            _log(f"risk block {pair}: {verdict}")
            return False

        contracts = await self.client.calc_contracts(pair, notional)
        if not contracts or contracts <= 0:
            _log(f"skip {pair}: sizing gave 0 contracts for ${notional:.0f}")
            return False

        side = "buy" if sig.direction == "long" else "sell"
        res = await self.client.place_order(pair, side, size=contracts,
                                            order_type="market")
        dry = (res == "DRYRUN")
        if res is False or res is None:
            _log(f"ENTRY FAILED {pair} — no position, nothing to protect")
            return False

        mark = await self.client.get_mark_price(pair)
        slip = mark * SLIPPAGE_BP / 10_000.0
        fill = (mark + slip) if side == "buy" else (mark - slip)
        cv = await self.contract_value(pair)

        # Protect BEFORE celebrating: stop first, then TP. In dry-run both
        # return "DRYRUN"; in live a failed stop triggers immediate flatten.
        stop_res = await self.client.place_tpsl(
            pair, sig.direction, sig.stop, margin_mode="cross")
        if not dry and (stop_res is None or stop_res is False):
            _log(f"🚨 {pair}: STOP FAILED after live entry — flattening")
            await self.client.place_order(
                pair, "sell" if side == "buy" else "buy",
                size=contracts, reduce_only=True, order_type="market")
            journal_row({"ts": time.time(), "pair": pair, "event": "abort",
                         "why": "stop_arm_failed"})
            return False

        tp_side = "sell" if side == "buy" else "buy"
        tp_res = await self.client.place_order(
            pair, tp_side, price=sig.target, size=contracts,
            reduce_only=True, order_type="limit")

        pos = {
            "side": sig.direction, "entry": fill, "stop": sig.stop,
            "target": sig.target, "contracts": contracts, "cv": cv,
            "notional": notional, "opened": time.time(), "dry": dry,
            "mode": EXIT_MODE, "high_usd": 0.0,
            "tpsl_id": stop_res if isinstance(stop_res, str) and stop_res != "DRYRUN" else None,
            "tp_ok": tp_res is True,
            "virtual": {m: None for m in ("fixed", "lock_once", "ratchet")},
        }
        self.state[pair] = pos
        save_state(self.state)
        journal_row({"ts": time.time(), "pair": pair, "event": "open",
                     "dry": dry, **{k: pos[k] for k in
                     ("side", "entry", "stop", "target", "contracts",
                      "notional", "mode")}})
        max_upnl = abs(sig.target - fill) * contracts * cv
        ladder_note = ""
        if EXIT_MODE != "fixed" and max_upnl < LOCK_ARM_USD:
            ladder_note = (f"\n⚠ ladder unreachable: max +${max_upnl:,.0f} "
                           f"< ${LOCK_ARM_USD:,.0f} arm — TP fills first")
            _log(f"{pair}: lock ladder unreachable "
                 f"(max +${max_upnl:.0f} < arm ${LOCK_ARM_USD:.0f})")
        journal_row({"ts": time.time(), "pair": pair, "event": "telemetry",
                     "max_upnl": round(max_upnl, 2),
                     "lock_arm": LOCK_ARM_USD,
                     "ladder_reachable": max_upnl >= LOCK_ARM_USD})
        tag = "🧪 DRY" if dry else "🤖 LIVE"
        await send(f"{tag} EXEC ▸ {sig.direction.upper()} {pair}\n"
                   f"entry {fill:,.6g} · stop {sig.stop:,.6g} · "
                   f"target {sig.target:,.6g}\n"
                   f"size {contracts} (${notional:,.0f}) · mode {EXIT_MODE} · "
                   f"max +${max_upnl:,.0f}{ladder_note}")
        return True

    # -- close ---------------------------------------------------------------

    async def close_position(self, pair: str, reason: str, exit_px: float):
        pos = self.state.get(pair)
        if not pos:
            return
        if not pos["dry"]:
            try:
                await self.client.cancel_all_tpsl(pair)
                await self.client.cancel_all_orders(pair)
                await asyncio.sleep(0.3)
                side = "sell" if pos["side"] == "long" else "buy"
                # If the exchange TP/SL already flattened us, this reduce-only
                # market order is a no-op rejection — harmless by design.
                await self.client.place_order(pair, side, size=pos["contracts"],
                                              reduce_only=True,
                                              order_type="market")
            except Exception as e:                     # noqa: BLE001
                _log(f"close error {pair}: {e!r}")
        sign = 1.0 if pos["side"] == "long" else -1.0
        gross = (exit_px - pos["entry"]) * sign * pos["contracts"] * pos["cv"]
        fees = pos["notional"] * FEE_RT
        net = gross - fees
        self.risk.record_close(pair=pair, net_pnl=net,
                               equity_after=await self.client.get_equity())
        journal_row({"ts": time.time(), "pair": pair, "event": "close",
                     "dry": pos["dry"], "reason": reason, "exit": exit_px,
                     "gross": round(gross, 2), "net": round(net, 2),
                     "high_usd": round(pos["high_usd"], 2),
                     "virtual": pos["virtual"], "mode": pos["mode"]})
        tag = "🧪 DRY" if pos["dry"] else "🤖 LIVE"
        await send(f"{tag} EXEC ▪ CLOSED {pair} — {reason}\n"
                   f"net {'+' if net >= 0 else ''}{net:,.2f} USD "
                   f"(peak +{pos['high_usd']:,.2f})")
        del self.state[pair]
        save_state(self.state)

    # -- watch ---------------------------------------------------------------

    async def watch_tick(self):
        if not self.state:
            return
        live_positions = None
        for pair in list(self.state.keys()):
            pos = self.state[pair]
            try:
                if pos["dry"]:
                    mark = await self.client.get_mark_price(pair)
                    if mark <= 0:
                        continue
                else:
                    if live_positions is None:
                        live_positions = await self.client.get_positions()
                    row = next((p for p in live_positions
                                if p.get("instId") == pair), None)
                    if row is None or abs(float(row.get("positions") or 0)) < 1e-9:
                        # Exchange order (TP or SL) filled while we slept.
                        side_long = pos["side"] == "long"
                        mark = await self.client.get_mark_price(pair)
                        hit_tp = (mark >= pos["target"]) if side_long else (mark <= pos["target"])
                        await self.close_position(
                            pair, "exchange-tp" if hit_tp else "exchange-stop",
                            pos["target"] if hit_tp else pos["stop"])
                        continue
                    mark = float(row.get("markPrice") or 0) or \
                        await self.client.get_mark_price(pair)

                sign = 1.0 if pos["side"] == "long" else -1.0
                upnl = (mark - pos["entry"]) * sign * pos["contracts"] * pos["cv"]
                if upnl > pos["high_usd"]:
                    pos["high_usd"] = upnl

                # counterfactual bookkeeping for ALL modes
                for m in ("fixed", "lock_once", "ratchet"):
                    if pos["virtual"][m] is None:
                        d = decide_exit(m, mark=mark, entry=pos["entry"],
                                        stop=pos["stop"], target=pos["target"],
                                        side=pos["side"], upnl=upnl,
                                        high_usd=pos["high_usd"])
                        if d:
                            r, px = d
                            g = (px - pos["entry"]) * sign * pos["contracts"] * pos["cv"]
                            pos["virtual"][m] = {"reason": r, "px": px,
                                                 "net": round(g - pos["notional"] * FEE_RT, 2)}

                d = decide_exit(pos["mode"], mark=mark, entry=pos["entry"],
                                stop=pos["stop"], target=pos["target"],
                                side=pos["side"], upnl=upnl,
                                high_usd=pos["high_usd"])
                save_state(self.state)
                if d:
                    await self.close_position(pair, d[0], d[1])
            except Exception as e:                     # noqa: BLE001
                _log(f"watch error {pair}: {e!r}")

    # -- scan-and-enter cycle -------------------------------------------------

    async def entry_cycle(self, pairs):
        free_slots = MAX_EXEC_SLOTS - len(self.state)
        if free_slots <= 0:
            return
        signals = []
        for pair in pairs:
            try:
                ltf = normalize_candles(await self.client.get_candles(
                    pair, bar=SCAN_CFG.ltf, limit=200))
                htf = normalize_candles(await self.client.get_candles(
                    pair, bar=SCAN_CFG.htf, limit=120))
                sig = self.scanner.scan_pair(pair, ltf, htf)
                if sig is not None:
                    signals.append(sig)
            except Exception as e:                     # noqa: BLE001
                _log(f"scan error {pair}: {e!r}")
            await asyncio.sleep(runner_ltf.THROTTLE_S)
        signals.sort(key=lambda s: -s.fee_mult)
        for sig in signals:
            if free_slots <= 0:
                break
            gate = []
            if self.scanner.maybe_alert(sig, gate.append):
                if await self.try_enter(sig):
                    free_slots -= 1


async def main() -> None:
    client = BloFinClient()
    ex = Executor(client)
    mode = "DRY-RUN (guards closed)" if _dry() else "⚠️ LIVE"
    _log(f"sniper-ltf-exec up — {mode}, exit_mode={EXIT_MODE}, "
         f"risk {RISK_PCT_PER_TRADE:.1%}/trade, slots {MAX_EXEC_SLOTS}, "
         f"crypto_only={CRYPTO_ONLY}, tracking {len(ex.state)} position(s)")
    await send(f"{'🧪' if _dry() else '🤖'} sniper-ltf-exec online — "
               f"{'dry-run' if _dry() else 'LIVE'}, mode {EXIT_MODE}, "
               f"{RISK_PCT_PER_TRADE:.1%} risk, {MAX_EXEC_SLOTS} slots")
    if "--once" in sys.argv:
        # smoke-test: one discovery, one entry cycle, one watch tick, exit.
        try:
            pairs, d = await runner_ltf.discover_pairs(client)
            _log(f"universe: {d['qualified']} pairs")
        except Exception as e:                         # noqa: BLE001
            pairs = list(runner_ltf.CORE_PAIRS)
            _log(f"discovery failed, core-6: {e!r}")
        await ex.entry_cycle(pairs)
        await ex.watch_tick()
        _log(f"--once complete: {len(ex.state)} position(s) tracked, "
             f"state at {STATE_PATH}")
        try:
            await client.close()
        except Exception:                              # noqa: BLE001
            pass
        return
    pairs, pairs_expiry = list(runner_ltf.CORE_PAIRS), 0.0
    next_bar = 0.0
    try:
        while True:
            now = time.time()
            if now >= pairs_expiry:
                try:
                    pairs, d = await runner_ltf.discover_pairs(client)
                    pairs_expiry = now + runner_ltf.PAIRS_TTL_S
                    _log(f"universe: {d['qualified']} pairs")
                except Exception as e:                 # noqa: BLE001
                    pairs, pairs_expiry = list(runner_ltf.CORE_PAIRS), now + 1800
                    _log(f"discovery failed, core-6 fallback: {e!r}")
            if now >= next_bar:
                await ex.entry_cycle(pairs)
                next_bar = (int(now) // BAR_S + 1) * BAR_S + GRACE_S
            await ex.watch_tick()
            await asyncio.sleep(WATCH_INTERVAL_S)
    finally:
        try:
            await client.close()
        except Exception:                              # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
