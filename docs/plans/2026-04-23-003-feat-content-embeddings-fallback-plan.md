---
title: "feat: Content embeddings as zero-shot fallback"
type: feat
status: landed
date: 2026-04-23
origin: docs/brainstorms/2026-04-21-content-embeddings-requirements.md
---

# Content embeddings as zero-shot fallback

## Overview

Add a deterministic 128-dim content-embedding substrate per card (hand-rolled
TF-IDF + truncated-SVD over structured port/keyword features) and wire an
audit-gated `embedding_contribution` term into `universal_scorer` that is
exponentially dampened by the number of rules already firing on the
`(commander, candidate)` pair. The intended effect: lift structurally-similar
candidates for heterogeneous-hi-syn commanders (Animar, Kess, Yuriko) and
day-0 Forge-imported cards without writing per-sub-cluster rules (which would
violate `memory/feedback_no_individual_rules.md`). Also adds a new
`bench.py audit --embedding-dedup` diagnostic that flags rule pairs whose
candidate-activation sets are near-parallel in embedding space.

Ships behind `_ENABLE_EMBEDDING_CONTRIBUTION = False`. Only the final unit
flips scores; all infrastructure units land `--expect-identity` clean.

## Problem Frame

Two structurally identical gaps in the current rule-based scorer
(see origin: `docs/brainstorms/2026-04-21-content-embeddings-requirements.md`):

1. **Heterogeneous hi-syn commanders.** Animar's hi-syn spans counter doublers,
   SpellCast-Draw creatures, ETB-Draw creatures, self-bouncers, mana dorks,
   X-cost creatures — no single mechanical rule unifies them. Writing one rule
   per sub-cluster would violate the "general not individual" memory
   constraint.
2. **Day-0 unreleased cards.** When a Forge cardsfolder refresh lands a new set,
   its cards have structural features (port tuples) but no hand-authored rule
   mentions their novel mechanics yet. The scorer silently under-scores them
   until humans catch up.

Both gaps share the same shape: a candidate whose port profile is
*structurally similar* to things the commander wants, without matching any
specific hand-written rule. A deterministic embedding over structured
features captures that similarity without training, without popularity data,
and without oracle-text n-grams.

This is the **structural** answer to the "no individual rules" constraint:
instead of writing more rules, let the embedding cover the long tail while
the rule-coverage decay keeps embeddings near-invisible on
well-rule-covered candidates.

## Requirements Trace

- **R1.** One 128-dim deterministic vector per card in the DB, derived entirely
  from structured features: `port_attributes` rows, port-row categorical fields
  (`port_type`, `event_class`, `zone_origin`, `zone_destination`, `counter_type`,
  `branch_kind`), and Scryfall `keywords`. No oracle-text, no numeric features,
  no popularity. (FR1)
- **R2.** Per-commander target vector = mean of commander's own port-feature
  vector and the vectors of its EDHREC golden-set hi-syn cards, if any. For
  commanders not in the golden set: target = commander's own port-feature
  vector. (FR2)
- **R3.** New additive scoring term per candidate:
  `embedding_contribution = w_emb · exp(-k · N_rules) · cosine(v_candidate, v_cmdr_target)`,
  where `N_rules` counts rules firing a positive contribution on this
  `(commander, candidate)` pair. Smooth exponential decay, no discontinuity. (FR3)
- **R4.** Cold-start works by construction: every card has a vector after
  `scripts/build_embeddings.py` runs against a refreshed DB. No special
  cold-start code path. (FR4)
- **R5.** `bench.py audit --embedding-dedup` flags rule pairs whose
  candidate-activation sets exceed mean-pairwise-cosine threshold (default
  0.95) in embedding space. Read-only diagnostic, no scoring impact. (FR5)
- **R6.** `recommend.py --explain` renders an `embedding_contribution` line
  plus the three nearest rule-covered neighbors when the contribution is
  nonzero. (FR6)
- **R7.** Ships behind `_ENABLE_EMBEDDING_CONTRIBUTION = False`. Audit-gated
  flip via weight sweep over `w_emb ∈ {0.1, 0.3, 0.5, 1.0}`,
  `k ∈ {0.5, 0.8, 1.2}`. No commander regresses beyond MARGINAL. Flag and
  weight constants plumbed into `ScoringConfigInputs` and
  `compute_config_hash` so tuning re-invalidates the pinned tensor. (FR7)
- **R8.** Determinism: same DB + same vectorizer version + same config →
  bitwise-identical vectors → bitwise-identical scores.
- **R9.** Inference latency ≤ 5% regression: per-candidate cost is one
  precomputed vector lookup + one dot product.

## Scope Boundaries

- No oracle-text features (n-grams or otherwise) anywhere in the pipeline.
- No neural / transformer / learned-weight embeddings.
- No popularity-adjacent features (EDHREC rank, deck percentages, reprint
  frequency).
- No k-NN queries at inference — only precomputed-vector dot products.
- No auto-propose from `--embedding-dedup` into the `rules` table — humans
  read the diagnostic report and decide. Matches the audit-every-change
  guardrail.
- No replacement of the rule-based scorer — embeddings are strictly additive
  fallback, not a new scoring backbone.
- No online re-training or continuous learning — vectors rebuild on DB
  re-import, period.

### Deferred to Separate Tasks

- **Morgan/ECFP circular fingerprints over typed port-nodes.** Brainstorm
  naturally-successor work; revisit once plan 003 typed-port-graph is
  generalized beyond the current 16 peer-tribal rules. Separate future
  brainstorm.
- **Dimensionality tuning (128 → N).** First cut is fixed at 128. Any sweep
  of SVD rank is a follow-up audit, not part of this plan.
