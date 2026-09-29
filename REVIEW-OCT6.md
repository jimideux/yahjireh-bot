# Gen 2 Viability Review — Prep (for October 6, 2026)

**Trigger:** GEN2.md §5 time-box — fewer than 12 closes by Oct 6. Status Sep 29: **n=6 in 21 days** (0.29 closes/day). Reaching 12 by the deadline requires ~3× the observed pace; this document assumes the review fires and prepares it. Decision owners: Jimi + consultants. Nothing changes before the review; the freeze holds.

## 1. What the sample says — and mostly cannot say

n=6: net −$11.90, PF 0.54, WR 33% (2W/4L), paper $1,866.10, reconciled PASS. **The 95% interval on 2/6 wins spans ~9–70%** — these numbers carry no verdict weight in either direction, which is exactly why the time-box (a sample-*rate* gate) exists separately from the kill line (a sample-*size* gate). At the observed pace, n=30 arrives ~**Dec 21** and n=12 ~**Oct 20**.

Column notes only (no action; freeze): one more peaked-under-arm death (XRP +0.84R high → full stop; Gen 1 logged two at 0.95–0.98R — now three across generations for the eventual arm-threshold column). Zero cluster collisions in 21 days — signals too sparse to overlap. Operations: zero errors, zero crashes; 3 restarts were unattended-upgrades maintenance, books penny-exact through all of them.

## 2. The question

The registered hypothesis (owner's as-coded 1H ruleset, 6 majors) produces ~0.3 decisions/day. Is a ~3.5-month clock to first verdict an acceptable price for testing an unbacktested hypothesis, or should the sample rate change under re-registration?

## 3. Options

**A — Extend the clock, change nothing.** Purest test; zero re-registration; freeze intact. Cost: verdict ~Dec 21; three months of calendar spent on a hypothesis with no backtest behind it. Paper-cheap, information-slow.

**B — Widen the universe (Gen 2.1 re-registration).** The ruleset is per-pair; adding pairs scales decision rate without touching one rule parameter. E.g., 6 → ~18 liquid pairs ≈ 3× pace → n=30 ~early Nov. Requirements if chosen: rules verbatim (diff-tested again), cluster map extended to every added pair, **era-tagged fresh counters** (the Gen-1 mixed-cohort lesson: no blending 6-pair and 18-pair samples), kill line re-anchored to the new era, GEN2.md amended and committed before the first widened trade. Honest caveat: the owner tuned this system on majors; alt-pair behavior (ATR profile, RSI ranges, BTC-beta) is part of what would then be under test.

**C — Stop Gen 2 on sample-rate grounds.** Legitimate if the panel judges the ruleset structurally too selective to validate at acceptable cost on any reasonable universe. Premature if B hasn't been priced.

**D — Loosen filters to manufacture signals. Rejected before discussion:** parameter changes to increase sample rate are retuning — banned by the freeze and the standing no-list; it would invalidate the hypothesis rather than test it faster.

## 4. Builder's input (one line, not a verdict)

B, if the panel confirms the pace: it buys ~3× information rate at the cost of one honest caveat, while A remains defensible for whoever prices purity above calendar. The decision is the review's.
