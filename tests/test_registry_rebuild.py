import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from mtg_synergy import tag_db


def test_rebuild_registry(tmp_db, tmp_path):
    """Registry rebuild collects tags with 3+ occurrences."""
    conn = sqlite3.connect(tmp_db)
    for i in range(3):
        conn.execute("INSERT INTO cards (oracle_id, name) VALUES (?, ?)", (f"card-{i}", f"Card {i}"))
        conn.execute("INSERT INTO provides (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "tokens-creature"))
        conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "etb-value"))
    for i in range(2):
        conn.execute("INSERT INTO provides (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "rare-tag"))
        conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", (f"card-{i}", "rare-want"))
    conn.commit()
    conn.close()

    output_path = str(tmp_path / "registry.json")
    tag_db.rebuild_registry(tmp_db, output_path, min_freq=3)

    with open(output_path) as f:
        registry = json.load(f)

    assert "tokens-creature" in registry["provides"]["tags"]
    assert "etb-value" in registry["wants"]["tags"]
    assert "rare-tag" not in registry["provides"]["tags"]
    assert "rare-want" not in registry["wants"]["tags"]
    assert registry["_meta"]["version"] == "4.0"
