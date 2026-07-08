"""Per-commander activation-poverty metric (coverage instrument).

Zero scoring-path impact. Turns an ``engine.page()`` result into three
coverage scalars. ``earned_top30`` is the headline: of the surfaced top-30,
how many earned real mechanical credit (>=1 non-zero synergy bucket) rather
than a flat staple bonus. See
``docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md``.

"Earned" is defined by synergy-bucket PRESENCE, not by numeric subtraction:
``rank_bonus``/``cmc_bonus``/``circuit_bonus`` are folded into ``total`` but
never exposed as ``scores`` keys (only ``embedding`` is), so any ``scores``
key outside ``_NON_RULE_BUCKET_KEYS`` is a fired complement rule. See
``universal_scorer.py`` (``to_legacy_buckets``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Keys that appear in ``Recommendation.scores`` but are NOT complement-rule
#: contributions. Everything else is a synergy/anti-synergy bucket.
_NON_RULE_BUCKET_KEYS: frozenset[str] = frozenset({"staple", "embedding", "concentration_dampen", "total"})

#: A bucket counts as "fired" only above this magnitude. Bucket values are
#: sums of IDF-weighted floats; when a synergy and an anti-synergy row map to
#: the same bucket and cancel, IEEE-754 non-associativity can leave a residual
#: like 1e-16 instead of exactly 0.0. A bare ``!= 0.0`` would then count that
#: candidate as earned from float noise, so require a real (>1e-9) magnitude.
#: Genuine IDF contributions are ~0.1+, orders of magnitude above this floor.
_EARNED_EPS: float = 1e-9


def _synergy_buckets(scores: dict[str, float]) -> dict[str, float]:
    return {k: v for k, v in scores.items() if k not in _NON_RULE_BUCKET_KEYS and abs(v) > _EARNED_EPS}


def is_earned(rec) -> bool:
    """True iff at least one complement-rule bucket fired for this candidate."""
    return bool(_synergy_buckets(rec.scores))


@dataclass(frozen=True)
class CoverageMetrics:
    earned_top30: int
    n_scored_cands: int
    n_synergy_buckets: int


def compute_coverage(items: Sequence, *, top_n: int = 30) -> CoverageMetrics:
    """Coverage scalars for one commander's full ranking.

    ``items`` is an ``engine.page(limit=large).items`` list (rank-ordered).
    """
    # Single pass: build each candidate's synergy buckets exactly once
    # (``earned_top30`` needs only the first ``top_n`` in rank order, while
    # ``n_scored``/``bucket_kinds`` span the whole pool).
    earned_top30 = 0
    n_scored = 0
    bucket_kinds: set[str] = set()
    for i, rec in enumerate(items):
        buckets = _synergy_buckets(rec.scores)
        if not buckets:
            continue
        n_scored += 1
        bucket_kinds.update(buckets)
        if i < top_n:
            earned_top30 += 1
    return CoverageMetrics(
        earned_top30=earned_top30,
        n_scored_cands=n_scored,
        n_synergy_buckets=len(bucket_kinds),
    )
