#!/usr/bin/env python3
"""Fetch EDHREC synergy scores + average decklists for commanders.

Fetches both data sources per commander:
1. Synergy scores: per-card synergy values from commander page
2. Average decklists: consensus 75-card deck

Usage:
    python3 fetch_edhrec_all.py                  # Fetch next 500 new commanders
    python3 fetch_edhrec_all.py --max 2000       # Fetch up to 2000 new
    python3 fetch_edhrec_all.py --refresh        # Re-fetch ALL existing + new
    python3 fetch_edhrec_all.py --refresh-top 200  # Re-fetch top 200 by rank + new
    python3 fetch_edhrec_all.py --stats          # Show coverage
    python3 fetch_edhrec_all.py --synergy-only   # Skip average deck fetch (2x faster)
"""
import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from mtg_synergy.db import get_connection

BASIC_LANDS = {"Plains", "Island", "Swamp", "Mountain", "Forest",
               "Wastes", "Snow-Covered Plains", "Snow-Covered Island",
               "Snow-Covered Swamp", "Snow-Covered Mountain", "Snow-Covered Forest"}


def name_to_slug(name):
    """Convert card name to EDHREC slug format.

    Handles DFCs (front face only), accented characters, and apostrophes.
    Examples:
        "Krenko, Mob Boss" -> "krenko-mob-boss"
        "Birgi, God of Storytelling // Harnfel, Horn of Bounty" -> "birgi-god-of-storytelling"
        "Glóin, Dwarf Emissary" -> "gloin-dwarf-emissary"
    """
    name = name.split(" // ")[0]
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    slug = name.lower()
    slug = slug.replace("'", "")
    slug = slug.replace("\u2019", "")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def _fetch_json(url):
    """Fetch JSON from URL with retry."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MTG-Synergy-Graph/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            if attempt < 2:
                time.sleep(1)
    return None


def fetch_synergy_cards(slug):
    """Fetch synergy scores from commander page."""
    data = _fetch_json(f"https://json.edhrec.com/pages/commanders/{slug}.json")
    if data is None:
        return None

    cards = []
    for section in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        header = section.get("header", "")
        for cv in section.get("cardviews", []):
            name = cv.get("name", "")
            synergy = cv.get("synergy")
            inclusion = cv.get("inclusion")
            num_decks = cv.get("num_decks")
            if name and synergy is not None:
                cards.append((name, synergy, inclusion, num_decks, header))
    return cards


def fetch_average_deck(slug):
    """Fetch average decklist."""
    data = _fetch_json(f"https://json.edhrec.com/pages/average-decks/{slug}.json")
    if data is None:
        return None

    cards = []
    for section in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        category = section.get("header", "unknown")
        for cv in section.get("cardviews", []):
            name = cv.get("name", "")
            if name and name not in BASIC_LANDS:
                cards.append((name, category))
    return cards


def _fetch_one_commander(slug, synergy_only):
    """Fetch data for one commander. Returns (slug, syn_cards, avg_cards)."""
    syn_cards = fetch_synergy_cards(slug)
    avg_cards = None if synergy_only else fetch_average_deck(slug)
    return slug, syn_cards, avg_cards


def ensure_schemas(conn):
    """Ensure both tables exist with correct schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edhrec_card_synergy (
            commander_slug TEXT NOT NULL,
            card_name TEXT NOT NULL,
            synergy REAL,
            inclusion INTEGER,
            num_decks INTEGER,
            section TEXT,
            UNIQUE(commander_slug, card_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edhrec_average_decks (
            commander_slug TEXT NOT NULL,
            card_name TEXT NOT NULL,
            category TEXT,
            PRIMARY KEY (commander_slug, card_name)
        )
    """)
    conn.commit()


def get_all_commander_slugs(conn, max_slugs=3000):
    """Get ALL legal commanders ordered by EDHREC rank."""
    commanders = conn.execute("""
        SELECT name, oracle_id, edhrec_rank FROM cards
        WHERE (type_line LIKE '%Legendary%Creature%'
               OR type_line LIKE '%Legendary%Planeswalker%'
               OR oracle_text LIKE '%can be your commander%'
               OR (type_line LIKE '%Vehicle%' AND oracle_text LIKE '%crew%'))
        AND edhrec_rank IS NOT NULL
        AND legal_commander = 1
        ORDER BY edhrec_rank ASC
        LIMIT ?
    """, (max_slugs,)).fetchall()

    return [(name_to_slug(name), name, rank) for name, _, rank in commanders]


def fetch_all(conn, max_new=500, refresh_top=0, refresh_all=False,
              synergy_only=False, workers=4):
    """Fetch synergy scores and average decklists.

    Thread safety: worker threads only perform HTTP fetches via
    ``_fetch_one_commander``.  All SQLite reads and writes happen in the
    calling (main) thread, so ``conn`` is never shared across threads.

    Args:
        conn: SQLite connection (used only from the main thread)
        max_new: max NEW commanders to fetch
        refresh_top: re-fetch top N commanders by rank (0 = no refresh)
        refresh_all: re-fetch ALL existing commanders
        synergy_only: skip average deck fetch (2x faster)
        workers: concurrent fetch threads
    """
    ensure_schemas(conn)

    all_slugs = get_all_commander_slugs(conn, max_slugs=max_new + 3000)
    existing_syn = set(r[0] for r in conn.execute(
        "SELECT DISTINCT commander_slug FROM edhrec_card_synergy"))

    # Build fetch list: refresh targets + new commanders
    to_fetch = []
    refresh_set = set()

    if refresh_all:
        # Re-fetch everything
        refresh_set = existing_syn.copy()
    elif refresh_top > 0:
        # Re-fetch top N by EDHREC rank
        for slug, name, rank in all_slugs[:refresh_top]:
            if slug in existing_syn:
                refresh_set.add(slug)

    # Add refresh targets first (they need DELETE + re-INSERT)
    for slug, name, rank in all_slugs:
        if slug in refresh_set:
            to_fetch.append((slug, name, True))  # is_refresh=True

    # Add new commanders
    n_new = 0
    for slug, name, rank in all_slugs:
        if slug not in existing_syn and slug not in refresh_set:
            if n_new < max_new:
                to_fetch.append((slug, name, False))
                n_new += 1

    n_refresh = sum(1 for _, _, r in to_fetch if r)
    n_new_actual = sum(1 for _, _, r in to_fetch if not r)
    print(f"Fetching: {n_refresh} refresh + {n_new_actual} new = {len(to_fetch)} total")
    if synergy_only:
        print(f"  Synergy-only mode (skipping average decks)")
    print(f"  {workers} concurrent workers")

    fetched_syn = 0
    fetched_avg = 0
    errors = 0
    t0 = time.time()

    # Rate-limited concurrent fetching
    # EDHREC can handle ~4 req/sec; with 4 workers and 0.3s sleep we get ~3 req/sec
    batch_size = workers
    for batch_start in range(0, len(to_fetch), batch_size):
        batch = to_fetch[batch_start:batch_start + batch_size]

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_fetch_one_commander, slug, synergy_only): (slug, name, is_refresh)
                for slug, name, is_refresh in batch
            }
            for future in as_completed(futures):
                slug, name, is_refresh = futures[future]
                try:
                    _, syn_cards, avg_cards = future.result()
                except Exception as exc:
                    print(f"  ERROR fetching {slug}: {exc}", file=sys.stderr)
                    errors += 1
                    continue

                # For refresh: delete old data first
                if is_refresh and syn_cards:
                    conn.execute("DELETE FROM edhrec_card_synergy WHERE commander_slug = ?", (slug,))
                    if not synergy_only:
                        conn.execute("DELETE FROM edhrec_average_decks WHERE commander_slug = ?", (slug,))

                if syn_cards:
                    for card_name, synergy, inclusion, num_decks, section in syn_cards:
                        conn.execute(
                            "INSERT OR REPLACE INTO edhrec_card_synergy "
                            "(commander_slug, card_name, synergy, inclusion, num_decks, section) "
                            "VALUES (?,?,?,?,?,?)",
                            (slug, card_name, synergy, inclusion, num_decks, section))
                    fetched_syn += 1
                else:
                    errors += 1

                if avg_cards:
                    for card_name, category in avg_cards:
                        conn.execute(
                            "INSERT OR REPLACE INTO edhrec_average_decks "
                            "(commander_slug, card_name, category) VALUES (?,?,?)",
                            (slug, card_name, category))
                    fetched_avg += 1

        conn.commit()
        # Rate limit between batches
        time.sleep(0.3 * len(batch))

        done = batch_start + len(batch)
        if done % 100 < batch_size or done == len(to_fetch):
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            remaining = (len(to_fetch) - done) / rate if rate > 0 else 0
            print(f"  {done}/{len(to_fetch)}: syn={fetched_syn} avg={fetched_avg} "
                  f"err={errors} ({rate:.1f}/s, ~{remaining:.0f}s left)")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s: {fetched_syn} synergy + {fetched_avg} avg decks "
          f"({errors} errors)")


def show_stats(conn):
    ensure_schemas(conn)
    syn = conn.execute("SELECT COUNT(DISTINCT commander_slug) FROM edhrec_card_synergy").fetchone()[0]
    syn_pairs = conn.execute("SELECT COUNT(*) FROM edhrec_card_synergy").fetchone()[0]
    avg = conn.execute("SELECT COUNT(DISTINCT commander_slug) FROM edhrec_average_decks").fetchone()[0]
    avg_cards = conn.execute("SELECT COUNT(*) FROM edhrec_average_decks").fetchone()[0]
    both = conn.execute("""
        SELECT COUNT(DISTINCT s.commander_slug) FROM edhrec_card_synergy s
        JOIN edhrec_average_decks a ON a.commander_slug = s.commander_slug
    """).fetchone()[0]

    total_legal = conn.execute("""
        SELECT COUNT(*) FROM cards
        WHERE (type_line LIKE '%Legendary%Creature%'
               OR type_line LIKE '%Legendary%Planeswalker%')
        AND legal_commander = 1 AND edhrec_rank IS NOT NULL
    """).fetchone()[0]

    print(f"EDHREC synergy: {syn} commanders, {syn_pairs:,} pairs")
    print(f"EDHREC avg decks: {avg} commanders, {avg_cards:,} cards")
    print(f"Both available: {both} commanders")
    print(f"Total legal commanders: {total_legal}")
    print(f"Coverage: {syn}/{total_legal} ({syn/total_legal*100:.0f}%)")
    print(f"Unfetched: {total_legal - syn}")


def main():
    parser = argparse.ArgumentParser(description="Fetch EDHREC data")
    parser.add_argument("--max", type=int, default=500,
                        help="Max new commanders to fetch (default: 500)")
    parser.add_argument("--refresh", action="store_true",
                        help="Re-fetch ALL existing commanders")
    parser.add_argument("--refresh-top", type=int, default=0,
                        help="Re-fetch top N commanders by EDHREC rank")
    parser.add_argument("--synergy-only", action="store_true",
                        help="Skip average deck fetch (2x faster)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Concurrent fetch threads (default: 4)")
    parser.add_argument("--stats", action="store_true",
                        help="Show coverage stats")
    args = parser.parse_args()

    conn = get_connection()
    if args.stats:
        show_stats(conn)
    else:
        fetch_all(conn, max_new=args.max, refresh_top=args.refresh_top,
                  refresh_all=args.refresh, synergy_only=args.synergy_only,
                  workers=args.workers)
        show_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
