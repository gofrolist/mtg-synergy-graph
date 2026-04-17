"""Cards with ``legal_commander=0`` from Scryfall must not appear in
recommendations. This is a hard filter — it runs before scoring, so
an illegal card can never reach the result page even if it matches
every mechanical rule.

The canonical victim is Unfinity-style acorn/silver-border content that
Forge ships in cardsfolder but Scryfall flags as not tournament legal.
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
# Scryfall fixtures with legal_commander column
# ---------------------------------------------------------------------------

#: One illegal row in the middle of the real fixture list. Everything else
#: keeps ``legal_commander=1`` so existing tests aren't affected.
_ROWS_WITH_LEGALITY = [
    # (oracle_id, name, type_line, edhrec_rank, legal_commander)
    ("01" * 16, "Cathars' Crusade", "Enchantment", 1200, 1),
    ("02" * 16, "Korvold, Fae-Cursed King", "Legendary Creature — Dragon", 180, 1),
    ("03" * 16, "Panharmonicon", "Artifact", 260, 1),
    ("04" * 16, "Rhystic Study", "Enchantment", 42, 1),
    ("05" * 16, "Scute Swarm", "Creature — Insect", 3100, 1),
    ("06" * 16, "Phyrexian Altar", "Artifact", 95, 1),
    ("07" * 16, "Dockside Extortionist", "Creature — Goblin Pirate", 12, 0),  # ILLEGAL
    ("08" * 16, "Tireless Tracker", "Creature — Human Scout", 880, 1),
    ("09" * 16, "Wrath of God", "Sorcery", 320, 1),
    ("0a" * 16, "Sol Ring", "Artifact", 1, 1),
    ("0b" * 16, "Urza, Lord High Artificer", "Legendary Creature — Human Artificer", 450, 1),
]


@pytest.fixture()
def legality_scryfall_db(tmp_path) -> Path:
    path = tmp_path / "tags_legality.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards (oracle_id TEXT, name TEXT, type_line TEXT, edhrec_rank INTEGER, legal_commander INTEGER)"
    )
    conn.executemany(
        "INSERT INTO cards VALUES (?, ?, ?, ?, ?)",
        _ROWS_WITH_LEGALITY,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def legacy_scryfall_db(tmp_path) -> Path:
    """No legal_commander column — legacy schema. Everything defaults to legal."""
    path = tmp_path / "tags_legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cards (oracle_id TEXT, name TEXT, type_line TEXT, edhrec_rank INTEGER)")
    conn.executemany(
        "INSERT INTO cards VALUES (?, ?, ?, ?)",
        [(oid, name, tl, rank) for (oid, name, tl, rank, _) in _ROWS_WITH_LEGALITY],
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Resolver: carries legal_commander through
# ---------------------------------------------------------------------------


def test_resolver_carries_legal_commander(legality_scryfall_db):
    conn = sqlite3.connect(legality_scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit_legal = _resolve_scryfall_meta("Sol Ring", None, resolver)
    hit_illegal = _resolve_scryfall_meta("Dockside Extortionist", None, resolver)

    assert hit_legal is not None and hit_illegal is not None
    assert hit_legal.legal_commander is True
    assert hit_illegal.legal_commander is False


def test_resolver_legacy_schema_defaults_to_legal(legacy_scryfall_db):
    """Scryfall DBs without the legal_commander column must behave as
    if every card were legal — never crash, never spuriously filter."""
    conn = sqlite3.connect(legacy_scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Dockside Extortionist", None, resolver)
    assert hit is not None
    assert hit.legal_commander is True


# ---------------------------------------------------------------------------
# Importer persists legal_commander into cards table
# ---------------------------------------------------------------------------


def test_importer_persists_legal_commander(legality_scryfall_db, tmp_path):
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=legality_scryfall_db)
        rows = conn.execute("SELECT name, legal_commander FROM cards").fetchall()
        by_name = {r["name"]: r["legal_commander"] for r in rows}

        assert by_name["Sol Ring"] == 1
        assert by_name["Dockside Extortionist"] == 0
        assert by_name["Korvold, Fae-Cursed King"] == 1
    finally:
        conn.close()


def test_importer_defaults_legal_commander_without_scryfall(tmp_path):
    """scryfall_db=None path — no legality info available, every card
    defaults to legal_commander=1 so existing behaviour is preserved."""
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)
        rows = conn.execute("SELECT legal_commander FROM cards").fetchall()
        assert rows
        assert all(r["legal_commander"] == 1 for r in rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Engine: legal_commander=0 cards are excluded from page() and legal_cards()
# ---------------------------------------------------------------------------


def test_engine_page_excludes_illegal_cards(legality_scryfall_db, tmp_path):
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=legality_scryfall_db)
    finally:
        conn.close()

    with SynergyEngine(synergy_path) as eng:
        page = eng.page("Korvold, Fae-Cursed King", limit=1_000_000)
        cards = {r.card for r in page.items}

    assert "Sol Ring" in cards  # sanity: legal cards still appear
    assert "Dockside Extortionist" not in cards  # the one flagged illegal


def test_engine_legal_cards_excludes_illegal(legality_scryfall_db, tmp_path):
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=legality_scryfall_db)
    finally:
        conn.close()

    with SynergyEngine(synergy_path) as eng:
        legal = set(eng.legal_cards("Korvold, Fae-Cursed King"))

    assert "Sol Ring" in legal
    assert "Dockside Extortionist" not in legal
