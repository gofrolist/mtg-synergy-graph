---
date: 2026-04-21
topic: content-embeddings
seed: docs/ideation/2026-04-21-recommendation-model-ideation.md (Survivor 6)
status: draft (brainstorm 6 of 7)
depends_on:
  - 2026-04-21-unified-eval-harness-requirements.md (audit gate + rule-activation tensor for dedup diagnostic)
---

# Requirements: Content Embeddings as Zero-Shot Fallback

## Problem Statement

Two classes of candidates are under-served by the current rule-based scorer:

1. **Heterogeneous hi-syn commanders.** Animar, Kess, Yuriko, and similar commanders have EDHREC hi-syn lists composed of cards with no single unifying mechanical rule (Animar's hi-syn spans counter doublers, SpellCast-Draw creatures, ETB-Draw creatures, self-bouncers, mana dorks, X-cost creatures). Writing one rule per sub-cluster would violate `memory/feedback_no_individual_rules.md`; leaving them uncovered misses real synergy.
2. **Unreleased cards.** When a new Magic set ships, its cards land in Forge's cardsfolder on day-0 but no hand-authored rule mentions the new mechanic yet. The scoring path silently under-scores them until humans catch up.

Both classes have the same underlying shape: a candidate whose port profile is *structurally similar* to things the commander wants, without matching any specific hand-written rule. A deterministic content embedding over ports + keywords captures that structural similarity without training and without popularity signal.

## Goals

1. Produce one 128-dimensional vector per card in the DB, derived entirely from structured features (port tuples + Scryfall keywords). No oracle-text n-grams, no popularity data.
2. Expose vectors for three uses: inference-time fallback on sparse-coverage candidates, day-0 cold-start for newly-imported cards, rule-redundancy diagnostic.
3. Keep the contribution strictly additive and rule-coverage-decayed — candidates well-covered by hand-written rules see near-zero embedding contribution.
4. Preserve determinism: same DB + same config → same vectors + same scores. No non-deterministic ML.
5. Stay under the `memory/feedback_audit_every_change.md` guardrail: embeddings enter the scoring path behind a feature flag, audited independently.

## Non-Goals

- Oracle-text n-grams or any free-text feature. Structured features only.
- Neural embeddings (transformers, learned weights). TF-IDF + SVD only.
- Popularity data of any kind (EDHREC rank, deck percentages, reprint frequency). Not in the feature set.
- Running at inference with any computation heavier than a precomputed dot product.
- Replacing rule-based scoring. Embeddings are strictly additive fallback, not a new scoring backbone.
- Online / continuous re-training. Vectors are rebuilt on DB re-import, not on user queries.

## Users and Scenarios

| Scenario | Before | After |
|---|---|---|
| Animar recommendation page | Hi-syn cards like Spellbinder, Experiment Kraj missed because no single rule fires on them | Embedding cosine to Animar's port centroid lifts them — they live in the same structural neighborhood even without specific rule coverage |
| Day-0 after Forge cardsfolder refresh | Newly-imported cards with novel mechanics get zero scoring signal until humans write rules | Their embeddings are computed automatically; cards structurally similar to existing ones get ranked sensibly immediately |
| Rule redundancy audit | `bench.py audit --collinearity` on Survivor 1 shows rule pairs correlated via activation overlap | New `bench.py audit --embedding-dedup` flags rule pairs whose activation sets have cosine > 0.95 in embedding space — a stronger orthogonality signal |
| `recommend.py --explain` for a card ranked high via embedding | `--explain` shows rule contributions summing to low total | A new `neighbor:` line shows the three nearest cards by embedding that ARE rule-covered, explaining why this card was lifted |

## Functional Requirements

### FR1 — Feature vectorizer

Per-card feature bag consists of:
- For each port row: `(port_type, event_class, valid_filter_token, zone_origin, zone_destination, counter_type, branch_kind)` as discrete categorical tokens.
- For each `port_attributes` row: `(attr_kind, value)` tokens.
- Scryfall keywords (`keywords` column on `cards`) as categorical tokens.
- No numeric features. No oracle-text n-grams.

Bag is TF-IDF vectorized across the full card corpus, then truncated-SVD reduced to 128 dimensions. Vectors L2-normalized.

Stored in new SQLite table:

```
card_embeddings (card_name TEXT PRIMARY KEY, vector BLOB, vectorizer_version INTEGER, built_at TEXT)
```

Vector stored as a 128×float32 blob (512 bytes per card → ~16 MB for 32 k cards). Rebuilt via `scripts/build_embeddings.py` on DB re-import.

### FR2 — Commander "target" vector

For each commander, compute a target vector = mean of its own port-feature vector and the vectors of its EDHREC golden-set hi-syn cards, if any. For commanders not in the golden set: target = commander's own port-feature vector. Cached in the `commander_cache` already used by the scorer.

Note: the golden-set-hi-syn input uses EDHREC for *vector computation*, not for ranking. It's still within the "no EDHREC at inference" guardrail because vectors are precomputed offline; inference is a cached lookup.

### FR3 — Inference-time contribution with exponential decay

The scorer gains a new additive contribution per candidate:

```
embedding_contribution(candidate) = w_emb · e^(-k · N_rules(candidate)) · cosine(v_candidate, v_commander_target)
```

where:
- `w_emb` = global scaling constant (initial value chosen by audit sweep; target order of magnitude same as a single average rule contribution).
- `k` = decay rate (initial `k = 0.8`; audit-tuned).
- `N_rules(candidate)` = count of rules currently firing a positive contribution on this (commander, candidate). Read from the per-(cmdr, cand) aggregate already built by Survivor 1's tensor (or computed locally if Survivor 1 hasn't landed yet).

