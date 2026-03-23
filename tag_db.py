"""
SQLite tag database for storing and querying card tags at scale.

Replaces loading entire merged JSON files into memory. Enables targeted
queries for specific cards, tags, or tag relationships.

Usage:
    python3 tag_db.py import data/top10000_tags.json         # import LLM tags
    python3 tag_db.py import data/kyler_merged.json          # import merged tags
    python3 tag_db.py stats                                   # show DB stats
    python3 tag_db.py query --provides token-generation       # find providers
    python3 tag_db.py query --wants creature-etb              # find wanters
"""

import argparse
import json
import os
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
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

CREATE TABLE IF NOT EXISTS provides (
    oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
    tag TEXT NOT NULL,
    PRIMARY KEY (oracle_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_provides_tag ON provides(tag);
CREATE INDEX IF NOT EXISTS idx_provides_oracle_id ON provides(oracle_id);

CREATE TABLE IF NOT EXISTS wants (
    oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
    tag TEXT NOT NULL,
    PRIMARY KEY (oracle_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_wants_tag ON wants(tag);
CREATE INDEX IF NOT EXISTS idx_wants_oracle_id ON wants(oracle_id);

CREATE TABLE IF NOT EXISTS scryfall_tags (
    oracle_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    weight INTEGER DEFAULT 0,
    PRIMARY KEY (oracle_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_scryfall_tags_tag ON scryfall_tags(tag);
CREATE INDEX IF NOT EXISTS idx_scryfall_tags_oid ON scryfall_tags(oracle_id);

CREATE TABLE IF NOT EXISTS abilities (
    oracle_id TEXT NOT NULL,
    ability_index INTEGER NOT NULL,
    ability_type TEXT NOT NULL,
    trigger_condition TEXT,
    trigger_tags TEXT,
    cost TEXT,
    effect TEXT,
    effect_tags TEXT,
    zone TEXT DEFAULT 'battlefield',
    targets TEXT,
    is_mana_ability BOOLEAN DEFAULT 0,
    PRIMARY KEY (oracle_id, ability_index),
    FOREIGN KEY (oracle_id) REFERENCES cards(oracle_id)
);
CREATE INDEX IF NOT EXISTS idx_abilities_type ON abilities(ability_type);
CREATE INDEX IF NOT EXISTS idx_abilities_oracle ON abilities(oracle_id);

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


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str = DB_PATH):
    """Create tables and indices if they don't exist."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.close()


def import_cards(cards: list[dict], db_path: str = DB_PATH, normalize: bool = True):
    """Import cards into the database. Upserts on oracle_id."""
    if normalize:
        from normalize_tags import normalize_cards
        normalize_cards(cards)

    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("BEGIN")
    for card in cards:
        oid = card.get("oracle_id")
        if not oid:
            continue

        cur.execute(
            "INSERT OR REPLACE INTO cards "
            "(oracle_id, name, type_line, oracle_text, mana_cost, cmc, colors, "
            "color_identity, keywords, power, toughness, loyalty, rarity, "
            "edhrec_rank, legal_commander, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                oid,
                card.get("name", ""),
                card.get("type_line", ""),
                card.get("oracle_text", ""),
                card.get("mana_cost", ""),
                card.get("cmc", 0),
                json.dumps(card.get("colors", [])),
                json.dumps(card.get("color_identity", [])),
                json.dumps(card.get("keywords", [])),
                card.get("power"),
                card.get("toughness"),
                card.get("loyalty"),
                card.get("rarity", ""),
                card.get("edhrec_rank"),
                1 if card.get("legalities", {}).get("commander") == "legal" else 0,
                card.get("role", ""),
            ),
        )

        # Clear existing tags for this card
        cur.execute("DELETE FROM provides WHERE oracle_id = ?", (oid,))
        cur.execute("DELETE FROM wants WHERE oracle_id = ?", (oid,))
        for tag in card.get("provides", []):
            cur.execute("INSERT OR IGNORE INTO provides VALUES (?, ?)", (oid, tag))
        for tag in card.get("wants", []):
            cur.execute("INSERT OR IGNORE INTO wants VALUES (?, ?)", (oid, tag))

    conn.commit()
    conn.close()


def backfill_scryfall(scryfall_path: str, db_path: str = DB_PATH):
    """Backfill Scryfall metadata (mana_cost, colors, legality, etc.) into existing cards."""
    with open(scryfall_path) as f:
        scryfall = json.load(f)

    lookup = {}
    for c in scryfall:
        oid = c.get("oracle_id")
        if not oid:
            continue
        text = c.get("oracle_text", "")
        if not text:
            faces = c.get("card_faces", [])
            if faces:
                text = " // ".join(f.get("oracle_text", "") for f in faces)
        lookup[oid] = {
            "type_line": c.get("type_line", ""),
            "oracle_text": text,
            "mana_cost": c.get("mana_cost", ""),
            "cmc": c.get("cmc", 0),
            "colors": json.dumps(c.get("colors", [])),
            "color_identity": json.dumps(c.get("color_identity", [])),
            "keywords": json.dumps(c.get("keywords", [])),
            "power": c.get("power"),
            "toughness": c.get("toughness"),
            "loyalty": c.get("loyalty"),
            "rarity": c.get("rarity", ""),
            "edhrec_rank": c.get("edhrec_rank"),
            "legal_commander": 1 if c.get("legalities", {}).get("commander") == "legal" else 0,
        }

    conn = get_connection(db_path)
    cur = conn.cursor()
    updated = 0
    for oid, info in lookup.items():
        res = cur.execute(
            "UPDATE cards SET type_line=?, oracle_text=?, mana_cost=?, cmc=?, "
            "colors=?, color_identity=?, keywords=?, power=?, toughness=?, "
            "loyalty=?, rarity=?, edhrec_rank=?, legal_commander=? "
            "WHERE oracle_id=?",
            (info["type_line"], info["oracle_text"], info["mana_cost"],
             info["cmc"], info["colors"], info["color_identity"],
             info["keywords"], info["power"], info["toughness"],
             info["loyalty"], info["rarity"], info["edhrec_rank"],
             info["legal_commander"], oid),
        )
        updated += res.rowcount
    conn.commit()
    conn.close()
    print(f"Backfilled {updated} cards with Scryfall metadata")


def import_file(path: str, db_path: str = DB_PATH):
    """Import a JSON file (tags or merged) into the database."""
    init_db(db_path)

    with open(path) as f:
        cards = json.load(f)

    print(f"Importing {len(cards)} cards from {path}...")
    import_cards(cards, db_path)
    print(f"Done. DB at {db_path}")


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
    # Include Scryfall metadata if available
    for field in ("mana_cost", "cmc", "colors", "color_identity", "keywords",
                  "power", "toughness", "loyalty", "rarity", "legal_commander"):
        try:
            card[field] = row[field]
        except (IndexError, KeyError):
            pass
    return card


def _attach_tags(conn: sqlite3.Connection, cards: list[dict]) -> list[dict]:
    """Batch-load provides/wants for a list of cards."""
    if not cards:
        return cards

    oids = [c["oracle_id"] for c in cards]
    oid_set = set(oids)
    card_idx = {c["oracle_id"]: c for c in cards}

    # Initialize empty tag lists
    for c in cards:
        c["provides"] = []
        c["wants"] = []
        c["scryfall_tags"] = []

    # Batch query tags using chunked IN clauses
    chunk_size = 500
    for i in range(0, len(oids), chunk_size):
        chunk = oids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))

        for row in conn.execute(
            f"SELECT oracle_id, tag FROM provides WHERE oracle_id IN ({placeholders})",
            chunk,
        ):
            card_idx[row[0]]["provides"].append(row[1])

        for row in conn.execute(
            f"SELECT oracle_id, tag FROM wants WHERE oracle_id IN ({placeholders})",
            chunk,
        ):
            card_idx[row[0]]["wants"].append(row[1])

        # Load scryfall community tags into separate field (validation only,
        # not used for graph edge building — avoids inheriting Scryfall's
        # inconsistencies into synergy scores)
        try:
            for row in conn.execute(
                f"SELECT oracle_id, tag FROM scryfall_tags WHERE oracle_id IN ({placeholders})",
                chunk,
            ):
                if row[0] in card_idx:
                    card_idx[row[0]]["scryfall_tags"].append(row[1])
        except Exception:
            pass  # scryfall_tags table may not exist in older DBs

    return cards


def get_card(oracle_id: str, db_path: str = DB_PATH) -> dict | None:
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cards WHERE oracle_id = ?", (oracle_id,)).fetchone()
    if not row:
        conn.close()
        return None
    card = _row_to_card(row)
    _attach_tags(conn, [card])
    conn.close()
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

    _attach_tags(conn, cards)
    conn.close()
    return cards


def get_cards_by_oids(oids: list[str], db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row

    cards = []
    chunk_size = 500
    for i in range(0, len(oids), chunk_size):
        chunk = oids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM cards WHERE oracle_id IN ({placeholders})", chunk
        ).fetchall()
        cards.extend(_row_to_card(r) for r in rows)

    _attach_tags(conn, cards)
    conn.close()
    return cards


def get_cards_providing(tag: str, db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.* FROM cards c JOIN provides p ON c.oracle_id = p.oracle_id WHERE p.tag = ?",
        (tag,),
    ).fetchall()
    cards = [_row_to_card(r) for r in rows]
    _attach_tags(conn, cards)
    conn.close()
    return cards


def get_cards_wanting(tag: str, db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.* FROM cards c JOIN wants w ON c.oracle_id = w.oracle_id WHERE w.tag = ?",
        (tag,),
    ).fetchall()
    cards = [_row_to_card(r) for r in rows]
    _attach_tags(conn, cards)
    conn.close()
    return cards


def fix_tribal_wants(db_path=None):
    """Remove wants tribal tags where oracle text doesn't mention the creature type.
    Returns list of dicts with removed entries: {oracle_id, name, tag, oracle_text}.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT w.oracle_id, w.tag, c.name, c.oracle_text
        FROM wants w JOIN cards c ON w.oracle_id = c.oracle_id
        WHERE w.tag LIKE '%-tribal'
    """).fetchall()

    removed = []
    for row in rows:
        tag = row["tag"]
        creature_type = tag.replace("-tribal", "")
        oracle = (row["oracle_text"] or "").lower()
        if creature_type.lower() not in oracle:
            removed.append({
                "oracle_id": row["oracle_id"],
                "name": row["name"],
                "tag": tag,
                "oracle_text": row["oracle_text"]
            })

    if removed:
        for entry in removed:
            conn.execute(
                "DELETE FROM wants WHERE oracle_id = ? AND tag = ?",
                (entry["oracle_id"], entry["tag"])
            )
        conn.commit()

    conn.close()
    return removed


def rebuild_registry(db_path=None, output_path=None, min_freq=3):
    """Rebuild synergy_tag_registry.json from all cards in the DB.
    Collects all provides/wants tags with min_freq or more occurrences.
    """
    if db_path is None:
        db_path = DB_PATH
    if output_path is None:
        output_path = os.path.join(os.path.dirname(__file__), "synergy_tag_registry.json")

    conn = sqlite3.connect(db_path)

    provides_counts = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM provides GROUP BY tag HAVING cnt >= ? ORDER BY tag",
        (min_freq,)
    ).fetchall()

    wants_counts = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM wants GROUP BY tag HAVING cnt >= ? ORDER BY tag",
        (min_freq,)
    ).fetchall()

    total_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    conn.close()

    provides_tags = [row[0] for row in provides_counts]
    wants_tags = [row[0] for row in wants_counts]

    # Load existing registry to preserve function_tags and themes
    existing = {}
    existing_path = os.path.join(os.path.dirname(__file__), "synergy_tag_registry.json")
    if os.path.exists(existing_path):
        with open(existing_path) as f:
            existing = json.load(f)

    registry = {
        "_meta": {
            "description": "Controlled vocabulary for 3-layer card tagging system.",
            "version": "4.0",
            "created": __import__('datetime').date.today().isoformat(),
            "stats": {
                "provides_count": len(provides_tags),
                "wants_count": len(wants_tags),
                "source": f"Rebuilt from {total_cards} cards in tags.db (min {min_freq} occurrences)",
                "min_frequency": min_freq
            }
        },
        "provides": {
            "_description": "What card gives to deck",
            "tags": provides_tags
        },
        "wants": {
            "_description": "Conditions making card better",
            "tags": wants_tags
        }
    }

    # Preserve function_tags and themes from existing registry
    if "function_tags" in existing:
        registry["function_tags"] = existing["function_tags"]
    if "themes" in existing:
        registry["themes"] = existing["themes"]

    with open(output_path, "w") as f:
        json.dump(registry, f, indent=2)

    return registry


def get_all_cards(db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM cards").fetchall()
    cards = [_row_to_card(r) for r in rows]
    _attach_tags(conn, cards)
    conn.close()
    return cards


def get_db_stats(db_path: str = DB_PATH) -> dict:
    conn = get_connection(db_path)
    stats = {}
    stats["cards"] = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    stats["provides_rows"] = conn.execute("SELECT COUNT(*) FROM provides").fetchone()[0]
    stats["wants_rows"] = conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0]
    stats["unique_provides"] = conn.execute("SELECT COUNT(DISTINCT tag) FROM provides").fetchone()[0]
    stats["unique_wants"] = conn.execute("SELECT COUNT(DISTINCT tag) FROM wants").fetchone()[0]

    # Scryfall tags stats
    try:
        stats["scryfall_tags_rows"] = conn.execute("SELECT COUNT(*) FROM scryfall_tags").fetchone()[0]
        stats["scryfall_tags_cards"] = conn.execute("SELECT COUNT(DISTINCT oracle_id) FROM scryfall_tags").fetchone()[0]
        stats["unique_scryfall"] = conn.execute("SELECT COUNT(DISTINCT tag) FROM scryfall_tags").fetchone()[0]
    except sqlite3.OperationalError:
        stats["scryfall_tags_rows"] = 0
        stats["scryfall_tags_cards"] = 0
        stats["unique_scryfall"] = 0

    # Top provides
    stats["top_provides"] = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM provides GROUP BY tag ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    # Top wants
    stats["top_wants"] = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM wants GROUP BY tag ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    conn.close()
    return stats


# ── Scryfall Tags ──


def import_scryfall_tags(cards: list[dict], db_path: str = DB_PATH):
    """Import scryfall tagger data into the scryfall_tags table.

    Accepts cards with either:
      - scryfall_tags_detail: [{name, weight}, ...] (full detail)
      - scryfall_tags: [str, ...] (names only, weight=0)
    """
    init_db(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("BEGIN")

    imported = 0
    for card in cards:
        oid = card.get("oracle_id")
        if not oid:
            continue

        # Get detailed tags (with weight) or plain tags
        detail = card.get("scryfall_tags_detail", [])
        plain = card.get("scryfall_tags", [])

        if detail:
            for t in detail:
                cur.execute(
                    "INSERT OR REPLACE INTO scryfall_tags (oracle_id, tag, weight) VALUES (?, ?, ?)",
                    (oid, t["name"], t.get("weight", 0)),
                )
        elif plain:
            for tag in plain:
                cur.execute(
                    "INSERT OR REPLACE INTO scryfall_tags (oracle_id, tag, weight) VALUES (?, ?, ?)",
                    (oid, tag, 0),
                )
        else:
            continue
        imported += 1

    conn.commit()
    conn.close()
    return imported


def import_scryfall_file(path: str, db_path: str = DB_PATH):
    """Import scryfall tags from a JSON file."""
    with open(path) as f:
        cards = json.load(f)
    print(f"Importing scryfall tags for {len(cards)} cards from {path}...")
    count = import_scryfall_tags(cards, db_path)
    print(f"Imported scryfall tags for {count} cards")



def get_cards_with_scryfall_tag(tag: str, db_path: str = DB_PATH) -> list[str]:
    """Get oracle_ids of cards that have a specific scryfall tag."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT oracle_id FROM scryfall_tags WHERE tag = ?", (tag,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_scryfall_tag_counts(db_path: str = DB_PATH) -> list[tuple[str, int]]:
    """Get all scryfall tags sorted by frequency."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM scryfall_tags GROUP BY tag ORDER BY cnt DESC"
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def get_cards_without_scryfall_tags(db_path: str = DB_PATH) -> list[dict]:
    """Get cards in the DB that don't have scryfall tags yet."""
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.* FROM cards c WHERE c.oracle_id NOT IN (SELECT DISTINCT oracle_id FROM scryfall_tags)"
    ).fetchall()
    cards = [_row_to_card(r) for r in rows]
    conn.close()
    return cards


def find_synergy_candidates(deck_cards: list[dict], db_path: str = DB_PATH,
                            commander: dict = None) -> list[dict]:
    """Find cards in DB that synergize with the given deck cards.

    Queries for cards that:
    1. Provide what deck cards want (direct match)
    2. Want what deck cards provide (direct match)
    3. (Commander-specific) Provide what bridges to commander's wants
    4. (Commander-specific) Want what bridges from commander's provides

    Bridge expansion only applies to the COMMANDER's tags (not the whole deck),
    keeping the candidate pool focused.

    Returns deduplicated candidate cards (excluding deck cards).
    """
    deck_oids = {c["oracle_id"] for c in deck_cards}

    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    candidate_oids = set()

    # Collect deck's wants and provides
    deck_wants = set()
    deck_provides = set()
    for card in deck_cards:
        deck_wants.update(card.get("wants", []))
        deck_provides.update(card.get("provides", []))

    # Cards that provide what deck wants (direct)
    for tag in deck_wants:
        for row in conn.execute(
            "SELECT oracle_id FROM provides WHERE tag = ?", (tag,)
        ):
            if row[0] not in deck_oids:
                candidate_oids.add(row[0])

    # Cards that want what deck provides (direct)
    for tag in deck_provides:
        for row in conn.execute(
            "SELECT oracle_id FROM wants WHERE tag = ?", (tag,)
        ):
            if row[0] not in deck_oids:
                candidate_oids.add(row[0])

    # Bridge expansion for commander AND top deck tags
    try:
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from mtg_synergy.constants import SEMANTIC_BRIDGES

        # Expand from commander's tags (threshold 0.6)
        bridge_wants = set()
        bridge_provides = set()
        if commander:
            bridge_wants.update(commander.get("wants", []))
            bridge_provides.update(commander.get("provides", []))

        # Also expand from deck's FREQUENT provides/wants (threshold 0.7 — stricter)
        # Count tag frequency across deck cards
        from collections import Counter
        deck_provides_freq = Counter()
        deck_wants_freq = Counter()
        for card in deck_cards:
            for p in card.get("provides", []):
                deck_provides_freq[p] += 1
            for w in card.get("wants", []):
                deck_wants_freq[w] += 1

        # Only bridge-expand tags appearing on 3+ deck cards (strategy-defining tags)
        for tag, count in deck_provides_freq.items():
            if count >= 3:
                bridge_provides.add(tag)
        for tag, count in deck_wants_freq.items():
            if count >= 3:
                bridge_wants.add(tag)

        # Cards providing something that bridges to deck's wants
        for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
            min_weight = 0.6 if w_tag in (commander.get("wants", []) if commander else []) else 0.7
            if w_tag in bridge_wants and weight >= min_weight:
                for row in conn.execute(
                    "SELECT oracle_id FROM provides WHERE tag = ?", (p_tag,)
                ):
                    if row[0] not in deck_oids:
                        candidate_oids.add(row[0])

        # Cards wanting something that bridges from deck's provides
        for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
            min_weight = 0.6 if p_tag in (commander.get("provides", []) if commander else []) else 0.7
            if p_tag in bridge_provides and weight >= min_weight:
                for row in conn.execute(
                    "SELECT oracle_id FROM wants WHERE tag = ?", (w_tag,)
                ):
                    if row[0] not in deck_oids:
                        candidate_oids.add(row[0])
    except ImportError:
        pass

    conn.close()

    # Load full card data
    candidates = get_cards_by_oids(list(candidate_oids), db_path)
    return candidates


# ── CLI ──


def main():
    parser = argparse.ArgumentParser(description="SQLite tag database")
    sub = parser.add_subparsers(dest="command")

    imp = sub.add_parser("import", help="Import tags from JSON file")
    imp.add_argument("file", help="JSON file to import")
    imp.add_argument("--db", default=DB_PATH, help="Database path")

    stats = sub.add_parser("stats", help="Show database statistics")
    stats.add_argument("--db", default=DB_PATH, help="Database path")

    query = sub.add_parser("query", help="Query cards by tag")
    query.add_argument("--provides", help="Find cards providing this tag")
    query.add_argument("--wants", help="Find cards wanting this tag")
    query.add_argument("--scryfall", help="Find cards with this scryfall tag")
    query.add_argument("--db", default=DB_PATH, help="Database path")

    scry = sub.add_parser("import-scryfall", help="Import scryfall tagger tags")
    scry.add_argument("file", help="Scryfall tags JSON file")
    scry.add_argument("--db", default=DB_PATH, help="Database path")

    bf = sub.add_parser("backfill", help="Backfill Scryfall metadata into cards table")
    bf.add_argument("--scryfall", default=os.path.join(DATA_DIR, "oracle_cards.json"),
                    help="Scryfall oracle cards JSON")
    bf.add_argument("--db", default=DB_PATH, help="Database path")

    scry_stats = sub.add_parser("scryfall-stats", help="Show scryfall tag statistics")
    scry_stats.add_argument("--db", default=DB_PATH, help="Database path")

    gap = sub.add_parser("scryfall-gap", help="Show cards missing scryfall tags")
    gap.add_argument("--db", default=DB_PATH, help="Database path")
    gap.add_argument("--limit", type=int, default=20, help="Max cards to show")

    fix_tribal_parser = sub.add_parser("fix-tribal", help="Remove false positive tribal wants tags")
    fix_tribal_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting")
    fix_tribal_parser.add_argument("--db", default=DB_PATH, help="Database path")

    rebuild_parser = sub.add_parser("rebuild-registry", help="Rebuild tag registry from all DB cards")
    rebuild_parser.add_argument("--min-freq", type=int, default=3, help="Minimum tag frequency (default: 3)")
    rebuild_parser.add_argument("--output", default=None, help="Output path (default: synergy_tag_registry.json)")

    args = parser.parse_args()

    if args.command == "backfill":
        init_db(args.db)
        backfill_scryfall(args.scryfall, args.db)

    elif args.command == "import":
        import_file(args.file, args.db)
        s = get_db_stats(args.db)
        print(f"\nDB stats: {s['cards']} cards, {s['provides_rows']} provides, "
              f"{s['wants_rows']} wants, {s['scryfall_tags_rows']} scryfall tags")

    elif args.command == "import-scryfall":
        import_scryfall_file(args.file, args.db)
        s = get_db_stats(args.db)
        print(f"\nDB: {s['scryfall_tags_cards']} cards with scryfall tags, "
              f"{s['scryfall_tags_rows']} tag assignments, {s['unique_scryfall']} unique tags")

    elif args.command == "stats":
        s = get_db_stats(args.db)
        print(f"Cards: {s['cards']}")
        print(f"LLM provides: {s['provides_rows']} rows, {s['unique_provides']} unique tags")
        print(f"LLM wants: {s['wants_rows']} rows, {s['unique_wants']} unique tags")
        print(f"Scryfall tags: {s['scryfall_tags_rows']} rows, {s['scryfall_tags_cards']} cards, "
              f"{s['unique_scryfall']} unique tags")
        print(f"\nTop LLM provides:")
        for tag, cnt in s["top_provides"]:
            print(f"  {tag:35s} {cnt}")
        print(f"\nTop LLM wants:")
        for tag, cnt in s["top_wants"]:
            print(f"  {tag:35s} {cnt}")

    elif args.command == "scryfall-stats":
        counts = get_scryfall_tag_counts(args.db)
        s = get_db_stats(args.db)
        print(f"Scryfall tags: {s['scryfall_tags_rows']} assignments, "
              f"{s['scryfall_tags_cards']} cards, {s['unique_scryfall']} unique tags")
        print(f"\nTop 30 scryfall tags:")
        for tag, cnt in counts[:30]:
            print(f"  {tag:40s} {cnt}")

    elif args.command == "scryfall-gap":
        missing = get_cards_without_scryfall_tags(args.db)
        print(f"Cards without scryfall tags: {len(missing)}")
        for c in missing[:args.limit]:
            print(f"  {c['name']}")
        if len(missing) > args.limit:
            print(f"  ... and {len(missing) - args.limit} more")

    elif args.command == "fix-tribal":
        if args.dry_run:
            conn = sqlite3.connect(args.db)
            rows = conn.execute("""
                SELECT w.oracle_id, w.tag, c.name, c.oracle_text
                FROM wants w JOIN cards c ON w.oracle_id = c.oracle_id
                WHERE w.tag LIKE '%-tribal'
            """).fetchall()
            conn.close()
            false_pos = []
            for oid, tag, name, oracle in rows:
                creature_type = tag.replace("-tribal", "")
                if creature_type.lower() not in (oracle or "").lower():
                    false_pos.append((name, tag))
            print(f"Would remove {len(false_pos)} false positive tribal wants:")
            for name, tag in false_pos[:20]:
                print(f"  {name}: {tag}")
            if len(false_pos) > 20:
                print(f"  ... and {len(false_pos) - 20} more")
        else:
            removed = fix_tribal_wants(args.db)
            print(f"Removed {len(removed)} false positive tribal wants tags")
            for entry in removed[:20]:
                print(f"  {entry['name']}: {entry['tag']}")
            if len(removed) > 20:
                print(f"  ... and {len(removed) - 20} more")

    elif args.command == "rebuild-registry":
        registry = rebuild_registry(output_path=args.output, min_freq=args.min_freq)
        meta = registry["_meta"]["stats"]
        print(f"Registry v4.0: {meta['provides_count']} provides, {meta['wants_count']} wants")
        print(f"Source: {meta['source']}")

    elif args.command == "query":
        if args.provides:
            cards = get_cards_providing(args.provides, args.db)
            print(f"Cards providing '{args.provides}': {len(cards)}")
            for c in cards[:20]:
                print(f"  {c['name']}")
        elif args.wants:
            cards = get_cards_wanting(args.wants, args.db)
            print(f"Cards wanting '{args.wants}': {len(cards)}")
            for c in cards[:20]:
                print(f"  {c['name']}")
        elif args.scryfall:
            oids = get_cards_with_scryfall_tag(args.scryfall, args.db)
            print(f"Cards with scryfall tag '{args.scryfall}': {len(oids)}")
            # Resolve names
            if oids:
                cards = get_cards_by_oids(oids[:20], args.db)
                for c in cards:
                    print(f"  {c['name']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
