"""Two-phase signal-based scoring (SPEC §7 evolution).

Phase 1 — **Signal Detection**: each detector emits ``Signal`` objects with
a normalised 0–1 confidence.  Detectors are fully independent — tuning one
cannot break another.

Phase 2 — **Ranking**: cards are sorted by *breadth* (how many independent
signal types fired) then *depth* (aggregate confidence), replacing the old
additive weighted-bucket sum.

Migration path: ``signals_from_scored()`` adapts the existing bucket output
so both ranking paths can coexist behind a feature flag in ``engine.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from .scoring import BUCKETS

# ---------------------------------------------------------------------------
# §1  Signal tiers — derived from existing weight bands
# ---------------------------------------------------------------------------

#: Tier 1 (strong, original weight >= 8): counts as 1.0 breadth unit.
#: Tier 2 (medium, original weight 4–7): counts as 0.7 breadth unit.
#: Tier 3 (weak, original weight 1–3): counts as 0.3 breadth unit.
SIGNAL_TIER: dict[str, int] = {
    # Tier 1 — strong mechanical signal
    "port_match":             1,
    "lord":                   1,
    "amplifier":              1,
    "effect_resonance":       1,
    "replacement_resonance":  1,
    "opponent_forcing":       1,
    "resource_density":       1,
    "trigger_resonance":      1,
    "scaling":                1,
    # Tier 2 — moderate signal
    "cost_synergy":           2,
    "sacrifice_synergy":      2,
    "counter_ecosystem":      2,
    "untap_synergy":          2,
    "token_etb_payoff":       2,
    "graveyard_synergy":      2,
    "counter_synergy":        2,
    "stat_scaling":           2,
    "spellcast_density":      2,
    "etb_value":              2,
    "replacement_producer":   2,
    "deck_hints":             2,
    # Tier 3 — weak / contextual
    "catchall":               3,
    "chain":                  3,
    "strategic":              3,
    "staple":                 3,
    "internal_synergy":       3,
    "graph_metrics":          3,
    # Negative signal — has a tier for completeness but excluded from
    # breadth; subtracts from depth instead.
    "replacement":            1,
}

#: Breadth contribution per tier.
TIER_BREADTH_WEIGHT: dict[int, float] = {1: 1.0, 2: 0.7, 3: 0.3}

#: Maximum raw delta each detector can produce (used to normalise
#: confidence to 0–1).  Values match the ``*_WEIGHT`` constants in
#: ``scoring.py``; kept here so signal logic has no circular import.
_DETECTOR_MAX_DELTA: dict[str, float] = {
    "port_match":             10.0,
    "catchall":                1.0,
    "cost_synergy":            6.0,
    "sacrifice_synergy":       6.0,
    "counter_synergy":         4.0,
    "counter_ecosystem":       6.0,
    "untap_synergy":           6.0,
    "token_etb_payoff":        6.0,
    "graveyard_synergy":       6.0,
    "etb_value":               4.0,
    "trigger_resonance":       8.0,
    "stat_scaling":            4.0,
    "spellcast_density":       4.0,
    "opponent_forcing":       12.0,
    "scaling":                 6.0,
    "deck_hints":              4.0,
    "chain":                   3.0,
    "lord":                   12.0,
    "amplifier":              10.0,
    "internal_synergy":        1.0,
    "graph_metrics":           3.0,  # max of the graph sub-weights
    "strategic":               2.0,
    "resource_density":        8.0,
    "effect_resonance":       10.0,
    "replacement_resonance":  10.0,
    "replacement_producer":    4.0,
    "staple":                  1.0,
    "replacement":            10.0,  # negative bucket — special handling
}


# ---------------------------------------------------------------------------
# §2  Core dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """A single detection emitted by a detector."""

    signal_type: str          # bucket name, e.g. "port_match"
    confidence: float         # 0.0–1.0, normalised per-detector
    tier: int                 # 1 = strong, 2 = medium, 3 = weak
    evidence: dict[str, Any]  # the existing match-record dict


@dataclass
class CandidateSignals:
    """All signals for one candidate card.

    Mutable during construction, but consumers should treat it as
    read-only after ``score_all_candidates`` returns.
    """

    signals: list[Signal] = field(default_factory=list)

    # cached_property requires a non-frozen dataclass; the cache is
    # invalidated implicitly (never mutate ``signals`` after first access).

    @cached_property
    def signal_types(self) -> frozenset[str]:
        """Distinct signal types that fired for this candidate."""
        return frozenset(s.signal_type for s in self.signals)

    @cached_property
    def breadth(self) -> float:
        """Tier-weighted count of distinct signal types.

        Tier 1 signals contribute 1.0, tier 2 → 0.7, tier 3 → 0.3.
        ``replacement`` (negative) is excluded from breadth.
        """
        best_tier: dict[str, int] = {}
        for s in self.signals:
            if s.signal_type == "replacement":
                continue
            prev = best_tier.get(s.signal_type)
            if prev is None or s.tier < prev:
                best_tier[s.signal_type] = s.tier
        return sum(TIER_BREADTH_WEIGHT.get(t, 0.3) for t in best_tier.values())

    @cached_property
    def depth(self) -> float:
        """Tier-weighted aggregate confidence.

        Each signal type's max confidence is multiplied by its tier weight
        (tier 1 → 1.0, tier 2 → 0.7, tier 3 → 0.3) so that strong signals
        contribute more to depth than weak ones. This prevents a cluster
        of tier-3 signals from outscoring a single tier-1 signal on depth.

        ``replacement`` signals subtract from depth instead of adding.
        """
        per_type: dict[str, tuple[float, int]] = {}  # type → (max_conf, tier)
        for s in self.signals:
            if s.signal_type == "replacement":
                continue
            cur = per_type.get(s.signal_type)
            if cur is None or s.confidence > cur[0]:
                per_type[s.signal_type] = (s.confidence, s.tier)
        total = sum(
            conf * TIER_BREADTH_WEIGHT.get(tier, 0.3)
            for conf, tier in per_type.values()
        )
        # Subtract replacement penalty (max confidence of replacement signals)
        repl = max(
            (s.confidence for s in self.signals if s.signal_type == "replacement"),
            default=0.0,
        )
        return total - repl

    @cached_property
    def composite_score(self) -> float:
        """Single sortable number: ``breadth * 100 + depth``.

        The 100× multiplier ensures breadth always dominates over depth.
        Preserves the higher-is-better invariant for ``total_score``.
        """
        return self.breadth * 100.0 + self.depth

    def to_legacy_buckets(self) -> dict[str, float]:
        """Convert back to the old bucket dict for backward compatibility.

        When signals carry evidence with ``_delta``, the original delta is
        summed directly. Otherwise, ``confidence * max_delta`` approximates
        the original value.
        """
        from .scoring import BUCKETS as _BUCKETS  # avoid stale import

        buckets: dict[str, float] = dict.fromkeys(_BUCKETS, 0.0)
        buckets["total"] = 0.0
        for s in self.signals:
            ev = s.evidence
            raw = ev.get("_delta") if isinstance(ev, dict) else None
            if raw is not None:
                buckets[s.signal_type] += raw
            else:
                max_delta = _DETECTOR_MAX_DELTA.get(s.signal_type, 10.0)
                buckets[s.signal_type] += s.confidence * max_delta
        buckets["total"] = sum(buckets[b] for b in _BUCKETS)
        return buckets


# ---------------------------------------------------------------------------
# §3  Helpers
# ---------------------------------------------------------------------------


def delta_to_confidence(bucket: str, delta: float) -> float:
    """Map a raw weighted delta to 0.0–1.0 confidence."""
    max_d = _DETECTOR_MAX_DELTA.get(bucket, 10.0)
    if max_d == 0:
        return 0.0
    return min(max(delta / max_d, 0.0), 1.0)


def tier_for(signal_type: str) -> int:
    """Return the tier (1/2/3) for a signal type, defaulting to 3."""
    return SIGNAL_TIER.get(signal_type, 3)


# ---------------------------------------------------------------------------
# §4  Adapter — convert existing bucket+match output to signals
# ---------------------------------------------------------------------------


def signals_from_scored(
    buckets: dict[str, float],
    match_records: list[dict[str, Any]],
) -> CandidateSignals:
    """Adapter: convert a legacy (buckets, matches) pair into
    :class:`CandidateSignals`.

    When match records carry a ``_delta`` key (stamped by ``_add_bucket``),
    each record becomes an individual :class:`Signal` with confidence
    derived from that match's delta.  This gives per-interaction
    granularity: a card with 3 port_match interactions emits 3 signals,
    letting ``depth`` = max(conf) pick the strongest one rather than
    clamping a summed bucket at 1.0.

    For buckets without per-match records (e.g. the bucket is non-zero
    but no records reference it), a single fallback signal is emitted
    using the bucket total — this keeps the adapter lossless.
    """
    # Group match records by bucket
    records_by_bucket: dict[str, list[dict[str, Any]]] = {}
    for rec in match_records:
        bkt = rec.get("bucket", "")
        records_by_bucket.setdefault(bkt, []).append(rec)

    signals: list[Signal] = []
    emitted_buckets: set[str] = set()

    # Emit per-match signals for buckets that have match records
    for bkt, recs in records_by_bucket.items():
        if bkt not in _DETECTOR_MAX_DELTA:
            continue
        t = tier_for(bkt)
        for rec in recs:
            delta = rec.get("_delta")
            if delta is not None:
                conf = delta_to_confidence(bkt, abs(delta))
            else:
                # Legacy records without _delta: split bucket total evenly
                raw = buckets.get(bkt, 0.0)
                conf = delta_to_confidence(bkt, abs(raw) / max(len(recs), 1))
            signals.append(Signal(
                signal_type=bkt,
                confidence=conf,
                tier=t,
                evidence=rec,
            ))
        emitted_buckets.add(bkt)

    # Fallback: emit one signal for non-zero buckets without match records
    for bkt in BUCKETS:
        if bkt in emitted_buckets:
            continue
        raw = buckets.get(bkt, 0.0)
        if raw == 0.0:
            continue
        conf = delta_to_confidence(bkt, abs(raw))
        signals.append(Signal(
            signal_type=bkt,
            confidence=conf,
            tier=tier_for(bkt),
            evidence={"raw_delta": raw},
        ))

    return CandidateSignals(signals=signals)
