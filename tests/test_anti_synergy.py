import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_anti_synergy_detection(tmp_db):
    """Cards with no strategy overlap and non-staple role should be flagged."""
    from synergy_graph import find_anti_synergy

    conn = sqlite3.connect(tmp_db)
    # Card with strategy overlap
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('good', 'Good Card', 'threat')")
    conn.execute("INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('good', 'tokens', 1.0)")

    # Card with no overlap, non-staple
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('bad', 'Bad Card', 'threat')")

    # Staple card with no overlap — should NOT be flagged
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('staple', 'Swords to Plowshares', 'removal')")

    conn.commit()
    conn.close()

    deck_oids = {"good", "bad", "staple"}
    active_strategies = {"tokens"}

    anti = find_anti_synergy(deck_oids, active_strategies, tmp_db)
    assert len(anti) == 1
    assert anti[0]["name"] == "Bad Card"


def test_all_staple_roles_exempt(tmp_db):
    """All cards in STAPLE_ROLES should be exempt from anti-synergy flagging."""
    from synergy_graph import find_anti_synergy, STAPLE_ROLES

    conn = sqlite3.connect(tmp_db)
    for i, role in enumerate(sorted(STAPLE_ROLES)):
        conn.execute(
            "INSERT INTO cards (oracle_id, name, role) VALUES (?, ?, ?)",
            (f"card-{i}", f"Staple {role}", role)
        )
    conn.commit()
    conn.close()

    deck_oids = {f"card-{i}" for i in range(len(STAPLE_ROLES))}
    active_strategies = {"tokens"}

    anti = find_anti_synergy(deck_oids, active_strategies, tmp_db)
    assert len(anti) == 0


def test_no_active_strategies_returns_empty(tmp_db):
    """When no strategies are active, find_anti_synergy returns empty list."""
    from synergy_graph import find_anti_synergy

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('x', 'Some Card', 'threat')")
    conn.commit()
    conn.close()

    anti = find_anti_synergy({"x"}, set(), tmp_db)
    assert anti == []


def test_high_synergy_not_flagged(tmp_db):
    """Cards with many synergy connections should not be flagged even without strategy match."""
    from synergy_graph import find_anti_synergy

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name, role) VALUES ('connected', 'Well Connected', 'threat')")
    conn.commit()
    conn.close()

    # Mock graph where the card has many connections
    mock_graph = {
        "adjacency": {
            "Well Connected": [
                {"target": "Deck Card 1", "source": "Well Connected", "score": 5.0, "signals": 1},
                {"target": "Deck Card 2", "source": "Well Connected", "score": 5.0, "signals": 1},
                {"target": "Deck Card 3", "source": "Well Connected", "score": 5.0, "signals": 1},
                {"target": "Deck Card 4", "source": "Well Connected", "score": 5.0, "signals": 1},
                {"target": "Deck Card 5", "source": "Well Connected", "score": 5.0, "signals": 1},
            ]
        }
    }
    deck_set = {"Well Connected", "Deck Card 1", "Deck Card 2", "Deck Card 3", "Deck Card 4", "Deck Card 5"}
    deck_oids = {"connected"}
    active_strategies = {"tokens"}

    anti = find_anti_synergy(deck_oids, active_strategies, tmp_db,
                             graph=mock_graph, deck_cards_set=deck_set)
    # Should NOT be flagged because it has 5 partners with 25.0 total score
    assert len(anti) == 0
