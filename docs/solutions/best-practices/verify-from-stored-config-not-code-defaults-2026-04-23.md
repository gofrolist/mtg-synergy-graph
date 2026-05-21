---
module: embeddings
date: 2026-04-23
problem_type: best_practice
component: tooling
severity: high
applies_when:
  - A subsystem builds derived data (vectors, embeddings, caches, sidecars) whose freshness is checked via a config-hash
  - The build script exposes user-facing CLI flags that feed into the hash (e.g., --min-df, --svd-dims)
  - The verifier runs in a different process or at a later time than the builder
  - The stored artifact is consumed silently — an empty result degrades scoring without raising
tags:
  - config-hash
  - build-consume-desync
  - derived-data
  - freshness-verification
  - silent-failure
  - cli-flags
  - embeddings
---

# Verify derived data from stored config, not from current code defaults

## Context

Plan 2026-04-23-003 (content embeddings) introduced a two-phase pipeline:
an offline build script (`scripts/build_embeddings.py`) writes 128-dim
TF-IDF/SVD vectors into SQLite alongside a stored config hash, and a
runtime loader (`src/mtg_synergy_graph/embeddings/contribution.py`)
verifies that stored hash before consuming the vectors.

The verifier was written to recompute the expected hash from
module-level constants (`_DEFAULT_MIN_DF = 2`, `_DEFAULT_SVD_DIMS = 128`)
rather than from the config values actually written to the store at
build time. A companion storage-layer guard
(`_EMBEDDING_DIM = 128` in `embeddings/store.py`) used a hardcoded
constant for the same reason.

This creates a category of latent build-consume desync: any build
invoked with non-default flags (`--min-df 3`, `--svd-dims 64`) produces
a valid artifact for those parameters, but the verifier silently
rejects it at consume time with a misleading "stale hash" message,
returning `{}` instead of raising a clear error.

The bug surfaces only for CI environments, teammates, or automated
workflows that pass non-default flags — the exact scenarios where the
artifact-plus-hash discipline is most valuable. A developer running
`uv run scripts/build_embeddings.py` with default flags will never hit
it. The bug is latent until someone flips
`_ENABLE_EMBEDDING_CONTRIBUTION=True` with non-default build flags, at
which point the scoring path silently degrades *in a way the bench.py
audit gate will not catch* — flag-off scores are identical to baseline
whether or not the embeddings load successfully.

## Guidance

When a build-time subsystem hashes its configuration to allow
freshness verification at consume time, the verifier must perform two
separate comparisons against values read back out of the store — not
against current-code defaults.

**Wrong pattern (what plan 003 shipped; deferred to FU-1):**

```python
# contribution.py — WRONG
def load_card_embeddings_verified(conn):
    # get_embedding_config_inputs() returns module-level constants,
    # not what was written to the store at build time.
    inputs = emb_config.get_embedding_config_inputs()
    emb_config.verify_current_or_raise(conn, inputs)
    # If build used --min-df 3 but _DEFAULT_MIN_DF == 2,
    # stored hash and recomputed hash will never match.
```

```python
# store.py — WRONG
_EMBEDDING_DIM = 128  # hardcoded; breaks for --svd-dims 64 builds

def load_card_embeddings(conn):
    ...
    if len(vector) != _EMBEDDING_DIM:
        continue  # silently drops every row, returns {}
```

**Correct pattern:**

```python
def verify_current_or_raise(conn, current_code_inputs):
    # Step 1: Read back the config that was actually written at build time
    stored = read_stored_config(conn)  # from the KV table populated by the build
    stored_inputs = EmbeddingConfigInputs(
        token_format_version=stored["token_format_version"],
        svd_dims=int(stored["svd_dims"]),
        min_df=int(stored["min_df"]),
        vectorizer_version=int(stored["vectorizer_version"]),
        vocab_version=stored["vocab_version"],
    )

    # Step 2: Internal consistency check — does the DB agree with itself?
    # Detects corruption: stored rows edited without updating stored hash.
    if compute_embedding_hash(stored_inputs) != stored["config_hash"]:
        raise EmbeddingConfigCorruptError(
            "Stored config rows no longer match stored config_hash — "
            "DB may be partially written. Re-run build_embeddings.py."
        )

    # Step 3: Staleness check — does the stored config match what
    # the code expects? Detects legitimate build/consume version drift.
    if stored_inputs != current_code_inputs:
        raise EmbeddingConfigStaleError(
            f"Embeddings were built with {stored_inputs}; "
            f"current code expects {current_code_inputs}. "
            f"Re-run build_embeddings.py with default parameters, "
            f"or pass matching flags."
        )
```

```python
# store.py — correct: read dim from stored config, not a module constant
def load_card_embeddings(conn):
    stored_dim = int(read_stored_config(conn)["svd_dims"])
    ...
    if len(vector) != stored_dim:
        raise EmbeddingDimMismatchError(
            f"Row dim {len(vector)} != stored svd_dims {stored_dim} — "
            f"corrupt artifact."
        )
```

The two comparisons in `verify_current_or_raise` answer different
questions:

- **Step 2 (corruption check):** "Are the rows in this DB internally
  consistent?" The stored hash must reproduce from the stored config
  values. If it does not, the artifact is corrupt.
- **Step 3 (staleness check):** "Does this artifact match what the
  consuming code expects?" The stored config (valid at build time)
  may differ from what the current code version considers canonical.

Both failure modes raise with a named exception whose message names
the diverging parameter(s). Neither returns `{}`.

## Why this matters

