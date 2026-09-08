# Generation 2 — Pre-Registration

**Registered: September 8, 2026 — before the first Gen-2 trade.** This document is the contract. A .gitignore collision delayed this commit minutes past service enable; the journal confirms zero Gen-2 trades in that window — registration still precedes trade #1. Nothing below changes mid-sample.

## 1. Provenance — stated plainly

Generation 2 runs the owner's **legacy trend ruleset exactly as coded** in `signals.py` + `trend.py`: a **1-hour** system. The oft-cited backtest (PF 1.33 → 1.61 with a daily filter; trailing → 0.60) described a **4H** configuration whose exact ruleset exists in no recoverable artifact. The as-coded 1H system — the owner's most-evolved, hand-tuned version — has **no backtest attached to this exact ruleset**. Generation 2 is therefore a **hypothesis test, not a confirmed-edge deployment**, and its kill line is set accordingly. Generation 1 (15m pullback transposition) was falsified Sep 8 at n=27, cost $0 real; its journal is archived in-repo as `journal_gen1.jsonl`.

## 2. The ruleset (verbatim from `signals.py`, parameters from `love.py`)

All conditions evaluated on **1H bars**. Direction: EMA20 vs EMA50. Entry zone: |price − EMA50| / EMA50 ≤ **0.008**. Regime: EMA separation ≥ **0.4%** of EMA50 **and** wider than two bars prior. Momentum: 3-bar change ≥ **−0.1%** for longs (≤ +0.1% for shorts). RSI(14): longs blocked above **55**; shorts blocked below **45** (below **35** when BTC is in strong bear, BTC-1H RSI < 40). Macro gate: BTC **1D vs EMA5 ± 0.5%** — bull blocks shorts, bear blocks longs, neutral blocks neither. Stop: **1.0 × ATR(14, 1H)**. Target: **max(2.0 × ATR, 0.3% of price)** — ≈ 2R by construction. Pairs: the six configured majors — **BTC, ETH, SOL, XRP, LINK, SUI** (USDT perps). Correctness proof: differential test, **235/235 series** in decision parity with the actual legacy engine, subfunctions identical to 1e-9, accept-path field parity on 40 accepts (`test_g2.py`, in repo).

## 3. Pre-registered deviations from as-coded (complete list)

1. **Closed bars only.** Legacy reads the forming candle (repaint); Gen 2 drops unconfirmed bars. Correctness fix, Gen-1 incident-log precedent.
2. **Chassis fee gate retained.** Executor takes A-grades only (target ≥ 8× round-trip fees and modeled EV > 0 at 42% WR). Legacy had no fee gate; the −$16k history and consultant 2's boundary decomposition say it stays.
3. **Ratchet ladder on top of the fixed ATR exits** (arm +1R → lock +0.2R; +1.6R → +0.8R; trail 0.8R). Validated +$16.10 over fixed in Gen 1. All three exit modes journaled per trade; the fixed counterfactual continues being measured, and `gate1.py`'s resolver cures any censoring.
4. **Risk 1.0%** of usable equity per trade (legacy engine used 1.5%); slots 2, notional cap $550, reserve $300 — the Gen-1-validated risk chassis (`risk_ltf.py`) unchanged except item 6.
5. **One-per-cluster rule, active from birth.** Clusters: BTC | ETH | {SOL, SUI} | XRP | LINK. Skipped candidates are journaled as `cluster_skip` shadow rows (side, entry, stop, target, group) so the rule's cost/benefit stays measurable. Gen-1 evidence: +$15.05 over 4 events.
6. **Scratch-aware breaker.** Losses smaller than 0.1R do not increment the 3-loss streak (consultant 1's accounting point; two Gen-1 lockouts traced to sub-0.1R lock exits). Full-size losses count as before; the 6h cooldown and release semantics are unchanged.
7. **Stop-first entry mechanics, exchange-resident stop/TP, 5s watcher, paper account** — the Gen-1 chassis as committed.

Nothing else differs. Same-cycle behavior, throttles, journaling schema, and the five-guard dry-run stack are inherited unchanged.

## 4. Expected signal rarity — on the record

The rule conjunction is narrow: grid searches over 1,600+ synthetic regimes found ~zero organic passes (in-zone + separated + widening + momentum-clean + RSI-cool rarely co-occur). Live signals are expected to be **rare** — possibly a handful per week across six pairs. That is the ruleset as the owner built it, and rarity is itself data: the time-box below exists precisely so a drought produces a verdict rather than a drift.

## 5. Gates, kill line, time-box — written before trade #1

- **Fresh books:** journal and paper account reset; paper baseline **$1,878.00**. Gen-1 artifacts archived (`journal_gen1.jsonl`, `paper_gen1.json`).
- **KILL LINE: cohort PF < 1.0 at n=30 closes → Generation 2 stops. No discussion.**
- **Checkpoint at n=30:** PF ≥ 1.2 **and** WR ≥ 33% → continue to n=50. Below either (but PF ≥ 1.0) → review with consultants before any continuation.
- **At n=50:** PF ≥ 1.3 and EV > 0 → demo candidacy; the demo→live bar remains consultant 2's standard (lower confidence bound of net expectancy after actual costs), per the two-tier contract in STRATEGY.md §8.
- **Time-box:** fewer than **12 closes by October 6** → viability review (sample-rate failure is a failure mode, not a waiting room).
- **Freeze:** no parameter, filter, pair, or exit change inside the sample. The unanimous no-list from STRATEGY.md §8 carries over in full.

## 6. Measurement

`stats.py` remains the sole aggregate source (reads the same journal path; reconciles the paper file; non-zero exit on imbalance). `gate1.py` remains the review instrument; extending its resolver to price `cluster_skip` shadow rows is a review-time task, not a mid-sample change. Every deploy keeps the sha-manifest + predicted-arithmetic discipline.
