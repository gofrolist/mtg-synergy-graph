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
    role TEXT DEFAULT '',
    edhrec_rank INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);

CREATE TABLE IF NOT EXISTS provides (
    oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
    tag TEXT NOT NULL,
    PRIMARY KEY (oracle_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_provides_tag ON provides(tag);

CREATE TABLE IF NOT EXISTS wants (
    oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
    tag TEXT NOT NULL,
    PRIMARY KEY (oracle_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_wants_tag ON wants(tag);

CREATE TABLE IF NOT EXISTS synergy_tags (
    oracle_id TEXT NOT NULL REFERENCES cards(oracle_id),
    tag TEXT NOT NULL,
    PRIMARY KEY (oracle_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_synergy_tags_tag ON synergy_tags(tag);
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
            "INSERT OR REPLACE INTO cards (oracle_id, name, type_line, oracle_text, role, edhrec_rank) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                oid,
                card.get("name", ""),
                card.get("type_line", ""),
                card.get("oracle_text", ""),
                card.get("role", ""),
                card.get("edhrec_rank"),
            ),
        )

        # Clear existing tags for this card
        cur.execute("DELETE FROM provides WHERE oracle_id = ?", (oid,))
        cur.execute("DELETE FROM wants WHERE oracle_id = ?", (oid,))
        cur.execute("DELETE FROM synergy_tags WHERE oracle_id = ?", (oid,))

        for tag in card.get("provides", []):
            cur.execute("INSERT OR IGNORE INTO provides VALUES (?, ?)", (oid, tag))
        for tag in card.get("wants", []):
            cur.execute("INSERT OR IGNORE INTO wants VALUES (?, ?)", (oid, tag))
        for tag in card.get("synergy_tags", []):
            cur.execute("INSERT OR IGNORE INTO synergy_tags VALUES (?, ?)", (oid, tag))

    conn.commit()
    conn.close()


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
    return {
        "oracle_id": row["oracle_id"],
        "name": row["name"],
        "type_line": row["type_line"],
        "oracle_text": row["oracle_text"],
        "role": row["role"],
        "edhrec_rank": row["edhrec_rank"],
    }


def _attach_tags(conn: sqlite3.Connection, cards: list[dict]) -> list[dict]:
    """Batch-load provides/wants/synergy_tags for a list of cards."""
    if not cards:
        return cards

    oids = [c["oracle_id"] for c in cards]
    oid_set = set(oids)
    card_idx = {c["oracle_id"]: c for c in cards}

    # Initialize empty tag lists
    for c in cards:
        c["provides"] = []
        c["wants"] = []
        c["synergy_tags"] = []

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

        for row in conn.execute(
            f"SELECT oracle_id, tag FROM synergy_tags WHERE oracle_id IN ({placeholders})",
            chunk,
        ):
            card_idx[row[0]]["synergy_tags"].append(row[1])

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


def get_all_cards(db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM cards").fetchall()
    cards = [_row_to_card(r) for r in rows]
    _attach_tags(conn, cards)
    conn.close()
    return cards


def count_cards_providing(tag: str, db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    result = conn.execute(
        "SELECT COUNT(*) FROM provides WHERE tag = ?", (tag,)
    ).fetchone()[0]
    conn.close()
    return result


def count_cards_wanting(tag: str, db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    result = conn.execute(
        "SELECT COUNT(*) FROM wants WHERE tag = ?", (tag,)
    ).fetchone()[0]
    conn.close()
    return result


def get_tag_counts(db_path: str = DB_PATH) -> dict[str, int]:
    """Get combined frequency counts for all tags (provides + wants)."""
    conn = get_connection(db_path)
    counts = {}
    for row in conn.execute("SELECT tag, COUNT(*) FROM provides GROUP BY tag"):
        counts[row[0]] = counts.get(row[0], 0) + row[1]
    for row in conn.execute("SELECT tag, COUNT(*) FROM wants GROUP BY tag"):
        counts[row[0]] = counts.get(row[0], 0) + row[1]
    conn.close()
    return counts


def get_db_stats(db_path: str = DB_PATH) -> dict:
    conn = get_connection(db_path)
    stats = {}
    stats["cards"] = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    stats["provides_rows"] = conn.execute("SELECT COUNT(*) FROM provides").fetchone()[0]
    stats["wants_rows"] = conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0]
    stats["synergy_tags_rows"] = conn.execute("SELECT COUNT(*) FROM synergy_tags").fetchone()[0]
    stats["unique_provides"] = conn.execute("SELECT COUNT(DISTINCT tag) FROM provides").fetchone()[0]
    stats["unique_wants"] = conn.execute("SELECT COUNT(DISTINCT tag) FROM wants").fetchone()[0]
    stats["unique_synergy"] = conn.execute("SELECT COUNT(DISTINCT tag) FROM synergy_tags").fetchone()[0]

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


def find_synergy_candidates(deck_cards: list[dict], db_path: str = DB_PATH) -> list[dict]:
    """Find cards in DB that synergize with the given deck cards.

    Queries for cards that:
    1. Provide what deck cards want
    2. Want what deck cards provide

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

    # Cards that provide what deck wants
    for tag in deck_wants:
        for row in conn.execute(
            "SELECT oracle_id FROM provides WHERE tag = ?", (tag,)
        ):
            if row[0] not in deck_oids:
                candidate_oids.add(row[0])

    # Cards that want what deck provides
    for tag in deck_provides:
        for row in conn.execute(
            "SELECT oracle_id FROM wants WHERE tag = ?", (tag,)
        ):
            if row[0] not in deck_oids:
                candidate_oids.add(row[0])

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
    query.add_argument("--db", default=DB_PATH, help="Database path")

    args = parser.parse_args()

    if args.command == "import":
        import_file(args.file, args.db)
        s = get_db_stats(args.db)
        print(f"\nDB stats: {s['cards']} cards, {s['provides_rows']} provides, "
              f"{s['wants_rows']} wants, {s['synergy_tags_rows']} synergy_tags")

    elif args.command == "stats":
        s = get_db_stats(args.db)
        print(f"Cards: {s['cards']}")
        print(f"Provides: {s['provides_rows']} rows, {s['unique_provides']} unique tags")
        print(f"Wants: {s['wants_rows']} rows, {s['unique_wants']} unique tags")
        print(f"Synergy tags: {s['synergy_tags_rows']} rows, {s['unique_synergy']} unique tags")
        print(f"\nTop provides:")
        for tag, cnt in s["top_provides"]:
            print(f"  {tag:35s} {cnt}")
        print(f"\nTop wants:")
        for tag, cnt in s["top_wants"]:
            print(f"  {tag:35s} {cnt}")

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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
