#!/usr/bin/env python3
"""Bulk-scrape EDHREC commander data and import into tags.db.

Fetches per-commander card data (synergy scores, inclusion rates) for all
legendary creatures in the DB, ordered by EDHREC rank. Caches responses
to data/edhrec_commanders/ and populates two DB tables:
  - edhrec_card_synergy: per-commander card synergy/inclusion data
  - edhrec_decklists: co-occurrence matrix (cards with >20% inclusion)

Usage:
    python3 scrape_edhrec_commanders.py                    # scrape missing (up to 500)
    python3 scrape_edhrec_commanders.py --max 200          # limit new scrapes
    python3 scrape_edhrec_commanders.py --import-only      # import from cache only
    python3 scrape_edhrec_commanders.py --stats            # show stats
    python3 scrape_edhrec_commanders.py --force            # re-scrape even if cached
"""

import argparse
import json
import os
import re
import sqlite3
import time
import unicodedata
import urllib.request
import urllib.error

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
CACHE_DIR = os.path.join(DATA_DIR, "edhrec_commanders")
EDHREC_BASE = "https://json.edhrec.com/pages/commanders"

RATE_LIMIT = 0.5  # seconds between requests

SCHEMA = """
CREATE TABLE IF NOT EXISTS edhrec_card_synergy (
    commander_slug TEXT NOT NULL,
    card_name TEXT NOT NULL,
    synergy REAL,
    inclusion INTEGER,
    num_decks INTEGER,
    section TEXT,
    UNIQUE(commander_slug, card_name)
);
CREATE INDEX IF NOT EXISTS idx_edhrec_synergy_slug
    ON edhrec_card_synergy(commander_slug);
CREATE INDEX IF NOT EXISTS idx_edhrec_synergy_card
    ON edhrec_card_synergy(card_name);

CREATE TABLE IF NOT EXISTS edhrec_decklists (
    commander_slug TEXT NOT NULL,
    card_name TEXT NOT NULL,
    UNIQUE(commander_slug, card_name)
);
CREATE INDEX IF NOT EXISTS idx_edhrec_decklists_slug
    ON edhrec_decklists(commander_slug);
CREATE INDEX IF NOT EXISTS idx_edhrec_decklists_card
    ON edhrec_decklists(card_name);
"""


