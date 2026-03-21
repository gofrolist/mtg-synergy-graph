import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tag_db


def test_tribal_cleanup_removes_false_positives(tmp_db):
    """Cards wanting X-tribal but not mentioning X in oracle text should be cleaned."""
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO cards (oracle_id, name, oracle_text) VALUES (?, ?, ?)",
        ("goblin-lord", "Goblin Chieftain", "Other Goblins you control get +1/+1 and have haste.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("goblin-lord", "goblin-tribal"))

    conn.execute(
        "INSERT INTO cards (oracle_id, name, oracle_text) VALUES (?, ?, ?)",
        ("random-creature", "Llanowar Elves", "Tap: Add {G}.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("random-creature", "goblin-tribal"))

    conn.execute(
        "INSERT INTO cards (oracle_id, name, oracle_text) VALUES (?, ?, ?)",
        ("human-lord", "Thalia's Lieutenant", "When Thalia's Lieutenant enters, put a +1/+1 counter on each other Human you control.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("human-lord", "human-tribal"))

    conn.commit()
    conn.close()

    removed = tag_db.fix_tribal_wants(tmp_db)
    assert len(removed) == 1
    assert removed[0]["name"] == "Llanowar Elves"
    assert removed[0]["tag"] == "goblin-tribal"

    conn = sqlite3.connect(tmp_db)
    remaining = conn.execute("SELECT oracle_id, tag FROM wants WHERE tag LIKE '%-tribal'").fetchall()
    conn.close()
    assert len(remaining) == 2
    remaining_ids = {r[0] for r in remaining}
    assert "goblin-lord" in remaining_ids
    assert "human-lord" in remaining_ids
    assert "random-creature" not in remaining_ids


def test_tribal_cleanup_handles_type_line_creatures(tmp_db):
    """Creatures OF a type but not MENTIONING the type in oracle text should lose tribal wants."""
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO cards (oracle_id, name, type_line, oracle_text) VALUES (?, ?, ?, ?)",
        ("mogg-fanatic", "Mogg Fanatic", "Creature — Goblin", "Sacrifice Mogg Fanatic: It deals 1 damage to any target.")
    )
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", ("mogg-fanatic", "goblin-tribal"))
    conn.commit()
    conn.close()

    removed = tag_db.fix_tribal_wants(tmp_db)
    assert len(removed) == 1
    assert removed[0]["name"] == "Mogg Fanatic"
