---
last_updated: 2026-05-04
module: scoring
title: BM25 IDF probe declined — per-commander cliff fired (null result)
tags:
  - scoring
  - idf
  - null-result
  - audit-gate
  - bm25
  - plan-2026-05-04-001
problem_type: best_practice
resolution_type: reference
applies_when:
  - Considering BM25-style IDF as a replacement for the current 1/log2(1+N) formula
  - Looking at "orthogonal directions to lift the recommendation model" and tempted to re-try BM25
  - Designing a future IDF-axis probe (the IDF axis is NOT exhausted; only BM25-style is)
created: 2026-05-04
plan_ref: docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md
brainstorm_ref: docs/brainstorms/2026-05-04-bm25-idf-requirements.md
---

# BM25 IDF probe declined — per-commander cliff fired

The BM25 IDF probe (plan `docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md`)
shipped Unit 1 (per-commander NDCG@30 reporting infrastructure) but
declined the formula change at Unit 4. The per-commander prerequisite
gate fired catastrophically: 65 of 500 commanders regressed beyond the
0.05 NDCG@30 threshold, with the worst at −0.5283 (Ghoulcaller Gisa).

This note captures the result so future engineers reaching for BM25 as
an "orthogonal direction" can read first and either re-evaluate or
pick a different IDF variant.

## Context

Plan `docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md` was the
first orthogonal-direction probe after a 2026-05-04 session exhausted
three direct improvement levers (per-rule weight optimizer, embedding
contribution flip, walker rule-shipping queue). BM25 was chosen as
the cheapest probe of the IDF axis, with NDCG@30 explicitly elevated
to primary metric (a scoped reframe of
`memory/feedback_edhrec_not_goal.md`).

Five outcomes were pre-committed: SHIP / INVESTIGATE / INCONCLUSIVE /
INVESTIGATE-FOR-RETUNE / DECLINE.

## Result

Outcome: **DECLINE**.

| Metric | Pinned (log2 IDF + tuned weights) | Live (BM25 IDF + freshly-tuned weights) | Δ |
|---|---|---|---|
| Aggregate NDCG@30 (500-cmdr) | 0.094208 | 0.087245 | **−0.006963** |
| hidden_gem_hit_rate (500-cmdr) | 0.7128 | 0.7535 | **+0.0407** |
| Per-commander violations (delta < −0.05) | — | **65 of 500** | — |
| Worst per-commander regression | — | Ghoulcaller Gisa **−0.5283** | — |
| Aggregate score Δ (sum of contributions) | — | +187,961 | — |
| Histogram (rank_shuffle_across_top30) | — | **332 of 500** | — |

**The per-commander prerequisite gate fired.** Per the plan's hard
gate #1 in Success Criteria: ANY commander losing more than 0.05
NDCG@30 → DECLINE. We had 65.

The aggregate also fails the SHIP gate independently (NDCG delta is
negative; SHIP requires ≥+0.010).

## Per-commander failure pattern

The 65 violations cluster around two archetypes:

- **Tribal commanders** with very high pinned NDCG: Edgar Markov
  (−0.298), Krenko, Mob Boss (−0.218), Lathril (−0.327), Hakbal of
  the Surging Soul (−0.218), Ezuri, Renegade Leader (−0.163).
- **Graveyard / aristocrats**: Ghoulcaller Gisa (−0.528, worst),
  Gisa, the Hellraiser (−0.450), Wilhelt (−0.298), Tegwyll (−0.294),
  Tormod (−0.377), Kess (−0.356), Yuriko (−0.146).

Best gains are scattered across less-supported commanders: Sakashima
the Impostor (+0.344), Trostani, Selesnya's Voice (+0.285), The
Necrobloom (+0.215). The pattern: BM25's flatter saturation curve
hurts commanders whose top-30 was carried by a few high-IDF rare
rules (tribal/graveyard density) and helps commanders whose top-30
was undifferentiated under the steeper log2 curve.

## What's interesting

**hidden_gem_hit_rate INCREASED by +0.0407** — well above the SHIP
gate's −0.010 floor. Under a hidden-gem-primary framing
(consistent with `memory/feedback_edhrec_not_goal.md`'s prior
position), this would be a SHIP-worthy result. BM25 is finding more
mechanically-plausible cards EDHREC's top-30 doesn't list, even as
NDCG@30 (EDHREC similarity) regresses.

The BM25 IDF probe was framed as NDCG-primary per the brainstorm's
explicit owner reframe (scoped to BM25 work only). The DECLINE
verdict honors that framing. **A future hidden-gem-primary probe
might want to revisit BM25 with different success criteria.**

## What this rules out

This DECLINE specifically rejects:
- BM25 IDF formula `log((N − df + 0.5) / (df + 0.5) + 1)` with strict
  N (total distinct candidates per commander) and df (per-key match
  count)
- For an NDCG@30-primary success target on the 500-cmdr fixture
- With audit-gated multipliers re-tuned by the optimizer (seed 17)

This DECLINE does **NOT** rule out:
- Other IDF formulations (smoothed-frequency, structural-overlap,
  per-rule-cluster, rank-aware) — the plan's deferred siblings
- BM25 IDF itself under hidden-gem-primary framing — the +0.0407 gem
  lift suggests a re-probe with different success criteria could
  ship
- Other orthogonal directions (richer commander-target composition
  for embeddings, port-signature feature expansion, anti-synergy
  rules, multi-card combo scoring)

