#!/usr/bin/env python3
"""
risk.py -- YahJireh isolated hard-constraint risk layer.

DESIGN CONTRACT
---------------
1. This module is a PURE FUNCTION of (inputs, persisted state). It imports
   nothing outside the stdlib and knows nothing about BloFin, love.py,
   journal.py, or any strategy. It cannot be broken by changes to those.
2. No caller may override a denial. `RiskDecision.allowed == False` is final.
   There is deliberately no `force=True` parameter anywhere in this file.
3. Sizing is returned, never requested. The strategy proposes an entry and a
   stop; the risk layer decides how much notional (if any) is permitted.

WHY SIZING AND NOT set-leverage
-------------------------------
BloFin returns 152404 on /api/v1/account/set-leverage for this account, so
per-asset leverage caps CANNOT be enforced exchange-side. They are enforced
here by capping notional: effective_leverage = notional / equity. If the
account default leverage is higher than the cap, the position is still safe
because the notional is small enough that the risk is bounded.

STATE
-----
Persisted to risk_state.json (add to .gitignore). Survives service restarts,
which matters: a restart must not reset the daily drawdown counter or the
consecutive-loss breaker.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["RiskLimits", "RiskDecision", "RiskEngine", "CORRELATION_GROUPS"]


# ---------------------------------------------------------------------------
# Correlation grouping
# ---------------------------------------------------------------------------
# A static group map beats a runtime Pearson correlation computed on 30 candles.
# Short-window correlation estimates are dominated by sampling error, and in a
# crash *everything* goes to 1.0 anyway -- which is precisely when you need the
# constraint to hold. Static groups encode that prior explicitly.

CORRELATION_GROUPS: Dict[str, set] = {
    "majors": {"BTC-USDT", "ETH-USDT"},
    "l1": {"SOL-USDT", "AVAX-USDT", "ADA-USDT", "DOT-USDT", "NEAR-USDT",
           "APT-USDT", "SUI-USDT", "SEI-USDT", "TON-USDT"},
    "payments": {"XRP-USDT", "XLM-USDT", "LTC-USDT", "BCH-USDT"},
    "defi": {"UNI-USDT", "AAVE-USDT", "LINK-USDT", "MKR-USDT", "CRV-USDT"},
    "meme": {"DOGE-USDT", "SHIB-USDT", "PEPE-USDT", "WIF-USDT", "BONK-USDT"},
    "privacy": {"ZEC-USDT", "XMR-USDT", "DASH-USDT"},
}


def group_of(pair: str) -> str:
    """Return the correlation group for a pair, or the pair itself if ungrouped."""
    p = pair.upper()
    for name, members in CORRELATION_GROUPS.items():
        if p in members:
            return name
    return f"_ungrouped:{p}"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

@dataclass
class RiskLimits:
    """All hard constraints. Every value is a ceiling, never a target."""

    # --- per-trade sizing ---
    risk_pct_per_trade: float = 0.010      # 1.0% of equity at risk per trade
    risk_pct_max: float = 0.015            # absolute ceiling, 1.5%

    # --- daily / account ---
    max_daily_drawdown_pct: float = 0.05   # 5% from the day's anchor equity
    max_total_drawdown_pct: float = 0.20   # 20% from all-time peak equity
    min_equity_floor: float = 0.0          # absolute USDT floor; 0 disables

    # --- loss streak ---
    consecutive_loss_limit: int = 3
    cooldown_minutes_after_streak: int = 240

    # --- leverage / exposure ---
    max_effective_leverage: float = 5.0    # notional / equity, per position
    max_leverage_by_group: Dict[str, float] = field(default_factory=lambda: {
        "majors": 5.0,
        "l1": 4.0,
        "payments": 4.0,
        "defi": 3.0,
        "meme": 2.0,
        "privacy": 2.0,
    })
    max_portfolio_exposure_pct: float = 3.0   # sum(notional) / equity
    max_group_exposure_pct: float = 1.5       # sum(notional in group) / equity
    max_concurrent_positions: int = 4
    max_positions_per_group: int = 2

    # --- liquidation safety ---
    liq_buffer_pct: float = 0.20           # stop must sit >=20% of the
                                           # entry->liq distance away from liq
    default_mmr: float = 0.005             # maintenance margin rate fallback

    # --- volatility regime ---
    atr_pct_ceiling: float = 0.08          # 8% ATR/price -> no-trade mode
    atr_pct_floor: float = 0.002           # 0.2% -> too dead, spread dominates

    # --- stop sanity ---
    min_stop_distance_pct: float = 0.003   # below this, fees dominate the risk
    max_stop_distance_pct: float = 0.12    # above this, sizing gets nonsensical

    # --- overtrading / failure modes ---
    max_trades_per_hour: int = 3
    max_trades_per_day: int = 12
    max_funding_bleed_pct_per_day: float = 0.005   # 0.5% of equity

    # --- fee awareness ---
    taker_fee_pct: float = 0.0006          # BloFin taker, one side
    min_rr_after_fees: float = 1.5         # reject setups that aren't worth it

    def leverage_cap_for(self, pair: str) -> float:
        return min(
            self.max_effective_leverage,
            self.max_leverage_by_group.get(group_of(pair), self.max_effective_leverage),
        )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass
class RiskDecision:
    allowed: bool
    reason: str = "ok"
    notional: float = 0.0          # USDT notional permitted
    qty: float = 0.0               # base-asset quantity (notional / entry)
    margin: float = 0.0            # notional / effective leverage
    risk_amount: float = 0.0       # USDT lost if the stop fills exactly
    effective_leverage: float = 0.0
    warnings: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed

    @staticmethod
    def deny(reason: str, **detail) -> "RiskDecision":
        return RiskDecision(allowed=False, reason=reason, detail=detail)


# ---------------------------------------------------------------------------
# Math helpers (pure, independently testable)
# ---------------------------------------------------------------------------

def liq_distance_pct(leverage: float, mmr: float) -> float:
    """
    Approximate fractional distance from entry to liquidation, isolated margin.

        long  liq ~= entry * (1 - 1/L + mmr)
        short liq ~= entry * (1 + 1/L - mmr)

    Magnitude is symmetric, so one function serves both sides. This IGNORES
    fees and funding accrued against the position, so it is slightly optimistic
    -- always prefer the exchange-reported liquidation price when you have it.
    """
    if leverage <= 0:
        return 0.0
    return max(0.0, (1.0 / leverage) - mmr)


def stop_distance_pct(entry: float, stop: float) -> float:
    if entry <= 0:
        return 0.0
    return abs(entry - stop) / entry


def rr_after_fees(entry: float, stop: float, target: float,
                  taker_fee_pct: float) -> float:
    """
    Reward:risk net of a taker round trip on both legs.
    Fees are charged on notional, so they scale out of the ratio as
    2 * fee / stop_distance -- which is exactly why tight stops are expensive.
    """
    sd = stop_distance_pct(entry, stop)
    td = stop_distance_pct(entry, target)
    if sd <= 0:
        return 0.0
    round_trip = 2.0 * taker_fee_pct
    net_reward = td - round_trip
    net_risk = sd + round_trip
    if net_risk <= 0:
        return 0.0
    return net_reward / net_risk


def _utc_day_key(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class RiskEngine:
    """
    Stateful risk gate. One instance per process. Safe to construct in both
    trend.py (entry decisions) and a monitoring process (read-only checks),
    though only one process should call record_close().
    """

    STATE_VERSION = 1

    def __init__(self,
                 limits: Optional[RiskLimits] = None,
                 state_path: str = "/root/trading/risk_state.json",
                 pause_flag_path: str = "/root/trading/PAUSED"):
        self.limits = limits or RiskLimits()
        self.state_path = state_path
        self.pause_flag_path = pause_flag_path
        self.state = self._load_state()

    # -- persistence ---------------------------------------------------------

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
            "day_key": _utc_day_key(),
            "day_anchor_equity": 0.0,
            "day_realized_pnl": 0.0,
            "day_funding_paid": 0.0,
            "day_trade_count": 0,
            "peak_equity": 0.0,
            "consecutive_losses": 0,
            "cooldown_until": 0.0,
            "recent_entry_ts": [],
            "halted": False,
            "halt_reason": "",
        }

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, "r") as fh:
                s = json.load(fh)
            if s.get("version") != self.STATE_VERSION:
                base = self._default_state()
                base.update({k: v for k, v in s.items() if k in base})
                base["version"] = self.STATE_VERSION
                return base
            return s
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self._default_state()

    def _save_state(self) -> None:
        """Atomic write. A torn risk_state.json would silently reset the breaker."""
        d = os.path.dirname(self.state_path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".risk_state.", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(self.state, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        except OSError as exc:
            # Never let a disk problem crash the trading loop, but make it loud.
            print(f"[risk] WARN could not persist state: {exc}")

    # -- lifecycle -----------------------------------------------------------

    def roll_day_if_needed(self, equity: float, now: Optional[float] = None) -> None:
        """Call once per loop before evaluate_entry(). Anchors the daily DD."""
        key = _utc_day_key(now)
        if self.state["day_key"] != key or self.state["day_anchor_equity"] <= 0:
            self.state.update({
                "day_key": key,
                "day_anchor_equity": equity,
                "day_realized_pnl": 0.0,
                "day_funding_paid": 0.0,
                "day_trade_count": 0,
                "recent_entry_ts": [],
            })
            # A new day clears the daily halt, but NOT the loss streak --
            # a 3-loss streak at 23:58 UTC is still a 3-loss streak at 00:02.
            if self.state.get("halt_reason", "").startswith("daily_"):
                self.state["halted"] = False
                self.state["halt_reason"] = ""
            self._save_state()

        if equity > self.state["peak_equity"]:
            self.state["peak_equity"] = equity
            self._save_state()

    def record_entry(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self.state["recent_entry_ts"].append(now)
        cutoff = now - 3600.0
        self.state["recent_entry_ts"] = [
            t for t in self.state["recent_entry_ts"] if t >= cutoff
        ]
        self.state["day_trade_count"] += 1
        self._save_state()

    def record_close(self, net_pnl: float, equity_after: float,
                     now: Optional[float] = None) -> None:
        """Call from the exit path with the realised net PnL of one position."""
        now = now if now is not None else time.time()
        self.state["day_realized_pnl"] += net_pnl

        if net_pnl < 0:
            self.state["consecutive_losses"] += 1
            if self.state["consecutive_losses"] >= self.limits.consecutive_loss_limit:
                self.state["cooldown_until"] = now + self.limits.cooldown_minutes_after_streak * 60.0
        else:
            self.state["consecutive_losses"] = 0

        if equity_after > self.state["peak_equity"]:
            self.state["peak_equity"] = equity_after
        self._save_state()

    def record_funding(self, amount_paid: float) -> None:
        """Positive amount = funding cost paid out."""
        self.state["day_funding_paid"] += max(0.0, amount_paid)
        self._save_state()

    def halt(self, reason: str) -> None:
        self.state["halted"] = True
        self.state["halt_reason"] = reason
        self._save_state()

    def clear_halt(self) -> None:
        self.state["halted"] = False
        self.state["halt_reason"] = ""
        self._save_state()

    # -- account-level gate --------------------------------------------------

    def account_status(self, equity: float, now: Optional[float] = None) -> RiskDecision:
        """Checks that don't depend on the proposed trade. Cheap; call first."""
        now = now if now is not None else time.time()
        L = self.limits

        if os.path.exists(self.pause_flag_path):
            return RiskDecision.deny("paused_flag_present", path=self.pause_flag_path)

        if self.state.get("halted"):
            return RiskDecision.deny("manually_halted",
                                     halt_reason=self.state.get("halt_reason", ""))

        if L.min_equity_floor > 0 and equity <= L.min_equity_floor:
            return RiskDecision.deny("equity_floor_breached",
                                     equity=equity, floor=L.min_equity_floor)

        anchor = self.state["day_anchor_equity"]
        if anchor > 0:
            day_dd = (anchor - equity) / anchor
            if day_dd >= L.max_daily_drawdown_pct:
                self.halt(f"daily_drawdown:{day_dd:.4f}")
                return RiskDecision.deny("daily_drawdown_limit",
                                         drawdown=round(day_dd, 4),
                                         limit=L.max_daily_drawdown_pct)

        peak = self.state["peak_equity"]
        if peak > 0:
            total_dd = (peak - equity) / peak
            if total_dd >= L.max_total_drawdown_pct:
                self.halt(f"total_drawdown:{total_dd:.4f}")
                return RiskDecision.deny("total_drawdown_limit",
                                         drawdown=round(total_dd, 4),
                                         limit=L.max_total_drawdown_pct)

        if now < self.state.get("cooldown_until", 0.0):
            mins = (self.state["cooldown_until"] - now) / 60.0
            return RiskDecision.deny("loss_streak_cooldown",
                                     minutes_remaining=round(mins, 1),
                                     consecutive_losses=self.state["consecutive_losses"])

        if self.state["consecutive_losses"] >= L.consecutive_loss_limit:
            return RiskDecision.deny("consecutive_loss_breaker",
                                     losses=self.state["consecutive_losses"])

        recent = [t for t in self.state.get("recent_entry_ts", []) if t >= now - 3600.0]
        if len(recent) >= L.max_trades_per_hour:
            return RiskDecision.deny("overtrading_hourly",
                                     trades_last_hour=len(recent),
                                     limit=L.max_trades_per_hour)

        if self.state["day_trade_count"] >= L.max_trades_per_day:
            return RiskDecision.deny("overtrading_daily",
                                     trades_today=self.state["day_trade_count"],
                                     limit=L.max_trades_per_day)

        if equity > 0:
            bleed = self.state["day_funding_paid"] / equity
            if bleed >= L.max_funding_bleed_pct_per_day:
                return RiskDecision.deny("funding_bleed_limit",
                                         bleed_pct=round(bleed, 5),
                                         limit=L.max_funding_bleed_pct_per_day)

        return RiskDecision(allowed=True, reason="account_ok")

    # -- entry gate ----------------------------------------------------------

    def evaluate_entry(self, *,
                       pair: str,
                       side: str,
                       entry_price: float,
                       stop_price: float,
                       equity: float,
                       target_price: Optional[float] = None,
                       atr_pct: Optional[float] = None,
                       open_positions: Sequence[Dict[str, Any]] = (),
                       mmr: Optional[float] = None,
                       now: Optional[float] = None) -> RiskDecision:
        """
        open_positions: list of plain dicts, e.g.
            [{"pair": "XRP-USDT", "side": "long", "notional": 420.0}, ...]
        Build this from whatever your position object looks like -- the risk
        layer deliberately does not import your types.
        """
        now = now if now is not None else time.time()
        L = self.limits
        side = side.lower()
        warnings: List[str] = []

        acct = self.account_status(equity, now=now)
        if not acct.allowed:
            return acct

        # --- input sanity ---
        if entry_price <= 0 or stop_price <= 0 or equity <= 0:
            return RiskDecision.deny("invalid_inputs",
                                     entry=entry_price, stop=stop_price, equity=equity)
        if side not in ("long", "short"):
            return RiskDecision.deny("invalid_side", side=side)
        if side == "long" and stop_price >= entry_price:
            return RiskDecision.deny("stop_on_wrong_side",
                                     side=side, entry=entry_price, stop=stop_price)
        if side == "short" and stop_price <= entry_price:
            return RiskDecision.deny("stop_on_wrong_side",
                                     side=side, entry=entry_price, stop=stop_price)

        sd = stop_distance_pct(entry_price, stop_price)
        if sd < L.min_stop_distance_pct:
            return RiskDecision.deny("stop_too_tight",
                                     stop_distance=round(sd, 5),
                                     minimum=L.min_stop_distance_pct)
        if sd > L.max_stop_distance_pct:
            return RiskDecision.deny("stop_too_wide",
                                     stop_distance=round(sd, 5),
                                     maximum=L.max_stop_distance_pct)

        # --- volatility regime ---
        if atr_pct is not None:
            if atr_pct >= L.atr_pct_ceiling:
                return RiskDecision.deny("volatility_spike_no_trade",
                                         atr_pct=round(atr_pct, 5),
                                         ceiling=L.atr_pct_ceiling)
            if atr_pct <= L.atr_pct_floor:
                return RiskDecision.deny("volatility_too_low",
                                         atr_pct=round(atr_pct, 5),
                                         floor=L.atr_pct_floor)

        # --- fee-adjusted expectancy ---
        if target_price is not None:
            rr = rr_after_fees(entry_price, stop_price, target_price, L.taker_fee_pct)
            if rr < L.min_rr_after_fees:
                return RiskDecision.deny("rr_after_fees_too_low",
                                         rr_net=round(rr, 3),
                                         minimum=L.min_rr_after_fees)
            if rr < L.min_rr_after_fees * 1.15:
                warnings.append(f"rr_net={rr:.2f} is close to the floor")

        # --- position count / correlation clustering ---
        grp = group_of(pair)
        if len(open_positions) >= L.max_concurrent_positions:
            return RiskDecision.deny("max_concurrent_positions",
                                     open=len(open_positions),
                                     limit=L.max_concurrent_positions)

        same_pair = [p for p in open_positions if p.get("pair", "").upper() == pair.upper()]
        if same_pair:
            return RiskDecision.deny("already_in_pair", pair=pair)

        same_group = [p for p in open_positions if group_of(p.get("pair", "")) == grp]
        if len(same_group) >= L.max_positions_per_group:
            return RiskDecision.deny("correlation_cluster_limit",
                                     group=grp,
                                     open_in_group=len(same_group),
                                     limit=L.max_positions_per_group)

        # Opposing directions inside one correlation group is usually an
        # accident of two strategies disagreeing, not a considered hedge.
        opposing = [p for p in same_group if p.get("side", "").lower() != side]
        if opposing:
            warnings.append(
                f"opposing exposure in group '{grp}': "
                f"{[p.get('pair') for p in opposing]}"
            )

        # --- sizing from risk ---
        risk_pct = min(L.risk_pct_per_trade, L.risk_pct_max)
        risk_amount = equity * risk_pct
        notional = risk_amount / sd

        # --- leverage cap (enforced here because set-leverage 152404) ---
        lev_cap = L.leverage_cap_for(pair)
        max_notional_by_lev = equity * lev_cap
        if notional > max_notional_by_lev:
            warnings.append(
                f"notional capped by leverage {lev_cap}x "
                f"({notional:.2f} -> {max_notional_by_lev:.2f})"
            )
            notional = max_notional_by_lev

        # --- portfolio exposure caps ---
        current_total = sum(float(p.get("notional", 0.0)) for p in open_positions)
        room_total = equity * L.max_portfolio_exposure_pct - current_total
        if room_total <= 0:
            return RiskDecision.deny("portfolio_exposure_exhausted",
                                     current_notional=round(current_total, 2),
                                     limit=round(equity * L.max_portfolio_exposure_pct, 2))
        if notional > room_total:
            warnings.append(f"notional capped by portfolio exposure "
                            f"({notional:.2f} -> {room_total:.2f})")
            notional = room_total

        current_group = sum(float(p.get("notional", 0.0)) for p in same_group)
        room_group = equity * L.max_group_exposure_pct - current_group
        if room_group <= 0:
            return RiskDecision.deny("group_exposure_exhausted",
                                     group=grp,
                                     current_notional=round(current_group, 2))
        if notional > room_group:
            warnings.append(f"notional capped by group '{grp}' exposure "
                            f"({notional:.2f} -> {room_group:.2f})")
            notional = room_group

        eff_lev = notional / equity if equity > 0 else 0.0

        # --- liquidation buffer ---
        # Checked against the EFFECTIVE leverage of the sized position, not the
        # account's nominal leverage setting.
        m = mmr if mmr is not None else L.default_mmr
        ld = liq_distance_pct(max(eff_lev, 1e-9), m)
        if ld <= 0:
            return RiskDecision.deny("leverage_implies_instant_liquidation",
                                     effective_leverage=round(eff_lev, 3), mmr=m)
        # The stop must fire with at least liq_buffer_pct of the entry->liq
        # distance still unused.
        max_allowed_stop = ld * (1.0 - L.liq_buffer_pct)
        if sd > max_allowed_stop:
            return RiskDecision.deny("liquidation_buffer_violation",
                                     stop_distance=round(sd, 5),
                                     liq_distance=round(ld, 5),
                                     max_stop_allowed=round(max_allowed_stop, 5),
                                     effective_leverage=round(eff_lev, 3))

        # --- final assembly ---
        qty = notional / entry_price
        margin = notional / eff_lev if eff_lev > 0 else notional
        realised_risk = notional * sd
        fee_cost = notional * L.taker_fee_pct * 2.0

        if realised_risk > equity * L.risk_pct_max * 1.0001:
            return RiskDecision.deny("sizing_invariant_violated",
                                     risk_amount=round(realised_risk, 4),
                                     ceiling=round(equity * L.risk_pct_max, 4))

        if fee_cost > realised_risk * 0.25:
            warnings.append(
                f"fees are {fee_cost / realised_risk:.0%} of trade risk"
            )

        return RiskDecision(
            allowed=True,
            reason="ok",
            notional=round(notional, 6),
            qty=round(qty, 8),
            margin=round(margin, 6),
            risk_amount=round(realised_risk, 6),
            effective_leverage=round(eff_lev, 4),
            warnings=warnings,
            detail={
                "pair": pair,
                "side": side,
                "group": grp,
                "stop_distance_pct": round(sd, 5),
                "liq_distance_pct": round(ld, 5),
                "est_round_trip_fees": round(fee_cost, 4),
                "day_realized_pnl": round(self.state["day_realized_pnl"], 4),
                "consecutive_losses": self.state["consecutive_losses"],
            },
        )

    # -- introspection -------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        return dict(self.state)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="risktest_")
    state = os.path.join(tmpdir, "risk_state.json")
    pause = os.path.join(tmpdir, "PAUSED")
    failures = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {name}")
        else:
            failures += 1
            print(f"  FAIL  {name} {extra}")

    EQ = 10_000.0
    print("\n== risk.py self-test ==\n")

    # 1. clean entry
    e = RiskEngine(state_path=state, pause_flag_path=pause)
    e.roll_day_if_needed(EQ)
    d = e.evaluate_entry(pair="XRP-USDT", side="long", entry_price=0.50,
                         stop_price=0.485, target_price=0.5375,
                         equity=EQ, atr_pct=0.02)
    check("clean long allowed", d.allowed, d.reason)
    check("risk == 1% of equity", abs(d.risk_amount - 100.0) < 0.5,
          f"got {d.risk_amount}")
    check("notional ~= risk/stop_dist", abs(d.notional - (100.0 / 0.03)) < 5.0,
          f"got {d.notional}")

    # 2. stop on the wrong side
    d = e.evaluate_entry(pair="SOL-USDT", side="long", entry_price=100.0,
                         stop_price=104.0, equity=EQ, atr_pct=0.02)
    check("inverted stop rejected", not d.allowed and d.reason == "stop_on_wrong_side",
          d.reason)

    # 3. volatility spike
    d = e.evaluate_entry(pair="SOL-USDT", side="long", entry_price=100.0,
                         stop_price=97.0, equity=EQ, atr_pct=0.15)
    check("vol spike blocks entry",
          not d.allowed and d.reason == "volatility_spike_no_trade", d.reason)

    # 4. RR after fees
    d = e.evaluate_entry(pair="SOL-USDT", side="long", entry_price=100.0,
                         stop_price=97.0, target_price=101.5,
                         equity=EQ, atr_pct=0.02)
    check("thin RR rejected after fees",
          not d.allowed and d.reason == "rr_after_fees_too_low", d.reason)

    # 5. correlation cluster
    opens = [{"pair": "BTC-USDT", "side": "long", "notional": 3000.0},
             {"pair": "ETH-USDT", "side": "long", "notional": 3000.0}]
    d = e.evaluate_entry(pair="BTC-USDT", side="long", entry_price=60000.0,
                         stop_price=58800.0, equity=EQ, atr_pct=0.02,
                         open_positions=opens)
    check("duplicate pair rejected", not d.allowed and d.reason == "already_in_pair",
          d.reason)

    opens2 = [{"pair": "SOL-USDT", "side": "long", "notional": 2000.0},
              {"pair": "AVAX-USDT", "side": "long", "notional": 2000.0}]
    d = e.evaluate_entry(pair="NEAR-USDT", side="long", entry_price=5.0,
                         stop_price=4.85, equity=EQ, atr_pct=0.02,
                         open_positions=opens2)
    check("third L1 blocked by cluster limit",
          not d.allowed and d.reason == "correlation_cluster_limit", d.reason)

    # 6. portfolio exposure ceiling
    heavy = [{"pair": "DOGE-USDT", "side": "long", "notional": 29_500.0}]
    d = e.evaluate_entry(pair="XRP-USDT", side="long", entry_price=0.50,
                         stop_price=0.485, equity=EQ, atr_pct=0.02,
                         open_positions=heavy)
    check("exposure ceiling caps or denies",
          (not d.allowed) or d.notional <= 500.1,
          f"allowed={d.allowed} notional={d.notional}")

    # 7a. THE CENTRAL INVARIANT: risk-based sizing makes liquidation unreachable.
    #     Because notional = risk_amt / stop_dist, effective leverage is
    #     risk_pct / stop_dist -- typically well under 2x. Liquidation only
    #     becomes reachable if you size from leverage instead of from risk.
    wide = RiskLimits(risk_pct_per_trade=0.015, max_effective_leverage=20.0,
                      max_portfolio_exposure_pct=20.0, max_group_exposure_pct=20.0,
                      min_stop_distance_pct=0.001)
    e2 = RiskEngine(limits=wide, state_path=os.path.join(tmpdir, "s2.json"),
                    pause_flag_path=pause)
    e2.roll_day_if_needed(EQ)
    d = e2.evaluate_entry(pair="BTC-USDT", side="long", entry_price=60000.0,
                          stop_price=57000.0, equity=EQ, atr_pct=0.02)
    check("risk-sized 5% stop is allowed", d.allowed, d.reason)
    check("risk-sizing keeps liq >10x further than stop",
          d.allowed and d.detail["liq_distance_pct"] > d.detail["stop_distance_pct"] * 10,
          f"liq={d.detail.get('liq_distance_pct')} stop={d.detail.get('stop_distance_pct')}")
    check("effective leverage stays under 1x", d.allowed and d.effective_leverage < 1.0,
          f"got {d.effective_leverage}")

    # 7b. Buffer path coverage: only reachable at absurd leverage, which is
    #     exactly its job -- it is a backstop that catches a sizing BUG, not a
    #     constraint that fires in normal operation.
    insane = RiskLimits(risk_pct_per_trade=0.50, risk_pct_max=0.50,
                        max_effective_leverage=200.0,
                        max_portfolio_exposure_pct=200.0,
                        max_group_exposure_pct=200.0,
                        min_stop_distance_pct=0.0001,
                        default_mmr=0.004,
                        max_leverage_by_group={"majors": 200.0})
    e2b = RiskEngine(limits=insane, state_path=os.path.join(tmpdir, "s2b.json"),
                     pause_flag_path=pause)
    e2b.roll_day_if_needed(EQ)
    d = e2b.evaluate_entry(pair="BTC-USDT", side="long", entry_price=60000.0,
                           stop_price=59850.0, equity=EQ, atr_pct=0.02)
    check("liq buffer fires at 200x effective leverage",
          not d.allowed and d.reason == "liquidation_buffer_violation",
          f"{d.reason} {d.detail}")

    # 7c. Pure-function check on the liquidation math itself.
    check("liq_distance_pct(10x, 0.5%) ~= 9.5%",
          abs(liq_distance_pct(10.0, 0.005) - 0.095) < 1e-9,
          f"got {liq_distance_pct(10.0, 0.005)}")
    check("liq_distance_pct clamps at zero",
          liq_distance_pct(500.0, 0.010) == 0.0)

    # 8. daily drawdown halt
    e3 = RiskEngine(state_path=os.path.join(tmpdir, "s3.json"), pause_flag_path=pause)
    e3.roll_day_if_needed(EQ)
    d = e3.evaluate_entry(pair="XRP-USDT", side="long", entry_price=0.50,
                          stop_price=0.485, equity=9_400.0, atr_pct=0.02)
    check("6% daily DD halts trading",
          not d.allowed and d.reason == "daily_drawdown_limit", d.reason)
    check("halt persists to state", e3.snapshot()["halted"] is True)

    # 9. consecutive losses -> cooldown
    e4 = RiskEngine(state_path=os.path.join(tmpdir, "s4.json"), pause_flag_path=pause)
    e4.roll_day_if_needed(EQ)
    for _ in range(3):
        e4.record_close(net_pnl=-100.0, equity_after=EQ - 100.0)
    d = e4.evaluate_entry(pair="XRP-USDT", side="long", entry_price=0.50,
                          stop_price=0.485, equity=EQ - 300.0, atr_pct=0.02)
    check("3 losses trigger cooldown",
          not d.allowed and d.reason == "loss_streak_cooldown", d.reason)

    e4.record_close(net_pnl=+50.0, equity_after=EQ)
    check("a win resets the streak counter",
          e4.snapshot()["consecutive_losses"] == 0)

    # 10. state survives a restart
    e5 = RiskEngine(state_path=os.path.join(tmpdir, "s3.json"), pause_flag_path=pause)
    check("halt survives process restart", e5.snapshot()["halted"] is True)

    # 11. PAUSED flag
    open(pause, "w").close()
    d = e.evaluate_entry(pair="XRP-USDT", side="long", entry_price=0.50,
                         stop_price=0.485, equity=EQ, atr_pct=0.02)
    check("PAUSED flag blocks entry",
          not d.allowed and d.reason == "paused_flag_present", d.reason)
    os.remove(pause)

    # 12. overtrading
    e6 = RiskEngine(state_path=os.path.join(tmpdir, "s6.json"), pause_flag_path=pause)
    e6.roll_day_if_needed(EQ)
    for _ in range(3):
        e6.record_entry()
    d = e6.evaluate_entry(pair="XRP-USDT", side="long", entry_price=0.50,
                          stop_price=0.485, equity=EQ, atr_pct=0.02)
    check("hourly trade cap enforced",
          not d.allowed and d.reason == "overtrading_hourly", d.reason)

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n== {'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'} ==\n")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
