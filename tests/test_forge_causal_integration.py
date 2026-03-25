"""End-to-end test: Forge import -> index -> graph -> score."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_import import (
    ensure_forge_schema, parse_forge_card_file, import_card_to_db
)
from mtg_synergy.causal.forge_indexer import build_forge_index
from mtg_synergy.causal.forge_graph_builder import build_forge_edges
from mtg_synergy.causal import ensure_causal_schema


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
Oracle:Whenever a creature enters, deals 2 damage."""

IMPACT = """Name:Impact Tremors
ManaCost:1 R
Types:Enchantment
T:Mode$ ChangesZone | ValidCard$ Creature.YouCtrl | Origin$ Any | Destination$ Battlefield | Execute$ TrigDmg | TriggerZones$ Battlefield
SVar:TrigDmg:DB$ DealDamage | Defined$ Player.Opponent | NumDmg$ 1
Oracle:Whenever a creature enters, deals 1 damage."""


def test_end_to_end_forge_graph(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    ensure_causal_schema(conn)

    for text in [KRENKO, PURPHOROS, IMPACT]:
        card = parse_forge_card_file(text)
        import_card_to_db(conn, card)
    conn.commit()

    idx = build_forge_index(conn)
    edges = build_forge_edges(idx)

    assert len(edges) > 0

    # Store edges
    from mtg_synergy.causal import store_edges
    count = store_edges(conn, edges)
    assert count > 0

    # Verify we can load and score
    from mtg_synergy.causal import CausalContext
    ctx = CausalContext(conn, "Krenko, Mob Boss", {"Purphoros, God of the Forge"})
    score = ctx.causal_score("Impact Tremors")
    assert score > 0  # Impact Tremors should synergize with Krenko

    conn.close()
