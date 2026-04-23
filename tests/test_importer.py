"""End-to-end importer tests against the 5 reference cards."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import parse_card_file
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import (
    _derive_cmc,
    _derive_colors,
    import_card,
    import_cards_folder,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "synergy.db")
    yield conn
    conn.close()


def test_imports_all_fixtures(db):
    cards, ports = import_cards_folder(db, FIXTURES, scryfall_db=None)
    # 5 Phase-1 reference cards + 4 Phase-2 Korvold-test cards
    # + Sol Ring & Urza Lord High Artificer (resource-density fixtures)
    # + Bloodghast (self-bridging cascade pathway fixture).
    assert cards == 12
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


_FORGE_CARDSFOLDER = Path(__file__).parent.parent / "data" / "forge" / "forge-gui" / "res" / "cardsfolder"
_integration = pytest.mark.integration
_requires_cardsfolder = pytest.mark.skipif(
    not _FORGE_CARDSFOLDER.exists(),
    reason="requires data/forge cardsfolder (see scripts/import_cardsfolder.py)",
)


@_integration
@_requires_cardsfolder
def test_change_type_attributes_populated_for_kaalia(tmp_path):
    """Kaalia of the Vast cheats Angel/Demon/Dragon into play via ChangeType.
    Those subtypes must land in port_attributes with attr_kind='change_type'.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = _FORGE_CARDSFOLDER / "k" / "kaalia_of_the_vast.txt"
    card = parse_card_file(card_path)
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT attr_kind, attr_value FROM port_attributes "
        "WHERE attr_kind='change_type' "
        "AND port_id IN (SELECT id FROM card_ports WHERE card_name=?)",
        ("Kaalia of the Vast",),
    ).fetchall()
    values = {r[1] for r in rows}
    assert {"Angel", "Demon", "Dragon"} <= values, f"Expected Angel/Demon/Dragon in change_type attrs, got {values}"
    conn.close()


@_integration
@_requires_cardsfolder
def test_token_script_attributes_populated_for_tireless_provisioner(tmp_path):
    """Tireless Provisioner's Token effect has TokenScript values for
    Food and Treasure tokens via GenericChoice expansion."""
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = _FORGE_CARDSFOLDER / "t" / "tireless_provisioner.txt"
    card = parse_card_file(card_path)
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT attr_kind, attr_value FROM port_attributes "
        "WHERE attr_kind IN ('token_color', 'token_subtype') "
        "AND port_id IN (SELECT id FROM card_ports WHERE card_name=?)",
        ("Tireless Provisioner",),
    ).fetchall()
    subtypes = {r[1] for r in rows if r[0] == "token_subtype"}
    # GenericChoice expansion must produce both Food and Treasure entries.
    assert {"Food", "Treasure"} <= subtypes, f"Expected both Food and Treasure in token_subtype attrs, got {subtypes}"
    conn.close()


def test_parse_token_script_handles_single_and_multi_choice():
    from mtg_synergy_graph.ports import _parse_token_script

    assert _parse_token_script("w_1_1_soldier") == [
        ("token_color", "W"),
        ("token_subtype", "Soldier"),
    ]
    assert _parse_token_script("w_1_1_human,u_1_1_merfolk") == [
        ("token_color", "W"),
        ("token_subtype", "Human"),
        ("token_color", "U"),
        ("token_subtype", "Merfolk"),
    ]
    assert _parse_token_script("") == []
    assert _parse_token_script("malformed") == []


def test_parse_token_script_handles_artifact_creature_format():
    """Artifact-creature tokens use 'a' at position [3] after P/T;
    subtype is at [4]. Commanders like Alibou (Thopter) rely on this."""
    from mtg_synergy_graph.ports import _parse_token_script

    # c_0_1_a_egg  -> Egg at [4], not 'A' at [3]
    result = _parse_token_script("c_0_1_a_egg")
    subtypes = [v for k, v in result if k == "token_subtype"]
    assert subtypes == ["Egg"], f"Expected [Egg], got {subtypes}"

    # c_1_1_a_thopter_flying -> Thopter at [4]
    result = _parse_token_script("c_1_1_a_thopter_flying")
    subtypes = [v for k, v in result if k == "token_subtype"]
    assert subtypes == ["Thopter"], f"Expected [Thopter], got {subtypes}"

    # b_2_2_a_necron_warrior -> Necron at [4]
    result = _parse_token_script("b_2_2_a_necron_warrior")
    subtypes = [v for k, v in result if k == "token_subtype"]
    assert subtypes == ["Necron"], f"Expected [Necron], got {subtypes}"


