---
title: "feat: Tensor-driven weight optimizer (foothold M1)"
type: feat
status: active
date: 2026-04-26
origin: docs/brainstorms/2026-04-26-tensor-weight-optimizer-requirements.md
---

# feat: Tensor-driven weight optimizer (foothold M1)

> **Plan revisions (post doc-review, applied during ce-work Phase 0):**
> - **Unit 1 (FR0) dropped.** Code-read of `complement_rules/__init__.py::find_all_complements`, `universal_scorer.py:_emit_tensor_rows` (line 813), and `bench/fixture.py::PinnedFixture.assert_identity` confirmed declarative rules already flow through the same aggregator + tensor-emission path as Python helpers; `--expect-identity` compares top-N scores + `config_hash`, not tensor row shape. There is no attribution gap to close. R1 / FR0 is a no-op.
> - **Unit 3 candidate-filter strategy resolved.** The `weight_grid_search.py` pattern (cache `complements` per commander once at handler entry, then per grid cell re-IDF and instantiate `UniversalScore(complements, idf_weights).score` for dampening-aware re-score) replaces the "extend `score_all_universal` with a `candidate_filter` arg" deferred decision. No public API change to `score_all_universal`.
> - **Self-test planted shift changed from `1.5×` to `2.0×`** (Unit 4) so the recovery `0.5×` lands exactly back on baseline — the previous `1.5×` plant required a `0.667×` recovery that wasn't on the grid.
> - **Split strategy: random fixed-seed 80/20**, no archetype stratification (`data/tags.db` lacks the required taxonomy). Per-color-identity DCG reporting in proposal output remains as a post-hoc skew detector. M2 may revisit with a hand-curated archetype mapping if foothold reveals real bias.
>
> Unit numbering renumbers 2→1, 3→2, 4→3, 5→4, 6→5. R1 deleted; R2-R13 keep their original labels for backwards-traceability to the origin brainstorm.

## Overview

Replace manual editing of `data/scoring_weights.json` with an offline Coordinate Ascent optimizer over `_RULE_QUALITY_MULTIPLIER`. The optimizer uses the persisted contribution tensor as a cheap top-K filter and calls `score_all_universal()` on those K candidates per commander to compute a composite α-blended objective (`α · mean_per_commander_nDCG + (1-α) · hidden_gem_hit_rate`). Output is a candidate `data/scoring_weights.json` diff for human review — the optimizer never mutates weights on disk. A planted-perturbation self-test makes "no improvement found" falsifiable.

## Problem Frame

Manual weight tuning is the only active lever for moving NDCG@30 (stuck at ~0.256). The persisted tensor would seem to enable O(1) re-scoring, but per `src/mtg_synergy_graph/bench/rule_ops.py:8-18` it stores **pre-dampening** contributions; a scalar dot-product re-score diverges from production above the 70% concentration threshold — exactly the multi-rule cards that matter for hidden gems. M1 therefore uses hybrid scoring: tensor as cheap filter, live `score_all_universal()` as production-faithful objective. (see origin: `docs/brainstorms/2026-04-26-tensor-weight-optimizer-requirements.md` Problem Statement.)

## Requirements Trace

- ~~**R1 (FR0).** Declarative-rule attribution in the persisted tensor must match Python-rule attribution before the optimizer runs.~~ **Dropped during ce-work Phase 0** — see plan revisions above.
- **R2 (FR1).** Coordinate Ascent over all 53 keys in `_RULE_QUALITY_MULTIPLIER`, multiplicative grid `[0.5×, 0.75×, 1.25×, 1.5×, 2.0×]`, alphabetical sweep order. Per-grid-cell scoring uses cached-complements + re-IDF + `UniversalScore.score` (production-faithful, no `score_all_universal` API change).
- **R3 (FR2).** Composite objective `α · mean_per_commander_nDCG@30 + (1-α) · hidden_gem_hit_rate`, default `α = 0.5`, both terms in [0, 1].
- **R4 (FR3).** Per-axis values (mean_ndcg, gem_rate) logged alongside composite; per-axis regression > 0.005 emits a stderr warning.
- **R5 (FR4).** Random fixed-seed 80/20 split (no archetype stratification); step accepted iff train composite ↑ AND held-out composite degraded by ≤ ε=0.005. Cumulative held-out drift check at sweep boundaries reverts the sweep if cumulative drop > 0.005. Per-color-identity DCG reporting in proposal output as post-hoc skew detector.
- **R6 (FR4b).** Weight clamp `[0.01, 5.0]` after every coordinate step.
- **R7 (FR4c).** Tensor-staleness precondition — refuse to run on hash mismatch.
- **R8 (FR5).** Termination: convergence (zero accepted in a full sweep) OR 5 sweeps OR 5 min wall-clock; partial-sweep abort flagged in proposal.
- **R9 (FR6).** Single artifact `.audit/optimize_proposal.json` with per-rule diffs, per-axis deltas, config hashes, run summary. Optimizer never mutates `data/scoring_weights.json`.
- **R10 (FR7).** Append-only `.audit/optimize_history.csv` with one row per attempted step, mirroring `bench/history.py` shape.
- **R11 (FR8).** New `bench.py audit --optimize [--max-sweeps N] [--seed N] [--no-self-test]` flag.
- **R12 (FR9).** Empty proposal still writes when no improvement found.
- **R13 (FR10).** Planted-perturbation self-test runs before the proposal phase; aborts with diagnostic if a known-good move is unrecoverable.

## Scope Boundaries

- M1 only optimizes `_RULE_QUALITY_MULTIPLIER`. `_FLAT_WEIGHT_OVERRIDES` is M2.
- M1 uses Coordinate Ascent only. SPSA / LambdaMART are M3.
- M1 does NOT auto-write `data/scoring_weights.json`; human applies the diff and runs `bench.py audit --repin --yes`.
- M1 does NOT add CI gating, auto-revert, or `--apply-proposal` shortcut.
- M1 does NOT extend the tensor schema — reuses existing `rule_contributions` table and `compute_config_hash`.
- M1 does NOT wire `weight_hint` into scoring (it remains a dead field on `data/rules_seed.json`; that is M2).

### Deferred to Separate Tasks

