# Content embeddings — pre-flag-flip follow-ups

Deferred findings from `/ce-code-review` on branch
`feat/content-embeddings-fallback` (run ID 20260423-213012-17f8d1eb,
plan `docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md`).

Current branch ships `_ENABLE_EMBEDDING_CONTRIBUTION = False`. The
items below are pre-flag-flip work — safe to merge the current branch
without resolving them, but they must be resolved before the audit
sweep that flips the flag. Grouped by urgency.

## Gate-blocking for the flag flip

### FU-1. CLI flags `--min-df` / `--svd-dims` are broken end-to-end

**Reviewer:** correctness CORR-001 (HIGH, 0.90) + CORR-002 (HIGH, 0.88),
adversarial ADV-003 (0.85), maintainability M-003 (0.82).

**Problem.** `load_card_embeddings_verified` calls
`get_embedding_config_inputs()` which returns the CURRENT code
defaults. If `scripts/build_embeddings.py` was run with `--min-df 3`,
the stored `config_hash` reflects `min_df=3` but the inference recompute
uses `min_df=2` → mismatch → silent `{}` with misleading "stale" error.

Separately, `_EMBEDDING_DIM = 128` is a module-level constant in
`store.py`. If `--svd-dims 64` is used at build time, every vector is
silently skipped on load because `len(vector) != _EMBEDDING_DIM`.

**Recommended fix.** Reconstruct `EmbeddingConfigInputs` from the stored
`card_embeddings_config` KV rows before calling the hash comparison.
The scorer's `ScoringConfigInputs.vectorizer_version` then serves as a
separate freshness signal against the tensor. Remove the
`_EMBEDDING_DIM = 128` constant; read the expected dim from the same
KV reconstruction.

**Alternative.** Restrict `--min-df` and `--svd-dims` to defaults-only
(emit error at build time if non-default). Simpler but loses the
planned tuning affordance.

**Files:** `src/mtg_synergy_graph/embeddings/contribution.py:148`,
`src/mtg_synergy_graph/embeddings/store.py:151`,
`src/mtg_synergy_graph/embeddings/config.py`.

## Must-fix before the flag flip

### FU-2. `_render_embedding_block` re-loads all 32k vectors per `--explain` candidate

**Reviewer:** performance PERF-002 (HIGH, 0.88), maintainability M-004 (0.75).

**Problem.** `_render_embedding_block` calls
`load_card_embeddings_verified(self._conn)` and
`build_commander_target_vector(...)` per candidate. A single
`recommend.py --explain` page with 30 rule-covered top-30 candidates
triggers up to 30 full table scans + 30 full blob deserializations
(~480MB of transient Python work).

**Recommended fix.** Cache on the `SynergyEngine` instance (`self._emb_vectors`, `self._emb_cmdr_target`) populated on first call in
`_render_embedding_block`. Clear the cache in `SynergyEngine._reset_score_cache` (if a similar method exists) to tie invalidation to existing cache lifecycle.

**Files:** `src/mtg_synergy_graph/engine.py:542–602`.

### FU-3. `functools.cache` on `sqlite3.Connection` identity — undocumented + unbounded

**Reviewer:** maintainability M-005 (0.80), reliability R-005 (0.75).

**Problem.** `_fetch_hi_syn_names_cached` uses `@functools.cache` with
`sqlite3.Connection` as part of the cache key, relying on CPython's
undocumented object-identity hashability for connections. In a
long-running process, dead connection references accumulate — no
production `clear_cache()` call exists.

**Recommended fix.** Switch to a manual dict keyed by `(id(conn),
commander_name, hi_syn_limit)`, with explicit size bound (e.g.,
`functools.lru_cache(maxsize=512)` applied to a wrapper that extracts
`id(conn)` first).

**Files:** `src/mtg_synergy_graph/embeddings/commander_target.py:54`.

### FU-4. NaN propagation breaks flag-off identity guarantee

**Reviewer:** adversarial ADV-002 (0.92).