def init_db(db_path=DB_PATH):
    """Create EDHREC tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def name_to_slug(name):
    """Convert a card name to an EDHREC slug.

    Handles DFCs (front face only), accented characters, and apostrophes.
    Examples:
        "Krenko, Mob Boss" -> "krenko-mob-boss"
        "Adrix and Nev, Twincasters" -> "adrix-and-nev-twincasters"
        "Y'Shtola, Night's Blessed" -> "yshtola-nights-blessed"
        "Birgi, God of Storytelling // Harnfel, Horn of Bounty" -> "birgi-god-of-storytelling"
        "Glóin, Dwarf Emissary" -> "gloin-dwarf-emissary"
    """
    # DFC: use front face only
    name = name.split(" // ")[0]
    # Normalize accented characters (é→e, ó→o, û→u, etc.)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    slug = name.lower()
    # Remove apostrophes
    slug = slug.replace("'", "")
    slug = slug.replace("\u2019", "")
    # Replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    return slug


def get_commander_list(db_path=DB_PATH):
    """Get all legendary creatures from DB, sorted by EDHREC rank.

    Returns list of (name, slug, edhrec_rank).
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT name, edhrec_rank FROM cards
        WHERE type_line LIKE '%Legendary%Creature%'
        AND legal_commander = 1
        ORDER BY COALESCE(edhrec_rank, 999999)
    """).fetchall()
    conn.close()

    results = []
    for name, rank in rows:
        slug = name_to_slug(name)
        results.append((name, slug, rank))

    return results


def fetch_commander_data(slug, force=False):
    """Fetch EDHREC data for one commander. Returns parsed JSON or None."""
    cache_path = os.path.join(CACHE_DIR, f"{slug}.json")

    if not force and os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    url = f"{EDHREC_BASE}/{slug}.json"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "mtg-synergy-graph/1.0",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"  HTTP {e.code} for {slug}: {e}")
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  Failed to fetch {slug}: {e}")
        return None

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(data, f)

    return data


def extract_all_cards(data):
    """Extract ALL cards from ALL cardlists in EDHREC commander data.

    Returns list of dicts: {name, synergy, inclusion, num_decks, section}.
    """
    cards = []
    json_dict = data.get("container", {}).get("json_dict", {})
    cardlists = json_dict.get("cardlists", [])

    for cl in cardlists:
        header = cl.get("header", "")
        for cv in cl.get("cardviews", []):
            name = cv.get("name", "")
            if not name:
                continue
            cards.append({
                "name": name,
                "synergy": cv.get("synergy", 0) or 0,
                "inclusion": cv.get("inclusion", 0) or 0,
                "num_decks": cv.get("num_decks", 0) or 0,
                "section": header,
            })

    return cards


def import_commander(conn, slug, data):
    """Import one commander's EDHREC data into DB tables.

    Returns number of cards imported.
    """
    cards = extract_all_cards(data)
    if not cards:
        return 0

    # Get commander-level num_decks for inclusion rate calculation
    json_dict = data.get("container", {}).get("json_dict", {})
    commander_num_decks = json_dict.get("card", {}).get("num_decks", 0)

    # Insert synergy data
    conn.executemany("""
        INSERT OR REPLACE INTO edhrec_card_synergy
            (commander_slug, card_name, synergy, inclusion, num_decks, section)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        (slug, c["name"], c["synergy"], c["inclusion"], c["num_decks"], c["section"])
        for c in cards
    ])

    # Insert co-occurrence data for cards with >20% inclusion rate
    high_inclusion = []
    for c in cards:
        denom = commander_num_decks or c["num_decks"]
        if denom and c["inclusion"] / denom > 0.2:
            high_inclusion.append((slug, c["name"]))

    if high_inclusion:
        conn.executemany("""
            INSERT OR IGNORE INTO edhrec_decklists (commander_slug, card_name)
            VALUES (?, ?)
        """, high_inclusion)

    return len(cards)


def scrape_commanders(max_new=500, force=False, db_path=DB_PATH):
    """Scrape EDHREC data for commanders not yet cached.

    Returns (scraped_count, skipped_count, error_count).
    """
    commanders = get_commander_list(db_path)
    print(f"Found {len(commanders)} legendary creatures in DB")

    # Determine which need scraping
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached = set()
    if not force:
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".json"):
                cached.add(f[:-5])

    to_scrape = [(name, slug, rank) for name, slug, rank in commanders
                 if slug not in cached]

    if not to_scrape:
        print("All commanders already cached.")
        return 0, len(commanders), 0

    to_scrape = to_scrape[:max_new]
    print(f"Scraping {len(to_scrape)} commanders ({len(cached)} already cached)")

    init_db(db_path)
    conn = sqlite3.connect(db_path)

    scraped = 0
    skipped = 0
    errors = 0
    start_time = time.time()

    try:
        for i, (name, slug, rank) in enumerate(to_scrape):
            data = fetch_commander_data(slug, force=force)

            if data is None:
                skipped += 1
            else:
                count = import_commander(conn, slug, data)
                scraped += 1

                if scraped % 50 == 0:
                    conn.commit()

            if (i + 1) % 10 == 0:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                print(f"  [{i + 1}/{len(to_scrape)}] "
                      f"scraped={scraped} skipped={skipped} "
                      f"({rate:.1f} req/s)")

            time.sleep(RATE_LIMIT)

    except KeyboardInterrupt:
        print(f"\nInterrupted! Saving {scraped} commanders scraped so far...")

    conn.commit()
    conn.close()

    elapsed = time.time() - start_time
    print(f"\nDone: {scraped} scraped, {skipped} skipped (404), "
          f"{elapsed:.0f}s elapsed")

    return scraped, skipped, errors