- **Full Survivor #2 — `--writer-trace` UX + per-rule auditor inspect for declarative rules.** This plan lifts only the auditor-side predicate compilation prerequisite (Unit 1). The full UX is a separate plan.
- **`α` calibration sweep on sandbox.** Default `α = 0.5` ships in M1; calibration is a one-off post-merge run that doesn't affect the M1 implementation surface.
- **Search grid sweep over alternative coarseness (e.g., `[0.6×, 0.8×, 1.25×, 1.66×]`).** One-off post-merge benchmark; M1 ships with the doc's default.

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/bench/cli.py` — `_HANDLERS` dict (line 51), `_resolve_mode` (line 299), mutually-exclusive flag group (line 120). New mode `optimize` is added here.
- `src/mtg_synergy_graph/bench/__init__.py` — registration site for handler bindings (lines 46-65). New `_cli.register("optimize", handle_optimize)` joins the others.
- `src/mtg_synergy_graph/bench/handlers.py` — canonical handler shape: open DB → call core function → render via `_emit_rendered` → return exit code (0/1/2).
- `src/mtg_synergy_graph/bench/embedding_dedup_handler.py` and `bench/forge_oracle_handler.py` — closest sibling precedents for a standalone handler module.
- `src/mtg_synergy_graph/bench/history.py` — append-only CSV pattern (frozen `HistoryRow` dataclass + `CSV_FIELDS` tuple + `append_run` writer with `tell() == 0` empty-file detection + `read_last` validator). The optimizer's `optimize_history.csv` mirrors this shape.
- `src/mtg_synergy_graph/bench/tensor.py` — `TensorWriter` context manager, `compute_config_hash()` at lines 38-95. Hash is invalidated by any change to `_RULE_QUALITY_MULTIPLIER`, so the optimizer's per-step patch automatically flips the hash for `proposed_config_hash`.
- `src/mtg_synergy_graph/bench/hidden_gems.py` — `hidden_gem_hit_rate_for_commander(commander, our_top_30, edhrec_top_30, contributions)` at line 181 + `aggregate_hidden_gem_hit_rate` at line 225. Takes already-scored top-30 + contributions iterable.
- `src/mtg_synergy_graph/bench/rule_ops.py:8-18` — explicit pre-dampening contract on the tensor; load-bearing for the hybrid-scoring justification.
- `src/mtg_synergy_graph/universal_scorer.py` — `score_all_universal` (line 644), `_RULE_QUALITY_MULTIPLIER` dict (line 579), `_load_scoring_weights` (line 479). Reads module globals — no weights override parameter, so the optimizer **patches the global dict in-place via `.clear() + .update()`** then restores in a `try/finally`. This is the established test pattern (see `tests/bench/test_universal_scorer_identity.py`).
- `src/mtg_synergy_graph/validate.py::compute_ndcg` (line 45) — `(predicted: Sequence[str], labels: dict[str, float], *, k: int = 30) -> float`. Reused as-is for the per-commander nDCG term in FR2; no new primitive needed (mean is just a loop + average).
- `src/mtg_synergy_graph/validate.py::_fetch_edhrec_sections` and `edhrec_helpers.fetch_high_synergy_top_n` — EDHREC label source; produces `hi_syn` set used to build graded labels (3.0 / 1.0 / 0).
- `src/mtg_synergy_graph/port_graph/interpreter.py::_compile_gate_predicate` (line 205) — returns `Callable[[PortRow], bool]`. **FR0 calls this in the tensor-build path** so declarative rules emit per-port-attributed contribution rows.
- `src/mtg_synergy_graph/complement_rules/__init__.py::find_all_complements` — routes between Python helpers and `RuleInterpreter` based on `DECLARATIVE_RULE_IDS`. The FR0 change ensures both branches emit the same shape of port-attributed `PortComplement` rows for tensor accounting.
- `tests/test_scoring_weights.py:309-324 (canonical .clear()+.update() pattern) and tests/bench/test_tensor_write.py:81 (patch.dict(..., clear=True) precedent)` — canonical test fixture: real DB at `tmp_path/"synergy.db"`, seeded `cards` + `card_ports` rows; assert bitwise float equality across runs.
- `tests/bench/test_audit_integration.py:34-103, 84-103` — synthetic EDHREC DB (~4 lines: `CREATE TABLE edhrec_card_synergy + executemany`) and `argparse.Namespace(...)` constructed manually for handler unit tests.
- `scripts/weight_grid_search.py` (deprecated) — proves the per-(cmdr, comps, labels) caching pattern works at ~100x speedup vs naive. Implementation reference for the inner score-with-overrides loop, with the caveat that its scorer omits dampening (M1 uses live `score_all_universal()` instead).

### Institutional Learnings

- `docs/solutions/best-practices/extract-python-dict-to-json-sidecar-2026-04-25.md` — `data/scoring_weights.json` schema is `{section: {key: {value, comment}}}`. `value` flips the config hash; `comment` does not. Optimizer must preserve `comment` fields verbatim when emitting the proposal diff.
- `docs/solutions/best-practices/sweep-writers-not-just-readers-on-source-of-truth-refactor-2026-04-25.md` — recurring blind spot: regex-based scaffolders silently no-op after a literal is deleted. Optimizer never writes to disk, so this is N/A; but call out in code review that any future "auto-apply proposal" extension must enumerate `scripts/` writers, not just import graph.
- `docs/solutions/best-practices/rule-quality-gates-2026-04-24.md` — the writer-side audit gap that `ward_2_tribal` exploited. FR0 closes the analog gap in the auditor's per-port attribution path.
- `memory/feedback_audit_every_change.md` — every scoring-path change must pass `bench.py audit`. Unit 1 (FR0) and Unit 3 (hybrid scoring) both touch the scoring path; both must pass audit identity check on baseline weights.
- `memory/feedback_audit_metric_too_coarse.md` — addressed by Unit 4's per-axis logging (FR3/FR6/FR7 all log mean_ndcg + gem alongside composite).

### External References

External research skipped per Phase 1.2 — local patterns dense (existing `bench/` modules, `weight_grid_search.py` precedent, `compute_ndcg`). The Saito & Joachims caveat from the brainstorm doc does not apply to M1 (we use mean per-commander nDCG, in-policy).

## Key Technical Decisions

- **Hybrid scoring (Unit 3) is non-negotiable.** Tensor is pre-dampening per `bench/rule_ops.py:8-18`; scalar dot-product re-score diverges above the 70% concentration threshold. Use tensor for top-K filter only; call `score_all_universal()` for the K candidates that actually matter.
- **Patch-and-restore on `_RULE_QUALITY_MULTIPLIER`** rather than introducing a non-mutating `compute_config_hash_for(...)` variant. The dict is module-global; tests already use `.clear() + .update()` to preserve identity. `compute_config_hash()` automatically picks up the patch via `get_scoring_config_inputs()` (which returns live references). Resolves origin OQ #3.
- **FR0 reuses `_compile_gate_predicate`** rather than writing parallel compilation logic. The interpreter already returns a `Callable[[PortRow], bool]`; FR0 hooks it into the auditor's per-port gate path during tensor build.
- **Composite objective replaces λ-floor entirely.** Both terms in [0, 1], default `α = 0.5`, single configurable constant. Eliminates the unbacked `λ = 100` calibration from the brainstorm.
- **Stratified split by archetype tag, not color identity.** Color identity is a weak proxy for which rules fire; archetype tags (tribal / graveyard / voltron / spell-density / lifegain / token / counter / aristocrats / unbucketed) directly map to rule firings.
- **Optimizer never writes to disk.** The artifact is `.audit/optimize_proposal.json`; human applies the diff manually and runs `bench.py audit --repin --yes`. Keeps M1 inside the existing `feedback_audit_every_change.md` discipline.
- **Self-test before proposal (FR10).** Distinguishes "weights near optimum" from "gates mis-calibrated" — without it, `n_steps_accepted: 0` is unfalsifiable. Opt-out via `--no-self-test` for cycles where calibration was just validated.

## Open Questions

### Resolved During Planning

- **OQ #1 from origin (search grid coarseness)** — keep the brainstorm default `[0.5×, 0.75×, 1.25×, 1.5×, 2.0×]` for M1 ship; alternative-grid sweep is deferred to a separate one-off benchmark task per Scope Boundaries.
- **OQ #2 from origin (raw DCG implementation)** — N/A in M1: composite objective uses **mean per-commander nDCG** (existing `compute_ndcg`), not raw DCG. No new primitive needed; FR2 was rewritten in the brainstorm review pass to use mean-nDCG specifically to avoid the coverage-bias problem with aggregate raw DCG.
- **OQ #3 from origin (non-mutating `compute_config_hash`)** — patch-and-restore on the module-global dict (see Key Decisions). No schema change.
- **Where is the archetype tag set defined?** Build it inline in Unit 2 from `data/tags.db` (`edhrec_card_synergy` + `edhrec_themes`-style joins; falls back to "unbucketed" when no tag matches). The 9 buckets are scoped to the 100 golden-set commanders; not a general project taxonomy.
- **Top-K size validation (Unit 3).** Validated empirically by Unit 3's recall test: production top-30 must be a subset of tensor-filtered top-200 on baseline weights. Test fails the build if recall < 99% on the 100-cmdr golden set.

### Deferred to Implementation

- **Exact column order for `optimize_history.csv`.** Brainstorm gives the column list; final ordering is a code-review nit, not a planning decision.
- **Multiprocess vs single-process per-commander scoring.** Brainstorm targets <3 min wall-clock. If single-process exceeds budget, planning option is `multiprocessing.Pool` per commander — but only adopt if the budget is missed; YAGNI otherwise.
- **Stderr summary table column widths.** Cosmetic; pick at code time.
- **Termination message wording for partial-sweep abort.** Cosmetic.

## Output Structure

New files added to existing `src/mtg_synergy_graph/bench/` and `tests/bench/` directories — no new directory hierarchy created.

- `src/mtg_synergy_graph/bench/optimize.py` (NEW) — composite objective + random split + EDHREC label loader (Unit 1) + cached-complements scorer (Unit 2) + Coordinate Ascent driver + self-test + dead-key detection (Unit 3) + `handle_optimize` (Unit 4) + proposal/history artifact writers (Unit 5).
- `.audit/optimize_proposal.json` (NEW runtime artifact) — gitignored.
- `.audit/optimize_history.csv` (NEW runtime artifact) — gitignored.
- `tests/bench/test_optimize.py` (NEW) — covers Units 1, 2, 3.
- `tests/bench/test_optimize_handler.py` (NEW) — covers Units 4, 5 + JSON/CSV schemas.
- Modified files: `src/mtg_synergy_graph/bench/cli.py`, `src/mtg_synergy_graph/bench/__init__.py` (registration only).

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
                                           bench.py audit --optimize
                                                   │
                                                   ▼
                            ┌──────────────── handle_optimize ────────────────┐
                            │                                                  │
                            │  1. Tensor staleness check (FR4c)                │
                            │     └─ refuse if config_hash mismatch            │
                            │                                                  │
                            │  2. Load splits (Unit 2)                         │
                            │     ├─ Stratified 80/20 by archetype tag         │
                            │     └─ EDHREC graded-label dicts per commander   │
                            │                                                  │
                            │  3. Self-test (FR10) — opt-out via --no-self-test│
                            │     ├─ pick random rule_id                       │
                            │     ├─ patch its weight to 1.5×                  │
                            │     ├─ run a single sweep                        │
                            │     └─ assert recovered to ±10% of original      │
                            │       fail → abort with diagnostic               │
                            │                                                  │
                            │  4. Coordinate Ascent driver (Unit 4)            │
                            │     for sweep in 1..max_sweeps:                  │
                            │       for rule_id in sorted(weights):            │
                            │         for mult in [0.5, 0.75, 1.25, 1.5, 2.0]: │
                            │           candidate_value = clamp(curr * mult)   │
                            │           train_obj = score_split(train, weights)│
                            │           held_obj  = score_split(held,  weights)│
                            │           if train↑ and held degraded ≤ ε:       │
                            │             accept, update curr, log row         │
                            │           else: log reject row, restore          │
                            │       if cumulative_held_drift < -0.005:         │
                            │         revert sweep, terminate (partial=true)   │
                            │       if zero accepted: terminate (converged)    │
                            │                                                  │
                            │  5. Emit artifacts (Unit 6)                      │
                            │     ├─ .audit/optimize_proposal.json             │
                            │     └─ .audit/optimize_history.csv (append)      │
                            └──────────────────────────────────────────────────┘

  score_split(commanders, weights)          ◀── pure function over (split, weights)
        │
        ▼
  for cmdr in commanders:                    ◀── Hybrid scoring primitive (Unit 3)
    1. patch _RULE_QUALITY_MULTIPLIER ← weights
    2. tensor top-K filter (K=200)
    3. score_all_universal(cmdr, K candidates)
    4. select top-30 from live scores
    5. compute_ndcg(top_30, edhrec_labels[cmdr])
    6. hidden_gem_hit_rate_for_commander(cmdr, top_30, edhrec_top_30, contributions)
    7. restore _RULE_QUALITY_MULTIPLIER

  return α · mean(ndcg_per_cmdr) + (1-α) · mean(gem_per_cmdr)
```

