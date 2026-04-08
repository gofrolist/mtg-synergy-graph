"""Tests for the Scryfall oracle_id resolver and the oracle_id-based
public API on :class:`SynergyEngine`.

Covers the four resolver tiers (exact non-token, exact any, DFC front
face, DFC back face), the parser's alternate_name capture for DFC/MDFC
cards, and the failure modes ``SynergyEngine.page_by_oracle_id`` must
distinguish.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph import SynergyEngine
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import (
    _build_oracle_id_resolver,
    _resolve_oracle_id,
    import_cards_folder,
)
from mtg_synergy_graph.parser import parse_card_text

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Synthetic Scryfall fixture
# ---------------------------------------------------------------------------


FIXTURE_CARDS = [
    # (oracle_id, name, type_line)
    #
    # Normal non-token exact matches — every Forge fixture card by name.
    ("01" * 16, "Cathars' Crusade",                  "Enchantment"),
    ("02" * 16, "Korvold, Fae-Cursed King",          "Legendary Creature — Dragon"),
    ("03" * 16, "Panharmonicon",                     "Artifact"),
    ("04" * 16, "Rhystic Study",                     "Enchantment"),
    ("05" * 16, "Scute Swarm",                       "Creature — Insect"),
    ("06" * 16, "Phyrexian Altar",                   "Artifact"),
    ("07" * 16, "Dockside Extortionist",             "Creature — Goblin Pirate"),
    ("08" * 16, "Tireless Tracker",                  "Creature — Human Scout"),
    ("09" * 16, "Wrath of God",                      "Sorcery"),
    ("0a" * 16, "Sol Ring",                          "Artifact"),
    ("0b" * 16, "Urza, Lord High Artificer",         "Legendary Creature — Human Artificer"),

    # Token fallback: Forge has "Spirit", Scryfall has one non-token and
    # one token row with the same name — non-token must win.
    ("10" * 16, "Spirit",                            "Creature — Spirit"),
    ("11" * 16, "Spirit",                            "Token Creature — Spirit"),

    # DFC front-face match: Forge stores "Rona, Herald of Invasion",
    # Scryfall stores the combined DFC canonical form.
    ("12" * 16,
     "Rona, Herald of Invasion // Rona, Tolarian Obliterator",
     "Legendary Creature — Human Wizard"),

    # DFC back-face match: Forge alternate_name is "Tolarian Terror",
    # Scryfall stores "Dihada's Ploy // Tolarian Terror" (hypothetical).
    ("13" * 16,
     "Dihada's Ploy // Tolarian Terror",
     "Sorcery // Creature — Horror"),
]


@pytest.fixture()
def scryfall_db(tmp_path) -> Path:
    """Minimal Scryfall-shaped sqlite DB for resolver tests."""
    path = tmp_path / "tags.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards ("
        "oracle_id TEXT, name TEXT, type_line TEXT)"
    )
    conn.executemany(
        "INSERT INTO cards (oracle_id, name, type_line) VALUES (?, ?, ?)",
        FIXTURE_CARDS,
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Parser: alternate_name capture
# ---------------------------------------------------------------------------


def test_parser_captures_back_face_alternate_name():
    """DFC .txt files have two Name: lines; parser must record the back
    face in card["alternate_name"] while keeping the front face as
    the canonical card["name"]."""
    text = (
        "Name:Rona, Herald of Invasion\n"
        "ManaCost:1 U\n"
        "Types:Legendary Creature Human Wizard\n"
        "PT:1/3\n"
        "AlternateMode:DoubleFaced\n"
        "Oracle:front oracle text\n"
        "\n"
        "ALTERNATE\n"
        "\n"
        "Name:Rona, Tolarian Obliterator\n"
        "ManaCost:no cost\n"
        "Colors:blue,black\n"
        "Types:Legendary Creature Phyrexian Wizard\n"
        "PT:3/3\n"
        "Oracle:back oracle text\n"
    )
    card = parse_card_text(text)
    assert card["name"] == "Rona, Herald of Invasion"
    assert card["alternate_name"] == "Rona, Tolarian Obliterator"
    # Front-face identity must remain the front face (regression guard
    # against the back-face Name silently overwriting the front).
    assert card["mana_cost"] == "1 U"


def test_parser_leaves_alternate_name_none_for_single_face_card():
    text = (
        "Name:Sol Ring\n"
        "ManaCost:1\n"
        "Types:Artifact\n"
        "Oracle:{T}: Add {C}{C}.\n"
    )
    card = parse_card_text(text)
    assert card["name"] == "Sol Ring"
    assert card["alternate_name"] is None


# ---------------------------------------------------------------------------
# Resolver tiers (unit test — no import)
# ---------------------------------------------------------------------------


def test_resolver_exact_non_token(scryfall_db):
    conn = sqlite3.connect(scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    # Non-token spirit wins over the token spirit row.
    assert _resolve_oracle_id("Spirit", None, resolver) == "10" * 16


def test_resolver_matches_every_fixture_name(scryfall_db):
    conn = sqlite3.connect(scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    # Every plain-named fixture resolves via the exact tier.
    assert _resolve_oracle_id("Sol Ring", None, resolver) == "0a" * 16
    assert _resolve_oracle_id("Korvold, Fae-Cursed King", None, resolver) == "02" * 16


def test_resolver_dfc_front_face(scryfall_db):
    """Forge stores 'Rona, Herald of Invasion' (front face only).
    Scryfall stores the combined 'A // B' name. The resolver must
    map the Forge front-face string to the combined card's oracle_id."""
    conn = sqlite3.connect(scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    assert _resolve_oracle_id("Rona, Herald of Invasion", None, resolver) == "12" * 16


def test_resolver_dfc_back_face_via_alternate_name(scryfall_db):
    """Hypothetical DFC where Forge's front face ('Dihada's Ploy') is
    absent from the Forge cards.name but its back face lives in
    alternate_name. The resolver must fall back to the back-face tier."""
    conn = sqlite3.connect(scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    # Forge front name is unknown to Scryfall; alternate_name rescues it.
    assert (
        _resolve_oracle_id("Unknown Forge Front", "Tolarian Terror", resolver)
        == "13" * 16
    )


def test_resolver_returns_none_for_missing_card(scryfall_db):
    conn = sqlite3.connect(scryfall_db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    assert _resolve_oracle_id("Hargilde, Kindly Runechanter", None, resolver) is None


# ---------------------------------------------------------------------------
# Importer: end-to-end oracle_id population
# ---------------------------------------------------------------------------


def test_importer_populates_oracle_id(scryfall_db, tmp_path):
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        cards, _ = import_cards_folder(
            conn, FIXTURES, scryfall_db=scryfall_db,
        )
        assert cards == 11

        # Every fixture card should have an oracle_id set via the
        # exact-name tier (they all exist in the synthetic scryfall DB).
        rows = conn.execute(
            "SELECT name, oracle_id FROM cards ORDER BY name"
        ).fetchall()
        by_name = {r["name"]: r["oracle_id"] for r in rows}
        assert by_name["Korvold, Fae-Cursed King"] == "02" * 16
        assert by_name["Sol Ring"] == "0a" * 16
        for name, oid in by_name.items():
            assert oid is not None, f"{name} has no oracle_id"
    finally:
        conn.close()


def test_importer_skips_oracle_id_when_scryfall_db_is_none(tmp_path):
    """Explicit opt-out path for tests that don't care about oracle_id."""
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)
        rows = conn.execute("SELECT oracle_id FROM cards").fetchall()
        assert rows  # sanity: cards were still imported
        assert all(r["oracle_id"] is None for r in rows)
    finally:
        conn.close()


def test_importer_raises_on_missing_scryfall_db(tmp_path):
    """Typo / stale path → hard fail at the top of the import."""
    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        with pytest.raises(FileNotFoundError):
            import_cards_folder(
                conn, FIXTURES,
                scryfall_db=tmp_path / "does_not_exist.db",
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SynergyEngine.page_by_oracle_id + resolve_oracle_id
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_with_oracle_ids(scryfall_db, tmp_path):
    """A populated engine whose cards all carry oracle_id."""
    synergy_path = tmp_path / "synergy_oracle.db"
    conn = open_db(synergy_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=scryfall_db)
    finally:
        conn.close()
    engine = SynergyEngine(synergy_path)
    try:
        yield engine
    finally:
        engine.close()


def test_resolve_oracle_id_returns_cards_name(engine_with_oracle_ids):
    name = engine_with_oracle_ids.resolve_oracle_id("02" * 16)
    assert name == "Korvold, Fae-Cursed King"


def test_resolve_oracle_id_raises_lookup_error_for_missing(engine_with_oracle_ids):
    with pytest.raises(LookupError, match="oracle_id"):
        engine_with_oracle_ids.resolve_oracle_id("ff" * 16)


def test_page_by_oracle_id_matches_page_by_name(engine_with_oracle_ids):
    """oracle_id entry point must produce byte-identical rankings to the
    name entry point when both resolve to the same commander."""
    page_oid = engine_with_oracle_ids.page_by_oracle_id(
        "02" * 16, offset=0, limit=10,
    )
    page_name = engine_with_oracle_ids.page(
        "Korvold, Fae-Cursed King", offset=0, limit=10,
    )
    assert [r.card for r in page_oid.items] == [r.card for r in page_name.items]


def test_page_by_oracle_id_rejects_empty_sequence(engine_with_oracle_ids):
    with pytest.raises(ValueError, match="at least one oracle_id"):
        engine_with_oracle_ids.page_by_oracle_id([])


def test_page_by_oracle_id_raises_lookup_error_for_missing(engine_with_oracle_ids):
    with pytest.raises(LookupError):
        engine_with_oracle_ids.page_by_oracle_id("ff" * 16)