- **Adding scipy to dependencies.** Only if `numpy.linalg.svd` profiling
  shows a real problem on the 32k × ~5k corpus. Default stance: hand-roll on
  numpy only (matches the existing zero-extra-dep posture).

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/complement_rules/pathway.py` — canonical
  `_ENABLE_*` flag pattern. Flag constant at module top; plumbed into
  `ScoringConfigInputs`; infra units land identity-clean; only the flip unit
  changes scores. This plan mirrors it exactly.
- `src/mtg_synergy_graph/universal_scorer.py` — `UniversalScore` dataclass
  (lines 270–286) is the injection site for an `embedding_contribution`
  field; `score_all_universal` results loop (lines 938–950) is where the
  term gets populated per candidate. `distinct_rules` `@cached_property`
  already gives us `N_rules` for free.
- `src/mtg_synergy_graph/engine.py` — `SynergyEngine._render_explanation`
  (lines 445–484) is where the new `embedding_contribution:` + nearest-neighbor
  explain lines go. `_score_cache` keyed by `tuple(cmdr_set)` is the existing
  commander-level cache.
- `src/mtg_synergy_graph/bench/collinearity.py` — direct numpy-based pairwise
  diagnostic template. `handle_collinearity` at `bench/handlers.py:408–424`
  is the handler-shape template for the new `--embedding-dedup`.
- `src/mtg_synergy_graph/bench/cli.py` — mutex-group mode registration
  pattern (lines 115–173); adding `--embedding-dedup` is one
  `mode.add_argument` + one `_HANDLERS` row + one `_resolve_mode` branch.
- `src/mtg_synergy_graph/bench/tensor.py` — `compute_config_hash` (lines
  38–77). Must grow three new inputs (`enable_embedding_contribution`,
  `embedding_w`, `embedding_k`) plus the `vectorizer_version` read from the
  `card_embeddings_config` KV table.
- `src/mtg_synergy_graph/forge_oracle/config.py` — `OracleConfigInputs`
  NamedTuple + `compute_oracle_hash` + `verify_current_or_raise`. Template
  for `EmbeddingConfigInputs` and the `card_embeddings_config` KV table.
- `src/mtg_synergy_graph/forge_oracle/gap_weight.py` — the graceful-fallback
  reader contract (`{}` on missing file / DB error / missing table / empty
  table / all-zero values, each with `logging.warning`). Copy verbatim for
  `load_card_embeddings`.
- `src/mtg_synergy_graph/attributes.py` — `explode_filter` + `classify_attr_token`.
  These are the canonical per-token expansion; `port_attributes` rows are
  already pre-tokenized, so FR1's tokenization question reduces to iterating
  existing rows.
- `scripts/build_graph_cache.py` — precedent for a standalone
  `scripts/build_*.py` user-invoked build step. `scripts/build_embeddings.py`
  mirrors this.
- `src/mtg_synergy_graph/schema.sql` — `graph_cache` table at lines 153–159
  is the blob-in-sqlite + versioned precedent. `rule_contributions` at
  lines 204–220 is the config-hash-keyed table precedent.
- `tests/conftest.py` (lines 46–64) — autouse `_stub_forge_sha_when_checkout_absent`
  fixture is the template for a CI stub when rebuilt-artifact tests would
  fail without dev-only setup.

### Institutional Learnings

- **`docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md`**
  — governing discipline for this plan. Infra identity-clean; `ScoringConfigInputs`
  field added in the same unit as the flip; audit-iterate with
  `hidden_gem_hit_rate` + aggregate delta; never chase aggregate gains at
  the cost of hidden-gem rate.
- **`docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md`**
  — partial application. The doc explicitly says "do not apply to inference-path
  subsystems," and embeddings are inference-path consumed. But the embedding
  **build** is offline-derived with configurable knobs (`svd_dims`,
  `vectorizer_version`, `tfidf_smoothing`, `min_df`) that can drift silently.
  **Hybrid resolution:** put the knobs in `ScoringConfigInputs` (so the pinned
  tensor invalidates) AND write them into a `card_embeddings_config` KV
  table (so a stale on-disk table is detected at inference startup).
  Both guards fire in different failure modes; keep both.
- **`docs/solutions/test-failures/forge-oracle-ci-git-checkout-stub-2026-04-23.md`**
  — rebuild-required tests need an autouse conftest stub established in the
  same commit as the first test, not retroactively after CI fails.
- **`docs/solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md`**
  — if any embedding metadata file is committed (e.g., a pinned
  `vectorizer_version` marker), use `!data/...` negation form; add a guard
  in `tests/test_seed_files_tracked.py`.
- **CLAUDE.md note on reverted `card_hints`-family rules** (`deck_hint_match`,
  `deck_needs_fulfilled`, `buffed_by_match`) — a critical warning. Those
  rules regressed NDCG@30 because curated-similarity signal diluted the
  mechanical port signal. Structural-similarity signals have the same
  failure mode. Mitigations: start with the smallest `w_emb`; lean on the
  exponential decay aggressively; measure `hidden_gem_hit_rate` alongside
  aggregate delta at every audit step; if `hidden_gem_hit_rate` drops, the
  flag stays `False` even if aggregate looks fine.

### External References

None needed. TF-IDF + truncated SVD is well-established IR math; the
brainstorm is prescriptive about the algorithm; the codebase patterns for
flag-gated scoring-path integration + numpy-only numerics + bench-handler
registration are all strong.

## Key Technical Decisions

- **D1. Hand-roll TF-IDF + `numpy.linalg.svd` on a dense matrix.** Zero-dep
  policy already in place; numpy is the only runtime numeric dep; ~32k ×
  ~5k fits comfortably in memory and SVD runs in seconds. Adding sklearn
  would be a major dep escalation. If profiling ever shows SVD is the
  bottleneck, add scipy as an optional `[embeddings]` extra — not today.
- **D2. Embedding table lives in the main `data/synergy.db`, not a sidecar.**
  Vectors are read at inference per candidate per commander; sidecar
  isolation is only right for offline-only data (forge_oracle pattern). New
  `card_embeddings` table mirrors the existing `graph_cache` blob-in-sqlite
  pattern.
- **D3. Hybrid hash strategy.** Add `enable_embedding_contribution`,
  `embedding_w`, `embedding_k`, `vectorizer_version` to `ScoringConfigInputs`
  AND write an `EmbeddingConfigInputs` NamedTuple into a
  `card_embeddings_config` KV table. The first protects the pinned audit
  tensor; the second detects stale on-disk vectors at startup. Neither
  subsumes the other.
- **D4. Separate `scripts/build_embeddings.py` user-invoked build step.**
  Matches `scripts/build_graph_cache.py` precedent. Importer stays lean;
  embedding rebuild is opt-in (typically after each cardsfolder refresh).
- **D5. Lazy commander-target vectors with module-level `functools.cache`.**
  Keyed by commander oracle_id. 100 golden-set commanders warm up during
  bench/audit runs; user queries pay a microsecond read on first hit.
  Simpler than a second persisted table.
- **D6. Graceful-fallback reader for the inference path.** Missing table /
  `sqlite3.DatabaseError` / missing columns / empty table / all-zero blobs
  → return `{}` + `logging.warning`. Never raise from the scorer. Copies
  `forge_oracle/gap_weight.py:load_forge_signals` contract.
- **D7. `--embedding-dedup` exits 2 on missing tensor or missing embeddings.**
  Matches the offline-oracle three-tier strictness split: diagnostics whose
  entire purpose is the data must fail loudly with a rebuild hint, not
  silently degrade.
- **D8. `port_attributes` rows are already pre-tokenized.** FR1's "exact
  feature tokenization for multi-value columns" open question dissolves:
  `attributes.explode_filter()` has already done the work at importer
  time. Vectorizer iterates existing rows; no re-parsing of raw
  `valid_filter` strings.
- **D9. All three flip knobs plumbed into `ScoringConfigInputs`.** The bool
  alone isn't enough — tuning `w_emb` or `k` changes scores and must
  invalidate the pinned tensor. Follow the `pathway` plan 001 precedent
  for "add the `ScoringConfigInputs` field in the same unit as the flip"
  to avoid re-pin churn during infra units.
- **D10. Pre-commit `bench-audit` regex extended to `embeddings/`.** So any
  change under the new subpackage triggers the advisory audit hook. Matches
  `memory/feedback_audit_every_change.md`.
- **D11. Embedding-dedup surfaces pairs for humans; no auto-merge.** The
  dedup signal is ideation input to rule-consolidation work, not a
  code-rewriting agent. Matches audit-every-change guardrail.
- **D12. Identity contract: flag-off bitwise-identical to current baseline.**
  `tests/bench/test_universal_scorer_identity.py` gains an assertion that
  flag-off scores match the pre-plan baseline. Same contract as plan 001
  Unit 3's `--expect-identity`.

## Open Questions

### Resolved During Planning

- **Feature tokenization for multi-value columns.** Resolved by D8 — reuse
  already-exploded `port_attributes` rows.
- **TfidfVectorizer vs hand-roll.** Resolved by D1 — hand-roll (zero
  extra deps; the math is ~40 lines).
- **SVD implementation.** Resolved by D1 — `numpy.linalg.svd` dense.
  Revisit only if profiling shows a problem.
- **Refresh cadence: in-importer vs separate.** Resolved by D4 — separate
  `scripts/build_embeddings.py`.
- **Commander target vector for non-golden-set commanders.** Resolved per
  FR2 — default to commander's own port-feature vector.
- **Dedup signal's interaction with the `rules` table.** Resolved by D11
  — surface for humans only, no auto-merge.
- **Sidecar vs main DB.** Resolved by D2 — main DB.
- **Hash strategy.** Resolved by D3 — hybrid (`ScoringConfigInputs` +
  `card_embeddings_config` KV).

### Deferred to Implementation

- **Exact feature-token names.** The vectorizer emits tokens like
  `port_type:Trigger`, `event_class:SpellCast`, `attr:subtype:Goblin`,
  `keyword:Flying`. Final naming convention ironed out in Unit 1 and
  captured as a frozen `TOKEN_FORMAT_VERSION`. Bumping this string bumps
  `vectorizer_version`.
- **Minimum document frequency (`min_df`) for TF-IDF dampening.** Tokens
  seen on fewer than N cards get pruned to reduce dimensionality and
  numerical noise. Initial value (2 or 3) picked during Unit 1 once the
  actual token-frequency distribution is seen.
- **Exact `w_emb` and `k` chosen post-landing.** Audit sweep is operational
  work, not planning. Candidates: `w_emb ∈ {0.1, 0.3, 0.5, 1.0}`,
  `k ∈ {0.5, 0.8, 1.2}` per FR7.
- **Whether the `--embedding-dedup` report writes to `.audit/`.** Mirror
  `--collinearity` behavior once Unit 6 is implemented.
- **Whether to add an optional scipy extra.** Only if Unit 1 profiling of
  `numpy.linalg.svd` on the real 32k × ~5k matrix shows > 30s build time.
  Default: no.

## Output Structure

```
src/mtg_synergy_graph/
  embeddings/
    __init__.py
    vectorizer.py        # Hand-rolled TF-IDF feature-bag builder + token format
    svd.py               # numpy truncated SVD + L2 normalization
    config.py            # EmbeddingConfigInputs NamedTuple + compute_embedding_hash + verify
    store.py             # write_vectors / read_vectors / load_card_embeddings
    commander_target.py  # build_commander_target_vector (functools.cache)
    contribution.py      # _ENABLE_EMBEDDING_CONTRIBUTION + _EMBEDDING_W + _EMBEDDING_K
                         # + embedding_contribution(score, target_vec, vectors) -> float
    dedup.py             # embedding_dedup(conn, threshold) -> list[RulePair]
  bench/
    embedding_dedup_handler.py   # handle_embedding_dedup(args)