## Implementation Units

- [ ] **Unit 1: Composite objective + random 80/20 split + EDHREC label loader**

**Goal:** Provide pure helper functions for the composite α-blended objective, the deterministic random train/held split, and the per-commander graded-label dicts the objective consumes.

**Requirements:** R3, R4, R5 (split portion).

**Dependencies:** None.

**Files:**
- Create: `src/mtg_synergy_graph/bench/optimize.py` (NEW; this unit's helpers are module-private functions in the same file as the driver)
- Test: `tests/bench/test_optimize.py` (NEW)

**Approach:**
- Define `composite_objective(per_commander_ndcg: dict[str, float], per_commander_gem: dict[str, float | None], alpha: float) -> CompositeObjective` returning `{composite, mean_ndcg, gem_rate, n_commanders, n_commanders_with_gem}`. Mean over commanders where `gem` is not None (commanders without EDHREC top-30 contribute to nDCG but not to gem mean).
- Define `random_split(commanders: Sequence[str], *, train_ratio: float = 0.8, seed: int) -> SplitResult` returning train/held tuples. `random.Random(seed).sample(...)` for determinism. No archetype stratification (see plan-revisions header). `SplitResult` carries the train/held lists plus per-color-identity bucket counts (read from `cards.color_identity`) for post-hoc skew reporting.
- Define `load_edhrec_labels(conn: sqlite3.Connection, commander: str) -> tuple[dict[str, float], set[str]]` returning `(graded_labels, top_30_set)`. Graded labels follow `weight_grid_search.py:103-107` shape (3.0 for hi_syn, 1.0 for on_page, 0 elsewhere). The set is the EDHREC-declared top-30 used by the gem metric.
- Frozen-dataclass return types; no class state.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/hidden_gems.py:225` — `aggregate_hidden_gem_hit_rate` and the `HiddenGemReport` frozen dataclass shape; mirror for `CompositeObjective`.
- `src/mtg_synergy_graph/validate.py:_fetch_edhrec_sections` and `scripts/weight_grid_search.py:103-107` — graded-label construction.
- `src/mtg_synergy_graph/bench/history.py:60` (`HistoryRow`) — frozen dataclass conventions.

**Test scenarios:**
- **Happy path (composite):** With `α=0.5`, `mean_ndcg=0.40`, `gem_rate=0.84`, composite = 0.62. Sweep `α ∈ {0.3, 0.5, 0.7}` to confirm linear blend.
- **Happy path (split determinism):** Same `commanders` + `seed` produce bitwise-identical train/held lists across runs. Different seeds produce different splits.
- **Color-identity bucket reporting:** A 100-commander synthetic list with mixed mono/2c/3c+ commanders produces a `SplitResult` whose per-bucket counts sum to the input set sizes. (Reporting only — no stratification gate on the counts.)
- **Edge case (composite):** `gem_rate` mean over zero commanders (no commander has EDHREC top-30) returns composite = `α · mean_ndcg + 0` and reports `n_commanders_with_gem=0` distinctly from `n_commanders`.
- **Error path:** `α` outside [0, 1] raises `ValueError`.
- **Happy path (labels):** A known-EDHREC commander returns labels with hi_syn cards at 3.0 and on_page-only cards at 1.0; the top_30_set has at most 30 entries.
- **Edge case (labels):** Commander with no EDHREC entry returns `({}, set())` — caller treats as "no gem signal for this commander."

**Verification:**
- `pytest tests/bench/test_optimize.py -k "objective or split or labels"` green with ≥80% coverage of the helpers.

---

- [ ] **Unit 2: Cached-complements scorer (re-IDF + production-faithful re-score per grid cell)**

**Goal:** A pure function that, given a commander and a candidate weight vector, produces a production-faithful top-30 ranking and per-rule contributions. Caches `complements` once per commander to avoid the expensive `find_all_complements` traversal on every grid cell, then re-IDFs and re-scores cheaply for each candidate weight vector.

**Requirements:** R2 (inner loop), R3 (objective ingredients).

**Dependencies:** Unit 1 (objective + split helpers).

**Files:**
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (helpers in same file as the driver)
- Test: `tests/bench/test_optimize.py`

**Approach:**

The `score_all_universal` function does not accept a candidate filter, and adding one would change IDF semantics. Instead, follow the `weight_grid_search.py` pattern at higher fidelity:

- **Per commander, once:** call `find_all_complements(conn, [commander])` to build the full `list[PortComplement]` for that commander. Cache it in a `dict[str, list[PortComplement]]` keyed by commander. Also cache `EDHREC graded labels` and `EDHREC top-30 set` from Unit 1's `load_edhrec_labels`.
- **Per grid cell (called ~265 times per commander):**
  1. Patch `_RULE_QUALITY_MULTIPLIER` via `.clear() + .update(new_weights)` in a `try/finally`.
  2. Re-compute IDF weights from cached `complements` (this respects the new `_RULE_QUALITY_MULTIPLIER` because `_compute_idf_weights` reads it via the global). Implementation reference: `weight_grid_search.py:33-44`. The IDF re-compute is fast (frequency-table lookup over ~10k complements).
  3. Build `UniversalScore(complements, idf_weights)` per candidate and access `.score` (the `cached_property` at `universal_scorer.py:330`) — this includes concentration dampening, multi-rule bonus, pair bonus, anti-synergy, embedding contribution, etc. Production-faithful by construction.
  4. Sort `score_by_candidate` desc; take top-30.
  5. Restore `_RULE_QUALITY_MULTIPLIER` to baseline before returning.

Define `score_commander_with_weights(conn, commander, weights, baseline_weights, *, complements_cache, labels_cache) -> CommanderScoreResult` returning `{top_30: list[str], score_by_candidate: dict[str, float], contributions: list[tuple[str, str, float]]}` where the contribution shape matches what `hidden_gem_hit_rate_for_commander` consumes (extracted from `complements + idf_weights`).

The tensor is NOT used in the inner loop — it is read only at handler entry for the staleness check (Unit 4 / FR4c). The complements cache replaces the tensor's role as a "cheap filter."

**Execution note:** The patch-and-restore pattern is load-bearing for correctness. Add a test that explicitly raises mid-call (mock `_compute_idf_weights` to raise) and asserts dict identity is preserved.

**Patterns to follow:**
- `scripts/weight_grid_search.py:26-75` — the cache-complements + re-IDF pattern (omits dampening; we keep it via `UniversalScore.score`).
- `tests/test_scoring_weights.py:309-324` (canonical `.clear() + .update()` pattern) and `tests/bench/test_tensor_write.py:81` (`patch.dict(..., clear=True)` precedent) — patch-and-restore on `_RULE_QUALITY_MULTIPLIER`.
- `src/mtg_synergy_graph/universal_scorer.py:_compute_idf_weights` (line 582), `UniversalScore.score` (line 330) — reuse, do not fork.

**Test scenarios:**
- **Happy path (production-faithful match):** With baseline weights, `score_commander_with_weights` produces a top-30 BITWISE-IDENTICAL to `score_all_universal(conn, [commander])` top-30 on a real-DB fixture commander. Asserts complete fidelity, not a 28/30 approximation.
- **Cache reuse:** Calling the function 5 times for the same commander with different weights triggers `find_all_complements` exactly once (mock the function and assert call count = 1).
- **Patch-and-restore on exception:** Mock `_compute_idf_weights` to raise mid-call; assert `_RULE_QUALITY_MULTIPLIER` dict identity is preserved (same `id()`) AND contents bitwise-restored.
- **Weight-shift sensitivity:** With baseline weights × 2 on `attack_payoffs`, the top-30 differs from baseline (assert at least 1 candidate moves position). With baseline weights × 0 on `attack_payoffs`, top-30 differs in the opposite direction.
- **Edge case:** Commander with <30 candidates in the complements set (low-coverage commander). Function returns the full set sorted, no crash on the slice.
- **Determinism:** Same `weights` + commander produce bitwise-identical output across two calls.

**Verification:**
- `pytest tests/bench/test_optimize.py -k scorer` green.
- `bench.py audit --expect-identity` still passes on baseline weights (Unit 2 doesn't touch the tensor build path).

---

- [ ] **Unit 3: Coordinate Ascent driver + accept/reject gates + cumulative-drift check + planted-perturbation self-test**

**Goal:** The optimizer's main loop. Patches weights, scores both splits via Unit 2, gates accept/reject on train↑ AND held-out non-degrading, applies the weight clamp, watches cumulative drift, and runs the FR10 self-test.

**Requirements:** R2, R4 (per-axis warning emission), R5, R6, R8, R13.

**Dependencies:** Unit 1 (objective + split), Unit 2 (cached-complements scorer).

**Files:**
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (add the driver to the same file as Unit 1/2's helpers)
- Test: `tests/bench/test_optimize.py`

**Approach:**
- Define `OptimizerConfig` frozen dataclass: `alpha=0.5, top_k=200, grid=(0.5, 0.75, 1.25, 1.5, 2.0), clamp=(0.01, 5.0), max_sweeps=5, wall_clock_seconds=300, eps_step=0.005, eps_cumulative=0.005, train_ratio=0.8, split_seed=...`.
- Define `run_optimizer(conn, weights, config) -> OptimizerResult` returning `{per_rule_diffs, train_obj_baseline, held_obj_baseline, train_obj_final, held_obj_final, ...sweep counts, partial_sweep, dead_keys, history_rows}`.
- Inner loop: alphabetical sweep over `_RULE_QUALITY_MULTIPLIER` keys. For each key, evaluate the 5 grid perturbations on the train split via Unit 2 + Unit 1, pick the maximizing perturbation, then re-evaluate on held-out. Accept if both gates pass.
- Per-axis logging (FR3): each step logs `train_composite, held_composite, train_ndcg, held_ndcg, train_gem, held_gem, accepted, reject_reason`. Per-axis regression > 0.005 emits stderr warning even if composite passes.
- Cumulative drift (FR4): at sweep boundary, compute `held_composite_now - held_composite_baseline`. If < -0.005, revert all accepted steps from this sweep and terminate with `partial_sweep=true`.
- Termination (FR5): convergence (zero accepted), 5 sweeps, or 5-min wall-clock. Always finish the in-progress sweep before declaring convergence.
- **Self-test (FR10):** before the main run, pick a random rule via `random.Random(self_test_seed).choice(...)` (independent seed from split). Patch its weight to `2.0×` baseline. Run a single sweep over only that rule. Assert the optimizer recovers a value `≤ 1.05×` baseline (the grid step `0.5×` from `2.0×` lands exactly on baseline; ±5% tolerance accounts for floating-point noise but rejects "couldn't recover at all" failures). If not, raise `OptimizerSelfTestFailed` with a diagnostic naming the rule and which gate (train, held-out, or convergence) failed. Skipped when `--no-self-test` is set.
- **Dead-key detection:** while iterating `_RULE_QUALITY_MULTIPLIER`, if a key's `rule_id` is not in the current registry (Python-helpers ∪ `DECLARATIVE_RULE_IDS`), log a stderr warning, record it in `dead_keys`, and skip without consuming a sweep slot.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/handlers.py` — `try/finally` pattern for resource cleanup; `_emit_rendered` for stderr output (use directly for the per-step summary table).
- `tests/bench/test_audit_integration.py:34-103` — real-DB integration test fixture with a small synthetic golden set (5 commanders) + synthetic EDHREC DB for the labels.

**Test scenarios:**
- **Happy path (self-test recovers):** With baseline weights and a healthy fixture, the planted perturbation (2.0× on a random rule) is recovered to baseline within ±5%. Self-test exits cleanly.
- **Self-test fails when gates too strict (FR10 invariant):** Set `eps_step=0.0001` (artificially tight). Self-test fails with a diagnostic naming the rule and `reject_reason='held_out_eps'`. Asserts the diagnostic message is informative, not just "test failed."
- **Accept/reject gate (happy path):** Baseline weights + a synthetic commander where a rule's contribution dominates a known-EDHREC-top-30 candidate. A 1.5× weight on that rule produces train↑ and held-out non-degrading; accepted.
- **Accept/reject gate (rejection):** A 0.5× weight on a high-leverage rule produces train↑ on a narrow subset but held-out degrades by 0.01 > ε=0.005; rejected; `_RULE_QUALITY_MULTIPLIER` is bitwise-restored (dict identity preserved).
- **Cumulative drift trigger:** Construct a sequence of 10 single-step accepts that each pass `eps_step` but cumulatively drop held-out by -0.006. At sweep boundary, all 10 accepts are reverted and `partial_sweep=true`.
- **Termination — convergence:** With baseline weights on a tightly-converged synthetic fixture (zero meaningful gradient), first sweep completes with 0 accepts; optimizer terminates.
- **Termination — sweep cap:** With `max_sweeps=2` and a fixture that always finds an accept, the optimizer stops after sweep 2.
- **Termination — wall-clock:** With `wall_clock_seconds=1` and a slow fixture, the optimizer aborts mid-sweep with `partial_sweep=true` and emits best-so-far.
- **Weight clamp:** Starting weight 4.5 with grid `2.0×` would propose 9.0; clamped to 5.0. Starting weight 0.05 with grid `0.5×` would propose 0.025; clamped to 0.01.
- **Dead key detection:** Add a stale entry `"vanished_rule": {"value": 1.5, "comment": "..."}` to the test fixture's `_RULE_QUALITY_MULTIPLIER` mock. Optimizer emits one stderr warning, records it in `dead_keys`, skips it.
- **Per-axis regression warning:** A step that gains composite by raising nDCG and dropping gem by 0.006 emits a stderr warning naming gem regression but is still accepted.
- **Determinism:** Same fixture + same `OptimizerConfig` produce bitwise-identical proposals across runs.

**Verification:**
- `pytest tests/bench/test_optimize_self_test.py` green.
- `pytest tests/bench/test_optimize_handler.py` green for driver-level cases.

---

- [ ] **Unit 4: CLI handler + tensor-staleness precondition + flag plumbing**

**Goal:** Wire `bench.py audit --optimize` into the existing CLI dispatcher; refuse on tensor-staleness mismatch (FR4c); emit the no-improvement-found notice when applicable (FR9).

**Requirements:** R7, R11, R12.

**Dependencies:** Unit 3 (driver).

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cli.py` (add `--optimize` flag to mutually-exclusive group, register `"optimize"` in `_HANDLERS`, add `--max-sweeps`, `--seed`, `--no-self-test` parsers, extend `_resolve_mode` to return `"optimize"` when applicable)
- Modify: `src/mtg_synergy_graph/bench/__init__.py` (add `_cli.register("optimize", handle_optimize)` line)
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (export `handle_optimize`)
- Test: `tests/bench/test_optimize_handler.py` (NEW)

**Approach:**
- Add `--optimize`, `--max-sweeps INT`, `--seed INT`, `--no-self-test` to the existing `mode = audit.add_mutually_exclusive_group()` at `cli.py:120` (for `--optimize`); the others are companion args attached to the audit subcommand.
- `handle_optimize(args)`:
  1. Open DB via `open_db(args.db)` — established `bench/handlers.py` pattern.
  2. Tensor-staleness check (FR4c): compute `compute_config_hash(get_scoring_config_inputs())`; query the latest row from `rule_contributions_config`; if mismatch, `print(f"error: tensor stale ({tensor_hash[:12]}... vs config {config_hash[:12]}...). Run `bench.py audit --repin --yes` first.", file=sys.stderr); return 2`.
  3. Build `OptimizerConfig` from args + brainstorm defaults.
  4. Call `run_optimizer(conn, current_weights, config)`.
  5. Emit artifacts via Unit 6.
  6. If `result.n_steps_accepted == 0`, print `"no improvement found"` (FR9); still emit the proposal JSON.
  7. Return 0 on success, 2 on usage errors (no fixture, stale tensor), 1 on self-test failure.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/cli.py:51-73` — `_HANDLERS` table.
- `src/mtg_synergy_graph/bench/__init__.py:46-65` — registration site.
- `src/mtg_synergy_graph/bench/handlers.py:46-100` — handler return-code conventions (0 success / 1 dry-run-or-soft-fail / 2 usage error).
- `src/mtg_synergy_graph/bench/embedding_dedup_handler.py` — closest sibling pattern for a standalone module + handler export.

**Test scenarios:**
- **Happy path (smoke):** `argparse.Namespace(optimize=True, max_sweeps=1, seed=42, no_self_test=True, db=tmp_db, ...)` invokes `handle_optimize`, returns 0, writes `.audit/optimize_proposal.json`.
- **Tensor staleness refusal:** Pin a tensor at one config_hash, then mutate `_RULE_QUALITY_MULTIPLIER` (without re-pin), invoke `handle_optimize`. Returns 2; stderr matches the staleness diagnostic.
- **No-improvement-found (FR9):** Force `run_optimizer` to return `n_steps_accepted=0` (mock or use a converged fixture). Handler still writes `.audit/optimize_proposal.json` with `n_steps_accepted: 0`; stderr shows `"no improvement found"`.
- **Self-test failure (FR10 wiring):** Pass a config that triggers self-test failure (artificially tight `eps_step`). Handler returns 1; stderr shows the FR10 diagnostic.
- **Mutual exclusion:** `--optimize` together with `--repin` raises an `argparse` error and exits with code 2 (the CLI's standard usage-error path).
- **`--no-self-test` propagates:** With the flag, the handler skips the self-test phase even on a config that would otherwise fail it.

**Verification:**
- `pytest tests/bench/test_optimize_handler.py` green.
- `bench.py audit --optimize --max-sweeps 1 --no-self-test` runs end-to-end on the real fixture without crashing; produces `.audit/optimize_proposal.json`.

---

- [ ] **Unit 5: Output artifacts (`optimize_proposal.json` + `optimize_history.csv`)**

**Goal:** Emit the human-review proposal artifact and append the convergence-log CSV row(s) for every attempted step.

**Requirements:** R9, R10.

**Dependencies:** Unit 3 (driver produces the data).

**Files:**
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (add `write_proposal_json` and `append_history_rows` helpers + frozen `OptimizeProposal` and `OptimizeHistoryRow` dataclasses)
- Test: extend `tests/bench/test_optimize_handler.py` (proposal schema + CSV schema)

**Approach:**
- `write_proposal_json(result, path)` writes `.audit/optimize_proposal.json` with fields per FR6: `baseline_config_hash, proposed_config_hash, per_rule_diffs, aggregate_train_composite_delta, aggregate_held_composite_delta, train_ndcg, held_ndcg, gem_rate_train, gem_rate_held, n_iterations, n_steps_accepted, n_steps_rejected, partial_sweep, dead_keys`.
- `per_rule_diffs` only includes rules whose value actually changed. Each diff entry: `{rule_id, old_value, new_value, composite_delta_train, composite_delta_held, ndcg_delta_train, ndcg_delta_held, gem_delta_train, gem_delta_held, accepted_iteration}`.
- `proposed_config_hash` is computed by patching `_RULE_QUALITY_MULTIPLIER` to the proposed weights, calling `compute_config_hash(get_scoring_config_inputs())`, then restoring. Resolves origin OQ #3 without a new variant.
- `append_history_rows(rows, path)` mirrors `bench/history.py::append_run`: `mkdir parents=True, exist_ok=True`, open `"a" newline=""`, header on `tell() == 0`, write rows, wrap in try/except OSError/csv.Error with stderr-warn-and-continue degradation.
- CSV columns (FR7 finalized order): `timestamp, run_id, sweep_n, rule_id, old_value, new_value, train_composite, held_composite, train_ndcg, held_ndcg, train_gem, held_gem, accepted, reject_reason`.
- Stderr summary table prints sorted by `|composite_delta_train|` descending.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/history.py` — `CSV_FIELDS` tuple, `HistoryRow` frozen dataclass, `append_run` writer with `tell() == 0` empty-detection, `fmt_float` for None-safe float rendering, the `try/except OSError, csv.Error → stderr.warn` pattern.

**Test scenarios:**
- **Happy path (proposal JSON schema):** Run optimizer end-to-end on a 5-cmdr synthetic fixture; assert `.audit/optimize_proposal.json` parses as JSON and has all 14 top-level fields. Round-trip via `json.loads(path.read_text())` and assert equality of expected keys.
- **`per_rule_diffs` includes only changed rules:** A run that accepts steps for 3 of 53 keys produces a `per_rule_diffs` array of length 3 (not 53).
- **`proposed_config_hash` flips:** When `n_steps_accepted > 0`, `proposed_config_hash != baseline_config_hash`. When `n_steps_accepted == 0`, they are equal.
- **CSV append (header on first run, no header on subsequent):** First run on a fresh `optimize_history.csv` writes the header + N rows. Second run appends rows without re-writing the header (asserted via row count and header-line uniqueness).
- **CSV malformed-row tolerance:** A pre-existing file with one corrupted row (header valid, body row missing a column) does not crash the appender; the malformed row is left in place; new rows append cleanly. (Mirrors `bench/history.py::read_last` warning-and-continue behavior; verify by reading the file back via standard csv).
- **Comment field preservation:** The proposal `per_rule_diffs` carries only `(rule_id, value)` pairs — does NOT mention `comment`. Test asserts `"comment"` is not a key in any diff entry.
- **Edge case — `dead_keys` non-empty:** Inject a stale `_RULE_QUALITY_MULTIPLIER` entry; assert the proposal's `dead_keys` field is non-empty and names the offending rule_id.
- **Float rendering:** `fmt_float` is reused for None-safe rendering; rows with `held_dcg = None` (e.g., commanders without EDHREC labels in the held-out) write `""` for that column, not `"None"` or crash.

**Verification:**
- `pytest tests/bench/test_optimize_handler.py::test_proposal_schema` green.
- `pytest tests/bench/test_optimize_handler.py::test_history_csv_*` green.
- After a real `bench.py audit --optimize --max-sweeps 1 --no-self-test` run, `.audit/optimize_proposal.json` opens cleanly in a JSON viewer and `.audit/optimize_history.csv` opens cleanly in a spreadsheet.

## System-Wide Impact

- **Interaction graph:** Unit 2 (cached-complements scorer) calls `find_all_complements` once per commander (cached) and `_compute_idf_weights` + `UniversalScore.score` per grid cell, with patched `_RULE_QUALITY_MULTIPLIER` on each cell — must restore on every exit path. Unit 4 (CLI handler) adds a new mode to the `bench.py audit` subcommand; the mutually-exclusive flag group must accept the new mode without breaking existing flags (`--repin`, `--inspect`, `--collinearity`, etc.). The persisted tensor's build path is **NOT modified** — Unit 1 (FR0) was dropped after code-read confirmed declarative rules already attribute correctly.
- **Error propagation:** Unit 4 returns 2 on tensor-staleness mismatch (treat as usage error, like missing fixture in `handle_repin`). Unit 3's self-test failure surfaces as exit code 3 (treat as calibration soft-fail). Driver-internal exceptions (DB I/O, JSON write failure for the proposal) propagate up and surface as exit code 1 with stderr trace, never exit 0. The split between codes 1 and 3 lets CI distinguish "investigate the gates" from "investigate the infra."
- **State lifecycle risks:** `_RULE_QUALITY_MULTIPLIER` is a module-global dict. Every patch site uses `try/finally` with `.clear() + .update()` to preserve dict identity on every Python-handled exit path (function return, exception, `KeyboardInterrupt`, `SystemExit`). `try/finally` does NOT run on SIGKILL, `os._exit()`, or native segfaults — those leaks are tolerated because (a) `bench.py audit` is a single-process CLI and process death takes the patched dict with it, (b) no long-lived REPL/Jupyter usage is supported. A leaked patch across test runs would corrupt the entire test suite — assert dict identity post-call in every patch-site test, and add one explicit test that raises `KeyboardInterrupt` mid-`hybrid_score_commander` and asserts the dict is restored.
- **API surface parity:** `bench.py audit --optimize` adds a new exit code semantic (2 = stale tensor) that fits the existing convention (`--repin` already returns 2 for "no fixture"). No other CLI surface changes.
- **Integration coverage:** Unit 4's end-to-end test (driver → Unit 3 → Unit 2 → patches `_RULE_QUALITY_MULTIPLIER` → calls real `score_all_universal` → reads real EDHREC labels → writes real `.audit/optimize_proposal.json`) is the integration scenario that mocks won't cover. Run it on a 5-cmdr synthetic fixture with `pytest -m integration`.
- **Unchanged invariants:** `bench.py audit` (no flags) behavior is bit-identical to today. `bench.py audit --repin --yes` is bit-identical to today (Unit 1 / FR0 was dropped — no tensor-build-path changes in this plan). The `--expect-identity` mode must continue to pass on every PR commit. `data/scoring_weights.json` is never auto-mutated by this plan.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Cached `complements` go stale during a single optimizer run if anything outside `_RULE_QUALITY_MULTIPLIER` mutates between cache build and re-IDF (it shouldn't — `find_all_complements` is a pure function over `(conn, commander_set)` and the DB doesn't mutate during a run). | Snapshot the cache at handler entry; assert via test that mutating an unrelated module-global between cache build and re-score doesn't affect output. Single-threaded `bench.py` makes the invariant easy to hold. |
| `score_commander_with_weights` produces top-30 that diverges from `score_all_universal()` direct top-30 on some commander (cache-vs-fresh disagreement). | Unit 2's BITWISE-IDENTICAL fidelity test: top-30 from the cached path must equal top-30 from the live path on baseline weights. If it doesn't, the cache is wrong, not the test. |
| `_RULE_QUALITY_MULTIPLIER` patch-and-restore leaks across test runs, corrupting the suite. | Every patch site uses `try/finally` and post-call dict-identity assertions. Run the full test suite locally before each PR — leaked patches manifest as cascading test failures. |
| Composite objective with `α=0.5` produces strictly weaker NDCG-vs-EDHREC verdict than baseline (gem-axis dominates the trade). | Per-axis logging in FR3/FR6 surfaces the trade. If the first proposal regresses NDCG audit verdict despite improving composite, post-merge the team runs the deferred α-calibration sweep before re-running optimizer on production weights. |
| Wall-clock budget <3 min misses on a real 100-cmdr fixture. | With cached complements, the inner-loop cost per grid cell is ~O(complements_per_commander × log(idf_keys)) — much cheaper than `score_all_universal`'s full traversal. Budget is plausible but unproven; implementation may explore `multiprocessing.Pool` per commander as a deferred follow-up if the first benchmark misses. |
| Self-test (FR10) flakily fails on a borderline rule whose 2.0× shift pushes a single commander's top-30 across a tight boundary. | Self-test seed is independent of split seed; run multiple times if a single-instance flake is suspected. If genuinely unrecoverable on a specific rule, that rule has a too-tight gate boundary and the diagnostic surfaces it correctly — not a test bug. |
| `proposed_config_hash` computation requires patch-and-restore; an exception during the hash compute could leak the patch. | The patch+hash+restore happens in a single `try/finally` inside `write_proposal_json`. The patch is at most ~milliseconds; concurrency risk is zero (single-threaded `bench.py`). |

## Documentation / Operational Notes

- **CLAUDE.md Common Commands section** should add a single line documenting `bench.py audit --optimize` after the existing `--repin` line. Update post-merge.
- **`docs/RULE_PLANNING.md`** does NOT need updates — the optimizer is orthogonal to the gap_report → scaffold → audit cycle.
- **`memory/feedback_audit_every_change.md`** — the optimizer's emit step does NOT mutate `data/scoring_weights.json`, so it does not require an audit gate. Application of the proposal (human-driven) goes through the existing `--repin --yes` discipline. No update needed.
- **`.gitignore`** — `.audit/` already covers `.audit/optimize_proposal.json` and `.audit/optimize_history.csv`. No new entry needed.
- **Rollout** — purely additive flag; existing workflows (no-flag `audit`, `--repin`, etc.) are bit-identical. No feature flag, no deployment ceremony.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-26-tensor-weight-optimizer-requirements.md](../brainstorms/2026-04-26-tensor-weight-optimizer-requirements.md)
- **Ideation document:** [docs/ideation/2026-04-26-applying-built-tooling-ideation.md](../ideation/2026-04-26-applying-built-tooling-ideation.md) (Survivor #1)
- **Prior weight-tuning attempt:** `scripts/weight_grid_search.py` (deprecated; pattern reference)
- **Tensor pre-dampening contract:** `src/mtg_synergy_graph/bench/rule_ops.py:8-18`
- **Concentration dampening:** `src/mtg_synergy_graph/universal_scorer.py:329-379`
- **Score function:** `src/mtg_synergy_graph/universal_scorer.py::score_all_universal:644`
- **Module-global weight dict:** `src/mtg_synergy_graph/universal_scorer.py:_RULE_QUALITY_MULTIPLIER:579`, `_load_scoring_weights:479`
- **Tensor + hash:** `src/mtg_synergy_graph/bench/tensor.py:38-95`
- **CSV append pattern:** `src/mtg_synergy_graph/bench/history.py`
- **Hidden-gem metric:** `src/mtg_synergy_graph/bench/hidden_gems.py:181, 225`
- **nDCG primitive:** `src/mtg_synergy_graph/validate.py::compute_ndcg:45`
- **Declarative-rule predicate compiler:** `src/mtg_synergy_graph/port_graph/interpreter.py::_compile_gate_predicate:205`
- **Test fixture pattern:** `tests/test_scoring_weights.py:309-324 (canonical .clear()+.update() pattern) and tests/bench/test_tensor_write.py:81 (patch.dict(..., clear=True) precedent)`, `tests/bench/test_audit_integration.py:34-103`
- **Memory anchors:** `memory/feedback_edhrec_not_goal.md`, `memory/feedback_audit_every_change.md`, `memory/feedback_audit_metric_too_coarse.md`, `memory/feedback_hidden_gem_metric.md`
- **Institutional learnings:** `docs/solutions/best-practices/extract-python-dict-to-json-sidecar-2026-04-25.md`, `docs/solutions/best-practices/rule-quality-gates-2026-04-24.md`, `docs/solutions/best-practices/sweep-writers-not-just-readers-on-source-of-truth-refactor-2026-04-25.md`
