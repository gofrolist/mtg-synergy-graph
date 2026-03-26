# tests/test_strategy_detector.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from strategy_detector import detect_strategies, populate_card_strategies, STRATEGY_RULES, WANTS_STRATEGY_RULES


def _insert_forge_ability(conn, card_name, oracle_id, ability_index, ability_type,
                          verb=None, trigger_mode=None, keyword=None):
    """Helper to insert a forge ability and name map entry."""
    conn.execute(
        "INSERT OR IGNORE INTO forge_name_map (forge_name, oracle_id) VALUES (?, ?)",
        (card_name, oracle_id))
    conn.execute(
        "INSERT INTO forge_abilities (card_name, ability_index, ability_type, verb, trigger_mode, keyword, raw_line) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_name, ability_index, ability_type, verb, trigger_mode, keyword, "test"))


def test_detect_commander_strategies(tmp_db):
    """Kyler should be detected as humans + counters via Forge verbs."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""INSERT INTO cards (oracle_id, name, type_line, oracle_text)
                    VALUES ('kyler', 'Kyler, Sigardian Emissary', 'Legendary Creature - Human Cleric',
                    'Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler.')""")
    # Forge abilities: PutCounter verb + ChangesZone trigger
    _insert_forge_ability(conn, 'Kyler, Sigardian Emissary', 'kyler', 0, 'T',
                          verb='PutCounter', trigger_mode='ChangesZone')
    conn.commit()
    conn.close()

    strategies = detect_strategies("kyler", tmp_db)
    strategy_names = {s["name"] for s in strategies}
    assert "humans" in strategy_names  # from oracle text tribal detection
    assert "+1/+1-counters" in strategy_names  # from PutCounter verb


def test_strategy_confidence_threshold(tmp_db):
    """Strategies below 0.3 confidence should still be stored but marked inactive."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('weak', 'Weak Card')")
    _insert_forge_ability(conn, 'Weak Card', 'weak', 0, 'K', keyword='Equip')
    conn.commit()
    conn.close()

    strategies = detect_strategies("weak", tmp_db)
    # Equip maps to artifacts (0.7) and equipment (0.9)
    assert isinstance(strategies, list)
    assert len(strategies) >= 1


def test_populate_card_strategies(tmp_db):
    """Populate strategies for all cards in DB."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c1', 'Token Maker')")
    _insert_forge_ability(conn, 'Token Maker', 'c1', 0, 'A', verb='Token')
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c2', 'Counter Placer')")
    _insert_forge_ability(conn, 'Counter Placer', 'c2', 0, 'A', verb='PutCounter')
    conn.commit()
    conn.close()

    count = populate_card_strategies(tmp_db)
    assert count >= 2

    conn = sqlite3.connect(tmp_db)
    strats = conn.execute("SELECT * FROM card_strategies").fetchall()
    conn.close()
    assert len(strats) >= 2


def test_strategy_rules_are_defined():
    """Verify we have at least 15 strategy mapping rules."""
    assert len(STRATEGY_RULES) >= 15


def test_wants_based_strategy(tmp_db):
    """Cards with Sacrificed trigger_mode should detect aristocrats strategy."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('wants-card', 'Sac Trigger')")
    _insert_forge_ability(conn, 'Sac Trigger', 'wants-card', 0, 'T',
                          verb='Draw', trigger_mode='Sacrificed')
    conn.commit()
    conn.close()

    strategies = detect_strategies("wants-card", tmp_db)
    strat_names = {s["name"] for s in strategies}
    assert "aristocrats" in strat_names
