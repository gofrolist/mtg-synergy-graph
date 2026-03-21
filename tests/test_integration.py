# tests/test_integration.py
"""Integration test: runs the full pipeline on a small card set."""
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_full_pipeline(tmp_db):
    """Run tag cleanup → parse abilities → detect strategies → find combos."""
    import tag_db
    from ability_parser import parse_card, save_abilities_to_db
    from strategy_detector import detect_strategies
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Set up 3 cards mimicking a small Kyler deck
    cards = [
        ("cmdr", "Kyler, Sigardian Emissary", "Legendary Creature — Human Cleric",
         "Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler.",
         "enabler", ["human-tribal", "counter-placement", "creature-etb"], ["counter-placement"]),
        ("scales", "Hardened Scales", "Enchantment",
         "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
         "enabler", ["counter-amplification"], ["counter-placement-events"]),
        ("crusade", "Cathars' Crusade", "Enchantment",
         "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
         "enabler", ["counter-placement", "counter-placement-events"], ["creature-etb"]),
    ]

    for oid, name, tl, oracle, role, provs, wants in cards:
        conn.execute("INSERT INTO cards (oracle_id, name, type_line, oracle_text, role, keywords) VALUES (?,?,?,?,?,?)",
                     (oid, name, tl, oracle, role, "[]"))
        for p in provs:
            conn.execute("INSERT INTO provides (oracle_id, tag) VALUES (?,?)", (oid, p))
        for w in wants:
            conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?,?)", (oid, w))
    conn.commit()
    conn.close()

    # 1. Parse abilities
    parsed = []
    for oid, name, tl, oracle, role, provs, wants in cards:
        card = {"oracle_id": oid, "name": name, "type_line": tl, "oracle_text": oracle, "keywords": []}
        parsed.append((oid, parse_card(card)))
    save_abilities_to_db(parsed, tmp_db)

    # Verify abilities were stored
    conn = sqlite3.connect(tmp_db)
    ab_count = conn.execute("SELECT COUNT(*) FROM abilities").fetchone()[0]
    assert ab_count >= 3
    conn.close()

    # 2. Detect strategies for commander
    strategies = detect_strategies("cmdr", tmp_db)
    strat_names = {s["name"] for s in strategies}
    assert "humans" in strat_names or "+1/+1-counters" in strat_names

    # 3. Find combos (tiered)
    deck_oids = {"cmdr", "scales", "crusade"}
    combos = find_combos_tiered(deck_oids, tmp_db)
    assert len(combos) >= 1
