"""Unit tests for the Forge DSL parser (SPEC §5.2-§5.3)."""

from __future__ import annotations

from mtg_synergy_graph import (
    parse_deck_hints,
    parse_forge_line,
)
from mtg_synergy_graph.parser import parse_card_text

# ---------------------------------------------------------------------------
# parse_forge_line — regression set against v1.1 bugs
# ---------------------------------------------------------------------------


def test_parse_forge_line_uses_dollar_separator():
    out = parse_forge_line("Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield")
    assert out["Mode"] == "ChangesZone"
    assert out["ValidCard"] == "Creature.YouCtrl"
    assert out["Origin"] == "Any"
    assert out["Destination"] == "Battlefield"


def test_parse_forge_line_records_db_verb_prefix():
    out = parse_forge_line("DB$ PutCounter | Defined$ Self | CounterType$ P1P1 | CounterNum$ 1 | SubAbility$ DBDraw")
    assert out["DB"] == "PutCounter"
    assert out["_prefix"] == "DB"
    assert out["_verb"] == "PutCounter"
    assert out["CounterType"] == "P1P1"
    assert out["SubAbility"] == "DBDraw"


def test_parse_forge_line_ignores_blank_segments():
    out = parse_forge_line(" Mode$ Drawn |  | ValidCard$ Card.Self ")
    assert out == {"Mode": "Drawn", "ValidCard": "Card.Self"}


# ---------------------------------------------------------------------------
# parse_deck_hints — & between groups, $ between key/value, | between values
# ---------------------------------------------------------------------------


def test_parse_deck_hints_two_groups():
    assert parse_deck_hints("Type$Zombie & Keyword$Flying") == {
        "Type": ["Zombie"],
        "Keyword": ["Flying"],
    }


def test_parse_deck_hints_multivalue():
    assert parse_deck_hints("Ability$Token|Counters") == {
        "Ability": ["Token", "Counters"],
    }


# ---------------------------------------------------------------------------
# parse_card_file: scalar fields land at the right offsets
# ---------------------------------------------------------------------------


def test_cathars_basic_fields(cathars_crusade):
    assert cathars_crusade["name"] == "Cathars' Crusade"
    assert cathars_crusade["mana_cost"] == "3 W W"
    assert cathars_crusade["types"] == "Enchantment"
    # SVar:CatharsCounters:DB$ PutCounterAll | ... — must NOT include leading colon
    assert "CatharsCounters" in cathars_crusade["svars"]
    assert cathars_crusade["svars"]["CatharsCounters"].startswith("DB$ PutCounterAll")


def test_korvold_keywords_and_pt(korvold):
    assert korvold["name"] == "Korvold, Fae-Cursed King"
    assert korvold["pt"] == "4/4"
    assert korvold["types"].startswith("Legendary Creature Dragon")
    assert "Flying" in korvold["keywords"]
    assert korvold["deck_has"] == {"Ability": ["Counters"]}


def test_korvold_has_three_triggers_parsed(korvold):
    triggers = [a for a in korvold["abilities"] if a[0] == "trigger"]
    # ETB/attack TrigSac (1) + Secondary attack (2) + sacrifice TrigPutCounter (3)
    assert len(triggers) == 3


def test_panharmonicon_static_mode(panharmonicon):
    statics = [a for a in panharmonicon["abilities"] if a[0] == "static"]
    assert len(statics) == 1
    parsed = statics[0][1]
    assert parsed["Mode"] == "Panharmonicon"
    assert parsed["ValidMode"] == "ChangesZone,ChangesZoneAll"


def test_rhystic_study_trigger_with_unless_cost(rhystic_study):
    triggers = [a for a in rhystic_study["abilities"] if a[0] == "trigger"]
    assert len(triggers) == 1
    parsed = triggers[0][1]
    assert parsed["Mode"] == "SpellCast"
    assert parsed["ValidActivatingPlayer"] == "Opponent"
    assert parsed["Execute"] == "TrigDraw"
    # SVar should preserve UnlessCost so chain walker can find it
    assert "UnlessCost" in rhystic_study["svars"]["TrigDraw"]


def test_scute_swarm_branch_svars_present(scute_swarm):
    svars = scute_swarm["svars"]
    assert "TrigBranch" in svars
    assert "TrueSubAbility$" in svars["TrigBranch"]
    assert "FalseSubAbility$" in svars["TrigBranch"]
    assert "DBCopy" in svars
    assert "DBToken" in svars