scripts/
  build_embeddings.py    # User-invoked build step
tests/
  embeddings/
    test_vectorizer.py
    test_svd.py
    test_config.py
    test_store.py
    test_commander_target.py
    test_contribution.py
    test_dedup.py
  bench/
    test_embedding_dedup_handler.py
data/
  # card_embeddings table inside data/synergy.db (gitignored already)
```

*This tree is a scope declaration, not a constraint. Unit-level file lists
are authoritative.*

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for
> review, not implementation specification. The implementing agent should
> treat it as context, not code to reproduce.*

### Feature-bag tokenization (Unit 1)

Per card, emit a multiset of string tokens from structured sources:

```
for each row in card_ports WHERE card_name = ?:
    emit "port_type:{port_type}"
    emit "event_class:{event_class}"            if not null
    emit "zone_origin:{zone_origin}"            if not null
    emit "zone_destination:{zone_destination}"  if not null
    emit "counter_type:{counter_type}"          if not null
    emit "branch_kind:{branch_kind}"            if not null
for each row in port_attributes WHERE port_id IN (ports of card):
    emit "attr:{attr_kind}:{attr_value}"
for each kw in json.loads(cards.keywords):
    emit "keyword:{kw}"
```

TF-IDF over the corpus, truncated-SVD to 128 dims, L2-normalize. Token
format stability captured by `TOKEN_FORMAT_VERSION` constant; bumping it
bumps `vectorizer_version`, which participates in the hybrid hash.

### Scoring-path integration (Unit 7)

```
# inside score_all_universal's results loop:
n_rules = len(score.distinct_rules)           # existing @cached_property
if _ENABLE_EMBEDDING_CONTRIBUTION:
    v_cand = vectors.get(candidate_name)       # dict[str, ndarray] from load_card_embeddings
    v_cmdr = commander_target                  # precomputed for this cmdr_set
    if v_cand is not None and v_cmdr is not None:
        cos = float(v_cand @ v_cmdr)           # L2-normalized, so dot product IS cosine
        decay = math.exp(-_EMBEDDING_K * n_rules)
        score.embedding_contribution = _EMBEDDING_W * decay * cos
```

`score.score` sums the new field alongside existing bonuses. When
`_ENABLE_EMBEDDING_CONTRIBUTION = False` OR any lookup returns None, the
field stays 0.0 and scores are bitwise-identical to baseline.

### Dedup diagnostic (Unit 6)

```
# consumes rule_contributions tensor (plan 001) + card_embeddings
for rule in registered_rules:
    activation_set[rule] = {cand for (_, cand, r, contribution) in tensor where r == rule AND contribution > 0}
    mean_vec[rule] = mean(vectors[c] for c in activation_set[rule])   # L2-normalized
for (rule_a, rule_b) in pairs(registered_rules):
    cos_ab = mean_vec[rule_a] @ mean_vec[rule_b]
    if cos_ab > threshold:  # default 0.95
        flag (rule_a, rule_b, cos_ab)
```

Output is a sorted markdown table; JSON mode mirrors
`handle_collinearity`'s structure.

## Implementation Units

- [x] **Unit 1: Feature vectorizer (pure function)**

**Goal:** Hand-rolled TF-IDF + truncated-SVD vectorizer over structured
inputs. No DB writes yet — pure function from `{card_name: feature_tokens}`
to `{card_name: ndarray(128,)}`.

**Requirements:** R1, R8

**Dependencies:** None

**Files:**
- Create: `src/mtg_synergy_graph/embeddings/__init__.py`
- Create: `src/mtg_synergy_graph/embeddings/vectorizer.py`
- Create: `src/mtg_synergy_graph/embeddings/svd.py`
- Create: `tests/embeddings/__init__.py`
- Create: `tests/embeddings/test_vectorizer.py`
- Create: `tests/embeddings/test_svd.py`

**Approach:**
- `extract_card_tokens(conn) -> dict[str, tuple[str, ...]]` — pull structured
  features per card from `card_ports` + `port_attributes` + `cards.keywords`.
- `compute_tfidf(corpus: dict[str, tuple[str, ...]], min_df: int) -> tuple[ndarray, list[str], list[str]]`
  — returns `(tfidf_matrix, card_names, vocabulary)`. Plain math; no sklearn.
- `truncated_svd(tfidf: ndarray, k: int = 128) -> ndarray` — calls
  `numpy.linalg.svd`, takes top-k left singular vectors, L2-normalizes rows.
- `TOKEN_FORMAT_VERSION: str` constant (e.g., `"v1"`). Bump on any token
  scheme change.
- Freeze `min_df` default at 2 for first cut.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/collinearity.py` for the numpy-matrix posture.
- `src/mtg_synergy_graph/attributes.py` for the token-naming convention.

