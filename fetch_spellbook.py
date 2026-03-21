#!/usr/bin/env python3
"""Fetch Commander Spellbook combo data and import into tags.db.

One-time bulk download, cached to data/commander_spellbook.json.
Usage:
    python3 fetch_spellbook.py               # fetch + import
    python3 fetch_spellbook.py --fetch-only   # just fetch, don't import
    python3 fetch_spellbook.py --import-only  # import from cached file
    python3 fetch_spellbook.py --stats        # show stats from DB
"""

import json
import sqlite3
import os
import time
import urllib.request
import urllib.error

BASE_URL = "https://backend.commanderspellbook.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CACHE_PATH = os.path.join(DATA_DIR, "commander_spellbook.json")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
UNRESOLVED_PATH = os.path.join(DATA_DIR, "spellbook_unresolved.txt")


def parse_combo_response(raw):
    """Parse a single Spellbook API combo into our format.

    Returns dict or None if status is not OK.
    """
    if raw.get("status") != "OK":
        return None

    card_oracle_ids = []
    card_names = []
    for use in raw.get("uses", []):
        card = use.get("card", {})
        oid = card.get("oracleId")
        name = card.get("name", "Unknown")
        if oid:
            card_oracle_ids.append(oid)
            card_names.append(name)

    if not card_oracle_ids:
        return None

    results = []
    for prod in raw.get("produces", []):
        feature = prod.get("feature", {})
        name = feature.get("name", "")
        if name:
            results.append(name)

    return {
        "combo_id": str(raw["id"]),
        "card_oracle_ids": card_oracle_ids,
        "card_names": card_names,
        "result": ", ".join(results),
        "prerequisites": raw.get("easyPrerequisites", ""),
        "card_count": len(card_oracle_ids),
    }


def fetch_all_combos(limit_per_page=100, max_pages=None):
    """Fetch all combos from Commander Spellbook API with pagination.

    Returns list of parsed combo dicts.
    """
    combos = []
    url = f"{BASE_URL}/variants/?format=json&limit={limit_per_page}&offset=0"
    page = 0

    while url:
        if max_pages and page >= max_pages:
            break

        print(f"  Fetching page {page + 1}... ({len(combos)} combos so far)")

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"  HTTP error {e.code} on page {page + 1}, stopping")
            break
        except Exception as e:
            print(f"  Error on page {page + 1}: {e}, stopping")
            break

        for raw in data.get("results", []):
            combo = parse_combo_response(raw)
            if combo:
                combos.append(combo)

        url = data.get("next")
        page += 1
        time.sleep(1)  # Rate limit: 1 req/sec

    return combos


def import_combos_to_db(combos, db_path=None):
    """Import parsed combos into spellbook tables."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    for combo in combos:
        conn.execute("""
            INSERT OR REPLACE INTO spellbook_combos
            (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            combo["combo_id"],
            json.dumps(combo["card_oracle_ids"]),
            json.dumps(combo["card_names"]),
            combo["result"],
            combo["prerequisites"],
            combo["card_count"],
        ))

        # Junction table
        for oid in combo["card_oracle_ids"]:
            conn.execute("""
                INSERT OR REPLACE INTO spellbook_combo_cards (combo_id, oracle_id)
                VALUES (?, ?)
            """, (combo["combo_id"], oid))

    conn.commit()
    conn.close()


def show_stats(db_path=None):
    """Print stats about imported Spellbook data."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM spellbook_combos").fetchone()[0]
    by_size = conn.execute(
        "SELECT card_count, COUNT(*) FROM spellbook_combos GROUP BY card_count ORDER BY card_count"
    ).fetchall()
    unique_cards = conn.execute(
        "SELECT COUNT(DISTINCT oracle_id) FROM spellbook_combo_cards"
    ).fetchone()[0]

    # Check how many match our DB
    matched = conn.execute("""
        SELECT COUNT(DISTINCT sc.oracle_id)
        FROM spellbook_combo_cards sc
        JOIN cards c ON sc.oracle_id = c.oracle_id
    """).fetchone()[0]
    conn.close()

    print(f"Spellbook combos: {total}")
    for size, count in by_size:
        print(f"  {size}-card: {count}")
    print(f"Unique cards in combos: {unique_cards}")
    print(f"Cards matched to our DB: {matched} ({matched * 100 // max(unique_cards, 1)}%)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch Commander Spellbook combo data")
    parser.add_argument("--fetch-only", action="store_true", help="Only fetch, don't import to DB")
    parser.add_argument("--import-only", action="store_true", help="Import from cached file")
    parser.add_argument("--stats", action="store_true", help="Show DB stats")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages fetched (for testing)")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        exit(0)

    if not args.import_only:
        print("Fetching combos from Commander Spellbook...")
        combos = fetch_all_combos(max_pages=args.max_pages)
        print(f"Fetched {len(combos)} combos")

        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(combos, f)
        print(f"Cached to {CACHE_PATH}")

        if args.fetch_only:
            exit(0)
    else:
        with open(CACHE_PATH) as f:
            combos = json.load(f)
        print(f"Loaded {len(combos)} combos from cache")

    print("Importing to DB...")
    import_combos_to_db(combos)
    show_stats()
