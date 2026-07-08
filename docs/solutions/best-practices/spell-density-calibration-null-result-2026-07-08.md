---
last_updated: 2026-07-08
module: universal_scorer
title: spell_density is correctly weighted — down-weighting it regresses NDCG (calibration null-result)
tags:
  - density-rules
  - rule-quality-multiplier
  - calibration
  - null-result
  - flat-credit
  - kill-test
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering a per-rule _RULE_QUALITY_MULTIPLIER down-weight on spell_density or any large density family because it "dominates" the tensor contribution
  - Reading a `bench.py audit --forensics` OUTRANKED rule-family table and seeing spell_density / tribal_density as the top displacers
  - Tempted to treat "largest total contribution" or "flat commander-invariant bonus" as evidence of over-crediting
created: 2026-07-08
plan_ref: none (exploratory probe, no plan)
---

# `spell_density` down-weight: DECLINED (calibration null-result)

**Probe:** a non-mutating in-process NDCG counterfactual over the golden-100
fixture, sweeping `_RULE_QUALITY_MULTIPLIER["spell_density"]` ∈ {1.0, 0.5,
0.25, 0.0} via `universal_scorer.patched_rule_quality_multiplier`, scoring
each cell with `engine.page()` (`forensics.extract_live_ranking`) and
`validate.compute_ndcg(..., k=30)` against
`edhrec_labels_for_commander(grade_floor=0.0)`. Zero scoring-path changes;
nothing was committed to `src/mtg_synergy_graph/data/scoring_weights.json`
(which remains `_RULE_QUALITY_MULTIPLIER = {}` / `_FLAT_WEIGHT_OVERRIDES =
{}` — no per-rule tuning is applied anywhere today).

## The hypothesis (and why it looked plausible)

The de-blinded `--forensics` OUTRANKED rule-family table (see
[tensor-single-owner-slot-2026-07-08.md](tensor-single-owner-slot-2026-07-08.md)
for why de-blinding was needed) showed `spell_density` as the #1 displacer
of missed EDHREC cards: **24.6%** of OUTRANKED tensor contribution, and
**26,667 total contribution** in the persisted tensor — ~6× the next family
(`scaling` at 4,755). A `--rule spell_density` ablation showed it hands **7
of its 14 commanders the identical +2141.1** (Wort, Talrand, Riku,
Niv-Mizzet, Mizzix, Melek, Feather) — a flat, commander-invariant bonus.
Several spell_density-dominated commanders are also the worst NDCG
performers (Feather 0.011, Wort 0.051). The natural read: a flat "generic
spellslinger staples" bump is flooding spellslinger top-30s and displacing
EDHREC's commander-specific picks.

## The result: REFUTED — down-weighting regresses NDCG monotonically

| spell_density mult | 14-affected NDCG@30 | Δaff | whole-100 NDCG@30 | Δall |
|---:|---:|---:|---:|---:|
| **1.0 (current)** | 0.2511 | — | 0.2267 | — |
| 0.5 | 0.2300 | −0.0211 | 0.2238 | −0.0030 |
| 0.25 | 0.1585 | −0.0926 | 0.2138 | −0.0130 |
| 0.0 (removed) | 0.1608 | −0.0903 | 0.2141 | −0.0126 |

Monotone degradation on both the affected commanders and the whole fixture.
spell_density is **correctly weighted at multiplier 1.0** for the
EDHREC-NDCG objective. (Harness baseline reads 0.2267 vs the canonical
forensics 0.2364 — a small label-source / tie-window offset; the deltas are
all within-harness with only the multiplier changing, so the direction and
monotonicity are robust regardless of the absolute offset.)

**Why the "flat bonus" intuition was wrong:** spellslinger decks converge on
the same generic instants/sorceries, so a flat spell-count bonus is
*correctly aligned* with EDHREC's homogeneous top picks for that archetype.
"Largest total contribution" and "commander-invariant" are NOT evidence of
over-crediting when the archetype itself is homogeneous — the metrics that
matter are the counterfactual NDCG deltas, not the contribution magnitude.

## The one real signal: heterogeneity a flat multiplier can't exploit

Per-commander, removing spell_density is heterogeneous — it *helps* a few
(Melek +0.0992, Alela +0.0674, Wort +0.0017) and badly hurts most (Kess
−0.4540, Sram −0.3342, Talrand −0.1782, Niv-Mizzet −0.1390, Galea −0.1214).
A single scalar multiplier cannot "help Kess, demote Melek" — the same
per-candidate-discrimination ceiling that killed
[death-outlet-feeder](death-outlet-feeder-null-result-2026-07-07.md) (flat
per-class credit) and that the
[deck-context](deck-context-null-result-2026-07-06.md) DECLINE hit. The
heterogeneity is real headroom, but only a per-candidate mechanism (graded
efficiency, embedding-style dispersion) can reach it — not a
`_RULE_QUALITY_MULTIPLIER` tune.

## Corroborating: the optimizer never touched it

The last `--optimize` proposal (`.audit/optimize_proposal.json`, 2026-07-02)
**overfit** — train composite +0.0005 but held-out −0.0001, held NDCG
0.09849 → 0.09819 (regression) — which is why its diffs were never shipped
into `scoring_weights.json`. And every accepted diff was a *small* rule
(changeling_tribal, choose_tribal, counter_doubler, …); **spell_density
never appeared in the sweep**. So the largest scoring contributor had never
been calibration-tested until this probe — and the answer is that it's
already at its NDCG-optimal weight.

## What this does NOT rule out

- A per-candidate-discriminating refinement of spell_density (weighting
  individual spell candidates by relevance to the specific commander) —
  the heterogeneity above says this is where the headroom is.
- A different objective than EDHREC-NDCG. This probe measures against
  `edhrec_labels_for_commander`; the whole point of the project is that
  EDHREC is a proxy, not the goal (the "find hidden gems from mechanics"
  intent operationalized by `hidden_gem_hit_rate` — see the CLAUDE.md
  Evaluation section and `bench/hidden_gems.py`). A hidden-gem-oriented
  objective might value spell_density differently.

## Reproduce

Scratch probe (non-mutating): patch `spell_density` in
`_RULE_QUALITY_MULTIPLIER` via `patched_rule_quality_multiplier`, score the
14 commanders spell_density fires on (SQL: `SELECT DISTINCT commander FROM
rule_contributions WHERE config_hash=? AND rule_id='spell_density'`) at each
multiplier, hold the other 86 at baseline (spell_density does not fire on
them, so they are multiplier-invariant), aggregate over all 100. ~18s.

**Requires the persisted tensor to cover golden-100** to enumerate the
affected commanders — see the single-owner-slot caveat before re-pinning.