**Test scenarios:**
- Happy path: 5 synthetic cards with known port/keyword sets → hand-computed
  TF-IDF matches within 1e-9.
- Happy path: deterministic SVD — same input fed twice returns bitwise
  identical output.
- Edge case: all-zero column (token present on zero cards after min_df
  prune) does not appear in the vocabulary.
- Edge case: a card with zero tokens (hypothetical) returns a zero vector;
  L2 normalization handles zero-norm without NaN.
- Edge case: duplicate tokens on the same card (same keyword twice) are
  counted as-is (TF uses raw counts).
- Integration: vectorizer over a 50-card fixture DB produces 50 distinct
  128-dim unit vectors.

**Verification:**
- `uv run pytest tests/embeddings/test_vectorizer.py tests/embeddings/test_svd.py`
  passes.
- Pyright clean on the new modules.
- Manual smoke: running the vectorizer end-to-end on the real
  `data/synergy.db` (~32k cards) completes in under 60 seconds.
  Profile only if wall time exceeds this — decide whether scipy is worth it.

---

- [x] **Unit 2: Schema, config hash, and blob store**

**Goal:** Add the `card_embeddings` and `card_embeddings_config` tables;
define `EmbeddingConfigInputs` NamedTuple + `compute_embedding_hash` +
`verify_current_or_raise`; blob round-trip helpers.

**Requirements:** R1, R8, D3

**Dependencies:** Unit 1

**Files:**
- Modify: `src/mtg_synergy_graph/schema.sql` (add two tables)
- Create: `src/mtg_synergy_graph/embeddings/config.py`
- Create: `src/mtg_synergy_graph/embeddings/store.py`
- Create: `tests/embeddings/test_config.py`
- Create: `tests/embeddings/test_store.py`

**Approach:**
- Schema:
  ```sql
  CREATE TABLE IF NOT EXISTS card_embeddings (
      card_name          TEXT PRIMARY KEY REFERENCES cards(name),
      vector             BLOB NOT NULL,
      vectorizer_version INTEGER NOT NULL,
      built_at           TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS card_embeddings_config (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
  );
  ```
- `EmbeddingConfigInputs(NamedTuple)` fields: `token_format_version`,
  `svd_dims`, `min_df`, `vectorizer_version`, `port_signature_version`.
- `compute_embedding_hash(inputs) -> str` via `hashlib.sha256` over
  `repr(sorted(inputs._asdict().items()))`. Mirror
  `forge_oracle/config.py:compute_oracle_hash`.
- `write_vectors(conn, vectors, config_inputs)` — one transaction:
  upsert rows + write `config_hash` + one KV row per field for diagnostics.
- `read_vector(conn, name)` / `load_card_embeddings(conn)` — blob →
  `np.frombuffer(..., dtype=np.float32)`.
- `verify_current_or_raise(conn, config_inputs)` — raise
  `EmbeddingConfigStaleError` with the exact rebuild command in the
  message. Raise `EmbeddingConfigMissingError` if the KV row is absent.

**Patterns to follow:**
- `src/mtg_synergy_graph/forge_oracle/config.py` — full NamedTuple + hash
  + error-class + error-message template.
- `src/mtg_synergy_graph/forge_oracle/ingest.py` — transaction shape for
  writing data + config hash together.
- `src/mtg_synergy_graph/schema.sql` `graph_cache` table for the blob
  column pattern.

**Test scenarios:**
- Happy path: `write_vectors` + `load_card_embeddings` round-trip produces
  bitwise-identical ndarrays.
- Happy path: `compute_embedding_hash` stable across NamedTuple field
  reordering (via `sorted(items)`).
- Edge case: `load_card_embeddings` on a DB without the table → `{}` +
  `logging.warning`.
- Edge case: `load_card_embeddings` on an empty table → `{}` +
  `logging.warning`.
- Edge case: all-zero blob → excluded from result (all-zero vectors are
  unusable for cosine).
- Error path: `verify_current_or_raise` with mismatched hash →
  `EmbeddingConfigStaleError` whose message contains the rebuild command.
- Error path: `verify_current_or_raise` with missing KV row →
  `EmbeddingConfigMissingError`.
- Integration: full transaction is atomic — if an exception fires
  mid-`write_vectors`, no partial rows survive.

**Verification:**
- `uv run pytest tests/embeddings/test_config.py tests/embeddings/test_store.py`
  passes.
- Schema change is safely re-runnable (`CREATE TABLE IF NOT EXISTS`) —
  existing DBs without the tables get them added on next `open_db`.
- Coverage ≥ 80% on the new modules.

---

- [x] **Unit 3: Build script + CI conftest stub**

**Goal:** User-invoked `scripts/build_embeddings.py` that wires
Unit 1 (vectorizer) + Unit 2 (store) end-to-end. Autouse conftest stub so
embedding-consuming tests pass on CI even when the table hasn't been
rebuilt.

**Requirements:** R1, R4, R8

**Dependencies:** Units 1, 2

**Files:**
- Create: `scripts/build_embeddings.py`
- Modify: `tests/conftest.py` (add autouse stub)
- Create: `tests/embeddings/test_build_script.py`

**Approach:**
- Script skeleton mirrors `scripts/build_graph_cache.py`:
  1. `open_db(...)`
  2. `extract_card_tokens(conn)`
  3. `compute_tfidf(...)` → SVD → L2 → `vectors`
  4. `EmbeddingConfigInputs(...)` assembled from current constants
  5. `write_vectors(conn, vectors, config_inputs)` (single transaction)
  6. Print a one-line summary: count of cards, vocabulary size,
     `config_hash`, wall time.
- Runs stand-alone; importer does NOT invoke it. Users run manually after
  each cardsfolder refresh.
- CI stub in `tests/conftest.py`:
  ```python
  # pseudo-code, not implementation:
  # if `card_embeddings` table has zero rows, autouse fixture seeds a
  # deterministic 5-card in-memory fixture so downstream tests that
  # call load_card_embeddings get back a small, non-empty dict.
  # When the table is populated (dev machine), fixture is a no-op.
  ```
  Mirrors the `_stub_forge_sha_when_checkout_absent` fixture from
  `tests/conftest.py:46–64`.

**Patterns to follow:**
- `scripts/build_graph_cache.py` for the script shape.
- `tests/conftest.py` `_stub_forge_sha_when_checkout_absent` for the
  autouse stub pattern.
- `docs/solutions/test-failures/forge-oracle-ci-git-checkout-stub-2026-04-23.md`
  for the CI-first-commit discipline.

