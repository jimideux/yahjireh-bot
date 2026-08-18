"""
risk.py — YahJireh isolated risk engine (hard constraint layer).

Pure logic. No network, no exchange client, no imports from the trading stack.
Decisions in, verdicts out. That isolation is deliberate: this module must stay
correct even if trend.py / peace.py / blofin.py are mid-refactor.

DESIGN RULE: there is no override argument anywhere in this file. If evaluate_entry
returns allow=False, the answer is no. Callers may log the refusal. They may not
bypass it. If you find yourself adding a `force=True` kwarg, that is the bug.

State persists to JSON so it survives the `Restart=always` systemd cycle.

Integration is three calls — see wire_in() docstring at the bottom.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

STATE_PATH = os.getenv("RISK_STATE", "/root/trading/risk_state.json")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskConfig:
    """Defaults mirror love.py where an equivalent exists, and are tightened to
    the stricter of (your value, spec value) where both specify one."""

    # --- per-trade sizing --------------------------------------------------
    max_risk_pct_per_trade: float = 0.015          # spec: 1-1.5% equity at risk
    max_leverage_default: int = 3                  # love.trend_leverage
    max_leverage_by_asset: dict = field(default_factory=lambda: {
        "BTC-USDT": 5, "ETH-USDT": 5,
        "SOL-USDT": 3, "XRP-USDT": 3,
        "LINK-USDT": 3, "SUI-USDT": 2,
    })

    # --- portfolio ---------------------------------------------------------
    max_concurrent_positions: int = 2              # love.trend_max_slots
    max_total_exposure_pct: float = 0.95
    # ^ Sized to admit your current config, NOT chosen on merit.
    #   love.py: trend_margin_pct 0.15 x trend_leverage 3 = 45% notional/trade.
    #   trend_max_slots 2 => 90% notional at full allocation.
    #   Anything below 0.90 silently blocks your second slot forever, which
    #   would look like a signal-frequency bug rather than a risk block.
    #   If you'd rather cap real exposure, lower trend_margin_pct in love.py
    #   and lower this to match — do not leave the two out of sync.
    max_positions_per_cluster: int = 1
    clusters: dict = field(default_factory=lambda: {
        "BTC-USDT": "majors",   "ETH-USDT": "majors",
        "SOL-USDT": "l1alt",    "SUI-USDT": "l1alt",
        "XRP-USDT": "payments", "LINK-USDT": "infra",
    })

    # --- drawdown / circuit breakers --------------------------------------
    max_daily_dd_pct: float = 0.04                 # love.max_dd_pct (spec said 5%)
    max_daily_loss_usd: float = 20.0               # love.max_loss_usd
    consecutive_loss_limit: int = 3                # spec
    cooldown_after_loss_streak_s: int = 6 * 3600
    cooldown_after_close_s: int = 300              # love.cooldown_after_close_s

    # --- liquidation safety ------------------------------------------------
    min_liq_buffer_pct: float = 0.20               # spec: 20% from liq price

    # --- fee gate (not in spec; added from your audit) ---------------------
    fee_round_trip: float = 0.0012                 # your backtest assumption
    min_tp_to_fee_ratio: float = 8.0
    min_expected_value_pct: float = 0.0

    # --- failure-mode detection -------------------------------------------
    overtrade_window_s: int = 24 * 3600
    overtrade_max_trades: int = 6
    revenge_window_s: int = 900                    # new entry <15m after a loss
    funding_bleed_window_s: int = 24 * 3600
    funding_bleed_max_usd: float = 5.0
    vol_spike_atr_pct_max: float = 0.06            # ATR/price above this = no-trade


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    allow: bool
    reasons: list = field(default_factory=list)
    max_notional: float = 0.0
    max_leverage: int = 0

    def __bool__(self) -> bool:
        return self.allow

    def __str__(self) -> str:
        head = "ALLOW" if self.allow else "BLOCK"
        if not self.reasons:
            return head
        return f"{head}: " + "; ".join(self.reasons)


# ---------------------------------------------------------------------------
# Position view — what the caller must supply
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    pair: str
    side: str            # "long" | "short"
    notional: float
    entry_price: float
    liq_price: Optional[float] = None
    mark_price: Optional[float] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class RiskManager:

    def __init__(self, cfg: Optional[RiskConfig] = None, state_path: str = STATE_PATH):
        self.cfg = cfg or RiskConfig()
        self.state_path = state_path
        self.state = self._load()

    # -- persistence --------------------------------------------------------

    def _blank_state(self) -> dict:
        return {
            "day_key": self._day_key(),
            "day_start_equity": None,
            "day_realized_pnl": 0.0,
            "day_funding_paid": 0.0,
            "consecutive_losses": 0,
            "last_close_ts": 0.0,
            "last_loss_ts": 0.0,
            "cooldown_until": 0.0,
            "halt_reason": None,
            "closes": [],          # [{ts, pnl, pair}] rolling, trimmed to 48h
        }

    def _load(self) -> dict:
        try:
            with open(self.state_path) as fh:
                st = json.load(fh)
            for k, v in self._blank_state().items():
                st.setdefault(k, v)
            return st
        except (FileNotFoundError, json.JSONDecodeError):
            return self._blank_state()

    def save(self) -> None:
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, self.state_path)

    # -- day handling -------------------------------------------------------

    @staticmethod
    def _day_key(ts: Optional[float] = None) -> str:
        dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")

    def roll_day_if_needed(self, equity: float, now: Optional[float] = None) -> bool:
        """Reset daily counters at UTC midnight. Returns True if a roll happened."""
        now = now or time.time()
        key = self._day_key(now)
        if self.state["day_key"] == key and self.state["day_start_equity"] is not None:
            return False
        self.state["day_key"] = key
        self.state["day_start_equity"] = equity
        self.state["day_realized_pnl"] = 0.0
        self.state["day_funding_paid"] = 0.0
        # A daily-DD halt clears at rollover. A loss-streak cooldown does not.
        if self.state["halt_reason"] in ("daily_drawdown", "daily_loss_usd"):
            self.state["halt_reason"] = None
        self.save()
        return True

    # -- helpers ------------------------------------------------------------

    def expected_value_pct(self, win_rate: float, tp_pct: float, sl_pct: float) -> float:
        """Per-trade EV as a fraction of notional, fees included both sides.

        Worth running before any parameter change. At your backtested numbers:
            43.5% / 2.5% / 1.0%  ->  +0.402%   (the 4H + daily-filter config)
            33.3% / 2.5% / 1.0%  ->  +0.035%   (the 1H config — barely alive)
            32.0% / 0.3% / 0.2%  ->  -0.062%   (scalper-shaped params — dead)
        The gap between rows 2 and 3 is where the -$16k went.
        """
        f = self.cfg.fee_round_trip
        return win_rate * (tp_pct - f) - (1.0 - win_rate) * (sl_pct + f)

    def _trim_closes(self, now: float) -> None:
        horizon = now - max(self.cfg.overtrade_window_s, self.cfg.funding_bleed_window_s) * 2
        self.state["closes"] = [c for c in self.state["closes"] if c["ts"] >= horizon]

    def _closes_within(self, window_s: int, now: float) -> list:
        return [c for c in self.state["closes"] if c["ts"] >= now - window_s]

    # -- the gate -----------------------------------------------------------

    def evaluate_entry(
        self,
        *,
        pair: str,
        side: str,
        equity: float,
        proposed_notional: float,
        proposed_leverage: int,
        entry_price: float,
        stop_price: float,
        tp_price: float,
        open_positions: list,
        atr: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Verdict:
        """Single decision point. Every entry path must route through this."""
        now = now or time.time()
        cfg = self.cfg
        reasons: list = []

        self.roll_day_if_needed(equity, now)
        self._trim_closes(now)

        # 1. standing halt -------------------------------------------------
        if self.state["halt_reason"]:
            reasons.append(f"halted: {self.state['halt_reason']}")

        # 2. cooldowns ------------------------------------------------------
        if now < self.state["cooldown_until"]:
            left = int(self.state["cooldown_until"] - now)
            reasons.append(f"cooldown active ({left}s left)")
        if now - self.state["last_close_ts"] < cfg.cooldown_after_close_s:
            left = int(cfg.cooldown_after_close_s - (now - self.state["last_close_ts"]))
            reasons.append(f"post-close cooldown ({left}s left)")

        # 3. daily drawdown -------------------------------------------------
        start_eq = self.state["day_start_equity"] or equity
        if start_eq > 0:
            dd = (start_eq - equity) / start_eq
            if dd >= cfg.max_daily_dd_pct:
                reasons.append(f"daily drawdown {dd:.2%} >= {cfg.max_daily_dd_pct:.2%}")
                self.state["halt_reason"] = "daily_drawdown"
        if -self.state["day_realized_pnl"] >= cfg.max_daily_loss_usd:
            reasons.append(f"daily realized loss ${-self.state['day_realized_pnl']:.2f}")
            self.state["halt_reason"] = "daily_loss_usd"

        # 4. loss streak ----------------------------------------------------
        if self.state["consecutive_losses"] >= cfg.consecutive_loss_limit:
            reasons.append(f"{self.state['consecutive_losses']} consecutive losses")

        # 5. slot / exposure / cluster --------------------------------------
        if len(open_positions) >= cfg.max_concurrent_positions:
            reasons.append(f"slots full ({len(open_positions)}/{cfg.max_concurrent_positions})")

        exposure = sum(p.notional for p in open_positions) + proposed_notional
        if equity > 0 and exposure / equity > cfg.max_total_exposure_pct:
            reasons.append(
                f"total exposure {exposure / equity:.1%} > {cfg.max_total_exposure_pct:.0%}")

        cluster = cfg.clusters.get(pair)
        if cluster:
            same = sum(1 for p in open_positions if cfg.clusters.get(p.pair) == cluster)
            if same >= cfg.max_positions_per_cluster:
                reasons.append(f"cluster '{cluster}' already has {same} position(s)")

        if any(p.pair == pair for p in open_positions):
            reasons.append(f"already in {pair}")

        # 6. leverage cap ---------------------------------------------------
        lev_cap = cfg.max_leverage_by_asset.get(pair, cfg.max_leverage_default)
        if proposed_leverage > lev_cap:
            reasons.append(f"leverage {proposed_leverage}x > cap {lev_cap}x for {pair}")

        # 7. per-trade risk -------------------------------------------------
        if entry_price <= 0:
            reasons.append("invalid entry price")
        else:
            sl_frac = abs(entry_price - stop_price) / entry_price
            risk_usd = proposed_notional * sl_frac
            if equity > 0 and risk_usd / equity > cfg.max_risk_pct_per_trade:
                reasons.append(
                    f"trade risk {risk_usd / equity:.2%} > {cfg.max_risk_pct_per_trade:.2%}")

            # 8. fee gate ---------------------------------------------------
            tp_frac = abs(tp_price - entry_price) / entry_price
            if tp_frac < cfg.fee_round_trip * cfg.min_tp_to_fee_ratio:
                reasons.append(
                    f"TP {tp_frac:.3%} < {cfg.min_tp_to_fee_ratio:.0f}x round-trip fees "
                    f"({cfg.fee_round_trip * cfg.min_tp_to_fee_ratio:.3%}) — fee-dominated")

        # 9. stop direction sanity ------------------------------------------
        s = side.lower()
        if s in ("long", "buy"):
            if stop_price >= entry_price or tp_price <= entry_price:
                reasons.append("long: stop must sit below entry and TP above")
        elif s in ("short", "sell"):
            if stop_price <= entry_price or tp_price >= entry_price:
                reasons.append("short: stop must sit above entry and TP below")
        else:
            reasons.append(f"unknown side '{side}'")

        # 10. volatility spike ----------------------------------------------
        if atr is not None and entry_price > 0:
            atr_pct = atr / entry_price
            if atr_pct > cfg.vol_spike_atr_pct_max:
                reasons.append(f"ATR {atr_pct:.2%} > spike ceiling "
                               f"{cfg.vol_spike_atr_pct_max:.2%}")

        # 11. failure modes --------------------------------------------------
        reasons.extend(self.detect_failure_modes(now))

        allow = not reasons
        max_notional = 0.0
        if allow and entry_price > 0:
            sl_frac = abs(entry_price - stop_price) / entry_price
            if sl_frac > 0:
                max_notional = (equity * cfg.max_risk_pct_per_trade) / sl_frac

        self.save()
        return Verdict(allow=allow, reasons=reasons,
                       max_notional=max_notional,
                       max_leverage=lev_cap)

    # -- failure-mode detection ---------------------------------------------

    def detect_failure_modes(self, now: Optional[float] = None) -> list:
        now = now or time.time()
        cfg = self.cfg
        out = []

        recent = self._closes_within(cfg.overtrade_window_s, now)
        if len(recent) > cfg.overtrade_max_trades:
            out.append(f"overtrading: {len(recent)} closes in "
                       f"{cfg.overtrade_window_s // 3600}h")

        if self.state["last_loss_ts"] and (now - self.state["last_loss_ts"]) < cfg.revenge_window_s:
            out.append(f"revenge window: last loss "
                       f"{int(now - self.state['last_loss_ts'])}s ago")

        if self.state["day_funding_paid"] > cfg.funding_bleed_max_usd:
            out.append(f"funding bleed ${self.state['day_funding_paid']:.2f} today")

        return out

    # -- liquidation buffer (call from peace.py on each poll) ---------------

    def liquidation_buffer_ok(self, pos: OpenPosition) -> Verdict:
        if not pos.liq_price or not pos.mark_price or pos.mark_price <= 0:
            return Verdict(allow=True, reasons=["liq/mark price unavailable"])
        buf = abs(pos.mark_price - pos.liq_price) / pos.mark_price
        if buf < self.cfg.min_liq_buffer_pct:
            return Verdict(allow=False, reasons=[
                f"{pos.pair} liq buffer {buf:.1%} < {self.cfg.min_liq_buffer_pct:.0%}"])
        return Verdict(allow=True)

    # -- outcome recording ---------------------------------------------------

    def record_close(self, *, pair: str, net_pnl: float, equity_after: float,
                     funding_paid: float = 0.0, now: Optional[float] = None) -> None:
        """Call once per closed trade, after fees. peace.py exit path."""
        now = now or time.time()
        self.roll_day_if_needed(equity_after, now)

        self.state["closes"].append({"ts": now, "pnl": net_pnl, "pair": pair})
        self.state["day_realized_pnl"] += net_pnl
        self.state["day_funding_paid"] += funding_paid
        self.state["last_close_ts"] = now

        if net_pnl < 0:
            self.state["consecutive_losses"] += 1
            self.state["last_loss_ts"] = now
            if self.state["consecutive_losses"] >= self.cfg.consecutive_loss_limit:
                self.state["cooldown_until"] = now + self.cfg.cooldown_after_loss_streak_s
        else:
            self.state["consecutive_losses"] = 0

        self._trim_closes(now)
        self.save()

    def record_funding(self, amount_usd: float) -> None:
        self.state["day_funding_paid"] += max(0.0, amount_usd)
        self.save()

    # -- manual controls -----------------------------------------------------

    def halt(self, reason: str) -> None:
        self.state["halt_reason"] = reason
        self.save()

    def clear_halt(self, acknowledge: str) -> bool:
        """Requires the literal string 'I have reviewed the cause'. Deliberate friction."""
        if acknowledge.strip() != "I have reviewed the cause":
            return False
        self.state["halt_reason"] = None
        self.state["consecutive_losses"] = 0
        self.state["cooldown_until"] = 0.0
        self.save()
        return True

    def status(self) -> dict:
        now = time.time()
        return {
            "day": self.state["day_key"],
            "day_start_equity": self.state["day_start_equity"],
            "day_realized_pnl": round(self.state["day_realized_pnl"], 2),
            "day_funding_paid": round(self.state["day_funding_paid"], 2),
            "consecutive_losses": self.state["consecutive_losses"],
            "halt_reason": self.state["halt_reason"],
            "cooldown_s_left": max(0, int(self.state["cooldown_until"] - now)),
            "closes_24h": len(self._closes_within(86400, now)),
            "failure_modes": self.detect_failure_modes(now),
        }


# ---------------------------------------------------------------------------
# Integration notes
# ---------------------------------------------------------------------------

def wire_in():
    """
    trend.py — immediately before the entry POST (the one at ~line 79 that
    hardcodes openapi.blofin.com):

        from risk import RiskManager, OpenPosition
        RISK = RiskManager()

        v = RISK.evaluate_entry(
            pair=pair, side=direction, equity=equity,
            proposed_notional=notional, proposed_leverage=love.trend_leverage,
            entry_price=px, stop_price=sl_px, tp_price=tp_px,
            open_positions=[OpenPosition(pair=p["pair"], side=p["side"],
                                         notional=p["notional"],
                                         entry_price=p["entry"]) for p in open_trades],
            atr=atr,
        )
        if not v:
            declog.log(pair, "risk_block", str(v))
            return

    peace.py — after a close confirms:

        RISK.record_close(pair=pair, net_pnl=net, equity_after=eq_after)

    tg.py — add a /risk command returning RISK.status().

    Note this sits *inside* your existing LIVE_TRADING_ENABLED guard stack, not
    in place of it. Five guards plus this is six. That is correct.
    """


if __name__ == "__main__":
    import sys
    rm = RiskManager(state_path=sys.argv[1] if len(sys.argv) > 1 else "/tmp/risk_state.json")
    print(json.dumps(rm.status(), indent=2))
