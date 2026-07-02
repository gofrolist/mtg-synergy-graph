---
last_updated: 2026-07-02
module: scoring
title: Concave family-aggregation probe declined — archetype-is-the-family cliffs (null result)
tags:
  - scoring
  - dampening
  - monoculture
  - null-result
  - audit-gate
  - plan-2026-07-02-002
problem_type: best_practice
resolution_type: reference
applies_when:
  - Considering within-family concavity / concentration dampening extensions to curb monoculture top-30s
  - Tempted to re-try uniform single-family haircuts at any threshold or cap
  - Designing Unit-5-style payoff/body tiering (this null result is its mandate)
created: 2026-07-02
plan_ref: docs/plans/2026-07-02-002-fix-scoring-flaw-remediation-plan.md
---

# Concave family-aggregation probe declined — archetype-is-the-family cliffs

Plan 2026-07-02-002 Unit 4 probed extending the signal-concentration
dampener to single-rule candidates (the monoculture exemption flaw) at
a choke point shared by both totals (closing the dual-total split).
Three functional forms were measured against the post-Unit-2 pins;
all DECLINE on the per-commander cliff gate.

## What was tried (all flag-gated `_ENABLE_CONCAVE_FAMILY_AGG`)

| Variant | Single-rule extension scope | 500-cmdr agg NDCG | cliffs < −0.05 | worst |
|---|---|---:|---:|---|
| A (blanket) | all rules | +0.0005 | 6 | Kodama −0.1940 |
| B (flat-only) | `_FLAT_COUNT_RULES` | +0.0004 | 3 | Edgar −0.1026 |
| C (flat minus tribal) | flat except `tribal_density` | −0.0003 | 1 | Rionya −0.0520 |

Gem rate: +0.0007…+0.0013 across variants (never negative).
R8a re-sweep (`--optimize` on the conditioned scratch tensor):
train +0.00052 (sub-noise), held +0.00006 (~zero; run a89ada0941db, 1 iteration, partial sweep) — calibration ruled out as confound.

## The structural finding

Every surviving cliff is a commander whose EDHREC top-30 IS the
flooding family: Kodama/Bruenor/Lathiel (modified/equipment/lifegain
specific axes) under variant A; Edgar/Lathril (tribal) under B;
Rionya (cheap instants, spell_density) under C. Progressive family
exclusion converges on an empty extension: a uniform within-family
haircut cannot distinguish "flood as noise" (Adeline's vanilla
Humans) from "flood as archetype" (Edgar's Vampires) — that
distinction lives on the COMMANDER side (does the kit demand this
family?) and the CANDIDATE side (payoff vs body), not in the
candidate's family-share vector alone.

This is the mandate for the payoff/body two-tier design (Unit 5) and
pool-scaled weights (Unit 6): both carry commander/candidate evidence
the share-vector lacks.

## What survives

- The dual-total choke point (`_syn_concentration_factor` consumed by
  `UniversalScore.score`, `to_legacy_buckets`, and the optimizer's
  fused total) stays in the tree, flag OFF, bitwise-inert — any future
  dampening probe starts from a unified-totals substrate.
- `enable_concave_family_agg` stays registered in
  `ScoringConfigInputs`/`compute_config_hash` (flag flips invalidate
  tensors correctly).
- The full flag-gate test suite (16 tests incl. hand-computed pairs,
  dual-total agreement, hash flip) stays green with the flag OFF.

## Numbers (post-Unit-2 baseline: NDCG 0.2361, gem 0.8160/0.7128)

- Variant C, 100-cmdr: agg −0.0003, gem 0.8170, cliffs 0.
- Variant C, 500-cmdr: agg −0.0003, gem 0.7141 (+0.0013), cliffs 1
  (Rionya −0.0520).
- Gate: DECLINE on any 500-cmdr cliff < −0.05 (R6/R7 provenance:
  color-conditioned-idf-null-result-2026-07-02.md).
