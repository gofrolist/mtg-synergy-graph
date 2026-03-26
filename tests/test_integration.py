# tests/test_integration.py
"""Integration test: runs the full pipeline on a small card set."""
import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_full_pipeline(tmp_db):
    """Run parse abilities -> detect strategies -> find combos."""
    from ability_parser import parse_card, save_abilities_to_db
    from strategy_detector import detect_strategies
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Set up 3 cards mimicking a small Kyler deck
    cards_data = [
        ("cmdr", "Kyler, Sigardian Emissary", "Legendary Creature — Human Cleric",
         "Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler."),
        ("scales", "Hardened Scales", "Enchantment",
         "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead."),
        ("crusade", "Cathars' Crusade", "Enchantment",
         "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control."),
    ]

    for oid, name, tl, oracle in cards_data:
        conn.execute("INSERT INTO cards (oracle_id, name, type_line, oracle_text, role, keywords) VALUES (?,?,?,?,?,?)",
                     (oid, name, tl, oracle, "enabler", "[]"))

    # Insert Forge abilities for strategy detection
    conn.execute("INSERT INTO forge_name_map (forge_name, oracle_id) VALUES ('Kyler, Sigardian Emissary', 'cmdr')")
    conn.execute("INSERT INTO forge_abilities (card_name, ability_index, ability_type, verb, trigger_mode, raw_line) "
                 "VALUES ('Kyler, Sigardian Emissary', 0, 'T', 'PutCounter', 'ChangesZone', 'test')")
    conn.execute("INSERT INTO forge_abilities (card_name, ability_index, ability_type, verb, raw_line) "
                 "VALUES ('Kyler, Sigardian Emissary', 1, 'S', 'PumpAll', 'test')")

    conn.execute("INSERT INTO forge_name_map (forge_name, oracle_id) VALUES ('Hardened Scales', 'scales')")
    conn.execute("INSERT INTO forge_abilities (card_name, ability_index, ability_type, verb, raw_line) "
                 "VALUES ('Hardened Scales', 0, 'R', 'PutCounter', 'test')")

    conn.execute("INSERT INTO forge_name_map (forge_name, oracle_id) VALUES ('Cathars'' Crusade', 'crusade')")
    conn.execute("INSERT INTO forge_abilities (card_name, ability_index, ability_type, verb, trigger_mode, raw_line) "
                 "VALUES ('Cathars'' Crusade', 0, 'T', 'PutCounterAll', 'ChangesZone', 'test')")

    # Insert interaction edges for combo detection
    # Kyler triggers Cathars' Crusade (both care about creatures entering)
    conn.execute(
        "INSERT INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
        "VALUES ('cmdr', 'crusade', 'triggers', 0, 0, 1.5, '{\"event\": \"ChangesZone\"}')")
    conn.execute(
        "INSERT INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
        "VALUES ('crusade', 'cmdr', 'triggers', 0, 0, 1.5, '{\"event\": \"CounterAdded\"}')")

    conn.commit()
    conn.close()

    # 1. Parse abilities
    parsed = []
    for oid, name, tl, oracle in cards_data:
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
