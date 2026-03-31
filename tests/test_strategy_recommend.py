import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_strategy_weighted_scoring(tmp_db):
    """Cards matching active strategies should score higher."""
    conn = sqlite3.connect(tmp_db)
    # Commander: token strategy
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('cmdr', 'Token Commander')")

    # Candidate A: matches token strategy
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('cand-a', 'Token Card')")
    conn.execute("INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('cand-a', 'tokens', 1.0)")

    # Candidate B: no strategy match
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('cand-b', 'Random Card')")

    conn.commit()
    conn.close()

    from synergy_graph import compute_strategy_relevance

    # Active strategies: tokens
    active = {"tokens"}
    rel_a = compute_strategy_relevance("cand-a", active, tmp_db)
    rel_b = compute_strategy_relevance("cand-b", active, tmp_db)

    assert rel_a > rel_b
    assert rel_a >= 1.0
    assert rel_b == 0.5  # penalty for no match


def test_no_active_strategies_returns_one(tmp_db):
    """When no strategies are active, all cards return 1.0."""
    from synergy_graph import compute_strategy_relevance

    rel = compute_strategy_relevance("any-card", set(), tmp_db)
    assert rel == 1.0


def test_multiple_strategy_matches(tmp_db):
    """Card matching N strategies gets 1.0 + 0.2*N multiplier."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('multi', 'Multi-Strategy Card')")
    conn.execute("INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('multi', 'tokens', 0.9)")
    conn.execute("INSERT INTO card_strategies (oracle_id, strategy, confidence) VALUES ('multi', 'counters', 0.8)")
    conn.commit()
    conn.close()

    from synergy_graph import compute_strategy_relevance

    active = {"tokens", "counters"}
    rel = compute_strategy_relevance("multi", active, tmp_db)
    assert rel == 1.4  # 1.0 + 0.2*2
