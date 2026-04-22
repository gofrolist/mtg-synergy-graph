---
date: 2026-04-21
topic: recommendation-model
focus: improve recommendation model toward universal, Forge-DSL-based, no-EDHREC-at-inference, works on unreleased cards
mode: repo-grounded
---

# Ideation: Improving the Recommendation Model

## Grounding Context

**Codebase.** NDCG@30 ~0.262 on 100-commander golden set. 108 k ports from 32 k cards. 40+ complement rules across 5 families (primitives / density / archetype / axis-feeder / specialized). Scoring = commander ports × candidate ports → IDF-weighted sum in `src/mtg_synergy_graph/universal_scorer.py::_score_universe`. Rule authoring loop: `gap_report.py → scaffold_rule.py → _audit_rule_impact.py` (~10 min).

**Hard constraints.**
1. No EDHREC rank at inference (OK for eval NDCG@30 + as tiebreaker within same score band).
2. System must be independent and Forge-DSL based.
3. Universal — must work on currently unreleased cards from card text / Forge script alone (zero-shot).

**Anti-goals.** Per-commander rules · EDHREC hivemind as design oracle · cherry-picked Forge data · regex-per-mechanic · per-archetype rules · one-off game-rule patterns.

**Dark gaps.** `scales_with.xPaid` (94 cmdrs, 0% covered) · `scales_with.Valid[*]` (115 uncovered) · `cost.remove_counter[*]` (42 cmdrs, 0%).

**External techniques surfaced.** BM25 length normalization · Scryfall Tagger `otag:*` ingest · PPMI on pair co-occurrence · MI-VIF/mRMR rule orthogonality · Forge `BoosterDraftAI.java` synergy scorer · differentiable rule mining (DRUM / Neural LP) · content embeddings (Mamedov 2025, MTG paper 2024).

## Ranked Ideas

### 1. Unified eval harness + persisted rule-contribution tensor
**Description.** Persist per-(commander, candidate, rule) contribution values to a new SQLite table during scoring. Collapse `_audit_rule_impact.py`, `golden_set_track.py`, `compare_edhrec.py`, `weight_grid_search.py` into one `scripts/bench.py` where per-rule NDCG delta, per-commander winners/losers, and rule-pair collinearity (MI-VIF) are all SQL queries. Replace the binary TRIVIAL verdict with a rank-shuffle histogram. Optional: scale the golden set to all 2,761 EDHREC commanders once eval is O(1) in rule count.
**Rationale.** Every recent commit is a 10-min coordinate-descent step performed by hand because the audit is slow. Cutting audit from ~10 min to <30 s unlocks automated weight optimization, rule-orthogonality pruning, negative-pair regression testing, and blast-radius preview at scaffold time — half the rejected ideas fold into SQL queries against one table.
**Downsides.** Front-loaded infra cost. Tensor ~100 × 32 k × 40 ≈ 130 M sparse entries — tractable but needs discipline.
**Confidence:** 85% · **Complexity:** Medium · **Status:** Unexplored

### 2. Typed port-graph + rules-as-data over canonical event nodes
**Description.** Project every port into a small canonical vocabulary (`ETB`, `DIES`, `CAST`, `PAYMANA`, `TAP`, `COUNTER_PLACED`, `SACRIFICE`, `DISCARD`, `DRAW`, `DAMAGE`, `ZONECHANGE`, `STATIC_BUFF`, …) with typed attribute columns. Promote `EVENT_MATCH_MAP` + `COST_FEEDS_TRIGGER` from inline Python dicts into a materialized `port_nodes` view. Rewrite 40+ hand-written rules as rows in a `rules` table consumed by one interpreter. The 10 `*_feeder` helpers collapse to one `scales_with_axis` engine + one row per axis.
**Rationale.** Directly targets the "universal / unreleased cards" goal — a new Forge release gets coverage automatically if the node types hold. Collapses `complement_rules/` from ~2,500 LOC to ~400 LOC + a config table. Makes every future rule mineable and every weight tuneable. Implements the `feedback_no_individual_rules` memory note at structural level.
**Downsides.** Large blast radius. Aggressive node normalization can swallow signal. Rule-as-data loses Python escape hatch for imperative gates (mitigation: keep a ~5-rule code tier for edge cases).
**Confidence:** 75% · **Complexity:** Medium-High · **Sequencing:** land Survivor 1 first so refactor is auditable · **Status:** Unexplored

