"""Tests for Forge-native causal graph builder."""
import sqlite3
import pytest
from mtg_synergy.causal.forge_graph_builder import build_forge_edges, compute_filter_match
from mtg_synergy.causal.forge_indexer import build_forge_index
from mtg_synergy.causal.types import Edge
from mtg_synergy.parse.forge_import import ensure_forge_schema, parse_forge_card_file, import_card_to_db
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter


# Reuse card fixtures from test_forge_indexer
KRENKO = """Name:Krenko, Mob Boss
ManaCost:2 R R
Types:Legendary Creature Goblin Warrior
PT:3/3
A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin | TokenAmount$ X
SVar:X:Count$Valid Goblin.YouCtrl
Oracle:{T}: Create X 1/1 red Goblin creature tokens."""

PURPHOROS = """Name:Purphoros, God of the Forge
ManaCost:3 R
Types:Legendary Enchantment Creature God
PT:6/5
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 2
Oracle:Whenever a creature enters the battlefield under your control, deals 2 damage."""

GOBLIN_LORD = """Name:Goblin Chieftain
ManaCost:1 R R
Types:Creature Goblin
PT:2/2
T:Mode$ ChangesZone | ValidCard$ Goblin.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigPump | TriggerZones$ Battlefield
SVar:TrigPump:DB$ Pump | Defined$ Self | NumAtt$ +1 | NumDef$ +1
Oracle:Whenever a Goblin enters the battlefield under your control, Goblin Chieftain gets +1/+1."""


def _setup(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    for text in [KRENKO, PURPHOROS, GOBLIN_LORD]:
        card = parse_forge_card_file(text)
        import_card_to_db(conn, card)
    conn.commit()
    return conn


def test_filter_match_exact():
    """Goblin.YouCtrl filter matches a Goblin token producer -> exact."""
    responder_filter = parse_forge_filter("Goblin.YouCtrl")
    producer_detail = {"verb": "Token", "target": "r_1_1_goblin"}
    match = compute_filter_match(responder_filter, producer_detail, "ChangesZone")
    assert match == "exact"


def test_filter_match_broad():
    """Creature.YouCtrl filter matches any creature token -> broad."""
    responder_filter = parse_forge_filter("Creature.YouCtrl")
    producer_detail = {"verb": "Token", "target": "r_1_1_goblin"}
    match = compute_filter_match(responder_filter, producer_detail, "ChangesZone")
    assert match == "broad"


def test_filter_match_unfiltered():
    """No filter -> unfiltered."""
    responder_filter = parse_forge_filter("")
    producer_detail = {"verb": "Token"}
    match = compute_filter_match(responder_filter, producer_detail, "ChangesZone")
    assert match == "unfiltered"


def test_build_edges(tmp_db):
    conn = _setup(tmp_db)
    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)
    assert len(edges) > 0
    # Krenko -> Purphoros edge should exist (Token -> ChangesZone trigger)
    kr_pu = [e for e in edges if e.source == "Krenko, Mob Boss"
             and e.target == "Purphoros, God of the Forge"]
    assert len(kr_pu) >= 1
    conn.close()


def test_goblin_lord_gets_exact_match(tmp_db):
    conn = _setup(tmp_db)
    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)
    # Krenko -> Goblin Chieftain should be stronger than Krenko -> Purphoros
    kr_lord = [e for e in edges if e.source == "Krenko, Mob Boss"
               and e.target == "Goblin Chieftain"]
    kr_purph = [e for e in edges if e.source == "Krenko, Mob Boss"
                and e.target == "Purphoros, God of the Forge"]
    assert len(kr_lord) >= 1
    assert len(kr_purph) >= 1
    # Exact match (Goblin filter) should have higher strength than broad (Creature filter)
    assert kr_lord[0].strength > kr_purph[0].strength
    conn.close()


def test_no_self_edges(tmp_db):
    conn = _setup(tmp_db)
    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)
    for e in edges:
        assert e.source != e.target, f"Self-edge found: {e.source}"
    conn.close()
