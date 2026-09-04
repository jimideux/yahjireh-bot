# YahJireh LTF System — Strategy, Architecture & Roadmap

**Version 1.1 — September 4, 2026** · **Status:** Paper-trading (dry-run) · **Venue:** BloFin USDT perpetuals
**Repo:** github.com/jimideux/yahjireh-bot · **Owner:** Jimi
**Supersedes:** v1.0 (same day), the "Hyperliquid Quant Bot" origin spec, and the August 2026 handoff for everything named `sniper-ltf*`.

**v1.1 errata, prompted by external review.** Two corrections to v1.0, both caught by the owner's consultants within hours of circulation: (1) v1.0's headline aggregates were wrong — a prose subtotal dropped trade #14, overstating net P&L by exactly $10.65; corrected figures are below, and all aggregates now come from `stats.py`, which reconciles the journal against the paper ledger and refuses to balance otherwise. (2) v1.0 claimed no emergency-flatten tool existed; `PANIC.py` (320 lines, report-only by default) has been in the repo since the owner's repair session. Both errors were the same failure — the record trailing reality — and both were caught by audit, not losses. That is the system working, and embarrassing, in equal measure.

---

## 1. Executive summary

Two systemd services on a DigitalOcean droplet trade a 15-minute pullback-continuation strategy across all sufficiently liquid BloFin USDT perpetuals:

- **sniper-ltf** — read-only alert service. Scans the universe every 15m bar close, sends LONG/SHORT Telegram alerts for human discretionary trading. Contains no order code.
- **sniper-ltf-exec** — the executor. Takes only A-grade signals as simulated positions against a self-contained **paper account** ($1,878 baseline), with exchange-style stop/target mechanics, an R-based profit-lock ladder, and an isolated risk engine in front of every entry. It is structurally incapable of trading real money while `LIVE_TRADING_ENABLED=false` — the guard lives in the shared exchange client, below this code, and every mutating call returns a `DRYRUN` sentinel.