def import_from_cache(db_path=DB_PATH):
    """Import all cached EDHREC JSON files into DB without network requests."""
    if not os.path.isdir(CACHE_DIR):
        print("No cache directory found.")
        return

    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]
    print(f"Importing {len(files)} cached commander files...")

    init_db(db_path)
    conn = sqlite3.connect(db_path)

    # Clear existing data for clean import
    conn.execute("DELETE FROM edhrec_card_synergy")
    conn.execute("DELETE FROM edhrec_decklists")

    imported = 0
    total_cards = 0

    for i, filename in enumerate(sorted(files)):
        slug = filename[:-5]
        filepath = os.path.join(CACHE_DIR, filename)

        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  Error reading {filename}: {e}")
            continue

        count = import_commander(conn, slug, data)
        if count > 0:
            imported += 1
            total_cards += count

        if (i + 1) % 50 == 0:
            conn.commit()
            print(f"  [{i + 1}/{len(files)}] {imported} commanders, "
                  f"{total_cards} card entries")

    conn.commit()
    conn.close()

    print(f"\nImported {imported} commanders, {total_cards} total card entries")


def show_stats(db_path=DB_PATH):
    """Show statistics about EDHREC data in DB."""
    conn = sqlite3.connect(db_path)

    # Check if tables exist
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    if "edhrec_card_synergy" not in tables:
        print("No EDHREC tables found. Run import first.")
        conn.close()
        return

    # Synergy table stats
    n_commanders = conn.execute(
        "SELECT COUNT(DISTINCT commander_slug) FROM edhrec_card_synergy"
    ).fetchone()[0]

    n_entries = conn.execute(
        "SELECT COUNT(*) FROM edhrec_card_synergy"
    ).fetchone()[0]

    n_unique_cards = conn.execute(
        "SELECT COUNT(DISTINCT card_name) FROM edhrec_card_synergy"
    ).fetchone()[0]

    avg_synergy = conn.execute(
        "SELECT AVG(synergy) FROM edhrec_card_synergy"
    ).fetchone()[0] or 0

    # Decklists table stats
    n_decklist_commanders = conn.execute(
        "SELECT COUNT(DISTINCT commander_slug) FROM edhrec_decklists"
    ).fetchone()[0]

    n_decklist_entries = conn.execute(
        "SELECT COUNT(*) FROM edhrec_decklists"
    ).fetchone()[0]

    n_decklist_cards = conn.execute(
        "SELECT COUNT(DISTINCT card_name) FROM edhrec_decklists"
    ).fetchone()[0]

    # Section distribution
    sections = conn.execute("""
        SELECT section, COUNT(*) as cnt
        FROM edhrec_card_synergy
        GROUP BY section
        ORDER BY cnt DESC
    """).fetchall()

    # Cached files
    cached = len([f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]) \
        if os.path.isdir(CACHE_DIR) else 0

    # Top commanders by card count
    top = conn.execute("""
        SELECT commander_slug, COUNT(*) as cnt
        FROM edhrec_card_synergy
        GROUP BY commander_slug
        ORDER BY cnt DESC
        LIMIT 5
    """).fetchall()

    conn.close()

    print(f"\nEDHREC Commander Data Stats")
    print(f"{'='*50}")
    print(f"  Cached JSON files:       {cached}")
    print(f"\n  edhrec_card_synergy:")
    print(f"    Commanders:            {n_commanders}")
    print(f"    Total entries:         {n_entries}")
    print(f"    Unique cards:          {n_unique_cards}")
    print(f"    Avg synergy score:     {avg_synergy:.3f}")
    print(f"\n  edhrec_decklists (>20% inclusion):")
    print(f"    Commanders:            {n_decklist_commanders}")
    print(f"    Total entries:         {n_decklist_entries}")
    print(f"    Unique cards:          {n_decklist_cards}")

    print(f"\n  Section distribution:")
    for section, cnt in sections:
        print(f"    {section:<30} {cnt:>6}")

    print(f"\n  Top commanders by card count:")
    for slug, cnt in top:
        print(f"    {slug:<40} {cnt:>4} cards")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bulk-scrape EDHREC commander data")
    parser.add_argument("--max", type=int, default=500,
                        help="Max new commanders to scrape (default: 500)")
    parser.add_argument("--import-only", action="store_true",
                        help="Import from cached files only (no network)")
    parser.add_argument("--stats", action="store_true",
                        help="Show EDHREC data statistics")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even if cached")
    args = parser.parse_args()

    if args.stats:
        show_stats()
    elif args.import_only:
        import_from_cache()
    else:
        scrape_commanders(max_new=args.max, force=args.force)
