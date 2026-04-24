"""PPMI with RAPM-style lineup adjustment + Laplace smoothing.

Pure math — no SQLite, no filesystem. Consumers provide an iterable
of decks (each a set of string signatures) and a minimum-evidence
threshold. Returns a table keyed by canonical (a, b) pairs with a < b.

Formula:

    For each deck d (a ``frozenset`` of distinct signatures):
        pair_weight_d = 2 / (|d| * (|d| - 1))   if |d| >= 2 else 0
        marginal_weight_d = 1 / |d|             if |d| >= 1 else 0

    For each unordered pair (a, b) in d with a < b:
        joint_weighted[(a, b)] += pair_weight_d
    For each a in d:
        marginal_weighted[a] += marginal_weight_d

    N = sum(1 for d in decks if len(d) >= 2)   # decks contributing ≥ 1 pair
    V = |distinct signatures|
    P(a, b) = joint_weighted[(a, b)] / N
    P(a)    = marginal_weighted[a]   / N        (each deck sums to 1 in marginal-weight)
    PMI(a, b) = log( (P(a, b) + k) / ((P(a) + V*k) * (P(b) + V*k)) )
    PPMI(a, b) = max(PMI(a, b), 0)

The per-deck normalization (``pair_weight_d = 2 / |d|(|d|-1)``) is the
RAPM-style lineup adjustment: a 100-card deck contributes the same
total pair-weight as a 3-card deck, so ubiquitous mana rocks in big
decks don't swamp the signal from selective pairs in small decks.

See plan 2026-04-23-002-feat-forge-second-oracle Unit 4.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

#: Canonical default for add-k Laplace smoothing. Kept at zero because the
#: Forge precon corpus is too sparse (~1400 subkinds × ~667 decks) for a
#: positive ``k`` to do anything except overwhelm marginals and zero out
#: every PMI. See ``compute_ppmi_table`` docstring for the derivation.
#: Imported by the CLI / ingest / bench layers so a future adjustment
#: touches exactly one site.
DEFAULT_SMOOTHING_K: float = 0.0


@dataclass(frozen=True, slots=True)
class PpmiEntry:
    """One row of the computed PPMI table."""

    signature_a: str
    signature_b: str
    ppmi: float
    decks_count: int  # raw integer count of decks containing both (for threshold + display)


def _pmi(
    p_joint: float,
    p_a: float,
    p_b: float,
    *,
    smoothing_k: float,
    vocab_size: int = 1,
) -> float:
    """Laplace-smoothed pointwise mutual information.

    With ``smoothing_k = 0.0`` (the default), division-by-zero is
    structurally impossible inside ``compute_ppmi_table``: only pairs
    that co-occur at least once enter this function, so ``p_joint > 0``
    and each participating signature has ``marginal_weighted > 0``. The
    ``numerator <= 0`` / ``denominator <= 0`` guard below is a defensive
    fallback for direct callers, not a load-bearing invariant of the
    batch computation.
    """
    numerator = p_joint + smoothing_k
    denominator = (p_a + smoothing_k * vocab_size) * (p_b + smoothing_k * vocab_size)
    if numerator <= 0.0 or denominator <= 0.0:
        return -math.inf
    return math.log(numerator / denominator)


def compute_ppmi_table(
    decks: Iterable[frozenset[str]],
    *,
    min_decks_count: int,
    smoothing_k: float,
) -> dict[tuple[str, str], PpmiEntry]:
    """Aggregate PPMI + decks_count per signature pair across a corpus.

    Returns ``{(a, b): PpmiEntry}`` with ``a < b`` and only entries
    whose raw ``decks_count >= min_decks_count``.

    ``smoothing_k`` is the Laplace/add-k smoothing constant, applied in
    **probability space** (see :func:`_pmi`): ``numerator = p_joint +
    k`` and ``denominator = (p_a + k*V)(p_b + k*V)``. The default is
    ``0.0`` — every positive ``k`` drives ``k*V`` to dominate the
    marginal probabilities on sparse corpora (V ≈ 1400 subkinds × p_a ≈
    1e-5 → adding k=0.5 makes every marginal look like 25, and the
    denominator ≈ 62500 forces PMI negative for every pair).

    Zero-division when ``k=0`` is structurally impossible here: only
    pairs that co-occur at least once enter the accumulator, so every
    retained pair has ``p_joint > 0`` and both signatures have
    ``marginal_weighted > 0``. The ``max(pmi, 0.0)`` clamp below
    handles the separate case of negative PMI (rarely co-occurring
    pairs) rather than zero-division.

    A positive ``k`` is only useful when the vocabulary is small enough
    (roughly ``V * k ≪ typical_marginal_probability``) or the corpus is
    large enough to overwhelm the smoothing pull. The Forge precon
    corpus (667 decks, ~1400 subkinds) satisfies neither condition.
    """
    deck_list: list[frozenset[str]] = list(decks)

    # Aggregate weighted joint + marginal counts; also track raw decks_count.
    joint_weighted: dict[tuple[str, str], float] = {}
    raw_joint_count: dict[tuple[str, str], int] = {}
    marginal_weighted: dict[str, float] = {}
    vocab: set[str] = set()
    n_contributing_decks = 0

    for deck in deck_list:
        size = len(deck)
        if size == 0:
            continue
        vocab.update(deck)
        marginal_w = 1.0 / size
        for sig in deck:
            marginal_weighted[sig] = marginal_weighted.get(sig, 0.0) + marginal_w

        if size < 2:
            continue
        n_contributing_decks += 1
        sorted_sigs = sorted(deck)
        pair_w = 2.0 / (size * (size - 1))
        for i, a in enumerate(sorted_sigs):
            for b in sorted_sigs[i + 1 :]:
                key = (a, b)
                joint_weighted[key] = joint_weighted.get(key, 0.0) + pair_w
                raw_joint_count[key] = raw_joint_count.get(key, 0) + 1

    if n_contributing_decks == 0:
        return {}

    V = len(vocab)
    n = float(n_contributing_decks)
    result: dict[tuple[str, str], PpmiEntry] = {}

    for (a, b), raw_count in raw_joint_count.items():
        if raw_count < min_decks_count:
            continue
        p_joint = joint_weighted[(a, b)] / n
        p_a = marginal_weighted[a] / n
        p_b = marginal_weighted[b] / n
        pmi = _pmi(p_joint, p_a, p_b, smoothing_k=smoothing_k, vocab_size=V)
        ppmi = max(pmi, 0.0)
        result[(a, b)] = PpmiEntry(
            signature_a=a,
            signature_b=b,
            ppmi=ppmi,
            decks_count=raw_count,
        )

    return result
