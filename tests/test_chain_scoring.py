"""Tests for chain bonus scoring in CausalContext."""
import sqlite3
import json
import pytest
from mtg_synergy.causal import (
    build_and_store_graph, CausalContext, ensure_causal_schema
)
from mtg_synergy.parse import parse_card, save_parsed, ensure_parse_schema


def _setup_chain_scenario(conn):
    """Set up: Krenko → creates Goblins → Purphoros triggers → deals damage.

    Chain: Krenko → (creature_enters) → Purphoros → (deal_damage) → [end]
    A candidate that links Krenko to deck cards should get a chain bonus.
    """
    ensure_parse_schema(conn)
    ensure_causal_schema(conn)

    cards_data = [
        ("krenko", "Krenko, Mob Boss",
         "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
         "Legendary Creature — Goblin Warrior"),
        ("purphoros", "Purphoros, God of the Forge",
         "Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.",
         "Legendary Enchantment Creature — God"),
        ("sharpshooter", "Goblin Sharpshooter",
         "Whenever a creature dies, untap Goblin Sharpshooter.\n{T}: Goblin Sharpshooter deals 1 damage to any target.",
         "Creature — Goblin"),
        ("impact", "Impact Tremors",
         "Whenever a creature enters the battlefield under your control, Impact Tremors deals 1 damage to each opponent.",
         "Enchantment"),
    ]
    parsed = {}
    for oid, name, oracle, type_line in cards_data:
        abilities = parse_card(oracle, type_line)
        save_parsed(conn, oid, abilities)
        parsed[oid] = abilities
    conn.commit()
    build_and_store_graph(conn, parsed)
    return parsed


def test_chain_bonus_exists(tmp_db):
    """A candidate that the commander connects to AND that connects to deck cards gets bonus."""
    conn = sqlite3.connect(tmp_db)
    _setup_chain_scenario(conn)
    ctx = CausalContext(conn, "krenko", {"purphoros", "sharpshooter"})
    score_impact = ctx.causal_score("impact")
    assert score_impact > 0
    conn.close()


def test_chain_bonus_absent_for_unlinked(tmp_db):
    """A candidate with no commander link gets no chain bonus."""
    conn = sqlite3.connect(tmp_db)
    _setup_chain_scenario(conn)
    ctx = CausalContext(conn, "krenko", {"purphoros"})
    bonus = ctx._chain_bonus("sharpshooter")
    # Sharpshooter has no direct commander link via creature_enters, so bonus should be 0
    assert bonus == 0.0
    conn.close()


def test_forward_map_built(tmp_db):
    """CausalContext should build a forward map from commander's outgoing edges."""
    conn = sqlite3.connect(tmp_db)
    _setup_chain_scenario(conn)
    ctx = CausalContext(conn, "krenko", {"purphoros"})
    assert hasattr(ctx, '_cmdr_forward_map')
    assert isinstance(ctx._cmdr_forward_map, dict)
    assert len(ctx._cmdr_forward_map) > 0
    conn.close()
