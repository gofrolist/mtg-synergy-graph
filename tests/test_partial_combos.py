import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_partial_spellbook_match(tmp_db):
    """Should detect when deck is 1 card away from a Spellbook combo."""
    from synergy_graph import find_partial_combos

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-x', 'Card X')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-y', 'Card Y')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-z', 'Card Z')")

    # 3-card combo, but deck only has 2 of 3
    conn.execute("""INSERT INTO spellbook_combos (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
                    VALUES ('combo-2', '["oid-x","oid-y","oid-z"]', '["Card X","Card Y","Card Z"]', 'Infinite tokens', '', 3)""")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-2', 'oid-x')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-2', 'oid-y')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-2', 'oid-z')")
    conn.commit()
    conn.close()

    deck_oids = {"oid-x", "oid-y"}  # Missing oid-z
    partials = find_partial_combos(deck_oids, tmp_db)

    assert len(partials) >= 1
    assert partials[0]["missing_cards"] == ["Card Z"]
    assert "Infinite tokens" in partials[0]["result"]


def test_complete_combo_not_partial(tmp_db):
    """A complete combo in deck should NOT appear as partial."""
    from synergy_graph import find_partial_combos

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-1', 'Card 1')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-2', 'Card 2')")

    conn.execute("""INSERT INTO spellbook_combos (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
                    VALUES ('combo-complete', '["oid-1","oid-2"]', '["Card 1","Card 2"]', 'Infinite mana', '', 2)""")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-complete', 'oid-1')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-complete', 'oid-2')")
    conn.commit()
    conn.close()

    deck_oids = {"oid-1", "oid-2"}  # Complete — 0 missing
    partials = find_partial_combos(deck_oids, tmp_db)

    assert len(partials) == 0


def test_two_cards_missing_not_partial(tmp_db):
    """A combo with 2+ missing cards should NOT appear as partial (only 1-away)."""
    from synergy_graph import find_partial_combos

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-p', 'Card P')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-q', 'Card Q')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-r', 'Card R')")

    conn.execute("""INSERT INTO spellbook_combos (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
                    VALUES ('combo-far', '["oid-p","oid-q","oid-r"]', '["Card P","Card Q","Card R"]', 'Infinite draw', '', 3)""")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-far', 'oid-p')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-far', 'oid-q')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-far', 'oid-r')")
    conn.commit()
    conn.close()

    deck_oids = {"oid-p"}  # Missing 2 cards (oid-q and oid-r)
    partials = find_partial_combos(deck_oids, tmp_db)

    assert len(partials) == 0
