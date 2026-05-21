"""Fixture integration of hidden-gem metric (Unit 2 of plan 003).

Tests that ``build_fixture(..., edhrec_conn=...)`` computes per-commander
``hidden_gem_hit_rate`` + ``hidden_cards`` at fixture-build time and
persists them into ``FixtureEntry.legacy``. Backwards-compat: callers
without an ``edhrec_conn`` get the old behavior (no gem keys in
``legacy``).

Uses synthetic in-memory DBs (synergy + EDHREC) so tests are fast and
independent of ``data/synergy.db`` + ``data/edhrec.db``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.fixture import (
    FixtureEntry,
    PinnedFixture,
    build_fixture,
)
from mtg_synergy_graph.db import open_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_synergy_db(conn: sqlite3.Connection) -> None:
    """Minimal synergy DB with one commander + a handful of candidates.

    The scoring pass runs for real, producing tensor rows we can use.
    """
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
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Flicker Guy", "Creature", "", 3, "R", 600, 1),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Flicker Guy", "effect", "ChangesZone", "Creature.YouCtrl", "{flicker}"),
    )
    conn.commit()


def _make_edhrec_db(
    path: Path,
    rows: list[tuple[str, str, str, float]],
) -> sqlite3.Connection:
    """Build a synthetic EDHREC DB.

    ``rows`` is an iterable of
    ``(commander_slug, card_name, section, synergy)``.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)")
    conn.executemany(
        "INSERT INTO edhrec_card_synergy (commander_slug, card_name, section, synergy) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "synergy.db")
    _seed_synergy_db(conn)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy / backwards-compat paths
# ---------------------------------------------------------------------------


def test_build_fixture_without_edhrec_conn_omits_gem_keys(
    seeded_db: sqlite3.Connection,
) -> None:
    """Backwards compat: no edhrec_conn → legacy dict has no gem keys."""
    fresh = build_fixture(seeded_db, ["Test Commander"])
    assert len(fresh.entries) == 1
    entry = fresh.entries[0]
    assert "hidden_gem_hit_rate" not in entry.legacy
    assert "hidden_cards" not in entry.legacy


def test_build_fixture_with_edhrec_conn_populates_gem_keys(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Happy path: with EDHREC data, legacy gets rate + hidden_cards."""
    # EDHREC has Token Maker (section: High Synergy Cards). Flicker Guy
    # is NOT in EDHREC top-30, so it's a candidate hidden gem (whether
    # it passes plausibility depends on how many rules fire).
    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )
    try:
        fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
    finally:
        edhrec.close()

    assert len(fresh.entries) == 1
    entry = fresh.entries[0]
    assert "hidden_gem_hit_rate" in entry.legacy
    assert "hidden_cards" in entry.legacy
    rate = entry.legacy["hidden_gem_hit_rate"]
    assert isinstance(rate, float)
    assert 0.0 <= rate <= 1.0
    # hidden_cards is JSON-friendly (list, not tuple).
    assert isinstance(entry.legacy["hidden_cards"], list)


def test_build_fixture_edhrec_conn_none_is_same_as_no_conn(
    seeded_db: sqlite3.Connection,
) -> None:
    """``edhrec_conn=None`` is explicit backwards-compat; no gem keys."""
    fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=None)
    entry = fresh.entries[0]
    assert "hidden_gem_hit_rate" not in entry.legacy
    assert "hidden_cards" not in entry.legacy


# ---------------------------------------------------------------------------
# Skip cases (commander with no EDHREC data)
# ---------------------------------------------------------------------------


def test_commander_with_no_edhrec_rows_gets_no_gem_keys(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Commander with zero rows in edhrec_card_synergy → keys absent."""
    # EDHREC DB exists but has no rows for Test Commander.
    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("other-commander", "Some Card", "High Synergy Cards", 0.9)],
    )
    try:
        fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
    finally:
        edhrec.close()

    entry = fresh.entries[0]
    # edhrec_top_30 is None (no rows at all for this commander's slug)
    # — hidden_gem_hit_rate_for_commander returns None — gem keys
    # should be absent.
    assert "hidden_gem_hit_rate" not in entry.legacy
    assert "hidden_cards" not in entry.legacy


def test_commander_with_empty_high_synergy_section_is_skipped(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Rows exist under another section but 0 'High Synergy Cards' rows.

    Treated as "no reference data → skip": ``_edhrec_top_30`` returns
    ``None``, so ``hidden_gem_hit_rate_for_commander`` returns ``None``,
    and the legacy dict carries no gem keys. Returning an empty set
    instead would inflate the metric toward 1.0 (every top-30 card
    becomes "hidden"), desensitizing the warning threshold.
    """
    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Random Card", "Top Cards", 0.1)],
    )
    try:
        fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
    finally:
        edhrec.close()

    entry = fresh.entries[0]
    # Empty 'High Synergy Cards' section is equivalent to no EDHREC
    # data for our purposes — the commander is skipped.
    assert "hidden_gem_hit_rate" not in entry.legacy
    assert "hidden_cards" not in entry.legacy


# ---------------------------------------------------------------------------
# Round-trip + determinism
# ---------------------------------------------------------------------------


def test_gem_fields_round_trip_through_json(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Fixture with gem fields serializes and reloads with values intact."""
    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )
    try:
        fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
    finally:
        edhrec.close()

    fixture_path = tmp_path / "fixture.json"
    fresh.write(fixture_path)
    loaded = PinnedFixture.load(fixture_path)

    assert len(loaded.entries) == 1
    e = loaded.entries[0]
    original = fresh.entries[0]
    assert e.legacy["hidden_gem_hit_rate"] == original.legacy["hidden_gem_hit_rate"]
    assert e.legacy["hidden_cards"] == original.legacy["hidden_cards"]


