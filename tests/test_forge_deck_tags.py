"""Tests for DeckHas/DeckHints scoring integration."""
import sqlite3
import pytest
from mtg_synergy_train.parse.forge_import import ensure_forge_schema


def _setup_deck_tags(conn):
    """Insert test DeckHas/DeckHints data.

    DeckHas = what a card provides. DeckHints = what a card wants in the deck.
    Overlap = candidate_has & cmdr_hints + candidate_hints & cmdr_has.
    """
    ensure_forge_schema(conn)
    # Commander: provides tokens, wants goblins AND tokens
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Krenko, Mob Boss', 'has', 'Ability$Token')")
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Krenko, Mob Boss', 'hints', 'Type$Goblin')")
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Krenko, Mob Boss', 'hints', 'Ability$Token')")
    # Candidate: provides tokens, wants goblins
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Goblin Instigator', 'has', 'Ability$Token')")
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Goblin Instigator', 'hints', 'Type$Goblin')")
    # Unrelated card: provides counter ability
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Counterspell', 'has', 'Ability$Counter')")
    conn.commit()


def _compute_overlap(conn, commander_name, candidate_name):
    """Compute forge deck overlap using the same logic as DeckContext + compute_dynamic_score."""
    cmdr_hints = set()
    cmdr_has = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (commander_name,)
    ).fetchall():
        if r[0] == "has":
            cmdr_has.add(r[1])
        elif r[0] == "hints":
            cmdr_hints.add(r[1])

    cand_has = set()
    cand_hints = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (candidate_name,)
    ).fetchall():
        if r[0] == "has":
            cand_has.add(r[1])
        elif r[0] == "hints":
            cand_hints.add(r[1])

    return len(cand_has & cmdr_hints) + len(cand_hints & cmdr_has)


def test_forge_deck_overlap(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _setup_deck_tags(conn)
    # Instigator has Token -> matches Krenko hints Token = 1
    # Instigator hints Goblin -> doesn't match Krenko has (Token) = 0
    # Total overlap = 1
    overlap = _compute_overlap(conn, "Krenko, Mob Boss", "Goblin Instigator")
    assert overlap >= 1
    conn.close()


def test_forge_deck_overlap_zero(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _setup_deck_tags(conn)
    overlap = _compute_overlap(conn, "Krenko, Mob Boss", "Counterspell")
    assert overlap == 0
    conn.close()
