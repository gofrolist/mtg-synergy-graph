---
last_updated: 2026-05-22
module: embeddings
title: Infrastructure value without scoring activation (content embeddings null result)
tags:
  - flag-gated
  - null-result
  - audit-gate
  - hidden-gem-hit-rate
  - content-embeddings
  - plan-003
problem_type: best_practice
resolution_type: guideline
applies_when:
  - A flag-gated feature has cleared infrastructure review but the audit sweep produces a marginal signal.
  - The flip-decision rubric specifies a quantitative bar; the sweep delta is positive but below the bar.
  - The infrastructure has standalone value (diagnostic, authoring aid) beyond the gated scoring path.
created: 2026-04-24
plan_ref: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md
sweep_ref: scripts/sweep_embedding_weights.py
---

# Infrastructure value without scoring activation

When a flag-gated feature lands behind `_ENABLE_*` and the audit-gated
flip sweep shows a positive-but-marginal signal, the right answer is
often "ship the infrastructure, leave the flag off." This note
captures the specific rubric and the 003-content-embeddings null
result that surfaced it.

## Context

Plan `docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md`
landed the full content-embedding pipeline behind
`_ENABLE_EMBEDDING_CONTRIBUTION = False` in
`src/mtg_synergy_graph/embeddings/contribution.py`:

- 128-dim TF-IDF + truncated-SVD vectors per card
  (`src/mtg_synergy_graph/embeddings/vectorizer.py`,
  `src/mtg_synergy_graph/embeddings/svd.py`)
- Commander-target vector composition with `hi-syn` blending
  (`src/mtg_synergy_graph/embeddings/commander_target.py`)
- `embedding_contribution = w_emb * exp(-k * n_rules) * cosine(v_cand,
  v_cmdr)` additive term in `UniversalScore`
  (`src/mtg_synergy_graph/embeddings/contribution.py`)
- `bench.py audit --embedding-dedup` rule-redundancy diagnostic
  (`src/mtg_synergy_graph/bench/embedding_dedup_handler.py`)
- Hybrid hash discipline: `EmbeddingConfigInputs` + KV table +
  `ScoringConfigInputs.vectorizer_version`
- Pre-flag-flip gate documented at
  `docs/reviews/2026-04-23-content-embeddings-followups.md` FU-1..FU-5

The pipeline's stated FR7 target: a positive
`hidden_gem_hit_rate` delta vs baseline would justify flipping the
scoring flag on.

## The null result

A 3×3 grid over `w_emb ∈ {0.05, 0.1, 0.2}` × `k ∈ {0.6, 0.8, 1.0}` plus
a flag-off baseline, scored across 30 golden-set commanders with
`card_embeddings` populated (32,327 vectors), produced:

| Cell            | hit_rate | score_delta | Notes |
|-----------------|----------|-------------|-------|
| flag-off        | 0.7533   | 0.0         | baseline |
| w=0.05, k=0.6   | 0.7556   | 238.3       | +0.0023 |
| w=0.1,  k=0.6   | 0.7567   | 310.1       | +0.0034 |
| w=0.2,  k=0.6   | 0.7600   | 386.8       | +0.0067, advisory winner |
| w=0.2,  k=1.0   | 0.7567   | 332.7       | +0.0034 |

Best cell delta: **+0.0067** (+0.67% on a 30-commander mean). The
score-delta column shows the embedding term *is* noticeably reordering
candidates (sum of absolute per-candidate score deltas ≈ 387 across
3000 pinned candidates = ~0.13/candidate on a ~0.5 typical score), but
the reordering happens mostly *within* the top-30 and *within* the
plausibility-gate-passing set, so the hidden-gem-hit-rate metric
(which only counts mechanically-plausible-AND-not-in-EDHREC-top-30
candidates) does not shift.

Reproduce at any point:

```
uv run scripts/build_embeddings.py    # if data/synergy.db lacks vectors
uv run scripts/sweep_embedding_weights.py --commanders 30 --include-baseline
```

## Re-sweep 2026-05-22 (vocab v2 + AlterAttribute + Forge refresh)

Cited deltas justifying a re-sweep against the §"When to re-sweep"
preconditions (commit `e19f7a7`):

- **Vocabulary expansion** — `vocab_version` bumped to v2
  (`d527631`); `attr_kind='attribute'` rows added in PR #47
  (Prepared/Suspected/Plotted/Solved/Commander/Saddled/Harnessed) plus
  synthetic `AlternateMode:Prepare` keyword ports.