def test_parse_token_script_handles_x_x_creatures():
    """X/X creature tokens (b_x_x_demon) should extract subtype at [3]."""
    from mtg_synergy_graph.ports import _parse_token_script

    result = _parse_token_script("b_x_x_demon")
    subtypes = [v for k, v in result if k == "token_subtype"]
    assert subtypes == ["Demon"], f"Expected [Demon], got {subtypes}"


def test_parse_token_script_preserves_pure_artifact_format():
    """Regression: c_a_food_sac must still yield Food (not Sac)."""
    from mtg_synergy_graph.ports import _parse_token_script

    result = _parse_token_script("c_a_food_sac")
    subtypes = [v for k, v in result if k == "token_subtype"]
    assert subtypes == ["Food"], f"Expected [Food], got {subtypes}"


def test_parse_token_script_multi_color_prefix_expands_to_per_letter():
    """Multi-color prefixes (gw, rg, all) emit one token_color per letter
    and still extract the subtype correctly. Covers 250+ tokens in the
    Forge corpus that would otherwise drop color information silently.
    """
    from mtg_synergy_graph.ports import _parse_token_script

    assert _parse_token_script("gw_1_1_citizen") == [
        ("token_color", "G"),
        ("token_color", "W"),
        ("token_subtype", "Citizen"),
    ]
    result = _parse_token_script("all_1_1_human_wizard")
    colors = [v for k, v in result if k == "token_color"]
    subtypes = [v for k, v in result if k == "token_subtype"]
    assert colors == ["W", "U", "B", "R", "G"]
    assert subtypes == ["Human"]


def test_parse_token_script_named_script_without_color_prefix_is_skipped():
    """Named token scripts (kobolds_of_kher_keep) whose leading word is
    not a recognised color must be dropped entirely rather than emit a
    guessed subtype like 'Keep'.
    """
    from mtg_synergy_graph.ports import _parse_token_script

    assert _parse_token_script("kobolds_of_kher_keep") == []


def test_card_hints_populated_from_deck_needs_and_has(tmp_path):
    """A card with DeckNeeds:Type$Dragon and DeckHas:Ability$Token must
    produce (kind, category, value) rows in card_hints.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)

    card = {
        "name": "Test Dragonlord",
        "types": "Legendary Creature",
        "deck_needs": {"Type": ["Dragon"]},
        "deck_has": {"Ability": ["Token"]},
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(conn, card, oracle_id_resolver=None)

    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT kind, category, value FROM card_hints WHERE card_name=? ORDER BY kind, value",
            ("Test Dragonlord",),
        ).fetchall()
    ]
    assert ("has", "Ability", "Token") in rows
    assert ("needs", "Type", "Dragon") in rows
    conn.close()


def test_card_hints_reimport_replaces_existing(tmp_path):
    """A second import_card for the same card must clear stale hints."""
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)

    card_v1 = {
        "name": "Test Shifter",
        "deck_needs": {"Type": ["Shapeshifter"]},
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(conn, card_v1, oracle_id_resolver=None)

    card_v2 = {
        "name": "Test Shifter",
        "deck_needs": {"Type": ["Changeling"]},
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(conn, card_v2, oracle_id_resolver=None)

    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT kind, category, value FROM card_hints WHERE card_name=?",
            ("Test Shifter",),
        ).fetchall()
    ]
    assert rows == [("needs", "Type", "Changeling")]
    conn.close()


def test_buffed_by_svar_populates_card_hints_with_subtype(tmp_path):
    """SVar:BuffedBy:Elf,Permanent.Snow populates (kind='buffed_by', category='Type', value='Elf')."""
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)

    card = {
        "name": "Test Elvish Lord",
        "types": "Creature",
        "abilities": [],
        "svars": {"BuffedBy": "Elf,Permanent.Snow"},
        "keywords": [],
    }
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT kind, category, value FROM card_hints WHERE card_name=? AND kind='buffed_by' ORDER BY value",
        ("Test Elvish Lord",),
    ).fetchall()
    values = {tuple(r)[2] for r in rows}
    assert "Elf" in values, f"Expected Elf in buffed_by hints, got {values}"
    conn.close()


def test_buffed_by_svar_skips_non_type_tokens(tmp_path):
    """Controller qualifiers (YouCtrl) and cmc comparators (cmcLE3) must not
    produce buffed_by rows.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)

    card = {
        "name": "Test Skip Controller",
        "abilities": [],
        "svars": {"BuffedBy": "Creature.YouCtrl,cmcLE3"},
        "keywords": [],
    }
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT category, value FROM card_hints WHERE card_name=? AND kind='buffed_by'",
        ("Test Skip Controller",),
    ).fetchall()
    values = {tuple(r)[1] for r in rows}
    assert "Creature" in values
    assert "YouCtrl" not in values
    assert "cmcLE3" not in values
    conn.close()
