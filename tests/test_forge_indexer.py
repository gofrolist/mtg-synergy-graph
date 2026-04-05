"""Tests for Forge-native causal indexer."""
import sqlite3
import pytest
from mtg_synergy_train.causal.forge_indexer import ForgeIndex, build_forge_index
from mtg_synergy_train.parse.forge_import import ensure_forge_schema, parse_forge_card_file, import_card_to_db


KRENKO = """Name:Krenko, Mob Boss
ManaCost:2 R R
Types:Legendary Creature Goblin Warrior
PT:3/3
A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin | TokenAmount$ X | References$ X | SpellDescription$ Create X 1/1 red Goblin creature tokens.
SVar:X:Count$Valid Goblin.YouCtrl
DeckHas:Ability$Token
DeckHints:Type$Goblin
Oracle:{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control."""

PURPHOROS = """Name:Purphoros, God of the Forge
ManaCost:3 R
Types:Legendary Enchantment Creature God
PT:6/5
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield | TriggerDescription$ Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 2
Oracle:Whenever a creature enters the battlefield under your control, Purphoros, God of the Forge deals 2 damage to each opponent."""

IMPACT = """Name:Impact Tremors
ManaCost:1 R
Types:Enchantment
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield | TriggerDescription$ Whenever a creature enters the battlefield under your control, Impact Tremors deals 1 damage to each opponent.
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 1
Oracle:Whenever a creature enters the battlefield under your control, Impact Tremors deals 1 damage to each opponent."""

BLOOD_ARTIST = """Name:Blood Artist
ManaCost:1 B
Types:Creature Vampire
PT:0/1
T:Mode$ ChangesZone | ValidCard$ Creature | Origin$ Battlefield | Destination$ Graveyard | Execute$ TrigDrain | TriggerZones$ Battlefield | TriggerDescription$ Whenever Blood Artist or another creature dies, target opponent loses 1 life and you gain 1 life.
SVar:TrigDrain:DB$ LoseLife | Defined$ Player.Opponent | LifeAmount$ 1 | SubAbility$ DBGainLife
SVar:DBGainLife:DB$ GainLife | Defined$ You | LifeAmount$ 1
Oracle:Whenever Blood Artist or another creature dies, target opponent loses 1 life and you gain 1 life."""


def _setup_forge_db(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    for card_text in [KRENKO, PURPHOROS, IMPACT, BLOOD_ARTIST]:
        card = parse_forge_card_file(card_text)
        import_card_to_db(conn, card)
    conn.commit()
    return conn


def test_build_forge_index(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    assert isinstance(idx, ForgeIndex)
    assert idx.total_cards > 0
    conn.close()


def test_producers_for_token(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    # Krenko has Token verb -> produces ChangesZone(Destination=Battlefield)
    producers = idx.producers_for("ChangesZone")
    krenko_entries = [p for p in producers if p[0] == "Krenko, Mob Boss"]
    assert len(krenko_entries) >= 1
    conn.close()


def test_responders_for_changes_zone(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    # Purphoros and Impact Tremors respond to ChangesZone(Creature, Battlefield)
    responders = idx.responders_for("ChangesZone")
    names = {r[0] for r in responders}
    assert "Purphoros, God of the Forge" in names
    assert "Impact Tremors" in names
    conn.close()


def test_blood_artist_responds_to_dies(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    # Blood Artist triggers on ChangesZone(Battlefield->Graveyard) = creature dies
    responders = idx.responders_for("ChangesZone")
    ba = [r for r in responders if r[0] == "Blood Artist"]
    assert len(ba) >= 1
    conn.close()


def test_idf_computation(tmp_db):
    conn = _setup_forge_db(tmp_db)
    idx = build_forge_index(conn)
    idf = idx.compute_event_idf()
    # ChangesZone has multiple responders -> lower IDF than rare events
    assert "ChangesZone" in idf["responder"]
    conn.close()
