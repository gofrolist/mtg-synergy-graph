"""Tests for Forge DSL parsing and verb mapping."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_fallback import (
    parse_forge_ability_line, map_forge_verb, ensure_forge_schema,
    FORGE_VERB_MAP,
)


def test_parse_spell_ability():
    line = "A:SP$ DealDamage | Cost$ R | Tgt$ TgtCP | NumDmg$ 3 | SpellDescription$ deals 3 damage"
    result = parse_forge_ability_line(line)
    assert result is not None
    assert result["forge_verb"] == "DealDamage"
    assert result["amount"] == "3"


def test_parse_triggered_ability():
    line = "T:Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | ValidCard$ Creature.YouCtrl | Execute$ TrigDraw | TriggerDescription$ draw a card"
    result = parse_forge_ability_line(line)
    assert result is not None
    assert result["trigger_type"] == "ChangesZone"


def test_map_forge_verb_simple():
    assert map_forge_verb("DealDamage") == "deal_damage"
    assert map_forge_verb("DrawCard") == "draw"
    assert map_forge_verb("GainLife") == "gain_life"
    assert map_forge_verb("CreateToken") == "create"


def test_map_forge_verb_change_zone():
    assert map_forge_verb("ChangeZone", origin="Graveyard", destination="Battlefield") == "return"
    assert map_forge_verb("ChangeZone", origin="Hand", destination="Graveyard") == "discard"
    assert map_forge_verb("ChangeZone", origin="Battlefield", destination="Exile") == "exile"


def test_map_forge_verb_unknown():
    assert map_forge_verb("SomeNewVerb") is None


def test_forge_schema(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='forge_effects'"
    ).fetchone()[0]
    assert count == 1
    conn.close()
