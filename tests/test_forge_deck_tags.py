"""Tests for DeckHas/DeckHints scoring integration."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_import import ensure_forge_schema


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


def test_forge_deck_overlap(tmp_db):
    from mtg_synergy.recommend.scoring import compute_forge_deck_overlap
    conn = sqlite3.connect(tmp_db)
    _setup_deck_tags(conn)
    # Instigator has Token -> matches Krenko hints Token = 1
    # Instigator hints Goblin -> doesn't match Krenko has (Token) = 0
    # Total overlap = 1
    overlap = compute_forge_deck_overlap(conn, "Krenko, Mob Boss", "Goblin Instigator")
    assert overlap >= 1
    conn.close()


def test_forge_deck_overlap_zero(tmp_db):
    from mtg_synergy.recommend.scoring import compute_forge_deck_overlap
    conn = sqlite3.connect(tmp_db)
    _setup_deck_tags(conn)
    overlap = compute_forge_deck_overlap(conn, "Krenko, Mob Boss", "Counterspell")
    assert overlap == 0
    conn.close()