**Test scenarios:**
- Happy path: `scripts/build_embeddings.py` against a 50-card fixture DB
  populates `card_embeddings` with 50 rows and a valid `config_hash`.
- Edge case: re-running the script overwrites rows (upsert semantics;
  `built_at` refreshes).
- Error path: script is idempotent when nothing changed — hash stable
  across consecutive builds.
- Integration: autouse conftest stub activates when the table is missing
  or empty; tests calling `load_card_embeddings` get a non-empty fixture
  dict.

**Verification:**
- `uv run scripts/build_embeddings.py` against the real `data/synergy.db`
  completes and populates the table.
- `uv run pytest` passes on a fresh checkout (including CI, which has no
  pre-populated `card_embeddings`).
- Simulate-CI pre-push check works:
  `DROP card_embeddings; uv run pytest` still passes via the stub.

---

- [x] **Unit 4: Commander target vector (lazy + cached)**

**Goal:** `build_commander_target_vector(commander_set, vectors,
golden_hi_syn)` that returns the L2-normalized mean of the commander's own
port-feature vector and the EDHREC golden-set hi-syn vectors (if any).
Module-level `functools.cache` keyed by `tuple(commander_set)`.

**Requirements:** R2, D5

**Dependencies:** Units 1, 2

**Files:**
- Create: `src/mtg_synergy_graph/embeddings/commander_target.py`
- Create: `tests/embeddings/test_commander_target.py`

**Approach:**
- Read EDHREC golden-set hi-syn cards per commander from the existing
  golden-set fixture source (brainstorm FR2 — cached in
  `commander_cache` already; research report lines flag the precedent).
  Research: look at how `bench/` reads the 100-cmdr golden set.
- If hi-syn list non-empty: target = L2-normalize(mean(v_cmdr +
  v_hi_syn_1 + ... + v_hi_syn_N) / (N + 1))