## Why per-commander cliff fires under BM25

The current `1/log2(1+n)` IDF gives very rare rules (n=1, 2, 3) IDF
values around 1.0, 0.63, 0.5. BM25's saturation curve at strict
N=500 (typical for the 500-cmdr fixture) gives n=1 → IDF ≈ 5.5,
n=2 → IDF ≈ 4.8, n=3 → IDF ≈ 4.4. So **BM25 increases the absolute
scale of rare-rule IDF by ~5×** while leaving common-rule IDF
roughly comparable to log2.

Per-commander effect: commanders whose top-30 was carried by a
narrow tribal-density rule (n=1-3 specific Goblins / Zombies that
matched the lord-style payoff) saw those rules' contributions surge
by ~5× under BM25. That moved different cards into the top-30
positions. Some of those new cards happen to NOT be in EDHREC's
top-30 (driving the hidden-gem improvement) and ALSO not relevant
to the commander's actual mechanical synergy axis (driving the
per-commander NDCG cliff).

The optimizer's re-tuning (5 multipliers changed) didn't compensate
because the multipliers are per-RULE, not per-commander. The cliff
is structural to BM25 + this rule library + this fixture
composition.

## Reproduce

```bash
# 1. Apply the formula change (Unit 2 of the plan).
# Edit src/mtg_synergy_graph/universal_scorer.py:702:
#   bm25_idf = math.log(((total_n - df + 0.5) / (df + 0.5)) + 1.0)
#   base_idf_non_flat[key] = bm25_idf * cond_mult
# (Replace 1.0 / math.log2(1.0 + n) and add total_n computation upstream
# in _compute_idf_basis. See plan Unit 2 for details.)

# 2. Re-tune multipliers under BM25 (Unit 3).
uv run scripts/bench.py audit --optimize --self-test-seed 17

# 3. Apply the proposal to data/scoring_weights.json.
# Manual edit of the 5 rule multipliers per the proposal JSON.

# 4. Audit on 500-cmdr fixture.
uv run scripts/bench.py audit --fixture tests/fixtures/golden_set_run_500.json
uv run scripts/bench.py audit --per-commander-ndcg --fixture tests/fixtures/golden_set_run_500.json

# 5. Compare against the success criteria in the plan.
```

Expected outcome from these steps as of 2026-05-04:
- NDCG@30 aggregate delta ≈ −0.007 (negative)
- hidden_gem_hit_rate delta ≈ +0.04 (positive)
- ~65 commanders exceed the −0.05 per-commander cliff
- Per the plan's gate, this routes to DECLINE

## Next-step options for IDF-axis exploration

If a future probe wants to keep working on the IDF axis, the
documented alternatives (deferred per the BM25 plan's Out of Scope)
are:

1. **Smoothed-frequency IDF** — minor variant of current; smoothing
   constant `1 / log2(1 + α + n)` where α tunes the rare-rule slope.
   Doesn't have BM25's ~5× scale shift problem.
2. **Structural-overlap-corrected IDF** — collinearity correction
   when rules co-fire on the same candidates. Most novel,
   highest-effort.
3. **Per-rule-cluster IDF** — rules in same family share IDF basis
   (axis_feeders, peer_tribal_keyword, primitives). Reduces
   double-counting.
4. **Rank-aware IDF (TF-IDF dual)** — weight by per-commander
   rule-activation rarity. Targets selectivity-within-commander.

Any of these is a candidate for the next orthogonal-direction
brainstorm. None inherits the +0.005 NDCG lift from this probe — that
lift was nonexistent (BM25's NDCG was negative). Each must
independently clear the +0.010 ship bar.

## What was kept

- **Unit 1 (per-commander NDCG audit reporting):** general-purpose
  audit infrastructure that survives this probe's outcome. Lives at
  `src/mtg_synergy_graph/bench/per_commander_ndcg.py` with the CLI
  flag `bench.py audit --per-commander-ndcg`. Useful for any future
  audit-gated probe needing a per-commander prerequisite check.

What was reverted:
- BM25 IDF formula change in `src/mtg_synergy_graph/universal_scorer.py`
- Re-tuned `_RULE_QUALITY_MULTIPLIER` entries in `data/scoring_weights.json`
- `_PRODUCTION_HASH` pin in `tests/test_scoring_weights.py`

Reset was via `git reset --hard pre-bm25-baseline` followed by
cherry-pick of the Unit 1 commit. Clean revert verified by
`bench.py audit --expect-identity` PASS + full test suite PASS
(1837 tests).

## Sources

- Plan: [`docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md`](../../plans/2026-05-04-001-feat-bm25-idf-probe-plan.md)
- Brainstorm: [`docs/brainstorms/2026-05-04-bm25-idf-requirements.md`](../../brainstorms/2026-05-04-bm25-idf-requirements.md)
- Sibling null-result template: [`infrastructure-without-scoring-activation-2026-04-24.md`](infrastructure-without-scoring-activation-2026-04-24.md)
- Sibling: [`scaffold-queue-generator-exhaustion-2026-04-24.md`](scaffold-queue-generator-exhaustion-2026-04-24.md) — original "orthogonal directions" diagnosis
- Memory: [`feedback_edhrec_not_goal.md`](../../../memory/feedback_edhrec_not_goal.md) — gem-primary framing remains the project default; the BM25 reframe was explicitly scoped to that work only
