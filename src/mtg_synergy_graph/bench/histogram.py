"""Rank-shuffle histogram + 5-verdict rollup (Unit 5).

Replaces the binary TRIVIAL verdict that ``_audit_rule_impact.py`` used
with a five-bucket per-commander histogram (no_change,
rank_shuffle_within_top30, rank_shuffle_across_top30_boundary,
hi_syn_gain, hi_syn_loss). The original five-category rubric
(positive / MARGINAL / TRIVIAL / CONTENTIOUS / HARMFUL) is preserved as
a roll-up so downstream consumers — commit-message conventions,
docs/RULE_HISTORY.md entries — keep working.

The plan's Key Technical Decisions section calls out:

    TRIVIAL is redefined: only applies when all commanders are
    `no_change` (not when rank_shuffles net to zero).

so rank shuffles that would previously have been buried as TRIVIAL
(and informally noted in auto-memory
``feedback_audit_metric_too_coarse.md``) now surface as
``rank_shuffle_*`` buckets with their own verdict path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture


def _top_n_candidates(scores: dict[str, float], n: int) -> list[str]:
    """Top-N candidate names by score desc, tiebreaking by name for stability.

    Lives here rather than in ``report`` to avoid a circular import:
    ``report`` imports histogram machinery, and this helper is used by
    both modules. It is deliberately tiny so duplication cost is near
    zero.
    """
    return [name for name, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


class Bucket(StrEnum):
    """Per-commander movement classification."""

    NO_CHANGE = "no_change"
    RANK_SHUFFLE_WITHIN_TOP30 = "rank_shuffle_within_top30"
    RANK_SHUFFLE_ACROSS_TOP30_BOUNDARY = "rank_shuffle_across_top30_boundary"
    HI_SYN_GAIN = "hi_syn_gain"
    HI_SYN_LOSS = "hi_syn_loss"


class Verdict(StrEnum):
    """Five-category audit verdict — preserved for commit-log compat."""

    POSITIVE = "positive"
    MARGINAL = "MARGINAL"
    TRIVIAL = "TRIVIAL"
    CONTENTIOUS = "CONTENTIOUS"
    HARMFUL = "HARMFUL"


#: Threshold below which a score delta is considered noise rather than a
#: real MARGINAL improvement. Tuned against the audit verdict thresholds
#: in ``scripts/_audit_rule_impact.py`` (historically "ratio sum/touched
#: < 0.1" for MARGINAL); we model the same intent with an absolute
#: per-commander floor because per-rule touched counts aren't available
#: without running the ablation pass.
_MARGINAL_DELTA_THRESHOLD = 0.001

#: A commander whose hi_syn_hits dropped by this much is considered a
#: material hi_syn_loss for CONTENTIOUS verdict purposes. Mirrors the
#: 0.05 NDCG threshold in the original rubric at the hi_syn granularity.
_HI_SYN_LOSS_MATERIAL = 2


@dataclass(frozen=True)
class Histogram:
    """Per-bucket commander counts + commander-name lists."""

    no_change: int = 0
    rank_shuffle_within_top30: int = 0
    rank_shuffle_across_top30_boundary: int = 0
    hi_syn_gain: int = 0
    hi_syn_loss: int = 0

    #: Per-bucket commander rosters. Kept short (first ~10 per bucket)
    #: so the histogram can be rendered inline without bloating reports.
    samples: dict[Bucket, tuple[str, ...]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return (
            self.no_change
            + self.rank_shuffle_within_top30
            + self.rank_shuffle_across_top30_boundary
            + self.hi_syn_gain
            + self.hi_syn_loss
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "no_change": self.no_change,
            "rank_shuffle_within_top30": self.rank_shuffle_within_top30,
            "rank_shuffle_across_top30_boundary": self.rank_shuffle_across_top30_boundary,
            "hi_syn_gain": self.hi_syn_gain,
            "hi_syn_loss": self.hi_syn_loss,
            "samples": {b.value: list(names) for b, names in self.samples.items()},
        }


def classify_commander(pinned: FixtureEntry, live: FixtureEntry | None) -> Bucket:
    """Classify one commander's movement into a histogram bucket.

    Priority: hi_syn gain/loss (if both sides carry hi_syn data) wins
    over movement buckets, since hi_syn represents a semantic gain/loss
    rather than a reshuffle. A commander can only land in one bucket per
    audit — the bucket choice encodes the most salient thing that
    happened.
    """
    pinned_hs = pinned.legacy.get("hi_syn_hits")
    live_hs = live.legacy.get("hi_syn_hits") if live is not None else None
    if isinstance(pinned_hs, int) and isinstance(live_hs, int) and live_hs != pinned_hs:
        if live_hs > pinned_hs:
            return Bucket.HI_SYN_GAIN
        return Bucket.HI_SYN_LOSS

    pinned_top30 = _top_n_candidates(pinned.scores, 30)
    live_top30 = _top_n_candidates(live.scores if live is not None else {}, 30)

    if pinned_top30 == live_top30:
        # Same identity, same order. Still check for score drift on
        # cards inside the top-30: a score change that preserves the
        # full ranking is still a real change (e.g., uniform additive
        # bonus) and the audit should not call it "no change."
        if live is None or not _scores_match_bitwise(pinned, live, pinned_top30):
            return Bucket.RANK_SHUFFLE_WITHIN_TOP30
        return Bucket.NO_CHANGE

    if set(pinned_top30) == set(live_top30):
        # Same set, different order.
        return Bucket.RANK_SHUFFLE_WITHIN_TOP30

    # Membership changed.
    return Bucket.RANK_SHUFFLE_ACROSS_TOP30_BOUNDARY


def _scores_match_bitwise(pinned: FixtureEntry, live: FixtureEntry, names: list[str]) -> bool:
    """Every candidate in ``names`` has an identical live score.

    Helper kept local because the bitwise-equality nuance differs from
    the fixture.assert_identity path (which reports all mismatches).
    """
    return all(live.scores.get(name) == pinned.scores.get(name) for name in names)


def compute_histogram(pinned: PinnedFixture, live: PinnedFixture) -> Histogram:
    """Bucket every commander in ``pinned`` by movement."""
    live_by_cmdr = {e.commander: e for e in live.entries}
    counts = dict.fromkeys(Bucket, 0)
    samples: dict[Bucket, list[str]] = {b: [] for b in Bucket}

    for pinned_entry in pinned.entries:
        live_entry = live_by_cmdr.get(pinned_entry.commander)
        bucket = classify_commander(pinned_entry, live_entry)
        counts[bucket] += 1
        if len(samples[bucket]) < 10:
            samples[bucket].append(pinned_entry.commander)

    return Histogram(
        no_change=counts[Bucket.NO_CHANGE],
        rank_shuffle_within_top30=counts[Bucket.RANK_SHUFFLE_WITHIN_TOP30],
        rank_shuffle_across_top30_boundary=counts[Bucket.RANK_SHUFFLE_ACROSS_TOP30_BOUNDARY],
        hi_syn_gain=counts[Bucket.HI_SYN_GAIN],
        hi_syn_loss=counts[Bucket.HI_SYN_LOSS],
        samples={b: tuple(samples[b]) for b in Bucket},
    )


def rollup_to_verdict(hist: Histogram, aggregate_score_delta: float) -> Verdict:
    """Derive the 5-category audit verdict from the histogram + delta.

    Preserves the rubric from ``scripts/_audit_rule_impact.py:16-31``:

    * ``TRIVIAL`` — no commander moved (``no_change == total``).
    * ``HARMFUL`` — aggregate score dropped AND no hi_syn_gain lifted
      a commander out of the loss cohort.
    * ``CONTENTIOUS`` — aggregate dropped AND ≥1 hi_syn_gain (wins
      offset losses on at least one golden commander).
    * ``MARGINAL`` — aggregate up but below ``_MARGINAL_DELTA_THRESHOLD``.
    * ``POSITIVE`` — aggregate up above the threshold, no material
      hi_syn_loss.

    NDCG-granularity thresholds (±0.05) from the legacy rubric aren't
    available here without EDHREC data; we approximate via hi_syn
    counts, which the legacy fixture already carries.
    """
    if hist.total > 0 and hist.no_change == hist.total:
        return Verdict.TRIVIAL

    if aggregate_score_delta < 0:
        if hist.hi_syn_gain >= 1:
            return Verdict.CONTENTIOUS
        return Verdict.HARMFUL

    if aggregate_score_delta <= _MARGINAL_DELTA_THRESHOLD:
        return Verdict.MARGINAL

    if hist.hi_syn_loss >= _HI_SYN_LOSS_MATERIAL:
        # Aggregate positive but several commanders lost hi_syn hits —
        # the rule gains on some and costs on others. CONTENTIOUS.
        return Verdict.CONTENTIOUS

    return Verdict.POSITIVE