- If hi-syn list empty (commander not in golden set): target = v_cmdr
  (commander's own port-feature vector, already L2-normalized).
- Cache with `@functools.cache` so the 100-cmdr bench run warms up once;
  user queries hit the cache after the first lookup.
- Partner commanders: commander_set has len=2 → target computed over
  the concatenated hi-syn lists.

**Patterns to follow:**
- `memory/reference_edhrec_502.md` context for the golden set shape.
- `src/mtg_synergy_graph/engine.py:_score_cache` for the
  `tuple(cmdr_set)` keying convention.
- `functools.cache` use-sites already in the codebase (grep for current
  examples).

**Test scenarios:**
- Happy path: golden-set commander with known hi-syn list → target is the
  hand-computed L2-normalized mean.
- Happy path: non-golden-set commander → target equals its own port vector
  (no hi-syn contribution).
- Edge case: partner pair (len=2 cmdr_set) with hi-syn entries from both
  halves composes correctly.
- Edge case: commander with zero ports (hypothetical) → target is zero
  vector; caller handles it by skipping the contribution.
- Edge case: `@functools.cache` returns the SAME ndarray object on
  repeated calls (verify via `id()` or numpy `shares_memory`).

**Verification:**
- `uv run pytest tests/embeddings/test_commander_target.py` passes.
- Cache hit-rate exercised: calling the function 10 times with the same
  input only invokes the expensive path once.

---

- [x] **Unit 5: Inference-path reader with graceful-fallback contract**

**Goal:** `load_card_embeddings(conn) -> dict[str, np.ndarray]` and
`embedding_contribution(score, v_cmdr, vectors) -> float` that the scorer
will call in Unit 7. Graceful fallback: every failure mode returns
`0.0`/`{}` + `logging.warning`, never raises.

**Requirements:** R3, R9, D6

**Dependencies:** Units 2, 4

**Files:**
- Create: `src/mtg_synergy_graph/embeddings/contribution.py`
- Create: `tests/embeddings/test_contribution.py`

**Approach:**
- `_ENABLE_EMBEDDING_CONTRIBUTION: bool = False` (constant; flipped in
  Unit 7).
- `_EMBEDDING_W: float = 0.0` and `_EMBEDDING_K: float = 0.8` (constants;
  initial values tuned post-Unit 7 landing).
- `embedding_contribution(score: UniversalScore, v_cmdr: np.ndarray |
  None, vectors: Mapping[str, np.ndarray]) -> float`:
  - Returns `0.0` if `not _ENABLE_EMBEDDING_CONTRIBUTION`.
  - Returns `0.0` if `v_cmdr is None`.
  - Returns `0.0` if `vectors.get(score.candidate)` is None.
  - Else computes `_EMBEDDING_W * math.exp(-_EMBEDDING_K *
    len(score.distinct_rules)) * float(v_cand @ v_cmdr)`.
- `load_card_embeddings(conn)` — thin wrapper over `store.load_card_embeddings`
  with config-hash verification; on mismatch, log a warning and return
  `{}` (silent degrade for the inference path, per D6).
- This module is NOT called by the scorer yet — just shippable and
  independently tested.

**Execution note:** Start with a failing test asserting the three
graceful-fallback return paths before writing the function body.

**Patterns to follow:**
- `src/mtg_synergy_graph/forge_oracle/gap_weight.py:load_forge_signals`
  for the exact failure-mode taxonomy.
- `src/mtg_synergy_graph/complement_rules/pathway.py` for the
  `_ENABLE_*` constant style.

**Test scenarios:**
- Happy path: flag on + both vectors present + N_rules=0 →
  `w_emb * 1.0 * cos_sim`.
- Happy path: flag on + N_rules=5 → `w_emb * exp(-k*5) * cos_sim`
  (dampened ~10×).
- Edge case: flag on + `v_cmdr is None` → 0.0.
- Edge case: flag on + `score.candidate` not in vectors → 0.0.
- Error path: flag off (default) → 0.0 regardless of inputs.
- Error path: `load_card_embeddings` with stale config_hash → `{}` +
  warning; scorer call chain treats as "no vectors available" → 0.0.
- Error path: `load_card_embeddings` with missing table → `{}` + warning.
- Integration: `embedding_contribution` is pure (no side effects on
  `score`); scorer wiring in Unit 7 will assign the return value to
  `score.embedding_contribution`.

**Verification:**
- `uv run pytest tests/embeddings/test_contribution.py` passes.
- No scorer behavior change (flag is off by default).
- `uv run scripts/bench.py audit --expect-identity` passes against the
  pre-plan baseline (this unit touches no score-path code).

---

- [x] **Unit 6: `bench.py audit --embedding-dedup` diagnostic**

**Goal:** New audit mode that consumes the `rule_contributions` tensor +
`card_embeddings` and flags rule pairs whose candidate-activation sets
are near-parallel in embedding space.

**Requirements:** R5, D7, D11

**Dependencies:** Units 2, 3 (needs populated `card_embeddings`); upstream
Survivor 1 `rule_contributions` tensor (already landed).

**Files:**
- Create: `src/mtg_synergy_graph/embeddings/dedup.py` (pure logic)
- Create: `src/mtg_synergy_graph/bench/embedding_dedup_handler.py`
  (handler)
- Modify: `src/mtg_synergy_graph/bench/cli.py` (mutex-group registration)
- Modify: `src/mtg_synergy_graph/bench/__init__.py` (`_cli.register`)
- Modify: `src/mtg_synergy_graph/bench/_stubs.py` (add
  `embedding_dedup_stub`)
- Create: `tests/bench/test_embedding_dedup_handler.py`
- Create: `tests/embeddings/test_dedup.py`

**Approach:**
- Pure logic in `embeddings/dedup.py`:
  - Read activation set `{candidate | rule fires positively}` per rule
    from `rule_contributions WHERE config_hash = current`.
  - Compute per-rule mean embedding = L2-normalize(mean of vectors over
    activation set).
  - For each rule pair: `cos = mean_a @ mean_b`.
  - Return sorted list of `(rule_a, rule_b, cos)` where `cos > threshold`
    (default 0.95).
- Handler `handle_embedding_dedup(args)`:
  - Verify tensor rows exist for current `config_hash`; exit 2 with
    rebuild-hint message if not.
  - Verify `card_embeddings` present and fresh (via
    `verify_current_or_raise`); exit 2 with rebuild-hint if not.
  - Render markdown table (default) or JSON (`--format json`).
  - Optionally persist to `.audit/last_dedup.md` (matches
    `--collinearity` behavior).
- CLI registration: one `mode.add_argument("--embedding-dedup", ...)` in
  the mutex group.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/collinearity.py` for the matrix-consumption
  pattern.
- `src/mtg_synergy_graph/bench/handlers.py:handle_collinearity` for the
  handler shape (exit codes, md/json rendering, `.audit/` write).
- `src/mtg_synergy_graph/bench/cli.py` existing `--collinearity` wiring
  for the argparse + handler-registration pattern.

**Test scenarios:**
- Happy path: two synthetic rules with near-identical activation sets →
  cosine > 0.95 → flagged.
- Happy path: two synthetic rules with orthogonal activation sets →
  cosine ≈ 0 → not flagged.
- Edge case: threshold override via `--threshold` CLI flag.
- Edge case: rule with zero-card activation set is skipped (no NaN).
- Edge case: rule with one-card activation set still works (mean over
  one element).
- Error path: `--embedding-dedup` without `card_embeddings` present →
  exit 2 with rebuild hint pointing at `scripts/build_embeddings.py`.
- Error path: `--embedding-dedup` without tensor rows for current
  `config_hash` → exit 2 with rebuild hint pointing at
  `bench.py audit --repin`.
- Integration: mutex-group behavior — `--embedding-dedup --collinearity`
  together exits 2 with argparse error (mutex).

**Verification:**
- `uv run pytest tests/bench/test_embedding_dedup_handler.py
  tests/embeddings/test_dedup.py` passes.
- `uv run scripts/bench.py audit --embedding-dedup --help` shows the new
  flag.
- Running against real DB produces a coherent ranked report where at
  least one pair agrees with the existing `--collinearity` MI-VIF output
  (FR5 success criterion).

---

- [x] **Unit 7: The flip — plumb flag, wire scorer, render `--explain`**

**Goal:** Add the three constants to `ScoringConfigInputs`, wire the
`embedding_contribution` term into `score_all_universal`'s results loop,
extend `_render_explanation` with the new line + three nearest neighbors,
extend the pre-commit regex, and set `_ENABLE_EMBEDDING_CONTRIBUTION =
False` (default). Only unit that changes scores; all others stay
identity-clean.

**Requirements:** R3, R6, R7, R9, D9, D10, D12

**Dependencies:** Units 1–6

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (extend
  `UniversalScore`, `ScoringConfigInputs`, `score_all_universal` results
  loop)
- Modify: `src/mtg_synergy_graph/bench/tensor.py` (`compute_config_hash`
  picks up the three new fields)
- Modify: `src/mtg_synergy_graph/engine.py`
  (`_render_explanation` — the new line + nearest-neighbor lookup)
- Modify: `src/mtg_synergy_graph/embeddings/contribution.py` (flip
  `_ENABLE_EMBEDDING_CONTRIBUTION`, but remain `False` in this unit —
  the actual flip-to-`True` is a follow-up after the audit sweep)
- Modify: `.pre-commit-config.yaml` (extend bench-audit regex)
- Modify: `tests/bench/test_universal_scorer_identity.py` (add
  flag-off-identity assertion)
- Create: `tests/test_explain_embedding_render.py` (integration test
  for the new `--explain` line)

**Approach:**
- Add `embedding_contribution: float = 0.0` to `UniversalScore`
  (line ~286). Add to `score` sum (line ~336) and `to_legacy_buckets`
  (line ~378). Assign in the `score_all_universal` results loop (line
  ~938–950) from the Unit 5 function, passing the cached
  `commander_target_vector` from Unit 4 and the loaded vectors dict.
- Extend `ScoringConfigInputs` with three fields:
  `enable_embedding_contribution: bool`, `embedding_w: float`,
  `embedding_k: float`. Also include `vectorizer_version: int` read
  from the `card_embeddings_config` KV table at scorer init (so
  rebuilding the table with a new `TOKEN_FORMAT_VERSION` invalidates
  the tensor).
- `_render_explanation`: after the existing `self_bridging_cascade`
  line block, render:
  ```
  embedding_contribution: +0.047 (decay × cosine = 0.32 × 0.146)
    nearest covered neighbors: Spellbinder (cosine 0.88), Experiment Kraj (0.84), Soul of the Harvest (0.81)
  ```
  Only when `score.embedding_contribution > 0`. Nearest covered neighbors
  = top-3 by cosine among the candidates whose `len(distinct_rules) >=
  1` from the same `(commander, candidates)` run.
- Pre-commit regex extension:
  ```yaml
  # in .pre-commit-config.yaml bench-audit hook
  files: ^src/mtg_synergy_graph/(complement_rules/.*\.py|universal_scorer\.py|graph_engine\.py|embeddings/.*\.py)$
  ```
- **Flag stays `False` in this unit.** The flip-to-`True` happens after
  the audit sweep (see Operational Notes).

**Execution note:** Start with a failing identity test asserting that
flag-off scores are bitwise-identical to the pre-plan pinned fixture.
Add the field plumbing only after the identity test is red.

**Patterns to follow:**
- `src/mtg_synergy_graph/complement_rules/pathway.py` — canonical
  precedent for "add `ScoringConfigInputs` field in the same unit as
  the flip, not earlier." Identical discipline.
- Plan `docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md`
  — structural twin.
- `src/mtg_synergy_graph/engine.py:_render_explanation` existing
  `self_bridging_cascade:` path_info block (lines 475–483) — same
  rendering style.

**Test scenarios:**
- Happy path: flag off (default) → `score.embedding_contribution == 0.0`
  for every candidate; totals bitwise-identical to pre-plan baseline.
- Happy path: flag on + `w_emb=0.0` → `score.embedding_contribution ==
  0.0` → totals still identical.
- Happy path: flag on + `w_emb=0.5` + N_rules=0 → contribution =
  `0.5 * cos(v_cand, v_cmdr)`.
- Happy path: flag on + N_rules=5 → contribution dampened by
  `exp(-0.8*5) ≈ 0.018`.
- Edge case: flag on + `card_embeddings` table missing → graceful
  fallback; all contributions 0.0; scorer emits one `logging.warning`
  per run, not per candidate.
- Edge case: flag on + commander has zero port ports (novel commander)
  → target vector is zero; contributions 0.0.
- Integration: `--expect-identity` on the pre-plan pinned tensor PASSES
  (proves flag-off identity).
- Integration: `recommend.py --explain "Animar, Soul of Elements" |
  head -50` includes the new `embedding_contribution:` + `nearest
  covered neighbors:` lines when the flag is flipped locally.
- Integration: tensor audit with flag flipped + default weights →
  `config_hash` differs from pre-flip → `bench.py audit` detects drift
  and requires `--repin` (this is intentional — it enforces that the
  flip is auditable).

**Verification:**
- `uv run pytest` passes end-to-end including the new identity
  assertion.
- `uv run scripts/bench.py audit --expect-identity` with flag OFF passes.
- `uv run scripts/bench.py audit --expect-identity` with flag ON fails
  (expected — the flip changes scores by design). This is the point at
  which the audit sweep begins.
- Pre-commit hook regex change verified by staging a touch on
  `src/mtg_synergy_graph/embeddings/contribution.py` and observing the
  `bench-audit` advisory hook fires.
- `recommend.py --explain` on a commander with flag-on + nonzero
  `w_emb` shows the new lines correctly.

## System-Wide Impact

- **Interaction graph:**
  - `SynergyEngine.page()` → `score_all_universal()` → new `embedding_contribution`
    term populated per candidate, read from Unit 5's function, which pulls from
    Unit 4's commander target cache and Unit 2's blob store.
  - `bench.py audit` picks up three new `ScoringConfigInputs` fields via
    `compute_config_hash` → pinned tensor invalidates when any of them
    change.
  - Pre-commit `bench-audit` advisory hook triggers on edits under the new
    `embeddings/` subpackage.
  - `recommend.py --explain` output grows new lines only when
    `embedding_contribution > 0`.
- **Error propagation:** inference-path failures are silent (log + degrade
  to 0.0); diagnostic (`--embedding-dedup`) failures are loud (exit 2 with
  rebuild hint). No exception escapes the scorer from embedding code.
- **State lifecycle risks:**
  - Rebuilding `card_embeddings` without rebuilding the pinned tensor →
    `vectorizer_version` drift caught by the `ScoringConfigInputs` hash;
    next `bench.py audit` fails CLEAN with a clear hash-mismatch message
    (this is the hybrid-hash payoff).
  - Crashed mid-build: transaction in Unit 2 is atomic; no partial state.
  - Pruning a token type (bumping `TOKEN_FORMAT_VERSION`) requires a
    rebuild; the `card_embeddings_config` KV table detects the stale
    state on next read.
- **API surface parity:** no public API changes; all additions are opt-in
  via flag. `SynergyEngine.page()` signature unchanged.
- **Integration coverage:** the identity test in Unit 7 is the key
  mocks-don't-suffice scenario — it exercises the full scoring chain
  (`find_all_complements` → `score_all_universal` → `UniversalScore.score`)
  with a pinned fixture to prove flag-off preserves bit-identity. Unit-level
  mocks alone wouldn't catch a subtle field-ordering regression in
  `UniversalScore.score`.
- **Unchanged invariants:**
  - No EDHREC at inference ranking — EDHREC golden-set hi-syn is only
    read at **offline** commander-target-vector build time. The
    inference path reads precomputed vectors only. This stays within
    `memory/feedback_edhrec_hivemind.md`.
  - No popularity-adjacent features in the vectorizer inputs.
  - No new EDHREC dependency in the scoring path — the existing
    `--explain` tiebreaker is the only place EDHREC ever touches
    inference, and this plan does not change that.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| NDCG regression similar to the reverted `card_hints`-family rules (structural similarity diluting mechanical port signal). | Start with smallest `w_emb ∈ {0.1, 0.3}`; exponential decay defaults to `k=0.8` so N_rules≥3 candidates see near-zero embedding contribution; measure `hidden_gem_hit_rate` at every audit step; if hidden-gem rate drops, flag stays `False`. Revert to `False` is a one-line diff. |
| `numpy.linalg.svd` slow on 32k × ~5k dense matrix. | Profile in Unit 1; if > 60s, add scipy as optional `[embeddings]` extra with `scipy.sparse.linalg.svds`. Not in the default path. |
| Commander target vector drift when the golden-set source is bumped without rebuilding embeddings. | `vectorizer_version` + `EmbeddingConfigInputs` in `ScoringConfigInputs` → next `bench.py audit` fails with hash mismatch. Rebuild is required, not optional. |
| CI tests fail because `card_embeddings` is regenerable and CI doesn't run `scripts/build_embeddings.py`. | Autouse conftest stub in Unit 3 seeds a deterministic small-table fixture; the forge_oracle precedent (`docs/solutions/test-failures/forge-oracle-ci-git-checkout-stub-2026-04-23.md`) mandates this is established in the same commit as the first test. |
| Embedding module imports leaking into inference path before flag is flipped. | Structural: the scorer only calls `embedding_contribution(...)` which short-circuits on the flag. Behavioral: Unit 7's identity test catches any accidental score drift at flag-off. |
| Dedup diagnostic produces noisy false-positive rule pairs (e.g., two rules with tiny activation sets that happen to lie near each other). | Minimum-activation-set filter in Unit 6 (e.g., `|activation_set| >= 20`); makes the signal actionable without swamping humans. Threshold configurable via `--min-activation`. |
| Audit sweep lands a `w_emb` that regresses one commander beyond MARGINAL even if aggregate improves. | FR7 explicit gate: no commander regresses beyond CONTENTIOUS; if one does, inspect whether it's well-rule-covered (expected — decay should dampen) or sparse (unexpected — revert or tune). Operational, not a planning risk. |

## Documentation / Operational Notes

- Update `CLAUDE.md` "Common Commands" with:
  ```
  uv run scripts/build_embeddings.py                                       # Build card_embeddings after cardsfolder refresh
  uv run scripts/bench.py audit --embedding-dedup                          # Rule-pair redundancy diagnostic in embedding space
  uv run scripts/bench.py audit --embedding-dedup --threshold 0.90         # Looser threshold for exploration
  ```
- Update `CLAUDE.md` "Scoring Architecture" with a new section on the
  embedding contribution: what it is, when it fires, how decay works,
  where the flag lives. Cross-reference this plan and the
  flag-gated-pattern learning.
- **Audit sweep (post-Unit 7 operational work, NOT an implementation
  unit):**
  1. Flip `_ENABLE_EMBEDDING_CONTRIBUTION = True` locally; iterate over
     `w_emb ∈ {0.1, 0.3, 0.5, 1.0}` × `k ∈ {0.5, 0.8, 1.2}`.
  2. For each combo: `scripts/bench.py audit --repin --yes` →
     `scripts/bench.py audit` → record aggregate NDCG@30 delta,
     `hidden_gem_hit_rate`, and per-commander verdict.
  3. Choose the combo with the highest aggregate that also improves
     `hidden_gem_hit_rate` AND has no commander regress beyond
     MARGINAL.
  4. If CONTENTIOUS: inspect losers. If losses concentrate on well-covered
     commanders (expected), ship. If on sparse-coverage commanders
     (unexpected), tune or revert.
  5. If HARMFUL on any axis: revert. The flag stays `False`.
- The promotion-to-`True` commit is separate from this plan. It lands as
  `perf(audit): promote embedding contribution w_emb=X k=Y (+Δ NDCG, +Δ
  hidden_gem_hit_rate)` with pinned fixture refresh, mirroring
  `cb720b2 perf(audit): dampen counter_keyword 1.0 → 0.5` style.

## Success Metrics

1. **Animar-class uplift.** Animar, Kess, Yuriko, and at least 4 other
   heterogeneous-hi-syn commanders each gain ≥1 hi-syn card in their top-30
   after the flip. (FR Success 1)
2. **Cold-start demonstration.** A small set of day-0 Forge-imported cards
   rank sensibly relative to structurally-similar predecessors with no new
   rules written. (FR Success 2)
3. **Aggregate NDCG neutral-or-positive.** Aggregate NDCG@30 on 100-cmdr
   golden set ≥ current baseline. (FR Success 3)
4. **Well-covered commanders unaffected.** Atraxa, Korvold, Karador, Prossh
   show ≤ 0.001 NDCG delta. (FR Success 4)
5. **Inference latency ≤ 5%.** Per-candidate embedding cost is constant
   (one dict lookup + one dot product). (FR Success 5)
6. **Dedup diagnostic value.** `--embedding-dedup` flags ≥ 2 rule pairs
   that agree with `--collinearity` MI-VIF output AND ≥ 1 pair that
   MI-VIF missed. (FR Success 6)
7. **`hidden_gem_hit_rate` ≥ baseline.** This is the non-negotiable
   gate per the `card_hints` reversion lesson. Aggregate gains at the
   cost of hidden-gem rate = revert.

## Alternative Approaches Considered

- **sklearn `TfidfVectorizer` + `TruncatedSVD`.** Rejected: would add
  scikit-learn as a runtime dep (sizeable install footprint, extra
  lock-file entries). Hand-rolled math is ~40 lines and aligns with the
  project's zero-extra-dep posture. Revisit only if the corpus grows
  past the point where numpy.linalg.svd is unacceptably slow.
- **Oracle-text n-grams as features.** Rejected per origin doc §Non-Goals
  — structural features only. Text n-grams would recreate the `card_hints`
  failure mode at higher cardinality.
- **Neural / transformer embeddings.** Rejected per origin doc §Non-Goals
  — non-deterministic + non-explainable + requires training infrastructure.
- **Sidecar `data/card_embeddings.db`.** Rejected per D2 — vectors are
  consumed at inference; sidecar isolation is only right for offline-only
  data. Forge_oracle pattern doesn't apply here.
- **Persist commander target vectors to a second table.** Rejected per
  D5 — lazy + `functools.cache` is simpler; 100 golden-set commanders
  warm up during bench. Revisit only if cold-start latency on the first
  user query per commander becomes a user-visible issue.
- **Auto-propose rule merges from `--embedding-dedup`.** Rejected per D11
  — humans read the diagnostic; auto-merging rules would violate the
  audit-every-change guardrail and make rule consolidation opaque.
- **Morgan/ECFP circular fingerprints over typed port-nodes as the first
  embedding.** Deferred per origin doc §Out of Scope — circular
  fingerprints need plan 003's typed-port-graph vocabulary to be
  generalized past the current 16 rules. Promising phase-2 upgrade.

## Phased Delivery

### Phase 1 — Offline infrastructure (identity-clean)
- Unit 1: Vectorizer (pure function)
- Unit 2: Schema + config hash + store
- Unit 3: Build script + CI conftest stub

Lands without scoring-path changes. Vectors computable on demand; no
scorer wiring yet. `--expect-identity` clean throughout.

### Phase 2 — Commander target + inference reader (still identity-clean)
- Unit 4: Commander target vector (lazy, cached)
- Unit 5: `embedding_contribution` function with graceful-fallback contract

Reader ready but unused by the scorer. `--expect-identity` still clean.

### Phase 3 — Diagnostic (read-only)
- Unit 6: `bench.py audit --embedding-dedup`

Ships the dedup report without affecting scores. Humans can start using
the diagnostic to inform rule consolidation before embeddings go live.

### Phase 4 — The flip (audit-gated)
- Unit 7: `ScoringConfigInputs` plumb + scorer wire + `--explain` + regex

Default flag `False`. Operational audit sweep begins after this lands
(separate commits, not in this plan).

## Documentation Plan

- `CLAUDE.md` — new "Content Embeddings" section under "Scoring Architecture"
  describing the term, flag, decay, cache behavior; extend
  "Common Commands" with `scripts/build_embeddings.py` + `--embedding-dedup`.
- `docs/RULE_PLANNING.md` — note that `--embedding-dedup` is now part of
  the audit-iterate toolkit alongside `--collinearity` and hidden-gem
  inspection.
- `docs/RULE_HISTORY.md` — a single dated entry when the flip lands in
  the audit sweep, recording the chosen `w_emb` + `k` and the aggregate
  delta + `hidden_gem_hit_rate` delta.
- No `docs/solutions/` entries in this plan. If a non-obvious issue
  surfaces during execution (e.g., SVD determinism across numpy versions,
  CI fixture drift), capture it via `/ce-compound` post-landing.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-21-content-embeddings-requirements.md](../brainstorms/2026-04-21-content-embeddings-requirements.md)
- **Parent ideation:** [docs/ideation/2026-04-21-recommendation-model-ideation.md](../ideation/2026-04-21-recommendation-model-ideation.md) (Survivor 6 of 7)
- **Prerequisite plan (landed):** [docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md](2026-04-22-001-feat-unified-eval-harness-plan.md) — `rule_contributions` tensor consumed by FR5.
- **Structural twin plan:** [docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md](2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md) — same flag-gated discipline; mirror its unit sequencing.
- **Related landed plan:** [docs/plans/2026-04-22-002-feat-typed-port-graph-substrate-plan.md](2026-04-22-002-feat-typed-port-graph-substrate-plan.md) — phase-2 circular fingerprints will eventually layer on top of this vocabulary.
- **Governing learning:** [docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md](../solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md)
- **Partial-apply learning:** [docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md](../solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md) — hybrid hash discipline (`ScoringConfigInputs` + KV table).
- **CI fixture learning:** [docs/solutions/test-failures/forge-oracle-ci-git-checkout-stub-2026-04-23.md](../solutions/test-failures/forge-oracle-ci-git-checkout-stub-2026-04-23.md)
- **Gitignore learning:** [docs/solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md](../solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md)
- **Memory guardrails:**
  - `memory/feedback_audit_every_change.md` — every scoring-path flip is audit-gated.
  - `memory/feedback_no_individual_rules.md` — embedding is the structural answer.
  - `memory/feedback_edhrec_hivemind.md` — EDHREC offline-only, not inference.
  - `memory/feedback_edhrec_not_goal.md` — real goal is hidden gems.
  - `memory/feedback_hidden_gem_metric.md` — `hidden_gem_hit_rate` is the second axis.
  - CLAUDE.md note on reverted `card_hints`-family rules — structural-similarity failure mode warning.
