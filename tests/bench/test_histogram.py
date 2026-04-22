"""Unit 5 — rank-shuffle histogram + verdict rollup tests."""

from __future__ import annotations

import pytest

from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
from mtg_synergy_graph.bench.histogram import (
    Bucket,
    Histogram,
    Verdict,
    classify_commander,
    compute_histogram,
    rollup_to_verdict,
)

# ---------------------------------------------------------------------------
# classify_commander
# ---------------------------------------------------------------------------


def test_classify_no_change_for_identical_top30() -> None:
    """Identical scores across top-30 == NO_CHANGE bucket."""
    scores = {f"cand_{i}": 1.0 - i * 0.01 for i in range(35)}
    pinned = FixtureEntry(commander="C", scores=scores)
    live = FixtureEntry(commander="C", scores=dict(scores))
    assert classify_commander(pinned, live) == Bucket.NO_CHANGE


def test_classify_score_drift_preserving_top30_order() -> None:
    """Same ranking, different scores → RANK_SHUFFLE_WITHIN_TOP30.

    Even if the ordering is identical, a score change matters — a
    uniform additive shift still has to be auditable, not buried as
    no_change.
    """
    pinned_scores = {f"cand_{i}": 1.0 - i * 0.01 for i in range(35)}
    live_scores = {k: v + 0.1 for k, v in pinned_scores.items()}
    pinned = FixtureEntry(commander="C", scores=pinned_scores)
    live = FixtureEntry(commander="C", scores=live_scores)
    assert classify_commander(pinned, live) == Bucket.RANK_SHUFFLE_WITHIN_TOP30


def test_classify_reorder_within_same_top30() -> None:
    """Same top-30 members, different ranks → RANK_SHUFFLE_WITHIN_TOP30."""
    pinned_scores = {f"cand_{i}": 1.0 - i * 0.01 for i in range(35)}
    live_scores = dict(pinned_scores)
    # Swap cand_0 and cand_1 in ranking.
    live_scores["cand_0"] = 0.99
    live_scores["cand_1"] = 1.0
    pinned = FixtureEntry(commander="C", scores=pinned_scores)
    live = FixtureEntry(commander="C", scores=live_scores)
    assert classify_commander(pinned, live) == Bucket.RANK_SHUFFLE_WITHIN_TOP30


def test_classify_boundary_crossing() -> None:
    """Top-30 membership changes → RANK_SHUFFLE_ACROSS_TOP30_BOUNDARY."""
    pinned_scores = {f"cand_{i}": 1.0 - i * 0.01 for i in range(35)}
    live_scores = dict(pinned_scores)
    # Push cand_29 out; pull cand_30 in.
    live_scores["cand_29"] = 0.0
    live_scores["cand_30"] = 1.5
    pinned = FixtureEntry(commander="C", scores=pinned_scores)
    live = FixtureEntry(commander="C", scores=live_scores)
    assert classify_commander(pinned, live) == Bucket.RANK_SHUFFLE_ACROSS_TOP30_BOUNDARY


def test_classify_hi_syn_gain_wins_over_rank_shuffle() -> None:
    """hi_syn gain is more salient than rank shuffle; wins in the bucket race."""
    pinned_scores = {f"cand_{i}": 1.0 - i * 0.01 for i in range(35)}
    live_scores = dict(pinned_scores)
    live_scores["cand_29"] = 0.0  # membership change, would be boundary_crossing
    live_scores["cand_30"] = 1.5
    pinned = FixtureEntry(commander="C", scores=pinned_scores, legacy={"hi_syn_hits": 1})
    live = FixtureEntry(commander="C", scores=live_scores, legacy={"hi_syn_hits": 3})
    assert classify_commander(pinned, live) == Bucket.HI_SYN_GAIN


def test_classify_hi_syn_loss_preferred_over_shuffle() -> None:
    """hi_syn loss classification similarly takes precedence."""
    pinned = FixtureEntry(commander="C", scores={"a": 1.0}, legacy={"hi_syn_hits": 5})
    live = FixtureEntry(commander="C", scores={"a": 1.0}, legacy={"hi_syn_hits": 2})
    assert classify_commander(pinned, live) == Bucket.HI_SYN_LOSS


def test_classify_live_missing_is_boundary_crossing() -> None:
    """Missing live entry is treated as max movement: boundary crossing."""
    pinned = FixtureEntry(commander="C", scores={"a": 1.0})
    assert classify_commander(pinned, None) == Bucket.RANK_SHUFFLE_ACROSS_TOP30_BOUNDARY


# ---------------------------------------------------------------------------
# compute_histogram
# ---------------------------------------------------------------------------


