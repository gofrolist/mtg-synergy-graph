import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from mtg_synergy import tag_db


def test_new_tables_exist(tmp_db):
    """All tables should be created by SCHEMA."""
    conn = sqlite3.connect(tmp_db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    conn.close()
    assert "cards" in tables
    assert "card_strategies" in tables
    assert "spellbook_combos" in tables
    assert "spellbook_combo_cards" in tables


def test_card_strategies_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(card_strategies)").fetchall()]
    conn.close()
    assert "oracle_id" in cols
    assert "strategy" in cols
    assert "confidence" in cols