- **Corpus refresh** — Forge `f7ab132` added +297 cards (32,327 →
  32,624 vectors built).
- **Commander-target composition** — unchanged.

Same 3×3 grid as the April run, scored across the full 100-cmdr
golden-set fixture (vs the April run's 30-cmdr subset):

| Cell            | hit_rate | Δ vs baseline | score_delta | Notes |
|-----------------|----------|---------------|-------------|-------|
| flag-off (w=0)  | 0.8153   | 0.0           | 0.0         | baseline |
| w=0.05, k=0.60  | 0.8163   | **+0.0010**   | 632.3       | best Δ |
| w=0.05, k=0.80  | 0.8163   | +0.0010       | 559.2       | best Δ tie |
| w=0.05, k=1.00  | 0.8160   | +0.0007       | 490.0       | lowest score_delta |
| w=0.10, k=0.60  | 0.8130   | -0.0023       | 836.5       | regression |
| w=0.10, k=0.80  | 0.8130   | -0.0023       | 741.1       | regression |
| w=0.10, k=1.00  | 0.8133   | -0.0020       | 665.0       | regression |
| w=0.20, k=0.60  | 0.8077   | -0.0076       | 1096.8      | regression |
| w=0.20, k=0.80  | 0.8117   | -0.0037       | 980.4       | regression |
| w=0.20, k=1.00  | 0.8133   | -0.0020       | 886.2       | regression |

**Verdict: DECLINE.** Bar from §"Define the flip-decision bar
quantitatively before the sweep" requires Δ ≥ 0.02 hit_rate AND
|score_delta| ≤ 250. Best cell misses both: Δ = +0.0010 (20× below
the bar) and lowest cell `score_delta` is 490 (~2× above the cap).
Every `w_emb ≥ 0.10` cell *regresses* hit_rate vs baseline.

Two observations worth carrying forward:

1. **Baseline hit_rate climbed from 0.7533 → 0.8153** (+0.062) between
   2026-04-24 and 2026-05-22. The intervening rule-shipping work
   (Prepared mechanic, K:ETBReplacement walking, CopyFaceFrom, walker
   PRs) genuinely closed hidden-gem coverage that previously sat in
   the "rule-uncovered but embedding-near" zone. The embedding term
   now has less headroom to add value, not more — the vocabulary
   expansion *raised the floor* but did not change the ceiling, so
   the embedding contribution looks *smaller* in absolute terms than
   in April, not larger.
2. **The sweep's `[sweep] advisory winner` line is misleading** — it
   compares against `pinned=0.0000` because the current
   `tests/fixtures/golden_set_run.json` was pinned before
   `hidden_gem_hit_rate` was persisted in `FixtureEntry.legacy`. The
   flag-off cell (`w=0`) is the real baseline. Worth a small CLI
   fix: detect the missing-key case and either silence the advisory
   line or compare against the flag-off cell.

Reproduce:

```
uv run scripts/build_embeddings.py
uv run scripts/sweep_embedding_weights.py --include-baseline
# (default --w-grid 0.05,0.1,0.2 --k-grid 0.6,0.8,1.0,
#  default fixture tests/fixtures/golden_set_run.json = 100 cmdrs)
```

Wall time: ~22 min (sweep) + ~1 min (build) on the 100-cmdr fixture.

**Next-sweep preconditions are now stricter.** A future re-sweep should
not run unless one of:

- A *new* commander-target composition (richer EDHREC-free target
  source, different blending ratio), since the corpus-side levers
  (vocabulary + card count) have now both been measured and produced
  null deltas. The remaining unexplored axis is the commander-target
  vector itself.
- A vectorizer-architecture change (e.g., bumping `vectorizer_version`
  beyond TF-IDF + truncated-SVD — a learned encoder, a co-occurrence
  matrix factorization, etc.), which would change the geometry of the
  embedding space rather than its inputs.

Corpus refreshes alone (+Forge cardsfolder bumps) and incremental
vocabulary expansion (a handful of new `attr_kind` rows, new keyword
ports) are now **insufficient justification** for a re-sweep — the
2026-05-22 run measured both of those simultaneously and found no
shippable signal.

## Guidance

### 1. Define the flip-decision bar quantitatively before the sweep

A rubric like "positive hit-rate delta" is a satisficing bar, not a
decision bar. Plan documents should pre-commit to a minimum effect
size (e.g., `Δ ≥ 0.02`, matching the `HIDDEN_GEM_WARN_THRESHOLD` used
elsewhere in the codebase) so the sweep produces a yes/no answer, not
a judgment call. Retro-fitting "is +0.67% enough?" after the sweep
invites motivated reasoning.

The plan 003 rubric was under-specified. The right next version of a
flip-gate clause reads:

> Flip when the best cell's `hidden_gem_hit_rate` exceeds baseline by
> at least `HIDDEN_GEM_WARN_THRESHOLD` (0.02) on ≥ 30 commanders, with
> `|aggregate_score_delta|` ≤ 250 on the top-100 pinned candidates.

### 2. Infrastructure value can stand alone

Even with the flag off, the 003 pipeline pays off via:

- `bench.py audit --embedding-dedup` — surfaces rule pairs with
  near-parallel activation sets in content space. Complements
  `--collinearity` (different mechanism, same intent). Read-only,
  works today, no flag flip required.
- Rule-authoring aid — the embedding-dedup output informs which
  declarative rules in `data/rules_seed.json` are redundant
  candidates for consolidation.
- Hidden-gem diagnostic — the `--explain` renderer's embedding block
  (`src/mtg_synergy_graph/engine.py:_render_embedding_block`) surfaces
  the top-N nearest rule-uncovered neighbors per candidate, useful
  for "why did the scorer pick this card?" inspection regardless of
  whether embeddings contribute to the score.

Keeping the flag off does not undo the plan's value. The feature is
*infrastructure*, and infrastructure stays committed.

### 3. Null results must be documented as first-class outcomes

Without a written record, the next person asking "why is
`_ENABLE_EMBEDDING_CONTRIBUTION = False` when all the infrastructure
is there?" has no answer. They may re-run the sweep thinking it was
never done, or they may flip the flag assuming the discipline broke
down. Both failure modes are worse than the null result itself.

Format: sweep table + date + git SHA of the run + file path to the
sweep script. Store alongside the plan's follow-ups
(`docs/reviews/...-followups.md`) so the provenance is linked.

### 4. Keep the gate-blocking fixes landed even when the flip doesn't happen

FU-1 (stored-config verification, commit `1a61ad0`) and FU-4 (NaN
guard, same commit) fix latent correctness bugs regardless of the
flip decision. An orphan `_ENABLE_*=False` feature should still have
the verifier, NaN guard, store-side dim check, and test suite in a
shippable state — someone may flip the flag later with different
inputs (more data, better vectorizer, richer commander-target
composition), and those downstream experiments depend on the same
correctness primitives.

## Why this matters

Without a written null-result discipline, a team eventually either
(a) forgets the feature exists and re-invents it from scratch, or (b)
flips the flag blindly assuming the sweep would have caught problems.
Both outcomes erase the work the audit gate was supposed to protect.

The flip-rubric pre-commitment also removes one class of design
failure — "we built a knob, we'll see what it does." Either the knob
clears a pre-declared bar or it doesn't, and either answer lets the
team move on without lingering uncertainty.

## Examples from plan 003

- `scripts/sweep_embedding_weights.py` — the in-process grid-search
  tool that produced the null result. Reusable for any future
  re-sweep (e.g., after a richer commander-target composition).
- `docs/reviews/2026-04-23-content-embeddings-followups.md` — updated
  alongside this doc to close out the flip gate.
- `src/mtg_synergy_graph/embeddings/contribution.py` —
  `_ENABLE_EMBEDDING_CONTRIBUTION = False` stays as-is; module
  docstring already describes the flip procedure for any future
  revisit.

## When to re-sweep

This null result reflects the current (32,327-card corpus, v1
vectorizer, hi-syn-blended commander target) pipeline. A re-sweep is
justified when any of the following change materially:

- `TOKEN_FORMAT_VERSION` bumps (new port-feature columns, expanded
  attr-kind vocabulary).
- Commander-target composition changes (different blending ratio,
  richer EDHREC-free target source).
- The golden set changes in a way that shifts the rule-coverage
  baseline (different commander mix, different plausibility-median
  distribution).

A re-sweep without one of these changes is wasted work — the inputs
have not moved.

## References

- Plan: [`docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md`](../../plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md)
- Sweep tool: `scripts/sweep_embedding_weights.py`
- Followups (flip gate): [`docs/reviews/2026-04-23-content-embeddings-followups.md`](../../reviews/2026-04-23-content-embeddings-followups.md)
- FU-1/FU-4 gate-blocking fixes: commit `1a61ad0`
- Sibling learning (verify from stored config):
  [`verify-from-stored-config-not-code-defaults-2026-04-23.md`](verify-from-stored-config-not-code-defaults-2026-04-23.md)
