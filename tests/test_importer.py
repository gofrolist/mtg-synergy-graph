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
    resolve_copy_face_from_references,
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


# ---------------------------------------------------------------------------
# AlterAttribute Attributes$ → port_attributes (Prepared mechanic, plan 2026-05-19)
# ---------------------------------------------------------------------------


@_integration
@_requires_cardsfolder
def test_alter_attribute_prepared_exposed_on_abigale(tmp_path):
    """Abigale's TrigPrepare SubAbility (DB$ AlterAttribute | Attributes$ Prepared)
    must land in port_attributes with attr_kind='attribute' so the prepared_mechanic
    rule can join on it.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = _FORGE_CARDSFOLDER / "a" / "abigale_poet_laureate_heroic_stanza.txt"
    card = parse_card_file(card_path)
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT pa.attr_value FROM port_attributes pa "
        "JOIN card_ports cp ON pa.port_id = cp.id "
        "WHERE cp.card_name=? AND cp.event_class='AlterAttribute' "
        "AND pa.attr_kind='attribute'",
        ("Abigale, Poet Laureate",),
    ).fetchall()
    values = {r[0] for r in rows}
    assert values == {"Prepared"}, f"Expected attribute Prepared, got {values}"
    conn.close()


@_integration
@_requires_cardsfolder
def test_alter_attribute_suspected_exposed_on_existing_card(tmp_path):
    """The same code path must surface the pre-existing Suspected attribute.
    Repeat Offender (Murders at Karlov Manor) is the canonical example.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = _FORGE_CARDSFOLDER / "r" / "repeat_offender.txt"
    card = parse_card_file(card_path)
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT pa.attr_value FROM port_attributes pa "
        "JOIN card_ports cp ON pa.port_id = cp.id "
        "WHERE cp.card_name=? AND cp.event_class='AlterAttribute' "
        "AND pa.attr_kind='attribute'",
        ("Repeat Offender",),
    ).fetchall()
    values = {r[0] for r in rows}
    assert "Suspected" in values, f"Expected Suspected attribute, got {values}"
    conn.close()


@_integration
@_requires_cardsfolder
def test_alter_attribute_prepared_exposed_on_other_targeter(tmp_path):
    """Skycoach Waypoint prepares OTHER creatures (ValidTgts$ Creature). The
    attribute must still land in port_attributes — the rule joins on the
    attribute, not on the targeter shape.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = _FORGE_CARDSFOLDER / "s" / "skycoach_waypoint.txt"
    card = parse_card_file(card_path)
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT pa.attr_value FROM port_attributes pa "
        "JOIN card_ports cp ON pa.port_id = cp.id "
        "WHERE cp.card_name=? AND cp.event_class='AlterAttribute' "
        "AND pa.attr_kind='attribute'",
        ("Skycoach Waypoint",),
    ).fetchall()
    values = {r[0] for r in rows}
    assert values == {"Prepared"}, f"Expected attribute Prepared, got {values}"
    conn.close()


def test_alter_attribute_attributes_unit_synthetic(tmp_path):
    """Unit test on a synthetic card dict — confirms the explode path works
    independent of the Forge .txt parser.
    """
    from mtg_synergy_graph.parser import parse_forge_line

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card = {
        "name": "Test Prepare Caster",
        "types": "Creature",
        "abilities": [
            (
                "ability",
                parse_forge_line("AB$ AlterAttribute | Cost$ 2 | ValidTgts$ Creature | Attributes$ Prepared"),
            ),
        ],
        "svars": {},
        "keywords": [],
    }
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT pa.attr_value FROM port_attributes pa "
        "JOIN card_ports cp ON pa.port_id = cp.id "
        "WHERE cp.card_name=? AND pa.attr_kind='attribute'",
        ("Test Prepare Caster",),
    ).fetchall()
    assert {r[0] for r in rows} == {"Prepared"}
    conn.close()


# ---------------------------------------------------------------------------
# AlternateMode:Prepare → synthetic static port (Prepared mechanic, plan 2026-05-19)
# ---------------------------------------------------------------------------


@_integration
@_requires_cardsfolder
def test_alternate_mode_prepare_surfaced_as_port_for_abigale(tmp_path):
    """Abigale's top-level `AlternateMode:Prepare` header must surface as a
    queryable port — a synthetic static port with event_class='AlternateMode'
    and granted_keyword='Prepare'. Mirrors the keyword-port shape so the
    prepared_mechanic rule can join uniformly.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card_path = _FORGE_CARDSFOLDER / "a" / "abigale_poet_laureate_heroic_stanza.txt"
    card = parse_card_file(card_path)
    import_card(conn, card, oracle_id_resolver=None)

    row = conn.execute(
        "SELECT port_type, event_class, granted_keyword FROM card_ports "
        "WHERE card_name=? AND event_class='AlternateMode'",
        ("Abigale, Poet Laureate",),
    ).fetchone()
    assert row is not None, "Expected one AlternateMode port for Abigale"
    assert row["port_type"] == "static"
    assert row["granted_keyword"] == "Prepare"
    conn.close()


