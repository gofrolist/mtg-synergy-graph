# tests/test_strategy_detector.py
import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from strategy_detector import detect_strategies, populate_card_strategies


def test_detect_tribal_from_oracle_text(tmp_db):
    """Kyler should be detected as humans from oracle text tribal detection."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""INSERT INTO cards (oracle_id, name, type_line, oracle_text)
                    VALUES ('kyler', 'Kyler, Sigardian Emissary', 'Legendary Creature - Human Cleric',
                    'Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler.')""")
    conn.commit()
    conn.close()

    strategies = detect_strategies("kyler", tmp_db)
    strategy_names = {s["name"] for s in strategies}
    assert "humans" in strategy_names


def test_populate_card_strategies(tmp_db):
    """Populate strategies for all cards in DB."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""INSERT INTO cards (oracle_id, name, oracle_text)
                    VALUES ('c1', 'Goblin Lord', 'Other Goblins you control get +1/+1.')""")
    conn.execute("""INSERT INTO cards (oracle_id, name, oracle_text)
                    VALUES ('c2', 'Elf Druid', 'Whenever an Elf enters the battlefield, draw a card.')""")
    conn.commit()
    conn.close()

    count = populate_card_strategies(tmp_db)
    assert count >= 2

    conn = sqlite3.connect(tmp_db)
    strats = conn.execute("SELECT * FROM card_strategies").fetchall()
    conn.close()
    assert len(strats) >= 2
    strat_names = {s[1] for s in strats}
    assert "goblins" in strat_names
    assert "elves" in strat_names


def test_detect_returns_sorted_by_confidence(tmp_db):
    """detect_strategies should return strategies sorted by confidence descending."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""INSERT INTO cards (oracle_id, name, oracle_text)
                    VALUES ('multi', 'Multi Tribal',
                    'Whenever a Zombie or Vampire enters the battlefield, each other Zombie and Vampire gets +1/+1.')""")
    conn.commit()
    conn.close()

    strategies = detect_strategies("multi", tmp_db)
    assert len(strategies) >= 2
    confidences = [s["confidence"] for s in strategies]
    assert confidences == sorted(confidences, reverse=True)


def test_detect_no_strategies_for_vanilla(tmp_db):
    """Vanilla creature with no tribal text should return empty strategies."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("""INSERT INTO cards (oracle_id, name, oracle_text)
                    VALUES ('vanilla', 'Grizzly Bears', '')""")
    conn.commit()
    conn.close()

    strategies = detect_strategies("vanilla", tmp_db)
    assert strategies == []
