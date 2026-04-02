"""
SQLite tag database — card schema and query utilities.

This module provides the cards table schema and card lookup functions.
"""

import os
import sqlite3

from mtg_synergy.db import get_connection

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    oracle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type_line TEXT DEFAULT '',
    oracle_text TEXT DEFAULT '',
    mana_cost TEXT DEFAULT '',
    cmc REAL DEFAULT 0,
    colors TEXT DEFAULT '[]',
    color_identity TEXT DEFAULT '[]',
    keywords TEXT DEFAULT '[]',
    power TEXT,
    toughness TEXT,
    loyalty TEXT,
    rarity TEXT DEFAULT '',
    edhrec_rank INTEGER,
    legal_commander INTEGER DEFAULT 1,
    role TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);

CREATE TABLE IF NOT EXISTS card_strategies (
    oracle_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (oracle_id, strategy),
    FOREIGN KEY (oracle_id) REFERENCES cards(oracle_id)
);
CREATE INDEX IF NOT EXISTS idx_strategies_strategy ON card_strategies(strategy);
CREATE INDEX IF NOT EXISTS idx_strategies_oracle ON card_strategies(oracle_id, confidence DESC);

CREATE TABLE IF NOT EXISTS spellbook_combos (
    combo_id TEXT PRIMARY KEY,
    card_oracle_ids TEXT NOT NULL,
    card_names TEXT NOT NULL,
    result TEXT,
    prerequisites TEXT,
    card_count INTEGER
);

CREATE TABLE IF NOT EXISTS spellbook_combo_cards (
    combo_id TEXT NOT NULL,
    oracle_id TEXT NOT NULL,
    PRIMARY KEY (combo_id, oracle_id),
    FOREIGN KEY (combo_id) REFERENCES spellbook_combos(combo_id)
);
CREATE INDEX IF NOT EXISTS idx_spellbook_cards_oracle ON spellbook_combo_cards(oracle_id);
"""


# ── Query API ──


def _row_to_card(row: sqlite3.Row) -> dict:
    card = {
        "oracle_id": row["oracle_id"],
        "name": row["name"],
        "type_line": row["type_line"],
        "oracle_text": row["oracle_text"],
        "role": row["role"],
        "edhrec_rank": row["edhrec_rank"],
    }
    for field in ("mana_cost", "cmc", "colors", "color_identity", "keywords",
                  "power", "toughness", "loyalty", "rarity", "legal_commander"):
        try:
            card[field] = row[field]
        except (IndexError, KeyError):
            pass
    return card


def get_cards_by_names(names: list[str], db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row

    cards = []
    chunk_size = 500
    for i in range(0, len(names), chunk_size):
        chunk = names[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM cards WHERE name IN ({placeholders})", chunk
        ).fetchall()
        cards.extend(_row_to_card(r) for r in rows)

    conn.close()
    return cards
