# tests/test_fetch_spellbook.py
import json
import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fetch_spellbook import parse_combo_response, import_combos_to_db


def test_parse_combo_response():
    """Parse a single Spellbook API response into our format."""
    raw = {
        "id": "742-1295",
        "uses": [
            {
                "card": {
                    "name": "Demonic Consultation",
                    "oracleId": "oracle-demcon"
                },
                "quantity": 1,
            },
            {
                "card": {
                    "name": "Thassa's Oracle",
                    "oracleId": "oracle-thoracle"
                },
                "quantity": 1,
            }
        ],
        "produces": [
            {"feature": {"name": "Win the game"}, "quantity": 1}
        ],
        "status": "OK",
        "easyPrerequisites": "Both cards in hand. {1}{U}{U}{B} available.",
    }

    combo = parse_combo_response(raw)
    assert combo["combo_id"] == "742-1295"
    assert len(combo["card_oracle_ids"]) == 2
    assert "oracle-demcon" in combo["card_oracle_ids"]
    assert "oracle-thoracle" in combo["card_oracle_ids"]
    assert "Win the game" in combo["result"]
    assert combo["card_count"] == 2


def test_import_combos_to_db(tmp_db):
    """Import parsed combos into the database."""
    combos = [
        {
            "combo_id": "test-001",
            "card_oracle_ids": ["oid-a", "oid-b"],
            "card_names": ["Card A", "Card B"],
            "result": "Infinite damage",
            "prerequisites": "Both on battlefield",
            "card_count": 2,
        }
    ]

    import_combos_to_db(combos, tmp_db)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT * FROM spellbook_combos WHERE combo_id = 'test-001'").fetchone()
    assert row is not None

    combo_cards = conn.execute(
        "SELECT oracle_id FROM spellbook_combo_cards WHERE combo_id = 'test-001'"
    ).fetchall()
    assert len(combo_cards) == 2
    conn.close()


def test_skip_non_ok_status():
    """Combos with non-OK status should be skipped."""
    raw = {
        "id": "bad-001",
        "uses": [{"card": {"name": "X", "oracleId": "oid-x"}, "quantity": 1}],
        "produces": [],
        "status": "NOT_WORKING",
        "easyPrerequisites": "",
    }
    combo = parse_combo_response(raw)
    assert combo is None