At `N_rules = 0`: full embedding weight. At `N_rules = 5`: dampened ~10×. Smooth cutoff, no discontinuity.

### FR4 — Cold-start behavior

Every card in the DB has a vector by construction (vectors computed on DB re-import). Newly-imported cards with novel port shapes still produce sensible vectors because the vectorizer handles sparse new tokens gracefully (new tokens contribute zero IDF the first run; on the next rebuild they integrate). No special cold-start code path — the general mechanism handles this case by default.

### FR5 — Rule-redundancy diagnostic

New subcommand `bench.py audit --embedding-dedup` for each pair of registered rules:
1. Read from Survivor 1's rule-contribution tensor: the set of candidates where rule A fires, and where rule B fires.
2. Compute Jaccard similarity of those sets in the 128-d embedding space (mean pairwise cosine between the sets).
3. Pairs with similarity > 0.95 are flagged as candidates for merging.

This is the embedding-flavored complement to Survivor 1's FR6 MI-VIF collinearity — same intent, different mechanism. Useful input for Survivor 2's rule-consolidation migration.

### FR6 — Explainability

`recommend.py --explain` gets a new line under the breakdown when an `embedding_contribution > 0`:

```
  embedding_contribution: +0.047 (decay × cosine = 0.32 × 0.146)
    nearest covered neighbors: Spellbinder (cosine 0.88), Experiment Kraj (0.84), Soul of the Harvest (0.81)
```

Makes "why did this card rank here?" answerable even when no specific rule fired.

### FR7 — Feature flag + audit gate

Ship behind `_ENABLE_EMBEDDING_CONTRIBUTION = False` (default off). Enable via audit:
1. Flip flag; sweep `w_emb ∈ {0.1, 0.3, 0.5, 1.0}` and `k ∈ {0.5, 0.8, 1.2}`.
2. Pick the best `(w_emb, k)` pair by aggregate NDCG; require no commander regresses beyond CONTENTIOUS threshold.
3. If CONTENTIOUS: inspect per-commander losers. If losses concentrate on well-rule-covered commanders (expected — decay should dampen those): ship. If losses concentrate on sparse-coverage commanders: tune or revert.
4. If HARMFUL: revert. Embedding is rejected.

## Success Criteria

