#!/usr/bin/env python3
"""Fetch Commander preconstructed decklists from MTGJSON and import into tags.db.

Downloads deck metadata from MTGJSON DeckList.json, fetches individual Commander
deck files, and stores them in precon_decks/precon_cards tables.

Usage:
    python3 fetch_precons.py               # fetch + import
    python3 fetch_precons.py --import-only  # import from cached file
    python3 fetch_precons.py --stats        # show stats from DB
"""

import json
import sqlite3
import os
import time
import urllib.request
import urllib.error

MTGJSON_BASE = "https://mtgjson.com/api/v5"
DECKLIST_URL = f"{MTGJSON_BASE}/DeckList.json"
DECK_URL_TEMPLATE = f"{MTGJSON_BASE}/decks/{{fileName}}.json"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CACHE_PATH = os.path.join(DATA_DIR, "mtgjson_precons.json")
DB_PATH = os.path.join(DATA_DIR, "tags.db")

COMMANDER_TYPE = "Commander Deck"
RATE_LIMIT_DELAY = 0.2  # seconds between individual deck downloads


def fetch_url(url, timeout=30):
    """Fetch JSON from a URL. Returns parsed dict or None on error."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "mtg-synergy-graph/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP error {e.code} fetching {url}")
        return None
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return None


def fetch_deck_list():
    """Fetch the MTGJSON DeckList.json index and filter to Commander decks.

    Returns list of dicts with keys: name, fileName, code, releaseDate, type.
    """
    print("Fetching deck list from MTGJSON...")
    data = fetch_url(DECKLIST_URL)
    if not data:
        print("Failed to fetch DeckList.json")
        return []

    # DeckList.json is a top-level array or wrapped in {"data": [...]}
    decks = data if isinstance(data, list) else data.get("data", data)
    if isinstance(decks, dict) and not isinstance(decks, list):
        # Try common wrapper keys
        for key in ("data", "decks"):
            if key in decks and isinstance(decks[key], list):
                decks = decks[key]
                break

    if not isinstance(decks, list):
        print(f"Unexpected DeckList.json structure: {type(decks)}")
        return []

    commander_decks = [d for d in decks if d.get("type") == COMMANDER_TYPE]
    print(f"Found {len(commander_decks)} Commander decks out of {len(decks)} total")
    return commander_decks


def fetch_deck(file_name):
    """Fetch a single deck file from MTGJSON.

    Returns parsed deck dict with commander, mainBoard, etc., or None.
    """
    url = DECK_URL_TEMPLATE.format(fileName=file_name)
    raw = fetch_url(url)
    if not raw:
        return None

    # Deck data may be wrapped in {"data": {...}} or at top level
    deck = raw.get("data", raw) if isinstance(raw, dict) else raw
    return deck


def parse_deck(deck_meta, deck_data):
    """Parse a MTGJSON deck into our format.

    Args:
        deck_meta: entry from DeckList.json (name, fileName, code, releaseDate)
        deck_data: full deck JSON from individual deck file

    Returns dict with: name, commander, release_date, set_code, cards
    """
    # Extract commander name(s)
    commanders = deck_data.get("commander", [])
    commander_names = []
    for card in commanders:
        name = card.get("name", "")
        if name:
            commander_names.append(name)

    commander_str = " + ".join(commander_names) if commander_names else ""

    # Extract mainboard cards (including commander zone cards)
    cards = []
    seen = set()

    for card in deck_data.get("mainBoard", []):
        name = card.get("name", "")
        count = card.get("count", 1)
        if name and name not in seen:
            cards.append({"name": name, "quantity": count})
            seen.add(name)

    # Add commander cards if not already in mainboard
    for card in commanders:
        name = card.get("name", "")
        count = card.get("count", 1)
        if name and name not in seen:
            cards.append({"name": name, "quantity": count})
            seen.add(name)

    # Add sideboard (some precons list companion/extra cards there)
    for card in deck_data.get("sideBoard", []):
        name = card.get("name", "")
        count = card.get("count", 1)
        if name and name not in seen:
            cards.append({"name": name, "quantity": count})
            seen.add(name)

    return {
        "name": deck_meta.get("name", deck_data.get("name", "")),
        "commander": commander_str,
        "release_date": deck_meta.get("releaseDate", deck_data.get("releaseDate", "")),
        "set_code": deck_meta.get("code", deck_data.get("code", "")),
        "cards": cards,
    }


def fetch_all_precons(max_decks=None):
    """Fetch all Commander precon decklists from MTGJSON.

    Returns list of parsed deck dicts.
    """
    deck_list = fetch_deck_list()
    if not deck_list:
        return []

    precons = []
    total = len(deck_list)
    if max_decks:
        deck_list = deck_list[:max_decks]

    for i, meta in enumerate(deck_list):
        file_name = meta.get("fileName", "")
        name = meta.get("name", file_name)
        print(f"  [{i + 1}/{len(deck_list)}] Fetching {name} ({meta.get('code', '?')})...")

        deck_data = fetch_deck(file_name)
        if not deck_data:
            print(f"    Skipped (fetch failed)")
            continue

        parsed = parse_deck(meta, deck_data)
        if not parsed["cards"]:
            print(f"    Skipped (no cards found)")
            continue

        precons.append(parsed)

        if i < len(deck_list) - 1:
            time.sleep(RATE_LIMIT_DELAY)

    print(f"Fetched {len(precons)} Commander precon decks")
    return precons


def ensure_tables(db_path=None):
    """Create precon tables if they don't exist."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS precon_decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            commander TEXT,
            release_date TEXT,
            set_code TEXT,
            UNIQUE(name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS precon_cards (
            deck_id INTEGER NOT NULL,
            card_name TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            FOREIGN KEY(deck_id) REFERENCES precon_decks(id),
            UNIQUE(deck_id, card_name)
        )
    """)
    conn.commit()
    conn.close()


def import_precons_to_db(precons, db_path=None):
    """Import parsed precon decks into SQLite tables."""
    if db_path is None:
        db_path = DB_PATH
    ensure_tables(db_path)
    conn = sqlite3.connect(db_path)

    imported = 0
    for deck in precons:
        # Delete old cards for this deck before replacing the deck row
        # (INSERT OR REPLACE changes the rowid, orphaning old precon_cards)
        old_row = conn.execute(
            "SELECT id FROM precon_decks WHERE name = ?", (deck["name"],)
        ).fetchone()
        if old_row:
            conn.execute("DELETE FROM precon_cards WHERE deck_id = ?", (old_row[0],))
            conn.execute("DELETE FROM precon_decks WHERE id = ?", (old_row[0],))

        cursor = conn.execute("""
            INSERT INTO precon_decks (name, commander, release_date, set_code)
            VALUES (?, ?, ?, ?)
        """, (deck["name"], deck["commander"], deck["release_date"], deck["set_code"]))

        deck_id = cursor.lastrowid

        for card in deck["cards"]:
            conn.execute("""
                INSERT OR REPLACE INTO precon_cards (deck_id, card_name, quantity)
                VALUES (?, ?, ?)
            """, (deck_id, card["name"], card["quantity"]))

        imported += 1

    conn.commit()
    conn.close()
    print(f"Imported {imported} decks to DB")


def show_stats(db_path=None):
    """Print stats about imported precon data."""
    if db_path is None:
        db_path = DB_PATH
    ensure_tables(db_path)
    conn = sqlite3.connect(db_path)

    total_decks = conn.execute("SELECT COUNT(*) FROM precon_decks").fetchone()[0]
    total_cards = conn.execute("SELECT COUNT(*) FROM precon_cards").fetchone()[0]
    unique_cards = conn.execute(
        "SELECT COUNT(DISTINCT card_name) FROM precon_cards"
    ).fetchone()[0]

    print(f"Precon decks: {total_decks}")
    print(f"Total card slots: {total_cards}")
    print(f"Unique card names: {unique_cards}")

    # Decks by set
    by_set = conn.execute("""
        SELECT set_code, COUNT(*) FROM precon_decks
        GROUP BY set_code ORDER BY set_code
    """).fetchall()
    if by_set:
        print(f"\nDecks by set ({len(by_set)} sets):")
        for code, count in by_set:
            print(f"  {code}: {count}")

    # Recent decks
    recent = conn.execute("""
        SELECT name, commander, release_date, set_code
        FROM precon_decks ORDER BY release_date DESC LIMIT 10
    """).fetchall()
    if recent:
        print("\nMost recent decks:")
        for name, commander, date, code in recent:
            cmd = f" ({commander})" if commander else ""
            print(f"  {date} [{code}] {name}{cmd}")

    # Match card names to our cards table
    try:
        matched = conn.execute("""
            SELECT COUNT(DISTINCT pc.card_name)
            FROM precon_cards pc
            JOIN cards c ON pc.card_name = c.name
        """).fetchone()[0]
        print(f"\nCard name match to tags.db: {matched}/{unique_cards} "
              f"({matched * 100 // max(unique_cards, 1)}%)")

        # Unmatched sample
        unmatched = conn.execute("""
            SELECT DISTINCT pc.card_name FROM precon_cards pc
            LEFT JOIN cards c ON pc.card_name = c.name
            WHERE c.oracle_id IS NULL
            ORDER BY pc.card_name LIMIT 20
        """).fetchall()
        if unmatched:
            print(f"Sample unmatched cards ({min(20, len(unmatched))} shown):")
            for (name,) in unmatched:
                print(f"  - {name}")
    except sqlite3.OperationalError:
        # cards table may not exist
        print("\nCould not match to cards table (table may not exist)")

    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch MTGJSON Commander precon decklists")
    parser.add_argument("--import-only", action="store_true", help="Import from cached file")
    parser.add_argument("--stats", action="store_true", help="Show DB stats")
    parser.add_argument("--max-decks", type=int, default=None,
                        help="Limit number of decks fetched (for testing)")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        exit(0)

    if not args.import_only:
        precons = fetch_all_precons(max_decks=args.max_decks)
        if not precons:
            print("No precon decks fetched")
            exit(1)

        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(precons, f, indent=2)
        print(f"Cached to {CACHE_PATH}")
    else:
        if not os.path.exists(CACHE_PATH):
            print(f"Cache file not found: {CACHE_PATH}")
            print("Run without --import-only first to fetch data")
            exit(1)
        with open(CACHE_PATH) as f:
            precons = json.load(f)
        print(f"Loaded {len(precons)} decks from cache")

    print("Importing to DB...")
    import_precons_to_db(precons)
    show_stats()