def test_compute_histogram_counts_per_bucket() -> None:
    """Multi-commander fixture produces per-bucket counts + samples."""
    # Three commanders: one no_change, one rank shuffle, one boundary crossing.
    no_change_scores = {f"c{i}": 1.0 - i * 0.01 for i in range(35)}
    shuffled_scores = dict(no_change_scores)
    boundary_scores = dict(no_change_scores)
    boundary_scores["c29"] = 0.0
    boundary_scores["c30"] = 2.0

    # Make the shuffled one have a swapped rank by giving c0 and c1 tied
    # scores with different tiebreak (name order).
    shuffled_scores["c0"] = 0.99  # lower than c1's 0.99, forcing reordering

    pinned = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[
            FixtureEntry(commander="NC", scores=no_change_scores),
            FixtureEntry(commander="SH", scores=no_change_scores),
            FixtureEntry(commander="BN", scores=no_change_scores),
        ],
    )
    live = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[
            FixtureEntry(commander="NC", scores=dict(no_change_scores)),
            FixtureEntry(commander="SH", scores=shuffled_scores),
            FixtureEntry(commander="BN", scores=boundary_scores),
        ],
    )

    hist = compute_histogram(pinned, live)
    assert hist.no_change == 1
    assert hist.rank_shuffle_within_top30 == 1
    assert hist.rank_shuffle_across_top30_boundary == 1
    assert hist.total == 3

    assert hist.samples is not None
    assert "NC" in hist.samples[Bucket.NO_CHANGE]
    assert "BN" in hist.samples[Bucket.RANK_SHUFFLE_ACROSS_TOP30_BOUNDARY]


def test_histogram_total_property() -> None:
    """``total`` sums across all five buckets."""
    h = Histogram(no_change=1, rank_shuffle_within_top30=2, hi_syn_gain=3, samples={})
    assert h.total == 6


# ---------------------------------------------------------------------------
# rollup_to_verdict
# ---------------------------------------------------------------------------


def test_verdict_trivial_when_all_no_change() -> None:
    """All commanders in no_change → TRIVIAL (tightened vs legacy rubric)."""
    hist = Histogram(no_change=100, samples={})
    assert rollup_to_verdict(hist, 0.0) == Verdict.TRIVIAL


def test_verdict_not_trivial_on_rank_shuffle_even_if_delta_zero() -> None:
    """Rank shuffles that net to zero are NOT TRIVIAL anymore.

    Precisely the regression the plan's Key Technical Decisions section
    promised to fix: "TRIVIAL is redefined: only applies when all
    commanders are ``no_change`` (not when rank_shuffles net to zero)."
    """
    hist = Histogram(no_change=50, rank_shuffle_within_top30=50, samples={})
    assert rollup_to_verdict(hist, 0.0) != Verdict.TRIVIAL


def test_verdict_harmful_when_aggregate_drops_without_hi_syn_gain() -> None:
    hist = Histogram(no_change=50, rank_shuffle_across_top30_boundary=10, hi_syn_loss=5, samples={})
    assert rollup_to_verdict(hist, -0.05) == Verdict.HARMFUL


def test_verdict_contentious_when_drop_but_hi_syn_gain() -> None:
    """Aggregate drop offset by at least one hi_syn gain = CONTENTIOUS."""
    hist = Histogram(no_change=50, hi_syn_gain=2, hi_syn_loss=3, samples={})
    assert rollup_to_verdict(hist, -0.03) == Verdict.CONTENTIOUS


def test_verdict_marginal_when_gain_below_threshold() -> None:
    """Positive delta under the threshold → MARGINAL."""
    hist = Histogram(no_change=90, rank_shuffle_within_top30=10, samples={})
    assert rollup_to_verdict(hist, 0.0005) == Verdict.MARGINAL


def test_verdict_positive_on_clean_uplift() -> None:
    hist = Histogram(no_change=80, rank_shuffle_within_top30=15, hi_syn_gain=5, samples={})
    assert rollup_to_verdict(hist, 0.05) == Verdict.POSITIVE


def test_verdict_contentious_when_gain_but_material_hi_syn_loss() -> None:
    """Positive aggregate but ≥2 hi_syn_loss commanders → CONTENTIOUS.

    Captures the 2026-04-20 Prossh vs Breya/Zidane pattern from the
    project's real history (see the token_etb_damage comment in
    universal_scorer.py).
    """
    hist = Histogram(no_change=50, hi_syn_gain=1, hi_syn_loss=3, samples={})
    assert rollup_to_verdict(hist, 0.1) == Verdict.CONTENTIOUS


# ---------------------------------------------------------------------------
# End-to-end integration via build_report
# ---------------------------------------------------------------------------


def test_build_report_attaches_histogram_and_verdict() -> None:
    """AuditReport carries the histogram + verdict populated by build_report."""
    from mtg_synergy_graph.bench.report import build_report

    pinned = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={f"a{i}": 1.0 - i * 0.01 for i in range(35)})],
    )
    live = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={f"a{i}": 1.0 - i * 0.01 for i in range(35)})],
    )
    report = build_report("baseline.json", pinned, live)
    assert report.histogram.no_change == 1
    assert report.verdict == Verdict.TRIVIAL


def test_report_renders_verdict_in_markdown() -> None:
    """Markdown output carries the verdict + histogram table."""
    from mtg_synergy_graph.bench.report import build_report

    pinned = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={"a": 1.0})],
    )
    live = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={"a": 1.0})],
    )
    report = build_report("baseline.json", pinned, live)
    md = report.to_markdown()
    assert "Verdict" in md
    assert "TRIVIAL" in md
    assert "Histogram" in md
    assert "no_change" in md


def test_report_json_carries_verdict_and_histogram() -> None:
    import json

    from mtg_synergy_graph.bench.report import build_report

    pinned = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={"a": 1.0})],
    )
    live = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={"a": 1.5})],
    )
    report = build_report("baseline.json", pinned, live)
    parsed = json.loads(report.to_json())
    assert "verdict" in parsed
    assert "histogram" in parsed
    assert parsed["histogram"]["rank_shuffle_within_top30"] == pytest.approx(1)
