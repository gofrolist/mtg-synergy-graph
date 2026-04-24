"""Rule-pair embedding-dedup diagnostic (plan 2026-04-23-003 Unit 6).

Pure numpy / pure-Python logic that consumes:

1. Tensor rows ``(commander, candidate, rule_id, contribution)`` already
   filtered to ``contribution > 0`` by the caller — one row per rule
   firing on a ``(commander, candidate)`` pair.
2. A ``vectors`` mapping ``card_name -> L2-normalized float32 vector``
   already loaded via ``embeddings.store.load_card_embeddings``.

and returns a sorted list of ``DedupPair`` rows for every pair of rules
whose mean activation-set embedding vectors have cosine similarity
above the threshold.

The algorithm is the numpy-matrix template from
``src/mtg_synergy_graph/bench/collinearity.py`` — build the activation
dict, filter by ``min_activation``, compute a per-rule mean vector, L2-
normalize, take pairwise dot products. No SQL here; the handler is
responsible for loading rows and vectors.

Why separate from the handler: this function is unit-testable without
a DB or argparse, matches the FR5 "read-only diagnostic" contract, and
mirrors ``bench/collinearity.py`` vs ``bench/handlers.py:handle_collinearity``.

Plan: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md Unit 6.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

import numpy as np

#: Guard for the per-rule mean-vector L2 norm. If the activation cards'
#: vectors happen to sum to (approximately) zero — possible when they
#: land on opposite directions in the SVD subspace — we can't L2-
#: normalize without blowing up. Below this threshold the rule is
#: silently excluded from the comparison (not an error — rare numeric
#: edge case).
_MIN_NORM: float = 1e-12


@dataclass(frozen=True)
class DedupPair:
    """One flagged rule pair.

    Immutable record matching the ``CollinearPair`` style in
    ``bench/collinearity.py``. ``cosine`` is the dot product of the
    two rules' L2-normalized mean activation vectors; equals cosine
    similarity by construction. ``activation_*_size`` records the
    number of distinct candidates in each rule's activation set so
    callers can tell a tiny-set false positive from a broadly-firing
    pair at a glance.
    """

    rule_a: str
    rule_b: str
    cosine: float
    activation_a_size: int
    activation_b_size: int


def compute_embedding_dedup(
    tensor_rows: Sequence[tuple[str, str, str, float]],
    vectors: Mapping[str, np.ndarray],
    *,
    threshold: float = 0.95,
    min_activation: int = 20,
) -> list[DedupPair]:
    """Flag rule pairs whose mean activation vectors exceed ``threshold``.

    Parameters
    ----------
    tensor_rows:
        Sequence of ``(commander, candidate, rule_id, contribution)``
        tuples. Callers must have already filtered to
        ``contribution > 0``; this function does not re-check. The
        ``commander`` field is unused here (it's part of the tensor
        primary key but the dedup signal is per-candidate only).
    vectors:
        ``{card_name: ndarray(128,)}`` mapping. Each vector is assumed
        L2-normalized (the store guarantees this post-Unit 2). Cards
        absent from this mapping are silently excluded from the
        per-rule mean — matching the graceful-fallback posture of
        ``embeddings.store.load_card_embeddings``.
    threshold:
        Cosine similarity cutoff. Pairs with ``cosine > threshold``
        are flagged. Default 0.95 per plan FR5.
    min_activation:
        Rules whose activation set has fewer than this many distinct
        candidates are dropped entirely — suppresses the "two tiny
        sets happen to lie near each other" false-positive class
        called out in the Risks table.

    Returns
    -------
    list[DedupPair]
        Sorted by cosine descending; lexicographic ``(rule_a, rule_b)``
        as the stable tiebreaker. Each rule pair appears at most once
        (``combinations``, not ``permutations``).
    """
    # Step 1: group tensor rows into per-rule activation sets of
    # distinct candidate names. Ignore the commander + contribution
    # fields — the dedup signal is purely "does rule X fire on
    # candidate Y at all, across any commander?".
    activation: dict[str, set[str]] = {}
    for _commander, candidate, rule_id, _contribution in tensor_rows:
        activation.setdefault(rule_id, set()).add(candidate)

    # Step 2: drop rules whose activation set is below the floor. Keep
    # the dict immutable-in-spirit by building a new one rather than
    # mutating in place — matches the "no mutation" posture in the
    # common coding-style rules.
    filtered_activation = {rule_id: cands for rule_id, cands in activation.items() if len(cands) >= min_activation}

    # Step 3: compute a per-rule L2-normalized mean vector. Any rule
    # whose cards are all missing from ``vectors`` (or sum to a near-
    # zero vector) is silently excluded — consumer docs call this out
    # as a graceful-fallback behavior rather than an error.
    mean_vecs: dict[str, np.ndarray] = {}
    sizes: dict[str, int] = {}
    for rule_id, cands in filtered_activation.items():
        found = [vectors[c] for c in cands if c in vectors]
        if not found:
            continue
        stacked = np.stack(found, axis=0)
        mean_vec = stacked.sum(axis=0) / len(found)
        norm = float(np.linalg.norm(mean_vec))
        if norm < _MIN_NORM:
            continue
        mean_vecs[rule_id] = (mean_vec / norm).astype(np.float32, copy=False)
        sizes[rule_id] = len(cands)

    # Step 4: pairwise cosine similarity over ``combinations`` so
    # ``(a, b)`` and ``(b, a)`` are treated as the same pair. Sort the
    # rule ids first so the output order is reproducible and stable
    # under caller insertion order.
    sorted_rule_ids = sorted(mean_vecs)
    pairs: list[DedupPair] = []
    for rule_a, rule_b in combinations(sorted_rule_ids, 2):
        cos = float(np.dot(mean_vecs[rule_a], mean_vecs[rule_b]))
        if cos > threshold:
            pairs.append(
                DedupPair(
                    rule_a=rule_a,
                    rule_b=rule_b,
                    cosine=cos,
                    activation_a_size=sizes[rule_a],
                    activation_b_size=sizes[rule_b],
                )
            )

    # Step 5: sort by -cosine with lexicographic tiebreaker. The
    # tiebreaker ensures identical cosines (rare but possible in
    # synthetic tests) render in a deterministic order.
    pairs.sort(key=lambda p: (-p.cosine, p.rule_a, p.rule_b))
    return pairs
