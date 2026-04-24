"""Truncated SVD + row-wise L2 normalization (Unit 1).

Given a dense TF-IDF matrix, produce a fixed-dimension per-row
embedding by taking the top-``k`` left singular vectors scaled by the
corresponding singular values, then L2-normalizing each row. The
normalization means later cosine-similarity lookups reduce to a dot
product (see the Unit 5 inference-path reader).

No sklearn / scipy dependency — ``numpy.linalg.svd(full_matrices=False)``
is sufficient at the 32k × ~5k scale this codebase targets. If profiling
later shows this path is the bottleneck, an optional ``scipy.sparse``
path can be added behind a dependency extra; today's default is pure
numpy.

Plan: ``docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md``
Unit 1.
"""

from __future__ import annotations

import numpy as np

#: Guard against division-by-zero during L2 normalization. A row with
#: zero norm (e.g., a card with zero tokens) stays all-zeros after the
#: divide instead of becoming ``inf`` / ``NaN``. Picked to be well below
#: float32 precision so it never subtly rescales a legitimate row.
_ZERO_NORM_EPSILON: float = 1e-12


def truncated_svd(tfidf: np.ndarray, k: int = 128) -> np.ndarray:
    """Project ``tfidf`` to ``k`` dimensions via SVD and L2-normalize rows.

    Returns an ``(n_rows, k)`` ``float64`` array where each row has unit
    L2 norm (or is all-zeros if the input row had no tokens).

    Behavior notes:

    * ``k`` is clamped to ``min(k, n_rows, n_cols)``; a small corpus
      where ``min(n_rows, n_cols) < k`` still produces a valid (but
      lower-rank) embedding rather than raising.
    * Zero-norm rows survive the normalization as all-zeros; callers
      that skip them should check ``np.linalg.norm(row)`` themselves.
    * Deterministic: ``numpy.linalg.svd`` is a deterministic LAPACK
      call, so the same input bytes produce the same output bytes
      within a numpy build.
    """
    if tfidf.ndim != 2:
        raise ValueError(f"tfidf must be 2-D; got shape {tfidf.shape}")
    n_rows, n_cols = tfidf.shape
    if n_rows == 0 or n_cols == 0:
        # Degenerate corpus — return an (n_rows, k) zero matrix so
        # downstream callers get a consistent shape.
        return np.zeros((n_rows, max(k, 0)), dtype=np.float64)

    effective_k = min(k, n_rows, n_cols)
    if effective_k < 1:
        return np.zeros((n_rows, max(k, 0)), dtype=np.float64)

    # full_matrices=False gives the thin SVD: U is (n_rows, r),
    # s is (r,), Vt is (r, n_cols) where r = min(n_rows, n_cols).
    u, s, _vt = np.linalg.svd(tfidf, full_matrices=False)

    # Take the top-k left singular vectors scaled by the singular
    # values. The result is an (n_rows, k) matrix whose rows are the
    # card embeddings prior to normalization.
    embedding = u[:, :effective_k] * s[:effective_k]

    # If effective_k < k, pad with zero columns so downstream code
    # always sees the requested dimension (fixed-size vectors simplify
    # the storage schema).
    if effective_k < k:
        pad = np.zeros((n_rows, k - effective_k), dtype=embedding.dtype)
        embedding = np.concatenate([embedding, pad], axis=1)

    # Row-wise L2 normalization. ``max(norm, eps)`` keeps zero-norm
    # rows at zero instead of producing NaN.
    norms = np.linalg.norm(embedding, axis=1, keepdims=True)
    safe_norms = np.maximum(norms, _ZERO_NORM_EPSILON)
    normalized = embedding / safe_norms
    # Force the eps-divided zero rows back to true zero — otherwise
    # they'd carry tiny residuals from floating-point arithmetic.
    zero_mask = (norms <= _ZERO_NORM_EPSILON).ravel()
    if zero_mask.any():
        normalized[zero_mask] = 0.0
    return normalized