def test_gem_fields_present_in_raw_json(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """The gem keys survive the JSON write (via the legacy pass-through)."""
    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )
    try:
        fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
    finally:
        edhrec.close()

    fixture_path = tmp_path / "fixture.json"
    fresh.write(fixture_path)
    raw = json.loads(fixture_path.read_text())
    entry_raw = raw["entries"][0]
    assert "hidden_gem_hit_rate" in entry_raw
    assert "hidden_cards" in entry_raw


def test_build_fixture_hidden_gem_determinism(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Two identical build_fixture calls produce byte-identical rates."""
    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )
    try:
        first = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
        second = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
    finally:
        edhrec.close()

    rate_a = first.entries[0].legacy.get("hidden_gem_hit_rate")
    rate_b = second.entries[0].legacy.get("hidden_gem_hit_rate")
    assert rate_a is not None
    assert rate_a == rate_b  # bitwise float equality
    assert first.entries[0].legacy.get("hidden_cards") == second.entries[0].legacy.get("hidden_cards")


# ---------------------------------------------------------------------------
# Interaction with tensor_writer
# ---------------------------------------------------------------------------


def test_gem_fields_computed_even_when_tensor_writer_is_provided(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Dual-sink plumbing: writer AND local row capture both work.

    The metric needs rows in memory, while --repin wants rows persisted
    to SQLite. This test guards against "provided a writer ⇒ lost the
    metric" regressions.
    """
    from mtg_synergy_graph.bench.tensor import TensorWriter

    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )
    try:
        with TensorWriter(seeded_db) as writer:
            fresh = build_fixture(
                seeded_db,
                ["Test Commander"],
                tensor_writer=writer,
                edhrec_conn=edhrec,
            )
    finally:
        edhrec.close()

    entry = fresh.entries[0]
    assert "hidden_gem_hit_rate" in entry.legacy
    assert "hidden_cards" in entry.legacy

    # And the tensor still reached SQLite.
    row_count = seeded_db.execute("SELECT COUNT(*) FROM rule_contributions").fetchone()[0]
    assert row_count > 0


# ---------------------------------------------------------------------------
# Legacy preservation alongside new keys
# ---------------------------------------------------------------------------


def test_existing_legacy_fields_preserved_with_gem_keys_added(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """`build_fixture` adds gem keys without clobbering other legacy fields."""
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
    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )
    try:
        fresh = build_fixture(
            seeded_db,
            ["Test Commander"],
            existing=existing,
            edhrec_conn=edhrec,
        )
    finally:
        edhrec.close()

    entry = fresh.entries[0]
    # Old legacy fields preserved.
    assert entry.legacy["hi_syn_hits"] == 3
    assert entry.legacy["ndcg30"] == 0.5
    assert entry.legacy["top10"] == ["X"]
    # New gem fields added.
    assert "hidden_gem_hit_rate" in entry.legacy
    assert "hidden_cards" in entry.legacy


# ---------------------------------------------------------------------------
# Unicode / punctuation in card names
# ---------------------------------------------------------------------------


def test_hidden_cards_with_commas_survive_json(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Card names with commas (common in MTG) round-trip through JSON."""
    # Insert a candidate whose name has a comma; make it hidden by not
    # listing it in EDHREC. Its plausibility depends on tensor rows.
    seeded_db.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Korvold, Fae-Cursed King", "Creature", "Dragon", 5, "BRG", 50, 1),
    )
    seeded_db.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Korvold, Fae-Cursed King", "trigger", "Sacrificed", "Permanent.YouCtrl", "{sac}"),
    )
    seeded_db.commit()

    edhrec = _make_edhrec_db(
        tmp_path / "edhrec.db",
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )
    try:
        fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=edhrec)
    finally:
        edhrec.close()

    fixture_path = tmp_path / "fixture.json"
    fresh.write(fixture_path)
    loaded = PinnedFixture.load(fixture_path)
    # hidden_cards is a plain list of card names — reloading preserves
    # any unicode / comma content verbatim (JSON handles this natively).
    hidden_cards = loaded.entries[0].legacy.get("hidden_cards", [])
    for name in hidden_cards:
        assert isinstance(name, str)


# ---------------------------------------------------------------------------
# Corrupt EDHREC DB error handling (P2 fix #10)
# ---------------------------------------------------------------------------


def test_build_fixture_skips_commander_on_corrupt_edhrec_query(
    seeded_db: sqlite3.Connection,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Corrupt/missing edhrec table → stderr warning + commander skipped.

    The build continues for other commanders instead of aborting the
    whole audit when a single commander's EDHREC lookup raises
    ``sqlite3.DatabaseError`` (e.g. missing ``edhrec_card_synergy``
    table or corrupt rows).
    """
    # Create an EDHREC DB without the expected table so the query
    # raises OperationalError (a subclass of DatabaseError).
    bad_edhrec_path = tmp_path / "bad_edhrec.db"
    conn = sqlite3.connect(bad_edhrec_path)
    conn.row_factory = sqlite3.Row
    # Intentionally DO NOT create edhrec_card_synergy.
    conn.commit()

    try:
        fresh = build_fixture(seeded_db, ["Test Commander"], edhrec_conn=conn)
    finally:
        conn.close()

    # Commander row is still in the fixture; gem keys absent.
    assert len(fresh.entries) == 1
    entry = fresh.entries[0]
    assert "hidden_gem_hit_rate" not in entry.legacy
    assert "hidden_cards" not in entry.legacy
    # Stderr carries the warning.
    err = capsys.readouterr().err
    assert "edhrec query failed" in err.lower()
    assert "test commander" in err.lower()