1. **Animar-class uplift.** Animar, Kess, Yuriko, and at least 4 other heterogeneous-hi-syn commanders (identified during audit) each show ≥ 1 hi-syn gain in their top-30 after the embedding contribution lands. No commander regresses beyond MARGINAL.
2. **Cold-start demonstration.** Select a small set of Forge-imported cards with recent-release mechanics. Verify they receive a non-zero embedding-based score and rank sensibly relative to structurally-similar predecessor cards. Sanity check, not a metric.
3. **Aggregate NDCG neutral-or-positive.** Aggregate NDCG@30 on 100-cmdr golden set is ≥ current baseline after embedding lands.
4. **Well-covered commanders unaffected.** Atraxa, Korvold, Karador, Prossh (commanders with clear archetype + many firing rules) show ≤ 0.001 NDCG delta — the exponential decay should make embeddings near-invisible on them.
5. **Inference latency impact ≤ 5%.** Per-candidate embedding contribution is a precomputed vector lookup + cosine; overhead is constant per candidate.
6. **Dedup diagnostic value.** `bench.py audit --embedding-dedup` flags at least two rule pairs that agree with the MI-VIF collinearity analysis from Survivor 1 FR6, and at least one rule pair that MI-VIF missed (capturing structural similarity the rule-activation overlap doesn't).

## Constraints

- No non-deterministic operations anywhere in the pipeline. Same DB + same vectorizer version → same vectors.
- No popularity-adjacent features. If a feature could encode reprint frequency or set-presence, it's out.
- Vector dimensionality fixed at 128 for first cut. Tuning is a follow-up effort.
- Embedding table must fit alongside existing caches in memory. ~16 MB is safely below any reasonable budget.
- Inference-time computation bounded: one cosine per (commander, candidate) pair. No k-NN queries at inference.

## Open Questions (For Planning Phase)

- Exact feature tokenization for multi-value columns (e.g., `valid_filter` is often a comma-separated string; each element becomes its own token?).
- Whether to use `TfidfVectorizer` from scikit-learn or hand-roll the TF-IDF computation. Scikit-learn is heavy; hand-rolled is straightforward.
- SVD implementation — `scipy.sparse.linalg.svds` vs `numpy.linalg.svd` on dense feature matrix. Corpus size 32 k × ~5 k tokens sparse = manageable with either.
- Refresh cadence — rebuild on every `import_cardsfolder.py`, or a separate user-invoked command?
- Whether to support commander-target vectors for commanders NOT in the golden set by computing from commander ports alone.
- How embedding-dedup signal interacts with Survivor 2's `rules` table — does it auto-propose merges, or just surface pairs for humans?

## Out of Scope for This Brainstorm

- Oracle-text embeddings. Rejected as feature input.
- Neural / transformer embeddings. Rejected as non-deterministic + non-explainable.
- Runtime fine-tuning. No training anywhere in the pipeline.
- Cross-card path embeddings (Morgan/ECFP circular fingerprints). Promising alternative; revisit as a phase-2 follow-up if Survivor 2's canonical vocabulary lands — circular neighborhoods are cleaner over typed nodes than over raw port tuples.

## Related

- Seed idea: `docs/ideation/2026-04-21-recommendation-model-ideation.md` Survivor 6.
- Prerequisite (for FR5 rule-redundancy diagnostic): `2026-04-21-unified-eval-harness-requirements.md` FR2 contribution tensor.
- Natural successor: a phase-2 upgrade to Morgan/ECFP circular fingerprints once Survivor 2's canonical vocabulary makes neighborhoods clean.
- Guardrail: `memory/feedback_audit_every_change.md` — embedding contribution lands behind feature flag and is audit-gated.
- Memory alignment: `memory/feedback_no_individual_rules.md` — embedding is the structural response to heterogeneous hi-syn commanders where per-sub-cluster rules would be wrong.
- Memory constraint: `memory/feedback_edhrec_hivemind.md` — EDHREC appears only in *offline* commander-target vector computation, never at inference ranking; this is the same contract the existing `--explain` tiebreaker uses and stays within it.
