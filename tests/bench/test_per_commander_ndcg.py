"""Tests for the Unit 3 report plumbing in ``--per-commander-ndcg``.

Plan 2026-07-02-001 (color-conditioned IDF probe) Unit 3: the report
gains an aggregate summary block (mean pinned / mean live / mean delta
— the SHIP gate number — + violation count, R8) and an identity-size
slice table keyed on ``cards.color_identity`` pip count (R12).

Complements ``tests/test_bench_per_commander_ndcg.py`` (pre-Unit-3
table/sort/threshold behavior). DBs are built via ``open_db`` on
``tmp_path`` paths only — never project-relative literals (see
CLAUDE.md Conventions; a conftest autouse fixture fails the run if a
new ``*.db`` appears under the repo root or ``data/``).
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
from mtg_synergy_graph.bench.per_commander_ndcg import (
    PER_COMMANDER_REGRESSION_THRESHOLD,
    IdentitySliceRow,
    PerCommanderNdcgRow,
    compute_identity_slices,
    fetch_identity_classes,
    handle_per_commander_ndcg,
    identity_class_from_color_identity,
    render_per_commander_ndcg_markdown,
    sort_rows_ascending,
)
from mtg_synergy_graph.db import open_db


def _insert_card(conn: sqlite3.Connection, name: str, color_identity: str | None) -> None:
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, "Creature", "", 3, color_identity, 1000, 1),
    )


@pytest.fixture()
def cards_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Minimal production-schema DB with commanders across identity sizes."""
    conn = open_db(tmp_path / "synergy.db")
    _insert_card(conn, "Mono Cmdr", "W")
    _insert_card(conn, "Two Color Cmdr", "W,U")
    _insert_card(conn, "Colorless Cmdr", "")
    _insert_card(conn, "Null Identity Cmdr", None)
    _insert_card(conn, "Five Color Cmdr", "W,U,B,R,G")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Aggregate summary block (R8)
# ---------------------------------------------------------------------------


def test_aggregate_block_matches_hand_computed_means() -> None:
    """Happy path: aggregate line equals hand-computed mean of 3 deltas."""
    rows = [
        PerCommanderNdcgRow(commander="A", pinned_ndcg=0.30, live_ndcg=0.36, delta=0.06),
        PerCommanderNdcgRow(commander="B", pinned_ndcg=0.60, live_ndcg=0.48, delta=-0.12),
        PerCommanderNdcgRow(commander="C", pinned_ndcg=0.30, live_ndcg=0.30, delta=0.00),
    ]
    md = render_per_commander_ndcg_markdown(
        sort_rows_ascending(rows),
        fixture_path="fixture.json",
        config_hash="hash",
    )
    assert "## Aggregate summary" in md
    # mean pinned = 0.40, mean live = 0.38, mean delta = -0.02 (SHIP gate)
    assert "mean pinned NDCG@30" in md
    assert "0.4000" in md
    assert "mean live NDCG@30" in md
    assert "0.3800" in md
    assert "mean delta (SHIP gate)" in md
    assert "-0.0200" in md


def test_aggregate_block_counts_threshold_violations() -> None:
    """Violation count uses PER_COMMANDER_REGRESSION_THRESHOLD, exclusive."""
    rows = [
        # Exactly at the threshold: NOT a violation (strict <).
        PerCommanderNdcgRow(
            commander="At Threshold",
            pinned_ndcg=0.50,
            live_ndcg=0.45,
            delta=PER_COMMANDER_REGRESSION_THRESHOLD,
        ),
        PerCommanderNdcgRow(commander="Bad", pinned_ndcg=0.50, live_ndcg=0.40, delta=-0.10),
        PerCommanderNdcgRow(commander="Fine", pinned_ndcg=0.50, live_ndcg=0.50, delta=0.00),
    ]
    md = render_per_commander_ndcg_markdown(
        sort_rows_ascending(rows),
        fixture_path="fixture.json",
        config_hash="hash",
    )
    violations_line = next(line for line in md.splitlines() if line.startswith("violations"))
    assert violations_line.split()[-1] == "1"


def test_empty_rows_render_without_aggregate_block() -> None:
    """Empty fixture keeps the informative early-out, no aggregate math."""
    md = render_per_commander_ndcg_markdown(
        [],
        fixture_path="fixture.json",
        config_hash="hash",
    )
    assert "No commanders in fixture" in md
    assert "## Aggregate summary" not in md


# ---------------------------------------------------------------------------
# Identity-size classification (R12)
# ---------------------------------------------------------------------------


def test_identity_class_from_color_identity_pip_counts() -> None:
    assert identity_class_from_color_identity(None) == "colorless"
    assert identity_class_from_color_identity("") == "colorless"
    assert identity_class_from_color_identity("W") == "mono"
    assert identity_class_from_color_identity("W,U") == "2-color"
    assert identity_class_from_color_identity("W,U,B") == "3-color"
    assert identity_class_from_color_identity("W,U,B,R") == "4-color"
    assert identity_class_from_color_identity("W,U,B,R,G") == "5-color"


def test_fetch_identity_classes_maps_commanders(cards_db: sqlite3.Connection) -> None:
    """W / "W,U" / empty identities land in mono / 2-color / colorless."""
    classes = fetch_identity_classes(
        cards_db,
        ["Mono Cmdr", "Two Color Cmdr", "Colorless Cmdr", "Null Identity Cmdr", "Five Color Cmdr"],
    )
    assert classes == {
        "Mono Cmdr": "mono",
        "Two Color Cmdr": "2-color",
        "Colorless Cmdr": "colorless",
        "Null Identity Cmdr": "colorless",
        "Five Color Cmdr": "5-color",
    }


