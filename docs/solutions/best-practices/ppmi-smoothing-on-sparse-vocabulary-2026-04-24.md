---
last_updated: 2026-04-24
module: forge_oracle
title: Add-k smoothing in probability-space PMI swamps sparse vocabularies
tags:
  - ppmi
  - laplace-smoothing
  - forge-oracle
  - sparse-data
  - numerical-stability
  - plan-002
problem_type: best_practice
resolution_type: pattern
applies_when:
  - A pipeline computes pointwise mutual information (PMI) or a related log-ratio.
  - Smoothing is applied to probabilities after normalization (rather than to counts before).
  - The vocabulary size V is comparable to or larger than the per-signature marginal count.
created: 2026-04-24
plan_ref: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md
---

# Add-k smoothing in probability-space PMI swamps sparse vocabularies

When Laplace/add-k smoothing is applied in probability space to a PMI
computation, the smoothing constant `k` must be scaled with awareness
of the vocabulary size `V` and the expected magnitude of the marginal
probabilities. Blindly using a textbook default (e.g. `k=0.5`) can
produce uniformly-zero PPMI on sparse data, hiding what looks like a
working pipeline behind a "no signal" false-negative.

## Context

Plan `docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md` built
a Forge-precon-derived PPMI sidecar (`data/forge_oracle.db`) to
forge-signal-weight gap-report rule proposals. The intended flow:

```
gap_report.py propose-rules
  → rank_gaps (impact × forge_signal)
  → forge_signal from max-PPMI-per-subkind normalized into [1.0, 1.5]
```

The pipeline landed and passed all unit tests, but end-to-end
invocation produced:

```
forge_oracle.db at /.../data/forge_oracle.db is missing or empty —
falling back to volume-only ranking (forge_signal = 1.0 for all gaps).
```

Inspection revealed `data/forge_oracle.db` was fully populated (42,501
PPMI rows, valid hash) but every single `ppmi` value was `0.0`. The
`forge_signal` loader filters to positive PPMI, saw zero rows, and
silently degraded — as designed. The defect was upstream in the PPMI
math.

## Root cause

`src/mtg_synergy_graph/forge_oracle/ppmi.py` computes smoothed PMI in
probability space:

```python
numerator = p_joint + smoothing_k
denominator = (p_a + smoothing_k * vocab_size) * (p_b + smoothing_k * vocab_size)
pmi = log(numerator / denominator)
```

For the Forge corpus (`667 decks × ~1400 distinct subkinds`), typical
values are:

- `p_joint` for a co-occurring pair: `1e-4` to `1e-5`
- `p_a`, `p_b` (marginal probability of a single subkind): `1e-3` to
  `1e-2`
- `V ≈ 1400`

With the default `smoothing_k = 0.5`:

- `numerator = 1e-5 + 0.5 ≈ 0.5`
- `denominator = (1e-3 + 700)(1e-3 + 700) ≈ 700² = 490000`
- `pmi = log(0.5 / 490000) ≈ -13.8`
- `ppmi = max(pmi, 0) = 0`

Every single pair in the corpus produces `ppmi = 0`. The signal is
erased.

The mechanism: add-k smoothing in probability space adds `k * V` to
the marginals but only `k` to the joint. With `V ≫ 1`, the denominator
grows as `(k*V)²` while the numerator grows as `k`, so `PMI →
log(k / (k*V)²) = -log(k * V²)` regardless of the underlying data.

Verified with code:

```python
>>> p_joint, p_a, p_b, V = 9e-5, 7.5e-3, 7.5e-3, 500
>>> for k in (0.0, 0.01, 0.1, 0.5):
...     num = p_joint + k
...     denom = (p_a + k*V) * (p_b + k*V)
...     print(f'k={k}: pmi={math.log(num/denom):+.3f}')
k=0.0:  pmi=+0.485   # real signal
k=0.01: pmi=-7.818   # signal erased
k=0.1:  pmi=-10.126  # deeply negative
k=0.5:  pmi=-11.736  # same regardless of input
```

## Guidance

### 1. Prefer `smoothing_k = 0.0` when `min_decks_count >= 1`