### 3. BM25F + conditional (color/legal) IDF + synapomorphy depth weighting
**Description.** Three composable IDF reforms, any single one shippable in a day: (a) BM25 length normalization on port counts, (b) per-field weights for `{triggers, activated_abilities, static_buffs, replacement_effects, costs}`, (c) conditional denominator — IDF inside the color-identity × legal-commander pool, not the 32 k universe. Bonus: exponential reward for the most-derived matching port shape.
**Rationale.** Well-understood IR techniques with decades of tuning wisdom. Addresses two active pain points: port-heavy cards spuriously outranking focused ones, and IDF saying "Atraxa's baseline port is rare." Smallest-code, lowest-risk survivor.
**Downsides.** Must sweep `b` on golden set. Conditional denominator requires careful caching.
**Confidence:** 80% · **Complexity:** Low-Medium · **Status:** Unexplored

### 4. Forge BoosterDraftAI second oracle + Forge-precon co-occurrence
**Description.** Two Forge-internal training signals that never touch EDHREC: (a) Port Forge's Java `BoosterDraftAI` + `*AiController` synergy heuristics to Python; run as parallel scorer. Diffs become rule-seed candidates. (b) RAPM-style on/off co-occurrence inside Forge's bundled precon decks.
**Rationale.** Designer-authored ground truth that's categorically independent of EDHREC popularity. Uses Forge's own data — the engine this project is already parametrized by.
**Downsides.** Java→Python port is real work. Precon corpus is small; statistics are noisy.
**Confidence:** 75% · **Complexity:** Medium · **Status:** Unexplored

### 5. Multi-hop / pathway scoring through the port graph
**Description.** Extend `cost_feeds_trigger` from depth-1 to paths of length 2–3. Score by longest contiguous cascade (ETB → token → sacrifice → draw). Hypergraph extension handles 3-card enablers as first-class.
**Rationale.** Engine-grade commanders (Korvold, Muldrotha, Meren, Teysa) are inherently cascade-shaped; current pairwise matching misses the bridge cards. Dark-gap `scales_with.Valid[*]` at 34% coverage is largely relational/referential ports.
**Downsides.** Combinatorial blow-up without caps. Hardest to explain in `--explain` output.
**Confidence:** 65% · **Complexity:** Medium · **Status:** Unexplored

### 6. Content embeddings as zero-shot fallback
**Description.** One-time bag-of-features embedding per card: (port kind tuples, valid_filter tokens, oracle-text n-grams, canonical-event neighborhoods) → TF-IDF + SVD or Morgan/ECFP circular fingerprints → 128-d deterministic vectors in SQLite. Three uses: fallback signal when <2 hand rules match (Animar-class), cold-start for unreleased cards, rule-dedup diagnostic.
**Rationale.** Most direct answer to "universal for unreleased cards." Deterministic and explainable via nearest-port attribution — stays in "no ML at inference" envelope (inference is cached cosine lookup).
**Downsides.** Text n-grams risk encoding popularity-adjacent artifacts. Strictly additive / fallback-only.
**Confidence:** 60% · **Complexity:** Medium · **Status:** Unexplored

### 7. Useful-disagreement objective with NDCG floor
**Description.** Add `hidden_gem_hit_rate = (cards we rank top-30 ∧ EDHREC doesn't ∧ mechanically plausible) / 30` as primary target. Keep NDCG ≥ 0.20 as a floor, not the maximand. Alternative framing: pairwise Jaccard distance of top-30 across same-color-identity commanders.
**Rationale.** Memory says it three times — goal is "hidden gems from mechanics, not match the hivemind" — yet every recent commit is NDCG-vs-EDHREC driven. Live coherence gap between stated intent and optimization loop.
**Downsides.** Diversity metrics are game-able without the NDCG floor. Paradigm shift.
**Confidence:** 55% · **Complexity:** Small (metric) / Large (implications) · **Status:** Unexplored

## Cross-Cutting Guardrail

Every survivor's implementation is gated by NDCG@30 audit before/after. Pure-infra changes (Survivor 1, CDC on import) exempt from NDCG gate but must pass a fixed-rule-set identity check. See `memory/feedback_audit_every_change.md`.

## Rejection Summary

Full rejection list in scratch (`/tmp/compound-engineering/ce-ideate/a3f7c2e1/survivors.md`). Key cuts:
- `card_hints DeckNeeds` inversion — three prior revert cycles
- EDHREC pair PPMI — borderline popularity leak; Forge-precon variant (Survivor 4) is cleaner
- `4,000 auto-mined micro-rules` — re-introduces hivemind
- `20-axis typed vector scoring` — risks higher-D weight-tuning treadmill; revisit after Survivor 7
- `evolving query (deck-so-far)` — valuable UX but orthogonal to model quality
- `oracle-text-only baseline` — keep as one-shot diagnostic, not ongoing survivor
- ~15 diagnostic ideas fold into Survivor 1's `bench.py`
