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

