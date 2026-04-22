"""PinnedFixture JSON roundtrip + comparison tests (Unit 3, revised).

Post-fix: the fixture carries top-N scores + config_hash only. The full
per-(cmdr, cand, rule) contribution tensor lives in the SQLite
``rule_contributions`` table (populated by ``--repin``, queried by
``--rule`` / ``--inspect`` / ``--collinearity``). These tests lock the
new JSON shape and the identity-check contract.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.fixture import (
    SCHEMA_VERSION,
    TOP_N_PINNED,
    FixtureEntry,
    PinnedFixture,
    ScoreDelta,
    build_fixture,
    score_commander,
)
from mtg_synergy_graph.bench.tensor import TensorWriter
from mtg_synergy_graph.db import open_db


@pytest.fixture()
def seeded_db(tmp_path: Path) -> sqlite3.Connection:
    """Minimal DB with one commander that matches at least one rule."""
    conn = open_db(tmp_path / "synergy.db")
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Test Commander", "Creature", "", 4, "R", 1000, 1),
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Token Maker", "Creature", "", 3, "R", 500, 1),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Roundtrip I/O
# ---------------------------------------------------------------------------


def test_empty_fixture_roundtrip(tmp_path: Path) -> None:
    """An empty fixture survives write → read with full fidelity."""
    original = PinnedFixture(config_hash="abc123", created_at="2026-04-22T00:00:00")
    path = tmp_path / "fixture.json"
    original.write(path)
    loaded = PinnedFixture.load(path)

    assert loaded.config_hash == "abc123"
    assert loaded.created_at == "2026-04-22T00:00:00"
    assert loaded.schema_version == SCHEMA_VERSION
    assert loaded.entries == []


def test_populated_fixture_roundtrip(tmp_path: Path) -> None:
    """Fixture with entries + legacy fields roundtrips cleanly."""
    entry = FixtureEntry(
        commander="Korvold",
        scores={"Mayhem Devil": 0.234, "Pitiless Plunderer": 0.198},
        legacy={"hi_syn_hits": 1, "ndcg30": 0.0746, "top10": ["Savra, Queen of the Golgari"]},
    )
    fixture = PinnedFixture(
        config_hash="deadbeef",
        created_at="2026-04-22T00:00:00",
        entries=[entry],
    )

    path = tmp_path / "fixture.json"
    fixture.write(path)
    loaded = PinnedFixture.load(path)

    assert len(loaded.entries) == 1
    e = loaded.entries[0]
    assert e.commander == "Korvold"
    assert e.scores == {"Mayhem Devil": 0.234, "Pitiless Plunderer": 0.198}
    # Legacy fields preserved in ``legacy`` dict.
    assert e.legacy["hi_syn_hits"] == 1
    assert e.legacy["ndcg30"] == 0.0746
    assert e.legacy["top10"] == ["Savra, Queen of the Golgari"]


def test_fixture_has_no_tensor_rows_field(tmp_path: Path) -> None:
    """Post-fix contract: tensor_rows is NOT in the JSON shape anymore.

    Guards against a future refactor that accidentally reintroduces
    tensor_rows in JSON — that would balloon the fixture back into
    the 96 MB territory that motivated the split to SQLite.
    """
    entry = FixtureEntry(commander="C", scores={"A": 1.0})
    fixture = PinnedFixture(config_hash="x", created_at="t", entries=[entry])
    path = tmp_path / "f.json"
    fixture.write(path)
    raw = json.loads(path.read_text())
    assert "tensor_rows" not in raw["entries"][0]


def test_load_rejects_future_schema(tmp_path: Path) -> None:
    """A fixture from a newer tool version must not be silently read."""
    payload = {
        "schema_version": SCHEMA_VERSION + 99,
        "config_hash": "x",
        "created_at": "",
        "entries": [],
    }
    path = tmp_path / "future.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="schema_version"):
        PinnedFixture.load(path)


def test_legacy_fixture_without_new_fields_loads_cleanly(tmp_path: Path) -> None:
    """Existing golden_set_run.json shape (no scores) still loads."""
    payload = {
        "entries": [
            {
                "commander": "Korvold",
                "edhrec_top10": ["Mayhem Devil"],
                "hi_syn_hits": 1,
                "hi_syn_total": 10,
                "ndcg30": 0.075,
                "on_page_hits": 6,
                "top10": ["Savra"],
            }
        ],
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload))

    loaded = PinnedFixture.load(path)
    assert len(loaded.entries) == 1
    e = loaded.entries[0]
    assert e.commander == "Korvold"
    assert e.scores == {}
    assert e.legacy["edhrec_top10"] == ["Mayhem Devil"]
    assert e.legacy["ndcg30"] == 0.075


# ---------------------------------------------------------------------------
# assert_identity
# ---------------------------------------------------------------------------


def test_assert_identity_pass_on_same_fixture() -> None:
    """A fixture compared against itself always passes."""
    fixture = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={"A": 1.0})],
    )
    report = fixture.assert_identity(fixture)
    assert report.is_identical
    assert report.score_mismatches == []


def test_assert_identity_flags_score_mismatch() -> None:
    """One flipped score is caught with exact numbers in the report."""
    pinned = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={"A": 1.0})],
    )
    live = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="C", scores={"A": 1.5})],
    )
    report = pinned.assert_identity(live)
    assert not report.is_identical
    assert len(report.score_mismatches) == 1
    delta = report.score_mismatches[0]
    assert isinstance(delta, ScoreDelta)
    assert delta.pinned == 1.0
    assert delta.live == 1.5
    assert delta.delta == pytest.approx(0.5)


def test_assert_identity_flags_config_hash_drift() -> None:
    pinned = PinnedFixture(config_hash="abc", created_at="t")
    live = PinnedFixture(config_hash="def", created_at="t")
    report = pinned.assert_identity(live)
    assert report.config_hash_mismatch is not None
    assert "abc" in report.config_hash_mismatch
    assert "def" in report.config_hash_mismatch


def test_assert_identity_flags_missing_commander() -> None:
    pinned = PinnedFixture(
        config_hash="x",
        created_at="t",
        entries=[FixtureEntry(commander="Missing")],
    )
    live = PinnedFixture(config_hash="x", created_at="t", entries=[])
    report = pinned.assert_identity(live)
    assert report.missing_commanders == ["Missing"]


# ---------------------------------------------------------------------------
# score_commander / build_fixture integration
# ---------------------------------------------------------------------------


def test_score_commander_returns_top_n(seeded_db: sqlite3.Connection) -> None:
    """Integration: top_n scores returned + tensor rows captured."""
    top_scores, rows = score_commander(seeded_db, "Test Commander")
    assert len(top_scores) <= TOP_N_PINNED
    assert len(rows) > 0
    # Every tensor row's candidate is a real scored candidate.
    for row in rows:
        assert isinstance(row.candidate, str)


def test_score_commander_sink_mirrors_to_sqlite(
    seeded_db: sqlite3.Connection,
) -> None:
    """Integration: passing a TensorWriter sink persists rows."""
    with TensorWriter(seeded_db) as writer:
        top_scores, _ = score_commander(seeded_db, "Test Commander", tensor_sink=writer.sink)

    row_count = seeded_db.execute(
        "SELECT COUNT(*) FROM rule_contributions WHERE commander = ?",
        ("Test Commander",),
    ).fetchone()[0]
    assert row_count > 0
    # Top scores still returned to caller.
    assert len(top_scores) >= 1


def test_build_fixture_populates_scores(seeded_db: sqlite3.Connection) -> None:
    """``build_fixture`` returns a fixture with per-commander top-N scores."""
    fresh = build_fixture(seeded_db, ["Test Commander"])
    assert len(fresh.entries) == 1
    e = fresh.entries[0]
    assert e.commander == "Test Commander"
    assert len(e.scores) > 0
    assert len(e.scores) <= TOP_N_PINNED


def test_build_fixture_with_writer_persists_tensor(
    seeded_db: sqlite3.Connection,
) -> None:
    """``build_fixture(tensor_writer=...)`` mirrors tensor rows to SQLite."""
    with TensorWriter(seeded_db) as writer:
        build_fixture(seeded_db, ["Test Commander"], tensor_writer=writer)

    row_count = seeded_db.execute("SELECT COUNT(*) FROM rule_contributions").fetchone()[0]
    assert row_count > 0


def test_build_fixture_preserves_legacy_fields(seeded_db: sqlite3.Connection) -> None:
    """``--repin`` must not clobber legacy fields like ``ndcg30``."""
    existing = PinnedFixture(
        config_hash="old",
        created_at="t",
        entries=[
            FixtureEntry(
                commander="Test Commander",
                legacy={"hi_syn_hits": 3, "ndcg30": 0.5, "top10": ["X"]},
            )
        ],
    )
    fresh = build_fixture(seeded_db, ["Test Commander"], existing=existing)
    assert len(fresh.entries) == 1
    e = fresh.entries[0]
    assert e.legacy["hi_syn_hits"] == 3
    assert e.legacy["ndcg30"] == 0.5
    assert e.legacy["top10"] == ["X"]
    assert len(e.scores) > 0


def test_repin_then_expect_identity_roundtrip(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """End-to-end: build + write + load + identity-check on same DB = PASS."""
    fixture_path = tmp_path / "roundtrip.json"
    fresh = build_fixture(seeded_db, ["Test Commander"])
    fresh.write(fixture_path)

    pinned = PinnedFixture.load(fixture_path)
    live = build_fixture(seeded_db, ["Test Commander"], existing=pinned)

    report = pinned.assert_identity(live)
    assert report.is_identical, (
        f"expected identity after fresh repin, got mismatches: "
        f"scores={len(report.score_mismatches)}, "
        f"hash={report.config_hash_mismatch}"
    )