**Problem.** `0.0 * decay * NaN = NaN` in IEEE 754. If any vector
produces a NaN during cosine, the embedding term becomes NaN, the
score becomes NaN, Python sort is undefined. L2-normalized inputs
are unlikely to produce NaN but the short-circuit contract should be
tight.

**Recommended fix.** In `embedding_contribution`, add an explicit NaN
guard on the cosine value before returning — or use `math.fsum` /
clamp logic.

**Files:** `src/mtg_synergy_graph/embeddings/contribution.py:128`.

## Build-time / operational

### FU-5. TF-IDF matrix memory footprint (~1.3GB before SVD; peak ~4-5GB)

**Reviewer:** performance PERF-001 (0.90).

**Problem.** Dense `(32k, ~5k)` float64 matrix built before
`numpy.linalg.svd`. Peak allocation during LAPACK dense SVD is 3-4×
that — 4-5 GB. Risks OOM on 8GB dev machines and may exceed the
60s plan budget.

**Mitigations (any one sufficient):**
1. Cap vocabulary via `max_features` (e.g., top 3000 by IDF). Simpler.
2. Add scipy as optional `[embeddings]` extra and use
   `scipy.sparse.linalg.svds` on sparse TF-IDF. Lower RAM, slightly
   slower on small corpora.
3. Document the 16+GB machine requirement in CLAUDE.md and accept the
   cost.

**Measurement needed.** Actual vocabulary size after `min_df=2` pruning
on the real 32k corpus — could be lower than 5k, lowering the ceiling.

**Files:** `src/mtg_synergy_graph/embeddings/svd.py:61`,
`src/mtg_synergy_graph/embeddings/vectorizer.py:213–220`.

## Discretionary (P3, advisory)

Bundled for awareness; address case-by-case during the audit sweep or
subsequent maintenance:

- **CLI UX polish.** `build_embeddings.py --format json`, `--check`,
  help naming output tables; `.audit/last_dedup.md` side-effect
  documented + honor `--output -` for stdin consumers
  (cli-readiness CR-001/002/004/005/006).
- **Typing consistency.** `write_vectors` → `Mapping[str, np.ndarray]`;
  reconcile `TfidfResult` NamedTuple-with-ndarray `__eq__`
  (kieran-python KP-001, KP-007).
- **Hash stability.** `compute_embedding_hash` → `json.dumps(sort_keys=True)` instead of `repr(sorted(items))`
  (kieran-python KP-003).
- **Public API surface.** Add `EmbeddingConfigInputs` / `DedupPair`
  to root `__init__.__all__` or mark internal
  (api-contract AC-004).
- **Diagnostic perf.** `_nearest_covered_neighbors` Python loop → BLAS
  matmul; `dedup.fetchall()` → streaming group-by;
  `build_embeddings.py` TF matrix `np.add.at` instead of double-loop
  (performance PERF-003/004/005).
- **FK error message.** `build_embeddings.py` should validate
  `cards(name)` membership before bulk insert, or wrap the FK error
  with a user-actionable hint (api-contract AC-003,
  data-migrations DM-002).
- **Test brittleness.** `tests/embeddings/test_commander_target.py`
  asserts against `cache_info()` private attr (testing T-001).
- **Dedup filter.** `min_activation` uses `len(cands)` but mean vector
  uses `len(found)` subset — potential false positives with sparse
  embedding coverage (correctness CORR-004).
- **Empty-vocabulary cascade.** 32k all-zero vectors + 32k warnings
  when `min_df` prunes everything — could log once + summary count
  (adversarial ADV-004).
- **Docstring fix.** `read_vector` claims `--explain` use; unused in
  production (maintainability M-006).
- **Dedup OOM at scale.** Current fleet safe; revisit when rule count
  crosses ~300 (adversarial ADV-007).

## References

- **Review run artifact:** `.context/compound-engineering/ce-code-review/20260423-213012-17f8d1eb/`
- **Plan:** `docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md`
- **Re-pin commit:** `b887cf4` (hash-only change, Δ=+0.0000 across 100
  commanders)
- **First fix batch (safe_auto):** `ab8b2a3`
