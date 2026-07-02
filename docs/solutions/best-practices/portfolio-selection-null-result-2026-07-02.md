---
module: universal_scorer
tags:
  - portfolio-selection
  - diversity-reranking
  - mmr
  - kill-test
  - null-result
  - outranked
problem_type: null-result
---

# Portfolio selection (per-family diminishing returns at top-30 assembly): DECLINED at R0

**Cycle:** plan `docs/plans/2026-07-02-004-feat-portfolio-selection-plan.md`
(origin `docs/brainstorms/2026-07-02-portfolio-selection-requirements.md`) —
the funded OUTRANKED successor after the lift-normalization DECLINE.
**Verdict: DECLINE at the R0 kill-test, before any scoring-path
integration.** Units 3 and 5–8 never ran; zero scoring-path changes;
`--expect-identity` PASS throughout Phase A.

## What was tested

Greedy top-30 assembly with per-family diminishing returns over full
contribution vectors (MMR/submodular adaptation): effective score =
production total − Σ_f syn_f·(1−g(mass_f, λ)) for positive per-family
synergy, anti-synergy and non-rule residual undiscounted, dampener
semantics frozen. The load-bearing hypothesis (falsifiable, named in
the origin doc): cohort-demand survives because nothing behind the
cohort outscores discounted members, while floods shed to the labeled
cards behind them — i.e. the flood-vs-archetype demand question
emerges from score structure at LIST level even though plan 002
measured it inexpressible at the WEIGHT level.

## Kill-test design (the committed instrument)

`scripts/portfolio_sim.py` (`src/mtg_synergy_graph/bench/portfolio_sim.py`)
— the first COMMITTED ranking-transform kill-test harness (predecessors
were scratchpad-only). Live-rescoring instrument: one
`score_all_universal` pass per commander over the page-equivalent
pool, bitwise recomposition of the production total (λ=0 self-check
vs `engine.page()` enforced per commander — passed on all 100 and all
500), all λ cells re-assembled from cached `UniversalScore` objects.
Thresholds pinned BEFORE the sweep from per-commander bootstrap on
the 500-cmdr fixture (seed 17): NDCG noise band ±0.0136, gem
non-regression band 0.0355, R9a baseline predicate pass rates 0.2438
(monoculture) / 0.6363 (other).

## Results

**Step 1 — empirical addressable share (R7b).** At the reference
depth (λ=0.5, exp, full): 24/463 OUTRANKED misses cross = **0.0518**
(diff-family 19/274 = 0.069; same-family 5/186 = 0.027). The
pre-planning family-identity bound was 0.630 — the gap is the
finding: most "addressable" misses sit far below the marginal flood
member, out of reach of any marginal-crossing re-ranker.

**Step 2 — 100-cmdr grid (48 cells: {exp, harmonic} × λ ∈ {0.05,
0.1, 0.25, 0.5, 1, 2} × {committed, identity} map × {full, flat}
base).** 46/48 cells fail the survival predicate. Cliff count scales
monotonically with λ (λ=2/committed/full: 35 cliffs; Kess −0.27…−0.37,
Edgar −0.18…−0.25 across strong cells). Survivors: BOTH
λ=0.05/identity/full cells only —

| cell | Δndcg | cliffs | Δgem | monoculture-28 Δ | rank_bonus-ablated Δ |
|------|------:|-------:|-----:|-----------------:|---------------------:|
| exp/λ=0.05/identity/full | +0.0061 | 0 | −0.0263 | +0.0136 | +0.0044 |
| harmonic/λ=0.05/identity/full | +0.0057 | 0 | −0.0250 | +0.0139 | +0.0030 |

Gems drop in-band exactly as the honest-gate framing predicted
(labeled replacements displace unlabeled flood members that counted
as gems). The gain is not rank_bonus leakage (ablated Δ stays
positive).

**Step 3 — 500-cmdr confirmation (full R9 gate).** Both survivors
FAIL on two independent axes:

| cell | Δndcg (need > +0.0136) | cliffs (need 0) | Δgem | Magda | Elenda |
|------|------:|-------:|-----:|------:|-------:|
| exp/λ=0.05/identity/full | +0.0013 | 9 | −0.0103 | **−0.0571** | −0.0329 |
| harmonic/λ=0.05/identity/full | +0.0013 | 7 | −0.0091 | −0.0483 | −0.0328 |

