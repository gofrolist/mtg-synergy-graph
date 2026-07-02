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

import logging
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

SISAY_IDENTITY_JSON = '["B", "G", "R", "U", "W"]'

_DEFAULT_COLUMNS = ("oracle_id", "name", "color_identity")


def _scryfall_db(tmp_path, rows, *, columns=_DEFAULT_COLUMNS):
    """Minimal Scryfall-shaped sqlite DB with the given cards columns."""
    path = tmp_path / "tags.db"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE cards ({', '.join(f'{c} TEXT' for c in columns)})")
    conn.executemany(
        f"INSERT INTO cards VALUES ({', '.join('?' * len(columns))})",  # noqa: S608 — placeholders only
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _resolver_for(tmp_path, rows, *, columns=_DEFAULT_COLUMNS):
    """Build a resolver straight from fixture rows (connect/close handled)."""
    db = _scryfall_db(tmp_path, rows, columns=columns)
    conn = sqlite3.connect(db)
    try:
        return _build_oracle_id_resolver(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Resolver: color_identity capture
# ---------------------------------------------------------------------------


def test_resolver_carries_color_identity(tmp_path):
    """tags.db stores identity as a JSON array; the resolver must expose
    it as the engine's sorted comma-joined pip format."""
    resolver = _resolver_for(tmp_path, [("aa" * 16, "Sisay Test Captain", SISAY_IDENTITY_JSON)])

    hit = _resolve_scryfall_meta("Sisay Test Captain", None, resolver)
    assert hit is not None
    assert hit.color_identity == "B,G,R,U,W"


def test_resolver_color_identity_none_when_column_missing(tmp_path):
    """Legacy fixture DBs without the column must yield None (→ importer
    falls back to the cost-derived placeholder)."""
    resolver = _resolver_for(tmp_path, [("aa" * 16, "Sol Ring")], columns=("oracle_id", "name"))

    hit = _resolve_scryfall_meta("Sol Ring", None, resolver)
    assert hit is not None
    assert hit.color_identity is None


def test_resolver_color_identity_empty_array_is_colorless(tmp_path):
    """Scryfall ``[]`` is a real (colourless) identity, not missing data."""
    resolver = _resolver_for(tmp_path, [("aa" * 16, "Sol Ring", "[]")])

    hit = _resolve_scryfall_meta("Sol Ring", None, resolver)
    assert hit is not None
    assert hit.color_identity == ""


def test_resolver_color_identity_garbage_yields_none(tmp_path):
    """Unparseable identity values must degrade to None, never crash."""
    resolver = _resolver_for(tmp_path, [("aa" * 16, "Broken Card", "not json at all {{")])

    hit = _resolve_scryfall_meta("Broken Card", None, resolver)
    assert hit is not None
    assert hit.color_identity is None


def test_resolver_warns_on_identity_normalisation_failures(tmp_path, caplog):
    """Silent corpus-wide fallback is the failure mode this guards: a
    tags.db format drift must produce a loud WARNING with a count, not a
    quiet degradation to cost-derived identities."""
    rows = [
        ("aa" * 16, "Broken One", "not json"),
        ("bb" * 16, "Broken Two", '["C"]'),  # unknown pip → rejected
        ("cc" * 16, "Fine Card", '["W"]'),
    ]
    with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.importer"):
        _resolver_for(tmp_path, rows)

    warnings = [r for r in caplog.records if "color_identity normalisation failed" in r.message]
    assert len(warnings) == 1
    assert "2 Scryfall rows" in warnings[0].message


def test_token_row_never_supplies_color_identity(tmp_path):
    """A token printing sharing a real card's name may still resolve
    oracle_id (last-resort tier) but must never stamp its own colour
    identity onto the card — the cost-derived value stays."""
    resolver = _resolver_for(
        tmp_path,
        [("aa" * 16, "Test Red Blast", "Token Creature — Illusion", '["U"]')],
        columns=("oracle_id", "name", "type_line", "color_identity"),
    )

    hit = _resolve_scryfall_meta("Test Red Blast", None, resolver)
    assert hit is not None
    assert hit.oracle_id == "aa" * 16  # oracle_id still useful
    assert hit.color_identity is None  # token identity blanked

    conn = open_db(tmp_path / "synergy.db")
    try:
        with conn:
            import_card(conn, parse_card_text(RED_CANDIDATE_TXT), oracle_id_resolver=resolver)
        row = conn.execute("SELECT color_identity FROM cards WHERE name = 'Test Red Blast'").fetchone()
        assert row["color_identity"] == "R"  # cost-derived, not the token's U
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Importer: persisted row
# ---------------------------------------------------------------------------


def test_import_card_prefers_scryfall_identity_over_mana_cost(tmp_path):
    """The Sisay case: cost-derived colors stay 'W' but color_identity
    must come from Scryfall (WUBRG)."""
    resolver = _resolver_for(tmp_path, [("aa" * 16, "Sisay Test Captain", SISAY_IDENTITY_JSON)])

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
    resolver = _resolver_for(tmp_path, [("aa" * 16, "Sisay Test Captain")], columns=("oracle_id", "name"))

    conn = open_db(tmp_path / "synergy.db")
    try:
        with conn:
            import_card(conn, parse_card_text(SISAY_TXT), oracle_id_resolver=resolver)
        row = conn.execute("SELECT colors, color_identity FROM cards WHERE name = 'Sisay Test Captain'").fetchone()
        assert row["colors"] == "W"
        assert row["color_identity"] == "W"
    finally:
        conn.close()


def test_import_card_resolves_identity_even_when_oracle_id_preset(tmp_path):
    """Field-level gating regression guard: a card dict arriving with
    oracle_id already set must STILL receive the Scryfall colour identity
    — gating the whole resolver block on oracle_id would silently
    reintroduce the cost-derived placeholder (the original Sisay bug)."""
    resolver = _resolver_for(tmp_path, [("aa" * 16, "Sisay Test Captain", SISAY_IDENTITY_JSON)])

    card = parse_card_text(SISAY_TXT)
    card["oracle_id"] = "ee" * 16  # pre-resolved by a hypothetical caller

    conn = open_db(tmp_path / "synergy.db")
    try:
        with conn:
            import_card(conn, card, oracle_id_resolver=resolver)
        row = conn.execute("SELECT oracle_id, color_identity FROM cards WHERE name = 'Sisay Test Captain'").fetchone()
        assert row["oracle_id"] == "ee" * 16  # pre-set id is preserved
        assert row["color_identity"] == "B,G,R,U,W"  # identity still resolved
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Engine: candidate pool honours the Scryfall identity
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_with_five_color_sisay(tmp_path):
    resolver = _resolver_for(
        tmp_path,
        [
            ("aa" * 16, "Sisay Test Captain", SISAY_IDENTITY_JSON),
            ("bb" * 16, "Test Red Blast", '["R"]'),
        ],
    )

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
