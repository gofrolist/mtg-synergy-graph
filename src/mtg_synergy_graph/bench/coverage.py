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


def _synergy_buckets(scores: dict[str, float]) -> dict[str, float]:
    return {k: v for k, v in scores.items() if k not in _NON_RULE_BUCKET_KEYS and v != 0.0}


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
    earned_top30 = sum(1 for rec in items[:top_n] if is_earned(rec))
    n_scored = 0
    bucket_kinds: set[str] = set()
    for rec in items:
        buckets = _synergy_buckets(rec.scores)
        if buckets:
            n_scored += 1
            bucket_kinds.update(buckets)
    return CoverageMetrics(
        earned_top30=earned_top30,
        n_scored_cands=n_scored,
        n_synergy_buckets=len(bucket_kinds),
    )
