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
golden-500 H=0.0136, G=0.0235 — both reproduce the portfolio_sim precedents).

## Results (grid 3×3: K ∈ {10,20,30} × w ∈ {0.1,0.25,0.5})

- **Cohort fixture (33 archetype-payoff commanders):** ALL cells negative;
  best mean ΔNDCG@30 −0.0019, worst −0.0130; 1–4 cliffs; reach 0.
- **Golden-100:** mean Δ −0.0230..−0.0478; **19–35 cliffs (<−0.05) per 100
  commanders**; reach 0–5 vs the ≥100 floor. Traps: Kess −0.31, Edgar −0.18.
- **G4 whitelist-equivalence:** the hardcoded subtype whitelist through the
  SAME assembly path scores cohort Δ **+0.0531 (1 cliff) / +0.0697 (6
  cliffs)** — it strictly dominates the mechanism it was the bar for.

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
**recovers cohort NDCG (+0.05..+0.07) but eats gems (−0.05..−0.12) and
cliffs at b=0.5**. The cohort's EDHREC gap IS subtype-supply-shaped and IS
mechanically nameable. A future cycle should test a *narrow, IDF-weighted
subtype-supply rule* (declarative row keyed on the commander's payoff
subtype, weighted like any other rule — NOT a flat bonus, NOT a pool-context
pass). Binding obligations carried forward from plan 2026-07-03-001: such a
rule must beat this whitelist's numbers (else it IS the whitelist), clear
the golden-500 no-regression band, and not pay for NDCG with gems the way
the flat bonus does.

## What is now measured-dead for NO_RULES

1. Cost→supply resource-flow pairing (plan 2026-07-02-005).
2. Additive pool-context second pass, mean-of-IDF-sums form (this cycle).

Still open: subtype-supply rule (Whitelist Finding above); specificity-
normalized context forms (e.g. per-candidate context IDF over the pool
rather than raw sums) — unfunded, and any attempt must pre-pin gates on
this cycle's instrument, which ships as standing infra
(`scripts/context_sim.py`, reports to `.audit/context_sim/`).
