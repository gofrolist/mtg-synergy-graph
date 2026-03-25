"""Tests for full Forge DSL import pipeline."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_import import (
    parse_forge_card_file, ensure_forge_schema, shallow_svar_resolve,
    extract_ability_fields,
)


RHYSTIC_STUDY = """Name:Rhystic Study
ManaCost:2 U
Types:Enchantment
T:Mode$ SpellCast | ValidCard$ Card | ValidActivatingPlayer$ Opponent | TriggerZones$ Battlefield | Execute$ TrigDraw | TriggerDescription$ Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.
SVar:TrigDraw:DB$ Draw | Defined$ You | UnlessCost$ 1 | UnlessPayer$ TriggeredActivator | NumCards$ 1 | OptionalDecider$ You
Oracle:Whenever an opponent casts a spell, you may draw a card unless that player pays {1}."""

LIGHTNING_BOLT = """Name:Lightning Bolt
ManaCost:R
Types:Instant
A:SP$ DealDamage | ValidTgts$ Any | NumDmg$ 3 | SpellDescription$ CARDNAME deals 3 damage to any target.
Oracle:Lightning Bolt deals 3 damage to any target."""

SOL_RING = """Name:Sol Ring
ManaCost:1
Types:Artifact
A:AB$ Mana | Cost$ T | Produced$ C | Amount$ 2 | SpellDescription$ Add {C}{C}.
K:ETBReplacement:ETBTapped:Self
Oracle:{T}: Add {C}{C}."""

KRENKO = """Name:Krenko, Mob Boss
ManaCost:2 R R
Types:Legendary Creature Goblin Warrior
PT:3/3
A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin | TokenAmount$ X | References$ X | SpellDescription$ Create X 1/1 red Goblin creature tokens.
SVar:X:Count$Valid Goblin.YouCtrl
DeckHas:Ability$Token
DeckHints:Type$Goblin
Oracle:{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control."""


def test_parse_lightning_bolt():
    card = parse_forge_card_file(LIGHTNING_BOLT)
    assert card["name"] == "Lightning Bolt"
    assert len(card["abilities"]) == 1
    ab = card["abilities"][0]
    assert ab["ability_type"] == "A"
    assert ab["verb"] == "DealDamage"
    assert ab["amount"] == "3"


def test_parse_rhystic_study_svar_resolve():
    card = parse_forge_card_file(RHYSTIC_STUDY)
    assert card["name"] == "Rhystic Study"
    # Trigger should have verb resolved from SVar
    triggers = [a for a in card["abilities"] if a["ability_type"] == "T"]
    assert len(triggers) == 1
    assert triggers[0]["verb"] == "Draw"  # resolved from SVar:TrigDraw:DB$ Draw
    assert triggers[0]["trigger_mode"] == "SpellCast"
    assert triggers[0]["amount"] == "1"  # NumCards$ 1 from SVar


def test_parse_sol_ring_keyword():
    card = parse_forge_card_file(SOL_RING)
    keywords = [a for a in card["abilities"] if a["ability_type"] == "K"]
    assert len(keywords) >= 1


def test_parse_krenko_deck_tags():
    card = parse_forge_card_file(KRENKO)
    assert any(t["tag_type"] == "has" and "Token" in t["tag"] for t in card["deck_tags"])
    assert any(t["tag_type"] == "hints" and "Goblin" in t["tag"] for t in card["deck_tags"])


def test_parse_krenko_token_ability():
    card = parse_forge_card_file(KRENKO)
    ab = card["abilities"][0]
    assert ab["verb"] == "Token"
    assert ab["token_script"] == "r_1_1_goblin"


def test_svars_collected():
    card = parse_forge_card_file(RHYSTIC_STUDY)
    assert "TrigDraw" in card["svars"]
    assert "DB$ Draw" in card["svars"]["TrigDraw"]


def test_shallow_svar_resolve():
    svars = {"TrigDraw": "DB$ Draw | Defined$ You | NumCards$ 1"}
    fields = shallow_svar_resolve("TrigDraw", svars)
    assert fields["verb"] == "Draw"
    assert fields.get("amount") == "1"


def test_schema_creation(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'forge_%'"
    ).fetchall()]
    assert "forge_abilities" in tables
    assert "forge_deck_tags" in tables
    assert "forge_svars" in tables
    conn.close()