def test_dfc_keeps_front_face_identity_and_merges_back_face_abilities():
    """A Forge DFC file separates faces with ``ALTERNATE``. Front face
    contributes identity (name / mana / types) and BOTH faces contribute
    abilities + svars. Regression: previously the back face's ``Name:`` line
    overwrote the front face, leaving DFCs in the DB as their colourless
    back-face form which then leaked into every commander's pool.
    """
    text = (
        "Name:Front Face\n"
        "ManaCost:G\n"
        "Types:Legendary Creature Insect\n"
        "PT:1/2\n"
        "K:Deathtouch\n"
        "SVar:Trig1:DB$ Mill | NumCards$ 2\n"
        "AlternateMode:DoubleFaced\n"
        "\n"
        "ALTERNATE\n"
        "\n"
        "Name:Back Face\n"
        "ManaCost:no cost\n"
        "Colors:black,green\n"
        "Types:Legendary Planeswalker Front\n"
        "Loyalty:3\n"
        "A:AB$ Token | Cost$ AddCounter<1/LOYALTY> | TokenScript$ bg_1_1\n"
        "SVar:DBBack:DB$ PutCounter | CounterType$ Deathtouch\n"
    )
    card = parse_card_text(text)
    # Front face identity wins
    assert card["name"] == "Front Face"
    assert card["mana_cost"] == "G"
    assert card["types"] == "Legendary Creature Insect"
    assert card["pt"] == "1/2"
    # Both faces' abilities + svars merged
    assert "Trig1" in card["svars"]
    assert "DBBack" in card["svars"]
    # Back face's A: ability is present
    assert any(kind == "ability" for kind, _ in card["abilities"])


# ---------------------------------------------------------------------------
# CopyFaceFrom:<Name> capture (back face references another card by name)
# ---------------------------------------------------------------------------


def test_copy_face_from_captured_on_back_face():
    """A Prepared card whose back face is ``CopyFaceFrom:<X>`` must record
    the referenced card name in ``card['copy_face_from']`` so the importer's
    second pass can materialise the referenced card's ports onto the carrier.

    Mirrors the actual Forge shape (see Grave Researcher, Yavimaya Bloomsage):
    front face is a creature with AlternateMode:Prepare, back face has only
    the CopyFaceFrom directive.
    """
    text = (
        "Name:Test Prepare Carrier\n"
        "ManaCost:2 B\n"
        "Types:Creature Troll Warlock\n"
        "PT:3/3\n"
        "T:Mode$ Phase | Phase$ Upkeep | ValidPlayer$ You | Execute$ TrigPrepare\n"
        "SVar:TrigPrepare:DB$ AlterAttribute | Attributes$ Prepared\n"
        "AlternateMode:Prepare\n"
        "\n"
        "ALTERNATE\n"
        "\n"
        "CopyFaceFrom:Reanimate\n"
    )
    card = parse_card_text(text)
    assert card["name"] == "Test Prepare Carrier"
    assert card["copy_face_from"] == "Reanimate"


def test_copy_face_from_absent_when_back_face_inline():
    """Cards whose back face is defined inline (Abigale, Poet Laureate's
    Heroic Stanza) must NOT set ``copy_face_from`` — only the directive
    flavour triggers second-pass resolution.
    """
    text = (
        "Name:Inline Back Carrier\n"
        "ManaCost:1 W B\n"
        "Types:Legendary Creature Bird Bard\n"
        "PT:2/3\n"
        "AlternateMode:Prepare\n"
        "\n"
        "ALTERNATE\n"
        "\n"
        "Name:Heroic Stanza\n"
        "ManaCost:1 WB\n"
        "Types:Sorcery\n"
        "A:SP$ PutCounter | ValidTgts$ Creature | CounterType$ P1P1\n"
    )
    card = parse_card_text(text)
    assert card.get("copy_face_from") is None


def test_copy_face_from_absent_when_no_alternate_section():
    """Vanilla single-face cards must not have ``copy_face_from`` set."""
    text = "Name:Vanilla Creature\nManaCost:1 G\nTypes:Creature Bear\nPT:2/2\n"
    card = parse_card_text(text)
    assert card.get("copy_face_from") is None


def test_copy_face_from_on_front_face_ignored():
    """Defensive: ``CopyFaceFrom:`` only appears on the back face in real
    Forge data, but if it ever leaks onto the front face (data error or
    future Forge schema change), the parser must not crash and must not
    populate ``copy_face_from`` from the front face — it would shadow
    the legitimate back-face directive on a malformed card and silently
    inherit the wrong ports.
    """
    text = "Name:Bad Front\nManaCost:1 R\nTypes:Creature Goblin\nCopyFaceFrom:Lightning Bolt\n"
    card = parse_card_text(text)
    assert card.get("copy_face_from") is None