def test_fetch_identity_classes_missing_commander_is_unknown(cards_db: sqlite3.Connection) -> None:
    """Fixture commander absent from cards (post-refresh rename) → "unknown"."""
    classes = fetch_identity_classes(cards_db, ["Renamed Cmdr"])
    assert classes == {"Renamed Cmdr": "unknown"}


# ---------------------------------------------------------------------------
# Identity-size slice aggregation + rendering (R12)
# ---------------------------------------------------------------------------


def test_compute_identity_slices_groups_and_orders() -> None:
    rows = [
        PerCommanderNdcgRow(commander="Mono A", pinned_ndcg=0.5, live_ndcg=0.4, delta=-0.10),
        PerCommanderNdcgRow(commander="Mono B", pinned_ndcg=0.5, live_ndcg=0.54, delta=0.04),
        PerCommanderNdcgRow(commander="Two", pinned_ndcg=0.5, live_ndcg=0.52, delta=0.02),
        PerCommanderNdcgRow(commander="Ghost", pinned_ndcg=0.5, live_ndcg=0.5, delta=0.00),
    ]
    classes = {"Mono A": "mono", "Mono B": "mono", "Two": "2-color", "Ghost": "unknown"}
    slices = compute_identity_slices(rows, classes)
    assert [s.identity_class for s in slices] == ["mono", "2-color", "unknown"]
    mono = slices[0]
    assert mono == IdentitySliceRow(
        identity_class="mono",
        n=2,
        mean_delta=pytest.approx(-0.03),
        worst_delta=-0.10,
        violations=1,
    )


def test_zero_label_commander_included_at_delta_zero() -> None:
    """Zero-label commander (NDCG 0 both sides) → delta 0, no crash."""
    rows = [
        PerCommanderNdcgRow(commander="No Labels", pinned_ndcg=0.0, live_ndcg=0.0, delta=0.0),
    ]
    md = render_per_commander_ndcg_markdown(
        rows,
        fixture_path="fixture.json",
        config_hash="hash",
        identity_class_by_commander={"No Labels": "mono"},
    )
    assert "## Identity-size slices" in md
    slice_line = next(line for line in md.splitlines() if line.startswith("mono"))
    assert slice_line.split() == ["mono", "1", "+0.0000", "+0.0000", "0"]


def test_slice_table_omitted_without_identity_mapping() -> None:
    """Render without the mapping (legacy callers) → no slice table."""
    rows = [PerCommanderNdcgRow(commander="A", pinned_ndcg=0.5, live_ndcg=0.5, delta=0.0)]
    md = render_per_commander_ndcg_markdown(
        rows,
        fixture_path="fixture.json",
        config_hash="hash",
    )
    assert "## Aggregate summary" in md
    assert "## Identity-size slices" not in md


def test_row_missing_from_identity_mapping_falls_into_unknown() -> None:
    """A row whose commander has no mapping entry lands in "unknown"."""
    rows = [PerCommanderNdcgRow(commander="Unmapped", pinned_ndcg=0.5, live_ndcg=0.5, delta=0.0)]
    slices = compute_identity_slices(rows, {})
    assert [s.identity_class for s in slices] == ["unknown"]
    assert slices[0].n == 1


# ---------------------------------------------------------------------------
# Handler integration (exit-code semantics unchanged)
# ---------------------------------------------------------------------------


def test_handler_missing_fixture_exits_2(tmp_path: Path) -> None:
    """Error path preserved: missing fixture → exit 2."""
    args = argparse.Namespace(
        fixture=str(tmp_path / "does_not_exist.json"),
        db=str(tmp_path / "synergy.db"),
        edhrec_db=str(tmp_path / "tags.db"),
        output=None,
    )
    assert handle_per_commander_ndcg(args) == 2


def test_handler_end_to_end_emits_aggregate_and_slices(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full handler run on a minimal tmp DB → exit 0, both new blocks
    present, unknown slice for a renamed commander, no crash on a
    zero-label commander (empty EDHREC table → NDCG 0 both sides).
    """
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    _insert_card(conn, "Mono Cmdr", "W")
    conn.commit()
    conn.close()

    edhrec_path = tmp_path / "tags.db"
    edhrec_conn = sqlite3.connect(edhrec_path)
    edhrec_conn.execute(
        "CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)"
    )
    edhrec_conn.commit()
    edhrec_conn.close()

    fixture_path = tmp_path / "fixture.json"
    PinnedFixture(
        config_hash="deadbeefdeadbeef",
        created_at="2026-07-02T00:00:00+00:00",
        entries=[
            FixtureEntry(commander="Mono Cmdr", scores={"Some Card": 1.0}),
            FixtureEntry(commander="Renamed Cmdr", scores={}),
        ],
    ).write(fixture_path)

    args = argparse.Namespace(
        fixture=str(fixture_path),
        db=str(db_path),
        edhrec_db=str(edhrec_path),
        output=None,
    )
    assert handle_per_commander_ndcg(args) == 0

    out = capsys.readouterr().out
    assert "## Aggregate summary" in out
    assert "mean delta (SHIP gate)" in out
    assert "## Identity-size slices" in out
    mono_line = next(line for line in out.splitlines() if line.startswith("mono"))
    assert mono_line.split()[1] == "1"
    unknown_line = next(line for line in out.splitlines() if line.startswith("unknown"))
    assert unknown_line.split()[1] == "1"