Zero-division risk — the original motivation for smoothing — is
already bounded by the `min_decks_count` filter (no PMI is computed
for pairs below the threshold) and the `max(pmi, 0)` clamp (negative
infinity from a genuine zero becomes `ppmi = 0`). On sparse
vocabularies this combination is sufficient; additional smoothing
does not protect against any observable failure mode, and it erases
real signal.

### 2. When smoothing is genuinely needed, smooth counts, not probabilities

Textbook add-k smoothing operates on counts before normalization:

```python
smoothed_p_joint = (C_joint + k) / (N + k * V²)
smoothed_p_a     = (C_a + k) / (N + k * V)
```

This is mathematically equivalent to adding `k` fake observations to
every cell before normalizing, which is a principled Bayesian prior.
It scales sensibly with corpus size `N` — for `N ≫ k * V²`, the
smoothing barely perturbs the estimate; for small `N` it regularizes
correctly.

Probability-space smoothing (`p_a + k`) is algebraically different
from count-space smoothing (`(C_a + k) / (N + k*V)`) and does not
have the same theoretical grounding. If a future consumer adds
smoothing to `forge_oracle/ppmi.py`, it should operate in count-space.

### 3. Add a regression guard when changing a default that silently affects output

The bug here was invisible to unit tests: every existing PPMI test
explicitly passed `smoothing_k=0.5` and asserted consistency within
that parameter — not absolute correctness. Unit tests that parameterize
on the broken value don't catch the broken default.

The regression guard added in
`tests/test_forge_oracle_ppmi_math.py::test_compute_ppmi_sparse_corpus_default_smoothing_emits_positive_signal`
builds a synthetic sparse corpus (`200 signatures × 50 decks`) with a
known-correlated pair, uses the default `smoothing_k=0.0`, and asserts
that pair has `ppmi > 0` and at least 50% of all computed rows are
positive. This test would have failed under the old default.

### 4. Flag the silent-fallback pattern in the upstream consumer

`forge_oracle/gap_weight.load_forge_signals` degrades to `{}` on every
empty/missing failure mode, returning `{}` through the gap-report
re-ranking, which falls back to `forge_signal = 1.0` for every gap.
The CLI prints `"forge_oracle.db at ... is missing or empty"` — but
the DB wasn't missing, it was all-zeros, and the message does not
distinguish those cases.

Future consumers of this pattern should either:

- Emit a distinct warning for "table exists + is populated + every
  value is zero" (suggests upstream math bug), or
- Add a startup sanity check: if the loaded sidecar contains only
  rows whose signal is zero, raise rather than silently zero-degrade.

## Why this matters

The failure mode was silent and wore a plausible cover: a fresh
checkout would rebuild `forge_oracle.db` successfully (no error), tests
pass (they parameterize on the broken constant), and the re-ranked
output still looks reasonable (forge_signal = 1.0 on every gap is the
same as not re-ranking at all, so proposals look correct by volume).
Only a user inspecting the `forge_signal` column per proposal would
notice every value was `1.00`.

Sparse data + smoothing constants is a classic numerical-methods
landmine; this case adds a concrete example for the codebase's
internal reference.

## When to apply

Apply this guidance when:

- Adding Laplace/add-k smoothing to a PMI-adjacent computation (NPMI,
  LLR, etc.).
- The vocabulary size is large (~100+) or the corpus is small
  (~1000 decks or fewer).
- The smoothing constant is applied in probability space (watch for
  `p + k` and `p + k * V` patterns).

Do NOT worry about this when:

- The corpus is large and the vocabulary is small (e.g., natural
  language over a 10k-token vocab with a 10M-sentence corpus).
- Smoothing is applied to counts before normalization.
- The downstream consumer verifies non-zero output (sanity check on
  `max(ppmi)` at build time).

## References

- Plan: [`docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md`](../../plans/2026-04-23-002-feat-forge-second-oracle-plan.md)
- Fix commit: see `git log --oneline src/mtg_synergy_graph/forge_oracle/ppmi.py src/mtg_synergy_graph/forge_oracle/ingest.py`
- Regression guard: `tests/test_forge_oracle_ppmi_math.py::test_compute_ppmi_sparse_corpus_default_smoothing_emits_positive_signal`
- Sibling learning (silent-fallback semantics):
  [`offline-oracle-hash-pattern-2026-04-23.md`](offline-oracle-hash-pattern-2026-04-23.md)
