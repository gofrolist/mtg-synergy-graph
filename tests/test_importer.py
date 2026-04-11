"""End-to-end importer tests against the 5 reference cards."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import _derive_cmc, _derive_colors, import_cards_folder

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "synergy.db")
    yield conn
    conn.close()


def test_imports_all_fixtures(db):
    cards, ports = import_cards_folder(db, FIXTURES, scryfall_db=None)
    # 5 Phase-1 reference cards + 4 Phase-2 Korvold-test cards
    # + Sol Ring & Urza Lord High Artificer (resource-density fixtures).
    assert cards == 11
    assert ports >= 20

    rows = db.execute("SELECT name FROM cards ORDER BY name").fetchall()
    names = {r[0] for r in rows}
    for expected in (
        "Cathars' Crusade",
        "Korvold, Fae-Cursed King",
        "Panharmonicon",
        "Rhystic Study",
        "Scute Swarm",
        "Phyrexian Altar",
        "Dockside Extortionist",
        "Tireless Tracker",
        "Wrath of God",
    ):
        assert expected in names


def test_korvold_chain_persists_branch_metadata(db):
    import_cards_folder(db, FIXTURES, scryfall_db=None)
    row = db.execute(
        "SELECT branch_kind, source_svar, chain_depth, is_conditional "
        "FROM card_ports WHERE card_name = ? AND event_class = 'Draw'",
        ("Korvold, Fae-Cursed King",),
    ).fetchone()
    assert row is not None
    assert row["branch_kind"] == "subability"
    assert row["source_svar"] == "DBDraw"
    assert row["chain_depth"] == 2
    assert row["is_conditional"] in (0, False)


def test_scute_swarm_branches_marked_conditional(db):
    import_cards_folder(db, FIXTURES, scryfall_db=None)
    rows = db.execute(
        "SELECT event_class, branch_kind, is_conditional "
        "FROM card_ports WHERE card_name = ? AND event_class IN ('Token','CopyPermanent')",
        ("Scute Swarm",),
    ).fetchall()
    by_class = {r["event_class"]: r for r in rows}
    assert by_class["Token"]["branch_kind"] == "false"
    assert by_class["Token"]["is_conditional"] in (1, True)
    assert by_class["CopyPermanent"]["branch_kind"] == "true"
    assert by_class["CopyPermanent"]["is_conditional"] in (1, True)


def test_port_attributes_explode_creature_youctrl(db):
    import_cards_folder(db, FIXTURES, scryfall_db=None)
    # Cathars' trigger filter is "Creature.YouCtrl" → 2 attribute rows.
    rows = db.execute(
        """
        SELECT pa.attr_kind, pa.attr_value, pa.is_negated
        FROM port_attributes pa
        JOIN card_ports cp ON cp.id = pa.port_id
        WHERE cp.card_name = ?
          AND cp.port_type = 'trigger'
        ORDER BY pa.attr_kind
        """,
        ("Cathars' Crusade",),
    ).fetchall()
    pairs = {(r["attr_kind"], r["attr_value"]) for r in rows}
    assert ("type", "Creature") in pairs
    assert ("controller", "YouCtrl") in pairs


# ---------------------------------------------------------------------------
# Colour-identity derivation: Forge ``Colors:`` line uses full colour words
# (``black,green``), not pip letters. Suspend cards / costless walkers /
# back-faces have ``ManaCost:no cost`` and rely entirely on this line.
# Regression: previously these cards landed in the DB with empty
# ``color_identity`` and leaked into every commander's recommendation pool.
# ---------------------------------------------------------------------------


def test_derive_colors_handles_full_colour_words():
    assert _derive_colors(None, "black,green") == "B,G"
    assert _derive_colors(None, "white blue") == "U,W"
    assert _derive_colors("no cost", "red") == "R"


def test_derive_colors_still_handles_pip_letters_in_mana_cost():
    assert _derive_colors("2 G G", None) == "G"
    assert _derive_colors("W U", None) == "U,W"


def test_derive_cmc_treats_no_cost_as_unknown():
    assert _derive_cmc("no cost") is None
    assert _derive_cmc("3 W W") == 5.0
