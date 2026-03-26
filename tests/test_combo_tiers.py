import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_spellbook_confirmed_combo(tmp_db):
    """Deck containing all cards from a Spellbook combo should be labeled infinite-confirmed."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Two cards that form a Spellbook combo
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-a', 'Card A')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-b', 'Card B')")

    # Spellbook entry
    conn.execute("""INSERT INTO spellbook_combos (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
                    VALUES ('combo-1', '["oid-a","oid-b"]', '["Card A","Card B"]', 'Infinite damage', '', 2)""")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-1', 'oid-a')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-1', 'oid-b')")
    conn.commit()
    conn.close()

    deck_oids = {"oid-a", "oid-b"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    confirmed = [c for c in combos if c["tier"] == "infinite-confirmed"]
    assert len(confirmed) >= 1
    assert "Infinite damage" in confirmed[0]["result"]


def test_trigger_chain_combo_likely(tmp_db):
    """Cards with bidirectional causal edges should be labeled combo-likely."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Sanguine Bond + Exquisite Blood pattern:
    # Bidirectional causal edges represent the circular trigger chain
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-c', 'Sanguine Bond')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-d', 'Exquisite Blood')")

    # Bidirectional interaction edges (circular trigger chain)
    conn.execute(
        "INSERT INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
        "VALUES ('oid-c', 'oid-d', 'triggers', 0, 0, 1.5, '{\"event\": \"LifeGained\"}')")
    conn.execute(
        "INSERT INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
        "VALUES ('oid-d', 'oid-c', 'triggers', 0, 0, 1.5, '{\"event\": \"LoseLife\"}')")

    conn.commit()
    conn.close()

    deck_oids = {"oid-c", "oid-d"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    likely = [c for c in combos if c["tier"] == "combo-likely"]
    assert len(likely) == 1


def test_synergy_tier_fallback(tmp_db):
    """Pairs with one-way causal edge are detected as synergy tier."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-e', 'Card E')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-f', 'Card F')")

    # One-way interaction edge (E triggers F but not vice versa)
    conn.execute(
        "INSERT INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
        "VALUES ('oid-e', 'oid-f', 'triggers', 0, 0, 1.2, '{\"event\": \"ChangesZone\"}')")

    conn.commit()
    conn.close()

    deck_oids = {"oid-e", "oid-f"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    detected = [c for c in combos if c["tier"] in ("synergy", "combo-likely")]
    assert len(detected) >= 1


def test_trigger_chain_with_edges(tmp_db):
    """Bidirectional edges should produce combo-likely for token/sac pattern."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-tokener', 'Token Death')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-saccer', 'ETB Sac')")

    # Bidirectional interaction edges
    conn.execute(
        "INSERT INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
        "VALUES ('oid-tokener', 'oid-saccer', 'triggers', 0, 0, 1.3, '{\"event\": \"ChangesZone\"}')")
    conn.execute(
        "INSERT INTO interaction_edges (source_id, target_id, edge_type, ability_a, ability_b, strength, detail) "
        "VALUES ('oid-saccer', 'oid-tokener', 'triggers', 0, 0, 1.3, '{\"event\": \"Sacrificed\"}')")

    conn.commit()
    conn.close()

    deck_oids = {"oid-tokener", "oid-saccer"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    likely = [c for c in combos if c["tier"] == "combo-likely"]
    assert len(likely) >= 1, f"Expected combo-likely, got: {[c['tier'] for c in combos]}"
