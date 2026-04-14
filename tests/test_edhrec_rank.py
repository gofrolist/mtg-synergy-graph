"""Tests for the edhrec_rank Scryfall metadata path:

* Resolver returns ``(oracle_id, edhrec_rank)`` tuples.
* Importer persists the rank into ``cards.edhrec_rank``.
* ``SynergyEngine.page`` uses rank as a pure intra-tie tiebreaker
  without ever changing the displayed mechanical total.
* Backward compatibility with Scryfall DBs that lack the column.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph import SynergyEngine
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import (
    _build_oracle_id_resolver,
    _resolve_scryfall_meta,
    import_cards_folder,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Scryfall fixtures
# ---------------------------------------------------------------------------


#: Full schema: oracle_id + name + type_line + edhrec_rank. Mirrors the
#: real data/tags.db shape.
_RANKED_ROWS = [
    # (oracle_id, name, type_line, edhrec_rank)
    ("01" * 16, "Cathars' Crusade", "Enchantment", 1200),
    ("02" * 16, "Korvold, Fae-Cursed King", "Legendary Creature — Dragon", 180),
    ("03" * 16, "Panharmonicon", "Artifact", 260),
    ("04" * 16, "Rhystic Study", "Enchantment", 42),
    ("05" * 16, "Scute Swarm", "Creature — Insect", 3100),
    ("06" * 16, "Phyrexian Altar", "Artifact", 95),
    ("07" * 16, "Dockside Extortionist", "Creature — Goblin Pirate", 12),
    ("08" * 16, "Tireless Tracker", "Creature — Human Scout", 880),
    ("09" * 16, "Wrath of God", "Sorcery", 320),
    ("0a" * 16, "Sol Ring", "Artifact", 1),
    ("0b" * 16, "Urza, Lord High Artificer", "Legendary Creature — Human Artificer", 450),
    # Obscure: NULL edhrec_rank — must sort below any ranked card.
    ("0c" * 16, "Phantom Unobscure", "Creature — Phantom", None),
]


@pytest.fixture()
def ranked_scryfall_db(tmp_path) -> Path:
    """Minimal Scryfall-shaped sqlite DB with the edhrec_rank column."""
    path = tmp_path / "tags_ranked.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cards (oracle_id TEXT, name TEXT, type_line TEXT, edhrec_rank INTEGER)")
    conn.executemany(
        "INSERT INTO cards (oracle_id, name, type_line, edhrec_rank) VALUES (?, ?, ?, ?)",
        _RANKED_ROWS,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def legacy_scryfall_db(tmp_path) -> Path:
    """Scryfall-shaped DB WITHOUT the edhrec_rank column — used to check
    backward compat with pre-migration fixtures."""
    path = tmp_path / "tags_legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cards (oracle_id TEXT, name TEXT, type_line TEXT)")
    conn.executemany(
        "INSERT INTO cards (oracle_id, name, type_line) VALUES (?, ?, ?)",
        [(oid, name, tl) for (oid, name, tl, _) in _RANKED_ROWS],
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Resolver: value shape
# ---------------------------------------------------------------------------


def test_resolver_returns_oracle_id_and_rank_pairs(ranked_scryfall_db):
    conn = sqlite3.connect(ranked_scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Sol Ring", None, resolver)
    assert hit is not None
    assert hit == ("0a" * 16, 1)

    hit = _resolve_scryfall_meta("Dockside Extortionist", None, resolver)
    assert hit == ("07" * 16, 12)


def test_resolver_preserves_null_edhrec_rank(ranked_scryfall_db):
    """Rows with NULL rank must still populate oracle_id; only the
    rank field is None."""
    conn = sqlite3.connect(ranked_scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Phantom Unobscure", None, resolver)
    assert hit is not None
    assert hit[0] == "0c" * 16
    assert hit[1] is None


def test_resolver_tolerates_legacy_schema(legacy_scryfall_db):
    """Older fixtures without edhrec_rank column: every meta tuple
    must have rank=None, never crash on the missing column."""
    conn = sqlite3.connect(legacy_scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Sol Ring", None, resolver)
    assert hit is not None
    assert hit[0] == "0a" * 16
    assert hit[1] is None


def test_old_resolve_oracle_id_shim_still_returns_string(ranked_scryfall_db):
    """Back-compat: _resolve_oracle_id keeps the ``str | None`` return
    type even though the resolver now stores richer tuples."""
    from mtg_synergy_graph.importer import _resolve_oracle_id

    conn = sqlite3.connect(ranked_scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    assert _resolve_oracle_id("Sol Ring", None, resolver) == "0a" * 16
    assert _resolve_oracle_id("Nonexistent", None, resolver) is None


# ---------------------------------------------------------------------------
# Importer: cards.edhrec_rank population
# ---------------------------------------------------------------------------


def test_importer_writes_edhrec_rank(ranked_scryfall_db, tmp_path):
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=ranked_scryfall_db)
        rows = conn.execute("SELECT name, edhrec_rank FROM cards ORDER BY name").fetchall()
        by_name = {r["name"]: r["edhrec_rank"] for r in rows}

        # Every fixture card whose name appears in _RANKED_ROWS must
        # carry its rank verbatim.
        assert by_name["Sol Ring"] == 1
        assert by_name["Dockside Extortionist"] == 12
        assert by_name["Korvold, Fae-Cursed King"] == 180
        assert by_name["Cathars' Crusade"] == 1200
    finally:
        conn.close()


def test_importer_leaves_edhrec_rank_null_for_legacy_schema(
    legacy_scryfall_db,
    tmp_path,
):
    """oracle_id still populates, but edhrec_rank stays NULL because
    the Scryfall DB did not have the column. Import must not crash."""
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=legacy_scryfall_db)
        rows = conn.execute("SELECT name, oracle_id, edhrec_rank FROM cards").fetchall()
        assert rows
        for r in rows:
            assert r["oracle_id"] is not None
            assert r["edhrec_rank"] is None
    finally:
        conn.close()


def test_importer_skip_path_leaves_edhrec_rank_null(tmp_path):
    """scryfall_db=None path: rank stays NULL, no KeyError."""
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)
        rows = conn.execute("SELECT edhrec_rank FROM cards").fetchall()
        assert rows
        assert all(r["edhrec_rank"] is None for r in rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Engine: sort tiebreaker
# ---------------------------------------------------------------------------
#
# The end-to-end tiebreaker tests need a realistic DB where many cards
# share an identical mechanical score AND cmc — the fixture set is too
# small for that (every pair differs on at least one of total / cmc),
# so we gate these tests on /tmp/synergy_full.db. See the same pattern
# in test_counter_payoff.py.


def test_page_total_score_unchanged_by_tiebreaker(tmp_path):
    """Tiebreaker must never modify the displayed ``total_score``.
    Two runs — one with ranks populated, one with all ranks NULL —
    must produce identical score values for every card. Only the
    intra-tie ordering is allowed to differ."""
    scryfall_a = tmp_path / "ranked.db"
    scryfall_b = tmp_path / "unranked.db"
    for path, rank_fn in (
        (scryfall_a, lambda r: r),
        (scryfall_b, lambda _: None),
    ):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cards (oracle_id TEXT, name TEXT, type_line TEXT, edhrec_rank INTEGER)")
        conn.executemany(
            "INSERT INTO cards VALUES (?, ?, ?, ?)",
            [(oid, name, tl, rank_fn(rank)) for (oid, name, tl, rank) in _RANKED_ROWS],
        )
        conn.commit()
        conn.close()

    def _scores_for(scryfall_path: Path) -> dict[str, float]:
        synergy_path = tmp_path / f"syn_{scryfall_path.stem}.db"
        conn = open_db(synergy_path)
        try:
            import_cards_folder(conn, FIXTURES, scryfall_db=scryfall_path)
        finally:
            conn.close()
        with SynergyEngine(synergy_path) as eng:
            page = eng.page("Korvold, Fae-Cursed King", limit=20)
        return {r.card: r.total_score for r in page.items}

    scores_a = _scores_for(scryfall_a)
    scores_b = _scores_for(scryfall_b)
    # Same keys, same values.
    assert scores_a == scores_b
