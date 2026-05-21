"""Tests for ``bench.py audit --per-commander-ndcg`` reporting handler.

The handler emits per-commander NDCG@30 deltas (live - pinned) sorted
ascending so the worst regressions surface first. It exists to support
the BM25 IDF probe's per-commander prerequisite gate (any commander
losing more than 0.05 NDCG@30 → DECLINE).

Tests use synthetic fixtures + a stub EDHREC labels source to keep
the unit isolated from the production DB.
"""

from __future__ import annotations

from mtg_synergy_graph.bench.per_commander_ndcg import (
    PerCommanderNdcgRow,
    render_per_commander_ndcg_markdown,
    sort_rows_ascending,
)


def test_sort_rows_ascending_worst_first():
    """Worst-regression commanders surface first."""
    rows = [
        PerCommanderNdcgRow(commander="A", pinned_ndcg=0.30, live_ndcg=0.31, delta=+0.01),
        PerCommanderNdcgRow(commander="B", pinned_ndcg=0.40, live_ndcg=0.35, delta=-0.05),
        PerCommanderNdcgRow(commander="C", pinned_ndcg=0.20, live_ndcg=0.20, delta=0.00),
    ]
    sorted_rows = sort_rows_ascending(rows)
    assert [r.commander for r in sorted_rows] == ["B", "C", "A"]


def test_sort_rows_ascending_stable_on_ties():
    """Ties broken by commander name for stable output."""
    rows = [
        PerCommanderNdcgRow(commander="Z", pinned_ndcg=0.5, live_ndcg=0.5, delta=0.0),
        PerCommanderNdcgRow(commander="A", pinned_ndcg=0.5, live_ndcg=0.5, delta=0.0),
        PerCommanderNdcgRow(commander="M", pinned_ndcg=0.5, live_ndcg=0.5, delta=0.0),
    ]
    sorted_rows = sort_rows_ascending(rows)
    assert [r.commander for r in sorted_rows] == ["A", "M", "Z"]


def test_render_markdown_table_shape():
    """Markdown table has expected columns and rows."""
    rows = [
        PerCommanderNdcgRow(commander="Korvold, Fae-Cursed King", pinned_ndcg=0.300, live_ndcg=0.250, delta=-0.050),
        PerCommanderNdcgRow(commander="Atraxa, Praetors' Voice", pinned_ndcg=0.200, live_ndcg=0.220, delta=+0.020),
    ]
    md = render_per_commander_ndcg_markdown(
        rows,
        fixture_path="tests/fixtures/golden_set_run_500.json",
        config_hash="abc123def456",
    )
    assert "# bench.py audit --per-commander-ndcg" in md
    assert "tests/fixtures/golden_set_run_500.json" in md
    assert "abc123def456" in md or "abc123def456"[:12] in md
    # Both commanders in output; worst-first ordering preserved by caller.
    assert "Korvold, Fae-Cursed King" in md
    assert "Atraxa, Praetors' Voice" in md
    # Table header: commander, pinned, live, delta
    assert "commander" in md.lower()
    assert "pinned" in md.lower()
    assert "live" in md.lower()
    assert "delta" in md.lower()


def test_render_markdown_handles_empty_rows():
    """Empty fixture (or audit produced no commanders) yields informative empty output."""
    md = render_per_commander_ndcg_markdown(
        [],
        fixture_path="tests/fixtures/empty.json",
        config_hash="zerohash",
    )
    assert "no commanders" in md.lower() or "0 commanders" in md.lower() or "empty" in md.lower()


def test_per_commander_ndcg_row_delta_consistency():
    """Delta should equal live - pinned (sanity check on the data shape)."""
    row = PerCommanderNdcgRow(commander="X", pinned_ndcg=0.40, live_ndcg=0.38, delta=-0.02)
    assert abs(row.delta - (row.live_ndcg - row.pinned_ndcg)) < 1e-9


def test_render_markdown_includes_threshold_legend():
    """Output should signal the per-commander threshold (-0.05) for context."""
    rows = [
        PerCommanderNdcgRow(commander="X", pinned_ndcg=0.5, live_ndcg=0.4, delta=-0.10),
        PerCommanderNdcgRow(commander="Y", pinned_ndcg=0.5, live_ndcg=0.5, delta=0.0),
    ]
    md = render_per_commander_ndcg_markdown(
        rows,
        fixture_path="tests/fixtures/golden_set_run_500.json",
        config_hash="hash",
    )
    # Legend or summary should mention the -0.05 threshold so reviewers
    # can quickly spot violations.
    assert "0.05" in md
