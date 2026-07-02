"""Scryfall-sourced colour identity must override the mana-cost placeholder.

Regression suite for the "Sisay, Weatherlight Captain" bug: her mana cost
is ``2 W`` but her colour identity is WUBRG (the ``{W}{U}{B}{R}{G}``
activation cost counts). The importer's placeholder derived
``color_identity`` from cost pips only, so the engine restricted her
candidate pool to white/colourless cards.

The fix: the Scryfall resolver (tags.db) already carries a canonical
``color_identity`` column for every card — propagate it through
:class:`ScryfallMeta` into ``cards.color_identity`` at import time,
falling back to the cost-derived placeholder only when the Scryfall DB
lacks the column (tiny legacy fixtures).
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph import SynergyEngine
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import (
    _build_oracle_id_resolver,
    _resolve_scryfall_meta,
    import_card,
)
from mtg_synergy_graph.parser import parse_card_text

SISAY_TXT = (
    "Name:Sisay Test Captain\n"
    "ManaCost:2 W\n"
    "Types:Legendary Creature Human Soldier\n"
    "PT:2/2\n"
    "Oracle:{W}{U}{B}{R}{G}: Search your library for a legendary permanent card.\n"
)

RED_CANDIDATE_TXT = "Name:Test Red Blast\nManaCost:R\nTypes:Instant\nOracle:Deal 3 damage to any target.\n"


def _scryfall_db(tmp_path, rows, *, with_identity_column=True):
    """Minimal Scryfall-shaped sqlite DB, optionally with color_identity."""
    path = tmp_path / "tags.db"
    conn = sqlite3.connect(path)
    if with_identity_column:
        conn.execute("CREATE TABLE cards (oracle_id TEXT, name TEXT, color_identity TEXT)")
        conn.executemany("INSERT INTO cards VALUES (?, ?, ?)", rows)
    else:
        conn.execute("CREATE TABLE cards (oracle_id TEXT, name TEXT)")
        conn.executemany("INSERT INTO cards VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Resolver: color_identity capture
# ---------------------------------------------------------------------------


def test_resolver_carries_color_identity(tmp_path):
    """tags.db stores identity as a JSON array; the resolver must expose
    it as the engine's sorted comma-joined pip format."""
    db = _scryfall_db(tmp_path, [("aa" * 16, "Sisay Test Captain", '["B", "G", "R", "U", "W"]')])
    conn = sqlite3.connect(db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Sisay Test Captain", None, resolver)
    assert hit is not None
    assert hit.color_identity == "B,G,R,U,W"


def test_resolver_color_identity_none_when_column_missing(tmp_path):
    """Legacy fixture DBs without the column must yield None (→ importer
    falls back to the cost-derived placeholder)."""
    db = _scryfall_db(tmp_path, [("aa" * 16, "Sol Ring")], with_identity_column=False)
    conn = sqlite3.connect(db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Sol Ring", None, resolver)
    assert hit is not None
    assert hit.color_identity is None


def test_resolver_color_identity_empty_array_is_colorless(tmp_path):
    """Scryfall ``[]`` is a real (colourless) identity, not missing data."""
    db = _scryfall_db(tmp_path, [("aa" * 16, "Sol Ring", "[]")])
    conn = sqlite3.connect(db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Sol Ring", None, resolver)
    assert hit is not None
    assert hit.color_identity == ""


def test_resolver_color_identity_garbage_yields_none(tmp_path):
    """Unparseable identity values must degrade to None, never crash."""
    db = _scryfall_db(tmp_path, [("aa" * 16, "Broken Card", "not json at all {{")])
    conn = sqlite3.connect(db)
    try:
        resolver = _build_oracle_id_resolver(conn)
    finally:
        conn.close()

    hit = _resolve_scryfall_meta("Broken Card", None, resolver)
    assert hit is not None
    assert hit.color_identity is None


# ---------------------------------------------------------------------------
# Importer: persisted row
# ---------------------------------------------------------------------------


def test_import_card_prefers_scryfall_identity_over_mana_cost(tmp_path):
    """The Sisay case: cost-derived colors stay 'W' but color_identity
    must come from Scryfall (WUBRG)."""
    db = _scryfall_db(tmp_path, [("aa" * 16, "Sisay Test Captain", '["B", "G", "R", "U", "W"]')])
    sconn = sqlite3.connect(db)
    try:
        resolver = _build_oracle_id_resolver(sconn)
    finally:
        sconn.close()

    conn = open_db(tmp_path / "synergy.db")
    try:
        with conn:
            import_card(conn, parse_card_text(SISAY_TXT), oracle_id_resolver=resolver)
        row = conn.execute("SELECT colors, color_identity FROM cards WHERE name = 'Sisay Test Captain'").fetchone()
        assert row["colors"] == "W"
        assert row["color_identity"] == "B,G,R,U,W"
    finally:
        conn.close()


def test_import_card_falls_back_to_placeholder_without_identity_column(tmp_path):
    """Old-schema Scryfall DB → keep the cost-derived placeholder."""
    db = _scryfall_db(tmp_path, [("aa" * 16, "Sisay Test Captain")], with_identity_column=False)
    sconn = sqlite3.connect(db)
    try:
        resolver = _build_oracle_id_resolver(sconn)
    finally:
        sconn.close()

    conn = open_db(tmp_path / "synergy.db")
    try:
        with conn:
            import_card(conn, parse_card_text(SISAY_TXT), oracle_id_resolver=resolver)
        row = conn.execute("SELECT colors, color_identity FROM cards WHERE name = 'Sisay Test Captain'").fetchone()
        assert row["colors"] == "W"
        assert row["color_identity"] == "W"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Engine: candidate pool honours the Scryfall identity
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_with_five_color_sisay(tmp_path):
    db = _scryfall_db(
        tmp_path,
        [
            ("aa" * 16, "Sisay Test Captain", '["B", "G", "R", "U", "W"]'),
            ("bb" * 16, "Test Red Blast", '["R"]'),
        ],
    )
    sconn = sqlite3.connect(db)
    try:
        resolver = _build_oracle_id_resolver(sconn)
    finally:
        sconn.close()

    synergy_path = tmp_path / "synergy.db"
    conn = open_db(synergy_path)
    try:
        with conn:
            import_card(conn, parse_card_text(SISAY_TXT), oracle_id_resolver=resolver)
            import_card(conn, parse_card_text(RED_CANDIDATE_TXT), oracle_id_resolver=resolver)
    finally:
        conn.close()

    engine = SynergyEngine(synergy_path)
    try:
        yield engine
    finally:
        engine.close()


def test_engine_offers_off_color_cards_for_five_color_identity(engine_with_five_color_sisay):
    """A mono-white-cost commander with WUBRG identity must be offered
    red cards — the original bug filtered them out."""
    legal = engine_with_five_color_sisay.legal_cards("Sisay Test Captain")
    assert "Test Red Blast" in legal
