"""Integration tests for causal graph: build, store, query."""
import sqlite3
import json
import pytest
from mtg_synergy.causal import build_and_store_graph, causal_score
from mtg_synergy.parse import parse_card, save_parsed, ensure_parse_schema
from mtg_synergy.parse.ast_types import Ability


def _setup_cards(conn):
    ensure_parse_schema(conn)
    from mtg_synergy.causal import ensure_causal_schema
    ensure_causal_schema(conn)
    test_cards = [
        ("krenko", "Krenko, Mob Boss",
         "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
         "Legendary Creature — Goblin Warrior"),
        ("purphoros", "Purphoros, God of the Forge",
         "Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.",
         "Legendary Enchantment Creature — God"),
        ("altar", "Phyrexian Altar",
         "Sacrifice a creature: Add one mana of any color.",
         "Artifact"),
    ]
    for oid, name, oracle, type_line in test_cards:
        abilities = parse_card(oracle, type_line)
        save_parsed(conn, oid, abilities)
    conn.commit()
    return {oid: parse_card(oracle, type_line) for oid, _, oracle, type_line in test_cards}



def test_causal_score_direct_edge(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cards = _setup_cards(conn)
    build_and_store_graph(conn, cards)
    # Purphoros as candidate, Krenko as commander — edge exists (Krenko→Purphoros)
    score = causal_score("purphoros", "krenko", set(), conn)
    assert score > 0
    conn.close()


def test_causal_score_no_edge(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cards = _setup_cards(conn)
    build_and_store_graph(conn, cards)
    score = causal_score("nonexistent", "krenko", set(), conn)
    assert score == 0
    conn.close()
