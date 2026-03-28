import sqlite3
import json
import os
import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite DB with the full schema for testing."""
    import tag_db
    db_path = str(tmp_path / "test_tags.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(tag_db.SCHEMA)
    # Add Forge tables used by strategy_detector, combo detector, etc.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS forge_abilities (
            card_name TEXT NOT NULL,
            ability_index INTEGER NOT NULL,
            ability_type TEXT NOT NULL,
            verb TEXT,
            trigger_mode TEXT,
            trigger_filter TEXT,
            trigger_origin TEXT,
            trigger_destination TEXT,
            trigger_phase TEXT,
            trigger_zones TEXT,
            target TEXT,
            defined TEXT,
            amount TEXT,
            cost TEXT,
            keyword TEXT,
            token_script TEXT,
            counter_type TEXT,
            sub_ability TEXT,
            unless_cost TEXT,
            raw_line TEXT NOT NULL,
            PRIMARY KEY (card_name, ability_index)
        );
        CREATE INDEX IF NOT EXISTS idx_forge_ab_name ON forge_abilities(card_name);
        CREATE TABLE IF NOT EXISTS forge_name_map (
            forge_name TEXT PRIMARY KEY,
            oracle_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS interaction_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            ability_a INTEGER NOT NULL,
            ability_b INTEGER NOT NULL,
            strength REAL NOT NULL,
            detail TEXT NOT NULL,
            filter_precision TEXT,
            PRIMARY KEY (source_id, target_id, edge_type, ability_a, ability_b)
        );
        CREATE INDEX IF NOT EXISTS idx_edges_source ON interaction_edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON interaction_edges(target_id);
    """)
    conn.commit()
    yield db_path
    conn.close()

@pytest.fixture
def sample_cards():
    """Return a few well-known cards with oracle text for parser testing."""
    return [
        {
            "oracle_id": "kyler-001",
            "name": "Kyler, Sigardian Emissary",
            "type_line": "Legendary Creature — Human Cleric",
            "oracle_text": "Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler.",
            "mana_cost": "{3}{G}{W}",
            "keywords": []
        },
        {
            "oracle_id": "hardened-001",
            "name": "Hardened Scales",
            "type_line": "Enchantment",
            "oracle_text": "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
            "mana_cost": "{G}",
            "keywords": []
        },
        {
            "oracle_id": "cathars-001",
            "name": "Cathars' Crusade",
            "type_line": "Enchantment",
            "oracle_text": "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
            "mana_cost": "{3}{W}{W}",
            "keywords": []
        },
        {
            "oracle_id": "skirk-001",
            "name": "Skirk Prospector",
            "type_line": "Creature — Goblin",
            "oracle_text": "Sacrifice a Goblin: Add {R}.",
            "mana_cost": "{R}",
            "keywords": []
        },
        {
            "oracle_id": "gavony-001",
            "name": "Gavony Township",
            "type_line": "Land",
            "oracle_text": "{T}: Add {C}.\n{2}{G}{W}, {T}: Put a +1/+1 counter on each creature you control.",
            "mana_cost": "",
            "keywords": []
        },
        {
            "oracle_id": "beast-001",
            "name": "Beast Within",
            "type_line": "Instant",
            "oracle_text": "Destroy target permanent. Its controller creates a 3/3 green Beast creature token.",
            "mana_cost": "{2}{G}",
            "keywords": []
        },
        {
            "oracle_id": "jace-001",
            "name": "Jace, the Mind Sculptor",
            "type_line": "Legendary Planeswalker — Jace",
            "oracle_text": "+2: Look at the top card of target player's library. You may put that card on the bottom of that player's library.\n0: Draw three cards, then put two cards from your hand on top of your library.\n−1: Return target creature to its owner's hand.\n−12: Exile all cards from target player's library, then that player shuffles their hand into their library.",
            "mana_cost": "{2}{U}{U}",
            "keywords": []
        },
        {
            "oracle_id": "binding-001",
            "name": "Binding the Old Gods",
            "type_line": "Enchantment — Saga",
            "oracle_text": "I — Destroy target nonland permanent an opponent controls.\nII — Search your library for a Forest card, put it onto the battlefield tapped, then shuffle.\nIII — Exile this Saga, then return it to the battlefield transformed.",
            "mana_cost": "{2}{B}{G}",
            "keywords": []
        },
        {
            "oracle_id": "delver-001",
            "name": "Delver of Secrets // Insectile Aberration",
            "type_line": "Creature — Human Wizard // Creature — Human Insect",
            "oracle_text": "At the beginning of your upkeep, look at the top card of your library. You may reveal that card. If an instant or sorcery card is revealed this way, transform Delver of Secrets. // Flying",
            "mana_cost": "{U}",
            "keywords": ["flying", "transform"]
        },
        {
            "oracle_id": "bonecrusher-001",
            "name": "Bonecrusher Giant // Stomp",
            "type_line": "Creature — Giant // Instant — Adventure",
            "oracle_text": "Whenever Bonecrusher Giant becomes the target of a spell, Bonecrusher Giant deals 2 damage to that spell's controller. // Damage can't be prevented this turn. Stomp deals 2 damage to any target.",
            "mana_cost": "{2}{R}",
            "keywords": ["adventure"]
        },
    ]
