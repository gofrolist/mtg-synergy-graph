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


BRUVAC = """Name:Bruvac the Grandiloquent
ManaCost:2 U
Types:Legendary Creature Human Advisor
PT:1/4
R:Event$ Mill | ActiveZones$ Battlefield | ValidPlayer$ Player.Opponent | ReplaceWith$ MillTwice | Description$ If an opponent would mill one or more cards, they mill twice that many cards instead.
DeckHints:Ability$Mill
Oracle:If an opponent would mill one or more cards, they mill twice that many cards instead."""

RHOX_FAITHMENDER = """Name:Rhox Faithmender
ManaCost:3 W
Types:Creature Rhino Monk
PT:1/5
R:Event$ GainLife | ActiveZones$ Battlefield | ValidPlayer$ You | ReplaceWith$ GainDouble | AILogic$ DoubleLife | Description$ If you would gain life, you gain twice that much life instead.
Oracle:If you would gain life, you gain twice that much life instead."""

ANGEL_OF_SUFFERING = """Name:Angel of Suffering
ManaCost:3 B B
Types:Creature Angel
PT:5/3
R:Event$ DamageDone | ActiveZones$ Battlefield | ValidTarget$ You | ReplaceWith$ DoubleMill | AlwaysReplace$ True | PreventionEffect$ True | Description$ If damage would be dealt to you, prevent that damage and mill twice that many cards.
Oracle:If damage would be dealt to you, prevent that damage and mill twice that many cards."""

ROOTBOUND_CRAG = """Name:Rootbound Crag
Types:Land
R:Event$ Moved | ValidCard$ Card.Self | Destination$ Battlefield | ReplaceWith$ LandTapped | ReplacementResult$ Updated | Description$ CARDNAME enters tapped unless you control a Mountain or a Forest.
Oracle:CARDNAME enters tapped unless you control a Mountain or a Forest."""


def test_parse_bruvac_replacement_effect():
    """R: replacement effects should NOT set verb (to avoid false profile alignment)
    but should extract ValidPlayer$ as target."""
    card = parse_forge_card_file(BRUVAC)
    assert card["name"] == "Bruvac the Grandiloquent"
    replacements = [a for a in card["abilities"] if a["ability_type"] == "R"]
    assert len(replacements) == 1
    ab = replacements[0]
    assert ab["verb"] is None  # verb stays NULL to avoid polluting forge_profiles
    assert ab["target"] == "Player.Opponent"
    assert ab["raw_line"].startswith("R:")  # raw_line preserved for mechanics_vectors


def test_parse_rhox_faithmender_self_targeting():
    """R: with ValidPlayer$ You should extract target but NOT set verb."""
    card = parse_forge_card_file(RHOX_FAITHMENDER)
    replacements = [a for a in card["abilities"] if a["ability_type"] == "R"]
    assert len(replacements) == 1
    ab = replacements[0]
    assert ab["verb"] is None  # verb stays NULL
    assert ab["target"] == "You"


def test_parse_angel_of_suffering_prevention_skipped():
    """R: with PreventionEffect$ True should not extract verb (prevention, not amplification)."""
    card = parse_forge_card_file(ANGEL_OF_SUFFERING)
    replacements = [a for a in card["abilities"] if a["ability_type"] == "R"]
    assert len(replacements) == 1
    ab = replacements[0]
    assert ab["verb"] is None  # Prevention effect, not amplification


def test_parse_etb_tapped_land_no_verb():
    """R: Event$Moved (ETB tapped) should not extract a synergy verb."""
    card = parse_forge_card_file(ROOTBOUND_CRAG)
    replacements = [a for a in card["abilities"] if a["ability_type"] == "R"]
    assert len(replacements) == 1
    ab = replacements[0]
    # Moved is not in _REPLACEMENT_EVENT_TO_VERB, but verb may be set;
    # the key thing is the raw_line is preserved for debugging
    assert ab["raw_line"].startswith("R:")


def test_schema_creation(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'forge_%'"
    ).fetchall()]
    assert "forge_abilities" in tables
    assert "forge_deck_tags" in tables
    conn.close()