The fuel-tribe sub-shape named in advance as the falsification
target (Magda's Dwarves) is among the cliffs — the same population
that killed every plan-002 weight configuration. Marrow-Gnawer
(+0.0978) and Nissa (+0.011) survive at this depth; the mechanism's
per-cohort selectivity is real but the surviving decay is too weak
to matter (+0.0013 ≈ 0) and still not weak enough to avoid cliffs on
label-sparse 500-fixture commanders.

**Step 4 — confound check (R8a precedent).** One `--optimize` pass
(500-cmdr, 1 sweep): train composite +0.0005, held-out **−0.0001** —
no calibration headroom; miscalibration is not the confound.

**Map-granularity attribution (R6a):** both granularities swept; the
FINER (identity) map produced the only survivors, and the
sibling-merged (committed) map was uniformly worse. The DECLINE is a
mechanism failure, not a map artifact.

## Why it failed (mechanism)

Assembly-level per-family decay is still a UNIFORM transform over a
family cohort — it differs from scoring-time demotion only in being
positional. The two failure modes pincer every λ:

1. **Strong decay** (λ ≥ 0.1): displaces floods but cliffs
   cohort-demand commanders — the identical wall as concave
   aggregation, tiers, pool scaling, and lift baselines. Positional
   marginalism does not rescue it, because for an archetype
   commander the EDHREC top-30 IS deep in one family, and any decay
   strong enough to bound Kess's spells evicts Edgar's vampires.
2. **Weak decay** (λ ≈ 0.05): zero cliffs on the golden 100, but
   inflow is structurally tiny — the empirical addressable share
   (5.2% at mid-depth) shows OUTRANKED misses sit far below the
   marginal flood member's score, so marginal crossings cannot
   produce aggregate NDCG movement above noise.

The demand question does not emerge from score structure at the list
level within any measured (form, λ, granularity, base) cell. With
plans 002 (weight layer), 003 (baseline layer), and 004 (selection
layer) all measured closed, **uniform family transforms are dead at
every layer of the ranking stack.**

## Surviving infrastructure (all inert)

- `src/mtg_synergy_graph/data/family_map.json` + strict loader
  (`portfolio.py::load_family_map`) — retained data infrastructure
  (the `card_hints` precedent); no authoring obligation (the
  walker-closing coverage enforcement was Unit 3, never run).
- `scripts/portfolio_sim.py` — the committed kill-test instrument:
  live-rescoring, bitwise λ=0 self-check, bootstrap band derivation
  (`bands`), addressable-share readout (`share`), grid sweep with
  trap sidecar + rank_bonus ablation (`sweep`). This replaces the
  scratchpad-template convention for future ranking-transform
  probes.
- `portfolio.py::decompose_universal_score` — exact production-total
  decomposition (documented finding: the page total applies NO
  concentration dampener under flag-OFF; the dampener lives only in
  `UniversalScore.score`).

## Dispositions

- **Plan-002 Unit 3 (rank_bonus removal)**: its re-run condition
  attached to this cycle. The ablation sidecar shows rank_bonus was
  not the survivors' gain source, but its removal remains
  cliff-heavy per plan-002 data; still deferred, now unattached —
  needs its own cycle if pursued.
- **OUTRANKED lever**: the uniform-transform family is closed at all
  three layers. Remaining honest directions: (a) candidate-side
  evidence enrichment — new rules/ports for under-covered labels
  (the NEAR_MISS / DATA_GAP / NO_RULES buckets, the gap_report
  pipeline's home turf); (b) accepting high-rank OUTRANKED misses as
  justified divergence (the R9 forensics view already quantifies
  this); (c) any future re-ranking idea must first present a
  mechanism for per-commander demand that is NEITHER a classifier
  (plan 002: rejected) NOR uniform decay (this cycle: dead), and
  must run `portfolio_sim.py`-style kill-tests before integration.

## Artifacts

- Committed: Units 1–2 (family map, loader, sim harness, 43 tests) on
  branch `feat/portfolio-selection`.
- Session-local (`.audit/portfolio_sim/`, gitignored): `bands.json`,
  `share.json`, `sweep.json`, `sweep.md` for both fixtures.
- Baseline: tag `pre-lift-normalization` (commit `92fba39`) — pins
  unchanged; no re-pin occurred (Phase A is identity-clean).
