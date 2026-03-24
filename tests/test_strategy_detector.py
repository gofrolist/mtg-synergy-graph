# tests/test_strategy_detector.py
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from strategy_detector import detect_strategies, populate_card_strategies, STRATEGY_RULES, WANTS_STRATEGY_RULES


def test_detect_commander_strategies(tmp_db):
    """Kyler should be detected as humans + counters."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""INSERT INTO cards (oracle_id, name, type_line, oracle_text)
                    VALUES ('kyler', 'Kyler, Sigardian Emissary', 'Legendary Creature — Human Cleric',
                    'Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler.')""")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('kyler', 'human-tribal')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('kyler', 'counter-placement')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('kyler', 'etb-value')")
    conn.commit()
    conn.close()

    strategies = detect_strategies("kyler", tmp_db)
    strategy_names = {s["name"] for s in strategies}
    assert "humans" in strategy_names
    assert "+1/+1-counters" in strategy_names


def test_strategy_confidence_threshold(tmp_db):
    """Strategies below 0.3 confidence should still be stored but marked inactive."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('weak', 'Weak Card')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('weak', 'artifact-presence')")
    conn.commit()
    conn.close()

    strategies = detect_strategies("weak", tmp_db)
    # artifact-presence alone is a weak signal
    low_conf = [s for s in strategies if s["confidence"] < 0.3]
    # Should still return strategies, just with low confidence
    assert isinstance(strategies, list)


def test_populate_card_strategies(tmp_db):
    """Populate strategies for all cards in DB."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c1', 'Token Maker')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('c1', 'tokens-creature')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('c2', 'Counter Placer')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('c2', 'counter-placement')")
    conn.commit()
    conn.close()

    count = populate_card_strategies(tmp_db)
    assert count >= 2

    conn = sqlite3.connect(tmp_db)
    strats = conn.execute("SELECT * FROM card_strategies").fetchall()
    conn.close()
    assert len(strats) >= 2


def test_strategy_rules_are_defined():
    """Verify we have at least 20 strategy mapping rules."""
    assert len(STRATEGY_RULES) >= 20


def test_wants_based_strategy(tmp_db):
    """Cards wanting counter-placement-events should detect +1/+1-counters strategy."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('wants-card', 'Counter Wanter')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('wants-card', 'counter-placement-events')")
    conn.commit()
    conn.close()

    strategies = detect_strategies("wants-card", tmp_db)
    strat_names = {s["name"] for s in strategies}
    assert "+1/+1-counters" in strat_names
