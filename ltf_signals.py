"""
ltf_signals.py — 5m/15m signal engine for Telegram alerts.

ALERTS ONLY. This module has no execution path, no order placement, no exchange
client. It produces messages. You decide. That is deliberate — the whole reason
LTF is defensible here is that a human filters between signal and fill.

--- Why not EMA crossover ---------------------------------------------------
Your signals.py uses EMA20/50 crossover on 1H/4H. Do not port that to 5m. On a
5m chart EMA20/50 crosses on noise, and every cross is a fee event. The
100-trade scalper demo (-$155.91, 32% WR, $92.53 fees) is what that looks like.

The setup used here instead is HTF-trend + LTF-pullback + reclaim:
  - HTF (1H or 4H) must have a clear directional bias  <- your PF 1.33->1.61 edge
  - LTF pulls back into the EMA20 zone (trend breathes, doesn't break)
  - LTF reclaims: close back through EMA20 with the bar closing strong
  - stop goes under the pullback swing low + ATR buffer

The tight stop is the point. Stop distance is what sets R:R, and R:R is what
decides whether a 15m trade clears 0.12% round-trip fees. A setup without a
well-defined invalidation level cannot clear fees on LTF. That's the whole game.

--- Fee grading -------------------------------------------------------------
Structure gates (HTF bias, pullback, reclaim) always apply — they're quality.
Fee math grades rather than blocks, because a human is the final filter:

  grade A  target >= 8x taker round-trip AND EV > 0. Clean.
  grade B  thin vs fees. Fires anyway, with the taker EV and the maker-entry EV
           printed in the alert, so you can take it on a limit order or skip.
  blocked  target < 2x round-trip fees. Indefensible at any fee tier.

mode="strict" restores the old behavior (B-grades suppressed) if the phone
gets noisy.

Pure stdlib. You inject two callables (fetch candles, send message) so this does
not touch exchange/blofin.py or joy.py directly.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

ALERT_STATE = os.getenv("LTF_ALERT_STATE", "/root/trading/ltf_alerts.json")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LTFConfig:
    # --- timeframes --------------------------------------------------------
    ltf: str = "15m"                  # "5m" or "15m" — entry timing
    htf: str = "4H"                   # bias timeframe
    htf_ema_period: int = 50
    htf_slope_lookback: int = 3
    htf_min_slope_pct: float = 0.002  # above daily-noise floor; see htf_filter.py
    require_closed_bar: bool = True   # drop the still-forming candle (confirm=0)
                                      # before scanning. Signals never repaint;
                                      # alerts land within one sweep of bar close.

    # --- LTF structure -----------------------------------------------------
    ltf_ema_fast: int = 20
    ltf_ema_slow: int = 50
    atr_period: int = 14
    rsi_period: int = 14
    swing_lookback: int = 8           # bars to find the pullback extreme

    # pullback must actually reach the zone, but not break structure
    pullback_max_atr: float = 1.5     # how far below EMA20 still counts as pullback
    rsi_reset_long_max: float = 52.0  # RSI must have dipped to here during pullback
    rsi_reset_short_min: float = 48.0
    rsi_extreme_block: float = 22.0   # RSI beyond this = knife, not pullback

    # reclaim bar quality
    min_close_position: float = 0.60  # close in top 60% of bar range (longs)

    # --- targets -----------------------------------------------------------
    stop_atr_buffer: float = 0.35     # ATR padding below swing low
    target_r_multiple: float = 2.2    # TP = 2.2R
    max_stop_pct: float = 0.020       # reject setups needing >2% stop on LTF

    # --- fee grading -------------------------------------------------------
    mode: str = "advisory"            # "advisory": thin setups fire as grade B
                                      # "strict": thin setups are suppressed
    fee_round_trip: float = 0.0012    # taker both sides — worst-case basis
    fee_maker: float = 0.0002         # ⚠️ verify both against your BloFin tier
    fee_taker: float = 0.0006
    min_tp_fee_mult: float = 8.0      # A-grade line (vs taker round-trip)
    min_fee_mult_hard: float = 2.0    # below this: blocked even in advisory
    min_expected_value_pct: float = 0.0
    assumed_win_rate: float = 0.42    # for the EV display only — see note below

    # --- volatility sanity -------------------------------------------------
    min_atr_pct: float = 0.0008       # true dead-tape floor. Was 0.15%, which
                                      # silently muted BTC on 5m — wrong for an
                                      # alert system on the timeframe you asked for
    max_atr_pct: float = 0.035        # above this it's a spike, skip

    # --- alert hygiene -----------------------------------------------------
    alert_style: str = "minimal"      # "minimal": 2-3 lines, direction-first
                                      # "full": levels, EV, fee math, reasons
    cooldown_per_pair_s: int = 3600   # one alert per pair per hour, max
    max_alerts_per_day: int = 12
    dedupe_bars: int = 6              # don't re-alert same setup within N bars


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def ema(values: Sequence[float], period: int) -> list:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def _true_ranges(highs, lows, closes) -> list:
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    return tr


def atr(highs, lows, closes, period: int = 14) -> Optional[float]:
    """Wilder ATR, latest value."""
    tr = _true_ranges(highs, lows, closes)
    if len(tr) < period:
        return None
    val = sum(tr[:period]) / period
    for t in tr[period:]:
        val = (val * (period - 1) + t) / period
    return val


def rsi_series(closes: Sequence[float], period: int = 14) -> list:
    """Wilder RSI. Returns series aligned to closes[period:]."""
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out = []

    def _rsi(a_gain, a_loss):
        if a_loss == 0:
            return 100.0
        rs = a_gain / a_loss
        return 100.0 - (100.0 / (1.0 + rs))

    out.append(_rsi(ag, al))
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out.append(_rsi(ag, al))
    return out


# ---------------------------------------------------------------------------
# Candle normalization
# ---------------------------------------------------------------------------

def normalize_candles(raw) -> list:
    """Accept BloFin list-rows or dict-rows, return oldest-first dicts.

    Your handoff (§12) documents that BloFin returns newest-first. This sorts by
    timestamp ascending regardless, so it is safe either way.

    ⚠️ VERIFY the list-row column order against your actual client response
    before trusting this. Assumed: [ts, open, high, low, close, volume, ...].
    """
    out = []
    for row in raw:
        if isinstance(row, dict):
            out.append({
                "ts": float(row.get("ts") or row.get("timestamp") or row.get("time") or 0),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume") or row.get("vol") or 0.0),
                "confirm": int(float(row.get("confirm", 1))),
            })
        else:
            out.append({
                "ts": float(row[0]), "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
                # BloFin col 9: '1' = candle closed, '0' = still forming
                "confirm": int(float(row[8])) if len(row) > 8 else 1,
            })
    out.sort(key=lambda c: c["ts"])
    return out


def _cols(candles):
    return ([c["high"] for c in candles],
            [c["low"] for c in candles],
            [c["close"] for c in candles],
            [c["open"] for c in candles])


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    pair: str
    direction: str            # "long" | "short"
    timeframe: str
    ts: float

    entry: float
    stop: float
    target: float

    stop_pct: float
    target_pct: float
    r_multiple: float
    ev_pct: float
    fee_mult: float           # target_pct / round-trip fees

    htf_bias: str
    atr_pct: float
    rsi: float
    assumed_win_rate: float = 0.42
    grade: str = "A"                       # "A" clean | "B" thin vs fees
    style: str = "minimal"
    warnings: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def to_telegram(self) -> str:
        arrow = "▲ LONG" if self.direction == "long" else "▼ SHORT"

        if self.style == "minimal":
            # He decides everything else. Direction first, price, where the
            # setup is wrong. Nothing prescriptive, no footer nag.
            ltf_tag = self.timeframe.split("/")[0]
            inv = "<" if self.direction == "long" else ">"
            lines = [
                f"{arrow}  {self.pair} · {ltf_tag} · {self.entry:,.6g}",
                f"{self.grade} · invalid {inv} {self.stop:,.6g} · ATR {self.atr_pct:.2%}",
            ]
            if self.grade == "B":
                lines.append("thin vs fees — prefer limit entry")
            return "\n".join(lines)

        # ---- full style ----
        t = datetime.fromtimestamp(self.ts, tz=timezone.utc).strftime("%H:%M UTC")
        lines = [
            f"{arrow}  {self.pair}  [{self.timeframe}]  grade {self.grade}",
            f"{t}",
            "",
            f"entry   {self.entry:,.6g}",
            f"stop    {self.stop:,.6g}   ({self.stop_pct:.2%})",
            f"target  {self.target:,.6g}   ({self.target_pct:.2%})",
            "",
            f"R:R      {self.r_multiple:.2f}R",
            f"fees     target is {self.fee_mult:.1f}x round-trip",
            f"EV       {self.ev_pct:+.3%} /trade @ {self.assumed_win_rate:.0%} WR assumed",
            "",
            f"HTF      {self.htf_bias} ({self.timeframe.split('/')[-1] if '/' in self.timeframe else 'htf'})",
            f"ATR      {self.atr_pct:.2%}   RSI {self.rsi:.0f}",
            "",
            "why: " + "; ".join(self.reasons),
        ]
        for w in self.warnings:
            lines += ["", "⚠ " + w]
        lines += ["", "⚠️ alert only — not an order. verify before entry."]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class LTFScanner:

    def __init__(self, cfg: Optional[LTFConfig] = None,
                 state_path: str = ALERT_STATE):
        self.cfg = cfg or LTFConfig()
        self.state_path = state_path
        self.state = self._load()

    # -- alert state --------------------------------------------------------

    def _load(self) -> dict:
        try:
            with open(self.state_path) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"last_alert": {}, "day_key": "", "day_count": 0}

    def save(self):
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, self.state_path)

    def _day_key(self, ts=None):
        return datetime.fromtimestamp(ts or time.time(),
                                      tz=timezone.utc).strftime("%Y-%m-%d")

    def _quota_ok(self, now) -> bool:
        key = self._day_key(now)
        if self.state.get("day_key") != key:
            self.state["day_key"] = key
            self.state["day_count"] = 0
        return self.state["day_count"] < self.cfg.max_alerts_per_day

    def _cooldown_ok(self, pair, now) -> bool:
        last = self.state["last_alert"].get(pair)
        if last is None:          # never alerted on this pair — not "alerted at t=0"
            return True
        return (now - last) >= self.cfg.cooldown_per_pair_s

    def _mark_sent(self, pair, now):
        self.state["last_alert"][pair] = now
        self.state["day_count"] = self.state.get("day_count", 0) + 1
        self.save()

    # -- HTF bias -----------------------------------------------------------

    def htf_bias(self, htf_candles) -> Optional[str]:
        closes = [c["close"] for c in htf_candles]
        line = ema(closes, self.cfg.htf_ema_period)
        if len(line) < self.cfg.htf_slope_lookback + 1:
            return None
        now_e, prev_e = line[-1], line[-1 - self.cfg.htf_slope_lookback]
        if prev_e <= 0:
            return None
        slope = (now_e - prev_e) / prev_e
        last = closes[-1]
        if last > now_e and slope > self.cfg.htf_min_slope_pct:
            return "long"
        if last < now_e and slope < -self.cfg.htf_min_slope_pct:
            return "short"
        return None

    # -- EV helper ----------------------------------------------------------

    def expected_value_pct(self, win_rate, tp_pct, sl_pct) -> float:
        f = self.cfg.fee_round_trip
        return win_rate * (tp_pct - f) - (1.0 - win_rate) * (sl_pct + f)

    # -- the scan -----------------------------------------------------------

    def scan_pair(self, pair: str, ltf_candles: list,
                  htf_candles: list, now: Optional[float] = None) -> Optional[Signal]:
        """Return a Signal or None. Pure — does not mutate alert state."""
        cfg = self.cfg
        now = now or time.time()

        if cfg.require_closed_bar:
            # never judge a bar that's still being painted
            while ltf_candles and ltf_candles[-1].get("confirm", 1) == 0:
                ltf_candles = ltf_candles[:-1]
            while htf_candles and htf_candles[-1].get("confirm", 1) == 0:
                htf_candles = htf_candles[:-1]

        need = max(cfg.ltf_ema_slow, cfg.atr_period, cfg.rsi_period) + cfg.swing_lookback + 5
        if len(ltf_candles) < need or len(htf_candles) < cfg.htf_ema_period + 5:
            return None

        bias = self.htf_bias(htf_candles)
        if bias is None:
            return None

        highs, lows, closes, opens = _cols(ltf_candles)
        last = ltf_candles[-1]
        px = last["close"]
        if px <= 0:
            return None

        a = atr(highs, lows, closes, cfg.atr_period)
        if not a:
            return None
        atr_pct = a / px
        if atr_pct < cfg.min_atr_pct or atr_pct > cfg.max_atr_pct:
            return None

        e_fast = ema(closes, cfg.ltf_ema_fast)
        e_slow = ema(closes, cfg.ltf_ema_slow)
        if not e_fast or not e_slow:
            return None
        ef, es = e_fast[-1], e_slow[-1]

        rs = rsi_series(closes, cfg.rsi_period)
        if not rs:
            return None
        rsi_now = rs[-1]
        rsi_recent = rs[-cfg.swing_lookback:] if len(rs) >= cfg.swing_lookback else rs

        bar_range = last["high"] - last["low"]
        if bar_range <= 0:
            return None
        close_pos = (last["close"] - last["low"]) / bar_range   # 1.0 = closed at high

        window = ltf_candles[-cfg.swing_lookback:]
        reasons = []

        # ---------------- LONG ----------------
        if bias == "long":
            if not (ef > es):
                return None
            reasons.append("LTF EMA20>EMA50")

            # pullback happened: something in the window traded at/below EMA20
            swing_low = min(c["low"] for c in window)
            if swing_low > ef:
                return None
            if (ef - swing_low) > cfg.pullback_max_atr * a:
                return None      # too deep — structure broke, not a pullback
            reasons.append("pulled back into EMA20 zone")

            # RSI reset then recovered, but never knifed
            if min(rsi_recent) > cfg.rsi_reset_long_max:
                return None
            if min(rsi_recent) < cfg.rsi_extreme_block:
                return None
            reasons.append(f"RSI reset to {min(rsi_recent):.0f}, now {rsi_now:.0f}")

            # reclaim bar: closed back above EMA20, strong close
            if px <= ef:
                return None
            if close_pos < cfg.min_close_position:
                return None
            if last["close"] <= last["open"]:
                return None
            reasons.append(f"reclaim bar, close in top {close_pos:.0%} of range")

            stop = swing_low - cfg.stop_atr_buffer * a
            if stop >= px:
                return None
            risk = px - stop
            target = px + cfg.target_r_multiple * risk
            direction = "long"

        # ---------------- SHORT ----------------
        else:
            if not (ef < es):
                return None
            reasons.append("LTF EMA20<EMA50")

            swing_high = max(c["high"] for c in window)
            if swing_high < ef:
                return None
            if (swing_high - ef) > cfg.pullback_max_atr * a:
                return None
            reasons.append("rallied into EMA20 zone")

            if max(rsi_recent) < cfg.rsi_reset_short_min:
                return None
            if max(rsi_recent) > (100.0 - cfg.rsi_extreme_block):
                return None
            reasons.append(f"RSI reset to {max(rsi_recent):.0f}, now {rsi_now:.0f}")

            if px >= ef:
                return None
            if (1.0 - close_pos) < cfg.min_close_position:
                return None
            if last["close"] >= last["open"]:
                return None
            reasons.append(f"rejection bar, close in bottom {1 - close_pos:.0%} of range")

            stop = swing_high + cfg.stop_atr_buffer * a
            if stop <= px:
                return None
            risk = stop - px
            target = px - cfg.target_r_multiple * risk
            direction = "short"

        # ---------------- shared gates ----------------
        stop_pct = risk / px
        target_pct = abs(target - px) / px

        if stop_pct > cfg.max_stop_pct:
            return None

        fee_mult = target_pct / cfg.fee_round_trip
        ev = self.expected_value_pct(cfg.assumed_win_rate, target_pct, stop_pct)

        grade, warnings = "A", []
        if fee_mult < cfg.min_tp_fee_mult or ev <= cfg.min_expected_value_pct:
            if cfg.mode == "strict" or fee_mult < cfg.min_fee_mult_hard:
                return None          # suppressed (strict) or indefensible (<2x fees)
            grade = "B"
            rt_maker = cfg.fee_maker + cfg.fee_taker   # limit entry, stop/market exit
            ev_maker = (cfg.assumed_win_rate * (target_pct - rt_maker)
                        - (1.0 - cfg.assumed_win_rate) * (stop_pct + rt_maker))
            warnings.append(
                f"thin vs fees: target {fee_mult:.1f}x taker RT, EV {ev:+.3%} taker "
                f"/ {ev_maker:+.3%} maker-entry. Limit order or skip.")
        else:
            reasons.append(f"target clears fees {fee_mult:.1f}x")

        return Signal(
            pair=pair, direction=direction, timeframe=f"{cfg.ltf}/{cfg.htf}",
            ts=last["ts"] / 1000.0 if last["ts"] > 1e11 else (last["ts"] or now),
            entry=px, stop=stop, target=target,
            stop_pct=stop_pct, target_pct=target_pct,
            r_multiple=cfg.target_r_multiple, ev_pct=ev, fee_mult=fee_mult,
            htf_bias=bias, atr_pct=atr_pct, rsi=rsi_now,
            assumed_win_rate=cfg.assumed_win_rate,
            grade=grade, style=cfg.alert_style, warnings=warnings, reasons=reasons,
        )

    # -- alert dispatch -----------------------------------------------------

    def maybe_alert(self, sig: Signal, send_fn: Callable[[str], None],
                    now: Optional[float] = None) -> bool:
        """Apply cooldown + daily quota, then send. Returns True if sent."""
        now = now or time.time()
        if not self._quota_ok(now):
            return False
        if not self._cooldown_ok(sig.pair, now):
            return False
        send_fn(sig.to_telegram())
        self._mark_sent(sig.pair, now)
        return True

    def run_once(self, pairs, fetch_fn, send_fn, now=None) -> list:
        """One full sweep. fetch_fn(pair, interval, limit) -> raw candles."""
        cfg = self.cfg
        sent = []
        for pair in pairs:
            try:
                ltf = normalize_candles(fetch_fn(pair, cfg.ltf, 200))
                htf = normalize_candles(fetch_fn(pair, cfg.htf, 120))
            except Exception as e:              # noqa: BLE001 — never kill the sweep
                print(f"[ltf] fetch failed {pair}: {e}")
                continue
            sig = self.scan_pair(pair, ltf, htf, now)
            if sig and self.maybe_alert(sig, send_fn, now):
                sent.append(sig)
        return sent


# ---------------------------------------------------------------------------
# Viability — run this BEFORE trusting any alerts
# ---------------------------------------------------------------------------

def viability(atr_pct: float, cfg: Optional[LTFConfig] = None) -> dict:
    """Given a pair's typical ATR% on a timeframe, can a setup there clear fees?

    Models the median case: stop ~= 1.0 ATR (pullback low + buffer), target = R x stop.
    """
    cfg = cfg or LTFConfig()
    stop_pct = atr_pct * 1.0
    target_pct = stop_pct * cfg.target_r_multiple
    fee_mult = target_pct / cfg.fee_round_trip
    ev = (cfg.assumed_win_rate * (target_pct - cfg.fee_round_trip)
          - (1 - cfg.assumed_win_rate) * (stop_pct + cfg.fee_round_trip))
    return {
        "atr_pct": atr_pct, "stop_pct": stop_pct, "target_pct": target_pct,
        "fee_mult": fee_mult, "ev_pct": ev,
        "viable": fee_mult >= cfg.min_tp_fee_mult and ev > 0,
    }


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def wire_in():
    """
    New service `sniper-ltf`, or a loop inside tg.py. Two injected callables —
    this module never imports your exchange client or joy.py directly.

        from ltf_signals import LTFScanner, LTFConfig
        from exchange.blofin import fetch_candles      # your existing fn
        from joy import send                            # your telegram helper

        scanner = LTFScanner(LTFConfig(ltf="15m", htf="4H"))

        def fetch(pair, interval, limit):
            return fetch_candles(pair, interval, limit)   # adapt arg names

        while True:
            scanner.run_once(love.active_pairs, fetch, send)
            time.sleep(60)          # 15m candles: 60s poll is plenty

    ⚠️ Confirm two things against your real client before running:
      1. the interval strings ("15m" / "4H") match what BloFin expects
      2. normalize_candles() column order matches the actual response rows

    Rate limits: your handoff notes BloFin 429s on bulk candle fetches and that
    backtest.py throttles 0.4s. run_once() does 2 fetches per pair. Six pairs =
    12 calls per sweep. At 60s polling that is fine; if you widen to 24 pairs,
    add a throttle inside the loop or cache HTF candles (they only change every
    4 hours — refetching them every minute is 95% waste).
    """


if __name__ == "__main__":
    cfg = LTFConfig()
    print(f"Fee viability — BloFin {cfg.fee_round_trip:.2%} round-trip, "
          f"target {cfg.target_r_multiple}R, gate {cfg.min_tp_fee_mult:.0f}x\n")
    print(f"{'ATR%':>7}  {'stop%':>7}  {'target%':>8}  {'x fees':>7}  {'EV/trade':>9}  verdict")
    print("-" * 62)
    for a in (0.0010, 0.0015, 0.0020, 0.0030, 0.0045, 0.0060, 0.0090, 0.0120):
        v = viability(a, cfg)
        mark = "OK" if v["viable"] else "DEAD — fee-dominated"
        print(f"{a:>7.2%}  {v['stop_pct']:>7.2%}  {v['target_pct']:>8.2%}  "
              f"{v['fee_mult']:>7.1f}  {v['ev_pct']:>+9.3%}  {mark}")
