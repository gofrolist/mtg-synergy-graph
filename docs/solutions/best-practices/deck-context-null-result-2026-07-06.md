---
last_updated: 2026-07-06
module: universal_scorer
tags:
  - deck-context
  - two-pass
  - second-pass
  - archetype-payoff
  - kill-test
  - null-result
  - no-rules
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering any deck-level / second-pass / pool-context scoring mechanism (candidate scored against the commander's top-K pool instead of, or in addition to, the commander)
  - Considering an additive context term derived from mean-of-IDF-sums against a context pool
  - Looking for the NO_RULES lever after the resource-flow DECLINE (resource-flow-demand-null-result-2026-07-02.md)
  - Planning a subtype-supply mechanism for the archetype-payoff cohort — read the Whitelist Finding below FIRST
created: 2026-07-06
plan_ref: docs/plans/2026-07-06-001-feat-structural-gap-remediation-plan.md
---

# Deck-context second pass (two-pass pool-context scoring): DECLINED at Stage-1 kill test

**Cycle:** plan `docs/plans/2026-07-06-001-feat-structural-gap-remediation-plan.md`
Phase A — the funded NO_RULES successor after the resource-flow DECLINE
re-framed the bucket as an archetype-payoff-detection gap.
**Verdict: DECLINE before any scoring-path integration.** Tasks 6–8 never
ran; zero scoring-path changes; `--expect-identity` PASS throughout.

## What was tested

Two-pass scoring: pass-1 production ranking → top-K rule-covered candidates
become a *context pool* → every legal card earns an additive context term
`w_ctx * mean_over_K(IDF-weighted synergy vs each context card)`, using the
same complement rules and IDF form with the context card standing in the
commander slot. Hypothesis: candidates with zero commander-pairwise score
(Slimefoot→Saprolings, Yawgmoth→undying) become reachable via their matches
against the deck's own top pool.

Instrument: `scripts/context_sim.py` (`bench/context_sim.py`) — portfolio_sim-
style cached-sim assembly, w=0 bitwise self-check vs `engine.page()` (passed
on all 633 sims), bands pinned before sweep (cohort H=0.0567 at mean 0.2858;
golden-500 H=0.0136 — the NDCG bands reproduce the portfolio_sim
precedents; the gem band G=0.0235 is INSTRUMENT-LOCAL — do not compare
it to the audit's hidden_gem_hit_rate or other instruments' gem bands,
see the `_plausible_set` docstring).

## Results (grid 3×3: K ∈ {10,20,30} × w ∈ {0.1,0.25,0.5})

- **Cohort fixture (33 archetype-payoff commanders):** ALL cells negative;
  best mean ΔNDCG@30 −0.0019, worst −0.0130; 1–4 cliffs; reach 0.
- **Golden-100:** mean Δ −0.0230..−0.0478; **19–35 cliffs (<−0.05) per 100
  commanders**; reach 0–5 vs the ≥100 floor. Traps: Kess −0.31, Edgar −0.18.
- **G4 whitelist-equivalence:** the hardcoded subtype whitelist through the
  SAME assembly path strictly dominates the mechanism it was the bar for.
  CORRECTION (2026-07-07, PR #101 review): the original comparator queried
  the wrong column (`card_types` instead of `subtypes`), so the decision-time
  numbers (+0.0531 @1 cliff / +0.0697 @6 cliffs) measured a PRODUCER-ONLY
  whitelist. Re-measured with the fixed, token-anchored subtype query
  (bodies + producers): **+0.0147 (b=0.1, 1 cliff) / +0.0376 (b=0.25, 2
  cliffs) / +0.0523 (b=0.5, 5 cliffs)** — flooding all subtype bodies with a
  flat bonus dilutes harder and cliffs earlier, so the producer-only variant
  is the TOUGHER bar. Both variants dominate the mechanism's best cell
  (−0.0019); the DECLINE is unaffected.

## Root cause (measured, Slimefoot scale diagnostic)

The mean-of-IDF-sums context term is **flood-shaped**: ~22,031 candidates
receive a term; generic-breadth cards accumulate the largest (top ctx term
0.417 exceeds the #30 base total 0.275, so at w=0.5 they displace labeled
cards — hence the cliffs), while the zero-score labels the mechanism exists
to reach max out at ctx_mean ≈ 0.08 — 3–7× below the top-30 entry threshold
at any weight that doesn't shred the ranking. The base-total distribution is
also extremely flat past rank ~30 (#30 = 0.275 vs #100 = 0.269), so small
additive noise reshuffles the head violently. This is the same
flood-vs-specificity failure as plans 2026-07-02-002/003/004, now measured
at the pool-context level: **"synergy with the pool" without per-candidate
specificity normalization is just another density axis.**

## The Whitelist Finding (the useful positive result)

The G4 comparator — flat bonus to cards whose subtype matches the
commander's death-payoff subtype, or that produce that subtype's tokens —
**recovers cohort NDCG but eats gems and cliffs as the bonus grows**
(corrected full whitelist: +0.015..+0.052 with gems −0.003..−0.059;
producer-only variant, the tougher bar: +0.019..+0.070 with gems
−0.006..−0.122). The cohort's EDHREC gap IS subtype-supply-shaped and IS
mechanically nameable. A future cycle should test a *narrow, IDF-weighted
subtype-supply rule* (declarative row keyed on the commander's payoff
subtype, weighted like any other rule — NOT a flat bonus, NOT a pool-context
pass). Binding obligations carried forward from plan 2026-07-03-001: such a
rule must beat BOTH whitelist variants' numbers above (else it IS the
whitelist), clear
the golden-500 no-regression band, and not pay for NDCG with gems the way
the flat bonus does.

**Outcome (2026-07-07, plan 2026-07-07-001):** the narrow IDF-weighted
rule was built and swept. At the shipped cell (producer=1.5, body=0.5)
it beat both whitelist variants at a matched-or-better ≤1-cliff budget
(cohort ΔNDCG +0.0650 vs producer-only 0.25's +0.0531 and full 0.50's
+0.0523) without their gem/cliff bill (gemΔ −0.0232, 1 shallow cliff) —
but it did not clear the producer-only 0.50 bar (+0.0697) at any swept
cell, landing in the PARTIAL band and requiring a human-approved SHIP
rather than an outright S1 pass. It also did not reach the whitelist's
best 6-cliff cell (+0.0697) at any bounded cliff count; that headline
number remains unbeaten by a mechanically-narrow rule.

## What is now measured-dead for NO_RULES

1. Cost→supply resource-flow pairing (plan 2026-07-02-005).
2. Additive pool-context second pass, mean-of-IDF-sums form (this cycle).

Still open: specificity-normalized context forms (e.g. per-candidate
context IDF over the pool rather than raw sums) — unfunded, and any
attempt must pre-pin gates on this cycle's instrument, which ships as
standing infra (`scripts/context_sim.py`, reports to
`.audit/context_sim/`).

The subtype-supply rule (Whitelist Finding below) is now **TESTED and
SHIPPED** (plan `docs/plans/2026-07-07-001-feat-subtype-supply-rule-plan.md`,
2026-07-07): `subtype_supply_producer` / `subtype_supply_body`
(producer=1.5, body=0.5), verdict PARTIAL, human-approved SHIP on a
Pareto-dominance rationale — see `docs/RULE_HISTORY.md` 2026-07-07 entry
for the gate table.