def test_alternate_mode_absent_when_card_has_no_alternate_mode(tmp_path):
    """Cards without an AlternateMode header must NOT get a synthetic port."""
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card = {
        "name": "Test Plain Creature",
        "types": "Creature",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT 1 FROM card_ports WHERE card_name=? AND event_class='AlternateMode'",
        ("Test Plain Creature",),
    ).fetchall()
    assert rows == [], "Plain card must not have AlternateMode synthetic port"
    conn.close()


def test_alternate_mode_synthetic_unit(tmp_path):
    """Unit test on a synthetic card dict — confirms the synthesis path
    works independent of the Forge .txt parser.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card = {
        "name": "Test Prepare Payoff",
        "types": "Creature",
        "alternate_mode": "Prepare",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(conn, card, oracle_id_resolver=None)

    row = conn.execute(
        "SELECT port_type, event_class, granted_keyword FROM card_ports "
        "WHERE card_name=? AND event_class='AlternateMode'",
        ("Test Prepare Payoff",),
    ).fetchone()
    assert row is not None
    assert row["port_type"] == "static"
    assert row["granted_keyword"] == "Prepare"
    conn.close()


@pytest.mark.parametrize("value", ["Modal", "Adventure", "Split", "Flip", "Specialize", "Omen", "Meld", "DoubleFaced"])
def test_alternate_mode_non_prepare_values_do_not_emit_port(tmp_path, value):
    """Regression for ``_ALTERNATE_MODE_PORT_VALUES``: only ``Prepare``
    emits a synthetic AlternateMode port. Other values (Modal/Adventure/
    Split/Flip/Specialize/Omen/Meld/DoubleFaced) must NOT — emitting for
    them previously perturbed the depth-2 cascade walker's Stage-1
    relevant-event prefilter, causing a -0.21 regression on Tergrid.

    See ``docs/RULE_HISTORY.md`` 2026-05-19 entry and
    ``ports.py::_ALTERNATE_MODE_PORT_VALUES``.
    """
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    card = {
        "name": f"Test {value} DFC",
        "types": "Creature",
        "alternate_mode": value,
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(conn, card, oracle_id_resolver=None)

    rows = conn.execute(
        "SELECT 1 FROM card_ports WHERE card_name=? AND event_class='AlternateMode'",
        (f"Test {value} DFC",),
    ).fetchall()
    assert rows == [], f"AlternateMode:{value} must not emit a synthetic port"
    conn.close()


# ---------------------------------------------------------------------------
# CopyFaceFrom:<Name> resolution — two-pass importer
# Brainstorm: docs/brainstorms/2026-05-20-copy-face-from-resolution-requirements.md
# ---------------------------------------------------------------------------


def _card_port_shapes(conn, card_name: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT port_type, event_class FROM card_ports WHERE card_name = ?",
        (card_name,),
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


_REFERENCED_REANIMATE_SHAPE = {
    "name": "Reference Spell",
    "types": "Sorcery",
    "abilities": [
        (
            "ability",
            {
                "SP": "ChangeZone",
                "ValidTgts": "Creature.YouOwn",
                "Origin": "Graveyard",
                "Destination": "Battlefield",
            },
        ),
    ],
    "svars": {},
    "keywords": [],
}


def test_card_row_persists_copy_face_from(db):
    """The ``cards.copy_face_from`` column captures the directive so the
    second pass can find every carrier without re-parsing the .txt file.
    """
    carrier = {
        "name": "Carrier Creature",
        "types": "Creature Bear",
        "copy_face_from": "Reference Spell",
        "alternate_mode": "Prepare",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(db, _REFERENCED_REANIMATE_SHAPE, oracle_id_resolver=None)
    import_card(db, carrier, oracle_id_resolver=None)

    row = db.execute(
        "SELECT copy_face_from FROM cards WHERE name = ?",
        ("Carrier Creature",),
    ).fetchone()
    assert row is not None
    assert row["copy_face_from"] == "Reference Spell"


def test_resolve_copy_face_from_inherits_referenced_ports(db):
    """A carrier card with ``copy_face_from='X'`` must end up with copies
    of every X port row attached to its card_name after the second pass.
    Without this, Grave Researcher (CopyFaceFrom:Reanimate) carries no
    Reanimate ports, so reanimator commanders cannot see it.
    """
    carrier = {
        "name": "Carrier Creature",
        "types": "Creature Bear",
        "copy_face_from": "Reference Spell",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(db, _REFERENCED_REANIMATE_SHAPE, oracle_id_resolver=None)
    import_card(db, carrier, oracle_id_resolver=None)

    ref_shapes = _card_port_shapes(db, "Reference Spell")
    assert ref_shapes, "Reference card must have at least one port"
    carrier_shapes_before = _card_port_shapes(db, "Carrier Creature")
    assert ref_shapes - carrier_shapes_before, "Pre-resolution: carrier must not have referenced ports"

    summary = resolve_copy_face_from_references(db)

    carrier_shapes_after = _card_port_shapes(db, "Carrier Creature")
    assert ref_shapes <= carrier_shapes_after, (
        f"Post-resolution: carrier must inherit all referenced shapes. Missing: {ref_shapes - carrier_shapes_after}"
    )
    assert summary.carriers == 1
    assert summary.resolved == 1
    assert summary.unresolved == []


def test_resolve_copy_face_from_tags_provenance(db):
    """Each inherited port must carry a ``port_attributes`` row with
    ``attr_kind='via_copyfacefrom'`` and ``attr_value='<ReferencedName>'``
    so downstream audits / discounts can distinguish inherited from native.
    """
    referenced = {
        "name": "Spell X",
        "types": "Sorcery",
        "abilities": [
            ("ability", {"SP": "Draw", "Defined": "You", "NumCards": "3"}),
        ],
        "svars": {},
        "keywords": [],
    }
    carrier = {
        "name": "Carrier Y",
        "types": "Creature Wizard",
        "copy_face_from": "Spell X",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(db, referenced, oracle_id_resolver=None)
    import_card(db, carrier, oracle_id_resolver=None)
    resolve_copy_face_from_references(db)

    rows = db.execute(
        "SELECT pa.attr_value FROM card_ports cp "
        "JOIN port_attributes pa ON pa.port_id = cp.id "
        "WHERE cp.card_name = ? AND pa.attr_kind = 'via_copyfacefrom'",
        ("Carrier Y",),
    ).fetchall()
    assert rows, "Inherited ports must be tagged with via_copyfacefrom"
    assert all(r[0] == "Spell X" for r in rows), "Every tag must point at the referenced card name"


def test_resolve_copy_face_from_skips_alternate_mode_port(db):
    """Defensive: never inherit a ``static AlternateMode`` port via
    CopyFaceFrom. The AlternateMode marker is per-carrier and inheriting
    it would create false Prepared-mechanic matches between unrelated
    carriers if a referenced card itself were Prepared (not real today,
    but cheap to guard against).
    """
    referenced = {
        "name": "Weird Reference",
        "types": "Creature Cleric",
        "alternate_mode": "Prepare",  # itself synthesises a static AlternateMode port
        "abilities": [
            ("ability", {"SP": "GainLife", "Defined": "You", "LifeAmount": "2"}),
        ],
        "svars": {},
        "keywords": [],
    }
    carrier = {
        "name": "Carrier With Weird Ref",
        "types": "Creature Bear",
        "copy_face_from": "Weird Reference",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(db, referenced, oracle_id_resolver=None)
    import_card(db, carrier, oracle_id_resolver=None)
    resolve_copy_face_from_references(db)

    rows = db.execute(
        "SELECT pa.attr_value FROM card_ports cp "
        "JOIN port_attributes pa ON pa.port_id = cp.id "
        "WHERE cp.card_name = ? AND cp.event_class = 'AlternateMode' "
        "AND pa.attr_kind = 'via_copyfacefrom'",
        ("Carrier With Weird Ref",),
    ).fetchall()
    assert rows == [], "AlternateMode ports must not be inherited via CopyFaceFrom"

    # Sanity: the non-AlternateMode port from the reference did inherit.
    inherited = db.execute(
        "SELECT 1 FROM card_ports cp JOIN port_attributes pa ON pa.port_id = cp.id "
        "WHERE cp.card_name = ? AND pa.attr_kind = 'via_copyfacefrom' "
        "AND cp.event_class = 'GainLife'",
        ("Carrier With Weird Ref",),
    ).fetchall()
    assert inherited, "Non-AlternateMode reference ports must still inherit"


def test_resolve_copy_face_from_records_unresolved_in_summary(db):
    """A reference to a card not in the imported universe must not crash
    the importer. The carrier ends up with only its native ports and a
    summary entry records the unresolved name. Per-carrier warnings were
    intentionally dropped in favour of the single aggregate warning at
    the ``import_cards_folder`` call site.
    """
    carrier = {
        "name": "Orphan Carrier",
        "types": "Creature Bear",
        "copy_face_from": "Card Not In Universe",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(db, carrier, oracle_id_resolver=None)
    summary = resolve_copy_face_from_references(db)

    assert summary.unresolved == [("Orphan Carrier", "Card Not In Universe")]
    assert summary.resolved == 0
    assert summary.carriers == 1

    # No phantom via_copyfacefrom tag rows.
    tagged = db.execute("SELECT 1 FROM port_attributes WHERE attr_kind = 'via_copyfacefrom'").fetchall()
    assert tagged == []


def test_resolve_copy_face_from_is_idempotent(db):
    """Running the resolver twice must not duplicate inherited ports.
    Re-imports run the full pipeline; idempotency keeps the row counts
    stable so the audit deltas reflect signal change, not cardinality.
    """
    referenced = {
        "name": "Spell A",
        "types": "Sorcery",
        "abilities": [("ability", {"SP": "DealDamage", "Defined": "Targeted", "NumDmg": "3"})],
        "svars": {},
        "keywords": [],
    }
    carrier = {
        "name": "Carrier A",
        "types": "Creature Goblin",
        "copy_face_from": "Spell A",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    import_card(db, referenced, oracle_id_resolver=None)
    import_card(db, carrier, oracle_id_resolver=None)
    resolve_copy_face_from_references(db)

    rows_first = db.execute(
        "SELECT COUNT(*) FROM card_ports WHERE card_name = ?",
        ("Carrier A",),
    ).fetchone()[0]
    attrs_first = db.execute(
        "SELECT COUNT(*) FROM port_attributes pa "
        "JOIN card_ports cp ON cp.id = pa.port_id "
        "WHERE cp.card_name = ? AND pa.attr_kind = 'via_copyfacefrom'",
        ("Carrier A",),
    ).fetchone()[0]

    resolve_copy_face_from_references(db)

    rows_second = db.execute(
        "SELECT COUNT(*) FROM card_ports WHERE card_name = ?",
        ("Carrier A",),
    ).fetchone()[0]
    attrs_second = db.execute(
        "SELECT COUNT(*) FROM port_attributes pa "
        "JOIN card_ports cp ON cp.id = pa.port_id "
        "WHERE cp.card_name = ? AND pa.attr_kind = 'via_copyfacefrom'",
        ("Carrier A",),
    ).fetchone()[0]

    assert rows_first == rows_second, "Re-running the resolver duplicated inherited card_ports rows"
    assert attrs_first == attrs_second, "Re-running the resolver duplicated provenance tags"


def test_resolve_copy_face_from_handles_self_reference(db):
    """Depth-1 cycle guard: a card with ``copy_face_from`` pointing at
    itself must not infinitely recurse or duplicate its own ports.
    No real Forge data hits this case, but the resolver shouldn't hang
    if a future cardsfolder typo introduces one.
    """
    card = {
        "name": "Self Reference",
        "types": "Creature Spirit",
        "copy_face_from": "Self Reference",
        "abilities": [("ability", {"SP": "GainLife", "Defined": "You", "LifeAmount": "1"})],
        "svars": {},
        "keywords": [],
    }
    import_card(db, card, oracle_id_resolver=None)

    ports_before = db.execute(
        "SELECT COUNT(*) FROM card_ports WHERE card_name = ?",
        ("Self Reference",),
    ).fetchone()[0]

    summary = resolve_copy_face_from_references(db)

    ports_after = db.execute(
        "SELECT COUNT(*) FROM card_ports WHERE card_name = ?",
        ("Self Reference",),
    ).fetchone()[0]
    assert ports_before == ports_after, "Self-reference must not duplicate ports"
    # Self-references are reported as unresolved (no inheritance attempted).
    assert summary.unresolved and summary.unresolved[0] == ("Self Reference", "Self Reference")


def test_resolve_copy_face_from_warns_on_depth_2_chain(db, caplog):
    """A→B→C chain (B is itself a carrier) triggers a one-shot warning so
    a future Forge data refresh that introduces one doesn't go unnoticed.
    The brainstorm explicitly documents depth-2 as out-of-scope v1; this
    test guarantees the visibility part of the design.
    """
    leaf = {
        "name": "Leaf Spell",
        "types": "Sorcery",
        "abilities": [("ability", {"SP": "DealDamage", "Defined": "Targeted", "NumDmg": "1"})],
        "svars": {},
        "keywords": [],
    }
    middle = {
        "name": "Middle Carrier",
        "types": "Creature",
        "copy_face_from": "Leaf Spell",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    top = {
        "name": "Top Carrier",
        "types": "Creature",
        "copy_face_from": "Middle Carrier",
        "abilities": [],
        "svars": {},
        "keywords": [],
    }
    for c in (leaf, middle, top):
        import_card(db, c, oracle_id_resolver=None)

    with caplog.at_level("WARNING", logger="mtg_synergy_graph.copy_face_from"):
        resolve_copy_face_from_references(db)
    assert any("depth-2 CopyFaceFrom chains" in r.message for r in caplog.records), (
        "depth-2 chains must emit a one-shot warning so silent regressions are impossible"
    )


def test_import_cards_folder_resolves_after_all_cards_imported(tmp_path):
    """End-to-end: when the carrier .txt is imported BEFORE the referenced
    card's .txt, the second pass still resolves correctly. Validates the
    two-pass contract — order-independence in the cardsfolder walk.
    """
    cardsfolder = tmp_path / "cardsfolder"
    sub = cardsfolder / "a"
    sub.mkdir(parents=True)

    # Carrier filename sorts BEFORE the reference filename, so rglob
    # yields it first. The first pass would see an unresolvable reference;
    # the second pass must succeed because by then the reference card is in.
    (sub / "a_carrier.txt").write_text(
        "Name:Aaa Carrier\n"
        "ManaCost:1 G\n"
        "Types:Creature Bear\n"
        "PT:2/2\n"
        "AlternateMode:Prepare\n"
        "\n"
        "ALTERNATE\n"
        "\n"
        "CopyFaceFrom:Zzz Reference\n",
        encoding="utf-8",
    )
    (sub / "z_reference.txt").write_text(
        "Name:Zzz Reference\n"
        "ManaCost:G\n"
        "Types:Sorcery\n"
        "A:SP$ ChangeZone | ValidTgts$ Creature.YouOwn | Origin$ Graveyard | Destination$ Battlefield\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, cardsfolder, scryfall_db=None)

    inherited = conn.execute(
        "SELECT 1 FROM card_ports cp JOIN port_attributes pa ON pa.port_id = cp.id "
        "WHERE cp.card_name = ? AND pa.attr_kind = 'via_copyfacefrom' "
        "AND cp.event_class = 'ChangeZone'",
        ("Aaa Carrier",),
    ).fetchall()
    assert inherited, "Two-pass must resolve regardless of file order"
    conn.close()