**Track record (all simulated, from `stats.py`):** 17 trades since Aug 18 · net **+$27.48** · win rate **41.2%** · profit factor **1.44** — *below* the 1.6 design assumption · EV **+$1.62/trade** · currently in a 6-loss streak. The consistently-sized paper cohort (#9–17) is **−$8.75, PF 0.78 — currently losing.** n=17 supports no strong conclusion in either direction (95% CI on the win rate spans roughly 22–64%), and this document deliberately leads with the least flattering framing the data permits.

**Staging ladder (hard gates, in order):** dry-run → Gate-1 review at ~30 closes (mid-Sep) → BloFin **demo** endpoint (real order flow, fake money) → live funding decision. The prior live attempt of an earlier system generation lost ~$16k, dominated by fee drag; that history sets the evidence bar. Two external consultant reviews (Sep 4) are incorporated throughout; where they conflicted, the resolutions are recorded in §8.

**Governing principle:** *survival first, profit secondary.* Changes ship only through unit tests, sha-verified transfers, and commits whose insertion/deletion counts are predicted before they run. As of v1.1, performance claims additionally ship only through `stats.py`.

---

## 2. Signal strategy — the edge hypothesis

**Setup: higher-timeframe trend + lower-timeframe pullback + reclaim.** All conditions on **closed candles only** (BloFin's `confirm` flag; forming bars discarded so signals cannot repaint).

1. **4H bias** — price on the correct side of the 4H EMA50 *and* the EMA sloping the same direction above a noise floor (0.2%). No clear bias → no trade, either direction. Provenance: the owner's 4H backtest showed a daily-trend filter lifting PF 1.33 → 1.61; this transposes that finding to 4H-over-15m.
2. **15m structure** — EMA20 > EMA50 (mirrored for shorts); a pullback that reached the EMA20 zone but stayed shallower than 1.5 ATR; RSI reset through ~50 without knife-catch extremes.
3. **Reclaim bar** — the trigger candle closes back through the EMA20 in the top 60% of its own range (bottom 60% for shorts).
4. **Geometry** — stop under the pullback swing plus 0.35 ATR buffer, capped at 2% from entry; target fixed at **2.2R**. The tight structural stop is the entire fee thesis.
5. **Fee grading** — **A** if target ≥ 8× round-trip fees *and* modeled EV > 0 at 42% WR; **B** if thinner (alerts only). The **executor takes A only**. Consultant 2's decomposition sharpens the known weakness here: at the 8× boundary, gross edge is +0.344R and fees alone consume ~0.275R, leaving ~0.069R before spread, slippage, and funding — boundary A-grades are nearly edgeless. An EV-margin tightening and measured (not proxy) costs are Gate-1/2 items; a per-trade fee-drag column ships in the Gate-1 kit first.

**Universe:** all live BloFin USDT swaps with ≥ **$2M** 24h USD volume (`volCurrency24h × last`, verified against docs and candle notionals). ~466 listed; 15–24 typically qualify; refreshed daily. Executor skips non-crypto asset classes.

**Deliberately rejected features** (all from the owner's own trading data): grids and LTF crossovers (fee-event maximizers; the −$16k and a −$155.91 demo), trailing-as-default (owner's 4H test: PF 1.61 → 0.60), sentiment and RL (overfit fuel at two-digit n), funding intelligence (noise at 1–16h holds — now upgraded to a *measured pre-trade cost* item per consultant 2, rather than a strategy feature). Both consultants independently endorsed keeping every rejection.

---

## 3. Exit system

- **Baseline ("fixed"):** exchange-resident stop at the structure level, reduce-only limit TP at 2.2R. **Fixed remains the control** until the ratchet wins a clean, uncensored test.
- **Active mode ("ratchet"):** the fixed orders *plus* an R-based lock ladder, watcher-enforced every 5s: high-water ≥ **+1.0R** arms → floor **+0.2R**; ≥ **+1.6R** → floor **+0.8R**; beyond → floor trails by **0.8R**. Locks exit at the **observed mark** — fast moves gap past the line between ticks (production: first lock exit slipped $0.59). History: the owner's original "+$25 lock" was unreachable at current sizing (max attainable ≈ $24.20, proven by a $23.24 peak); converted to R-units Sep 2.
- **Counterfactual journaling:** every close records all three exit modes. **Censoring (disclosed):** when the ratchet exits first, the fixed outcome is unknown (`fixed: null`). The Gate-1 **ghost resolver** cures this by candle-walking to first stop/target touch — with consultant 2's correction built in: 15m bars cannot order a same-bar stop-and-target touch, so ambiguous windows drop to finer bars where history allows, are counted **pessimistically (stop-first)** otherwise, and the ambiguous count is reported alongside any conclusion.
- **Locks are software-side.** Re-arming the exchange stop at each lock floor is a **mandatory pre-live item** — unanimous across owner, both consultants. Until then, a dead server means fallback to the original exchange stop.
- **Stop-first discipline:** live entry → exchange stop → TP, in that order; a failed stop-arm triggers immediate flatten + journaled abort. Unit-tested; closes the old handoff's highest-priority gap.
- **Gate-2 decides the exit question properly:** fixed vs ratchet runs as a **randomized A/B in demo**, judged on net R after real fills and funding.

---

## 4. Risk engine (`risk_ltf.py`) — isolated, no override path

Every entry passes `evaluate_entry()`; there is deliberately **no force/override argument**. Gates, all active:

| Gate | Setting | Notes |
|---|---|---|
| Risk per trade | 1.0% of usable equity | usable = equity − $300 reserve |
| Notional cap | $550/position | legacy inheritance; binds before the risk budget; **frozen** until Gate-1 reconciliation + cluster map (both consultants) |
| Concurrent slots | 2 | total exposure ≤ 95% of usable |
| Fee gate | target ≥ 8× RT, EV > 0 | see §2 boundary-EV caveat |
| Stop sanity / leverage caps / vol-spike | yes | ATR > 6% = no-trade |
| Post-close cooldown / revenge window | 300s / 900s | |
| **3-loss circuit breaker** | 6h cooldown | trips on 3 consecutive losses; **releases when served** (deadlock fixed Sep 3, field-proven through two full cycles); win resets; further losses re-arm. Whether sub-0.1R lock scratches should feed the streak is a **Gate-1 decision with its own cost column** (consultant 1 wants the fix, consultant 2's freeze prevails until measured) |
| Daily stops | −4% DD or −$20 realized | clears at UTC rollover |
| Correlation clusters | partial | only 6 pairs mapped — the gap behind both same-cycle double-stops; see §8 |

**Paper account (dry only):** persisted balance, start $1,878, compounds every close; reconciliation penny-exact on every check to date and now enforced by `stats.py` (non-zero exit on mismatch). When guards open, sizing reverts to real equity automatically. The executor **yields to the human**: untracked positions, untracked pending orders, or ownership-registered pairs are skipped; manual trading is never touched.

---

## 5. Track record — all 17 simulated trades

Fees modeled at 0.12% RT (taker both sides); 2bp adverse slippage on entry — **a fiat assumption real books will not honor** (consultant 2), which is one reason demo exists. Trades 1–8 predate the paper account and were sized against real equity that swung $889 → $6.7k (disclosed inconsistency); 9–17 are the clean paper cohort.

| # | Pair | Side | Opened (UTC) | Exit | Net $ | Held | Peak $ |
|---|---|---|---|---|---|---|---|
| 1 | PUMP | L | Aug 18 16:22 | stop | −5.87 | 1.1h | 0.00 ¹ |
| 2 | WLFI | L | Aug 18 17:19 | stop | −5.17 | 20.3h | 3.53 |
| 3 | LINK | L | Aug 18 18:30 | target | +7.24 | 8.8h | 8.27 |
| 4 | LINK | L | Aug 19 03:30 | target | +22.23 | 16.1h | 23.24 |
| 5 | ZEC | L | Aug 20 01:45 | stop | −7.86 | 1.3h | 0.92 |
| 6 | POL | L | Aug 20 01:45 | stop | −4.08 | 1.3h | 0.50 |
| 7 | ETH | L | Aug 20 03:45 | target | +16.12 | 12.1h | 17.40 |
| 8 | PUMP | L | Aug 20 05:45 | target | +13.62 | 2.6h | 14.51 |
| 9 | SUI | S | Sep 02 00:30 | target | +6.53 | 1.2h | 7.75 |
| 10 | DOGE | S | Sep 02 00:30 | target | +5.25 | 1.3h | 6.06 |
| 11 | UNI | L | Sep 02 02:00 | target | +18.89 | 2.0h | 19.63 |
| 12 | ENA | L | Sep 02 05:00 | stop | −7.01 | 1.5h | 9.93 ² |
| 13 | HYPE | L | Sep 02 04:30 | stop | −3.58 | 3.4h | 5.30 ² |
| 14 | PUMP | L | Sep 03 00:30 | stop | −10.65 | 0.6h | 0.16 ³ |
| 15 | ASTER | L | Sep 03 15:45 | **lock@1** | −0.24 | 0.3h | 5.76 ⁴ |
| 16 | ZEC | L | Sep 03 23:00 | stop | −9.70 | 2.6h | 0.76 |
| 17 | XPL | L | Sep 03 23:00 | stop | −8.24 | 3.5h | 5.07 |

¹ Deploy-gap contaminated (57 min unwatched). ² Giveback losers (+1.56R / +1.82R peaks → full stops) that motivated the R-ladder. ³ The trade v1.0's aggregate dropped. ⁴ First lock exit; fixed counterfactual censored pending the ghost resolver.

**Aggregates (from `stats.py`):** wins 7 / losses 10 · gross +$89.88 / −$62.40 · **net +$27.48** · avg win $12.84 / avg loss $6.24 (payoff 2.06) · **PF 1.44** · EV **+$1.62/trade** · fees (simulated) $11.22 · paper balance **$1,869.25** ✓ reconciled. **Paper cohort #9–17: net −$8.75, PF 0.78.** Same-cycle entry groups: ZEC/POL **−$11.94**, SUI/DOGE **+$11.78**, ZEC/XPL **−$17.94** — correlation cuts both ways; a keep-first-ranked rule would have added +$7.07 across the three events (n far too small to adopt on).

---

## 6. Architecture & operations

**Services** (systemd, `Restart=always`): **sniper-ltf** (`runner_ltf.py`) — alerts; bar-aligned sweeps; top-5/sweep, 40/day, 1/pair/hour; 0.4s throttle honoring BloFin's 500 req/min/IP. **sniper-ltf-exec** (`ltf_executor.py`) — strict A-grades (own state, 4h/pair cooldown, 10/day); market entries; stop-first; 5s watcher; R-ladder; paper account; counterfactual journal; non-fatal telegram.

**Shared client** (`exchange/blofin.py`): five-guard `LIVE_TRADING_ENABLED` stack (mutations → `"DRYRUN"`); `IS_DEMO` switches to the demo endpoint; `place_tpsl` SL-only (mark-trigger, reduce-only); no orderId from `place_order` (hence market entries + position reconciliation); no working set-leverage (152404, open), cross hardcoded.

**Tooling:** `stats.py` — the sole authorized source of aggregates; sums the journal, reconciles the paper file, non-zero exit on imbalance. `PANIC.py` — emergency flatten, report-only by default, `--execute` gated, ownership-aware; **known gap:** executor positions are deliberately unregistered in `ownership`, so PANIC's current categories (bot-owned / everything-including-manual) cannot selectively flatten them — teaching it `ltf_exec_positions.json` as a third category is a Gate-2 item. Test suites (`test_ltf.py` 27 checks, `test_risk.py` 27 incl. exposure and breaker-release regressions) are in-repo as of v1.1; executor-suite packaging ships with the Gate-1 kit.

**Legacy stack:** trend/peace/resolver — inactive but enabled; disable-before-reboot recommended by both consultants; owner's trigger. `risk.py` (787-line RiskEngine) serves trend.py; `ownership` keeps peace.py away from executor and manual positions.

**Change control:** sha256-manifest tarballs → token grep gates → journal verification → commits with insertions/deletions predicted in advance (~10 clean deploys; the gate has caught stale files, an unknown tracked risk.py, and — via consultant audit — the v1.0 aggregate error, which motivated extending the same discipline to statistics).

---

## 7. Incident & decision log

| Date | Incident | Root cause | Fix / lesson |
|---|---|---|---|
| Aug 17 | Signals could repaint | Forming candle is `confirm=0` | Closed-bars only; regression tests |
| Aug 17 | Wrong file deployed ×2 | macOS Downloads collisions | sha-manifest bundles |
| Aug 19 | Exposure gate "never fired" | Real totalEquity had swung ≥$1,458 intraday | Not a bug; equity/usable/exposure journaled per entry since |
| Aug 18 | risk.py overwritten | A 787-line evolved engine was already tracked | Restored; LTF engine → `risk_ltf.py` |
| Aug 20–Sep 1 | 11-day silent outage | Dry sizing coupled to real equity; account emptied | Paper account; "an empty bank account must never stop a paper test" |
| Sep 1–2 | 2 unexplained restarts | Undetermined (no tracebacks) | Sends hardened non-fatal; zero crashes since |
| Sep 3 | Breaker deadlock | 3-loss block had no release path (11h dark) | Blocks only while cooldown runs; trip-serve-release-reset tested + field-proven ×2 |
| Sep 4 | **v1.0 aggregates overstated (+$38.13 vs true +$27.48)** | Prose subtotal dropped trade #14; no reconciliation gate on summaries | **Caught by external audit (consultant 2).** `stats.py` is now the only aggregate source |
| Sep 4 | v1.0 claimed PANIC unbuilt | Stale institutional memory (owner's repair session built it) | Caught by consultant 1; errata + executor-category gap specced |

Pattern: every failure surfaced through instrumentation or audit, never through losses.

---

## 8. Roadmap — the consultant-merged pipeline

Both external reviews (Sep 4) merged; conflicts resolved as noted. Ordering principle: **every future dollar is gated behind a measurement; buy the measurements in the cheapest order.**

| Phase | When | Contents |
|---|---|---|
| **0 — Truth base** ✅ this release | Sep 4 | Errata ×2; `stats.py`; suites committed; (owner) legacy disable + reboot |
| **1 — Instrumentation kit** | ~Sep 13, zero behavior change | Ghost resolver (finer-bar ambiguity handling, ambiguous count reported); journal columns: time-stop 8h/12h, HTF-regime vs outcome, one-per-cluster counterfactual, boundary-EV/fee-drag, breaker-lockout cost; per-entry spread & funding logging; executor test packaging |
| **2 — Gate-1 review** | n≈30 closes | Decisions **from columns**: cluster rule, time stop, breaker scratch-accounting, EV-margin tightening. Demo go/no-go on **consultant 1's checkpoint bar** (WR ~40%, payoff ≥2R net, clustering resolved). **Columns fail → this generation stops and the journal survives** |
| **3 — Demo** | 2–3 wks post-pass | Guards open vs fake money. **Randomized fixed/ratchet A/B**; **maker-entry arm with missed/partial-fill accounting**; spread/depth/funding become pre-trade **gates**; exchange-stop re-arm at lock floors; PANIC third category; real slippage replaces the 2bp fiat. Advancement bar switches to **consultant 2's standard: lower confidence bound of net expectancy after actual costs** (walk-forward, point-in-time universe; see White's Reality Check / PBO) |
| **4 — Scale** | Phase-3 pass only | Fund; retire flat $550 → **min(k × equity, executable-liquidity capacity, cluster limit)** with a **written daily max-loss in dollars**; maker entries if the arm won; growth unit = **equity**, never looser A-grades |

**Conflict resolutions on record:** same-cycle rule — *column-first* (taking both trades **is** the shadow experiment; applying the rule early destroys the skipped trade's data); breaker scratch fix — *deferred to Gate 1* (consultant 2's freeze; cost so far ≈ one 6h window); gate criteria — *two-tier* (C1's point-estimate checkpoint admits to free demo; C2's LCB rigor admits to real money).

**Unanimous no-list (standing):** no leverage increase, no third slot, no cap raise, no new indicators, no auto-B-grades, no ratchet-as-default before the A/B, no VIP chase, no signal retuning off a streak, no treating demo fills as paper marks.

**Standing items:** GitHub PAT expires ~mid-Nov (symptom: push 401 + misleading "Everything up-to-date") · OS reboot pending, legacy disable first · funding awareness graduates from "deferred feature" to "measured pre-trade cost" in Phases 1–3.

**Honest ceiling:** at current sizing even a fully validated edge yields ~$50–100/month (EV $1.62 × pace). Phase 4 roughly doubles the per-trade number; income beyond that is the compounding loop this pipeline exists to justify.

---

## 9. How to audit this

Reproduce everything from three artifacts: the git history (Aug 17 → HEAD; each deploy's arithmetic predicted in advance), `ltf_exec_trades.jsonl` (schema: `open`/`telemetry`/`close`; closes carry gross/net/high-water and the three-mode `virtual` block), and **`stats.py`**, whose output is the only performance claim this project makes — if its reconciliation line says FAIL, distrust every number above it. Strategy logic ~600 lines (`ltf_signals.py`), lifecycle ~530 (`ltf_executor.py`), risk ~470 (`risk_ltf.py`); stdlib-only above the exchange client. Both consultant reviews are on file with the owner; their catches are logged in §7 under their own dates.