**Silent degradation instead of a loud failure.** When the verifier
compares the stored hash against a recomputed hash from code defaults,
the mismatch surfaces as a logged "stale" warning and a `{}` return
from `load_card_embeddings`. No exception is raised. Callers that
check `if not embeddings` fall back to unscored behavior silently.

**The bench.py audit gate does not catch it.** Flag-off scores are
bitwise-identical to baseline whether or not the embeddings loaded.
The audit's `--expect-identity` check passes in both cases. The
degradation is invisible to the NDCG histogram and the hidden-gem
hit-rate. The only observable signal is in `logging.warning` output,
which is easy to miss.

**Misleading diagnostics.** The error prints stored and recomputed
hashes but does not tell the developer which config parameters
diverge. Root-causing requires comparing build invocation history
against current module constants — information that is not
colocated.

**Impossible to reproduce with defaults.** The bug surfaces only for
CI environments, teammates, or automated workflows that pass
non-default flags. Local development with `uv run python
scripts/build_embeddings.py` never hits it.

## When to apply

Apply this pattern whenever all of the following are true:

1. A build-time script writes derived data (vectors, embeddings,
   caches, compiled rule tables, oracle DBs) to a store.
2. A hash or version token is written alongside the data to enable
   freshness verification.
3. The build script accepts user-facing parameters that change the
   derived output (and therefore the hash).
4. The verifier runs in a different process, at a different time, or
   in a different deployment context than the builder.
5. A verification failure causes silent degradation (empty result,
   skipped rows, fallback to zero) rather than an immediate hard
   error visible to the caller.

Do not apply this pattern to in-process caches that are regenerated
atomically within the same process lifetime — in those cases, code
defaults and build-time values are always identical.

## Examples

### Plan 003 — Content Embeddings (the failure case, deferred to FU-1)

The bug is latent behind `_ENABLE_EMBEDDING_CONTRIBUTION=False` in
`src/mtg_synergy_graph/embeddings/contribution.py`. The fix is tracked
in [`docs/reviews/2026-04-23-content-embeddings-followups.md`](../../reviews/2026-04-23-content-embeddings-followups.md)
as FU-1. Before enabling the flag in any environment that uses
non-default `--min-df` or `--svd-dims` build parameters, the verifier
must be updated to read config from the store rather than from
`_DEFAULT_MIN_DF` / `_DEFAULT_SVD_DIMS`.

The dimension guard in `src/mtg_synergy_graph/embeddings/store.py`
(`_EMBEDDING_DIM = 128`) must similarly be replaced with a read from
the stored `svd_dims` KV entry, and the silent `continue` must become
a hard `raise`.

Cross-reviewer agreement at ce-code-review 20260423-213012-17f8d1eb:
- correctness CORR-001 (HIGH, 0.90) + CORR-002 (HIGH, 0.88) — the
  primary find.
- adversarial ADV-003 (0.85) — "dual hash systems with no cross-check"
  (the scoring config hash and the embedding config hash can drift
  independently).
- maintainability M-003 (MEDIUM, 0.82) — `_DEFAULT_SVD_DIMS` and
  `_DEFAULT_MIN_DF` extracted to constants in the first fix batch
  (commit `ab8b2a3`) so build-script and verifier at least agree on
  defaults; the stored-vs-code-defaults gap remains.

### Forge Oracle Sidecar — why the same seam works there

`scripts/forge_oracle.py` + `src/mtg_synergy_graph/forge_oracle/config.py`
also has a `get_oracle_config_inputs()` that reads code-level defaults.
It has not hit this failure mode in practice because its inputs are
*environmental*, not user-configurable: `read_current_forge_sha()`
re-reads the Forge checkout's current HEAD, `read_pinned_sha()` reads
a committed file, smoothing/min-decks are constants. Builder and
verifier therefore agree as long as the environment is stable.

The moment a user-facing CLI flag is added to `forge_oracle build`
that influences the hash, the same failure mode applies. Treat this
doc's guidance as the *generalization* of the offline-oracle pattern —
use forge_oracle's current implementation as a template only while its
inputs remain environmental.

### Generalized checklist for new offline sidecars

When authoring a new offline build pipeline:

- [ ] The build script writes all hash-contributing parameters to a
      KV or metadata table in the same transaction as the data rows.
- [ ] The verifier reads those KV rows first, reconstructs
      `StoredInputs`, and checks the stored hash against
      `compute_hash(StoredInputs)` (corruption check) before comparing
      to `CurrentCodeInputs` (staleness check).
- [ ] Any dimension or shape guard reads from stored config, not a
      module constant.
- [ ] Verification failure raises a named exception whose message
      names the diverging parameter(s) — never returns `{}` or
      logs-and-continues.
- [ ] A test exercises the non-default-flag path: build with
      `min_df=3`, verify with code defaults, assert a named
      staleness exception is raised (not a silent empty result).

## References

- Origin: plan [`docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md`](../../plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md)
  (landed as squash commit `4fcf5d1`)
- Deferred fix: [`docs/reviews/2026-04-23-content-embeddings-followups.md`](../../reviews/2026-04-23-content-embeddings-followups.md) FU-1
- Sibling pattern (inference-path hash discipline): [`flag-gated-multi-port-rule-pattern-2026-04-23.md`](flag-gated-multi-port-rule-pattern-2026-04-23.md)
- Related pattern (offline sidecar hash enforcement): [`offline-oracle-hash-pattern-2026-04-23.md`](offline-oracle-hash-pattern-2026-04-23.md) — this learning generalizes the verifier-inputs-provenance discipline that doc left implicit
- Review run artifact: `.context/compound-engineering/ce-code-review/20260423-213012-17f8d1eb/` (findings: correctness.json, adversarial.json, maintainability.json)
