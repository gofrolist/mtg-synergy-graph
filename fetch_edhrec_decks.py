#!/usr/bin/env python3
"""Fetch EDHREC average decklists for top commanders.

Fetches average deck JSON from EDHREC's API and stores card lists
in the edhrec_average_decks table. Rate-limited to 1 req/sec.

Usage:
    python3 fetch_edhrec_decks.py                   # Fetch top 1000
    python3 fetch_edhrec_decks.py --max 100          # Fetch top 100
    python3 fetch_edhrec_decks.py --stats            # Show fetch progress
"""
import argparse
import json
import sqlite3
import time
import urllib.request
import urllib.error

from mtg_synergy.db import get_connection

BASIC_LANDS = {"Plains", "Island", "Swamp", "Mountain", "Forest",
               "Wastes", "Snow-Covered Plains", "Snow-Covered Island",
               "Snow-Covered Swamp", "Snow-Covered Mountain", "Snow-Covered Forest"}

# NOTE: EDHREC JSON structure may change without notice. Verified 2026-03-25.
API_URL = "https://json.edhrec.com/pages/average-decks/{slug}.json"


def ensure_avg_deck_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edhrec_average_decks (
            commander_slug TEXT NOT NULL,
            card_name TEXT NOT NULL,
            category TEXT,
            PRIMARY KEY (commander_slug, card_name)
        )
    """)
    conn.commit()


def get_top_slugs(conn, max_slugs=1000):
    """Get top commander slugs by number of synergy entries (most popular first)."""
    rows = conn.execute(
        "SELECT commander_slug, COUNT(*) as cnt FROM edhrec_card_synergy "
        "GROUP BY commander_slug ORDER BY cnt DESC LIMIT ?",
        (max_slugs,)
    ).fetchall()
    return [r[0] for r in rows]


def fetch_average_deck(slug):
    """Fetch average deck from EDHREC JSON API. Returns list of (card_name, category) or None."""
    url = API_URL.format(slug=slug)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MTG-Synergy-Graph/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    cards = []
    cardlists = data.get("container", {}).get("json_dict", {}).get("cardlists", [])
    for section in cardlists:
        category = section.get("header", "unknown")
        for card_view in section.get("cardviews", []):
            name = card_view.get("name", "")
            if name and name not in BASIC_LANDS:
                cards.append((name, category))
    return cards


def fetch_all(conn, max_slugs=1000, resume=True):
    """Fetch average decks for top commanders."""
    ensure_avg_deck_schema(conn)
    slugs = get_top_slugs(conn, max_slugs)

    already = set()
    if resume:
        for r in conn.execute("SELECT DISTINCT commander_slug FROM edhrec_average_decks"):
            already.add(r[0])

    remaining = [s for s in slugs if s not in already]
    print(f"Fetching average decks: {len(remaining)} remaining "
          f"({len(already)} already cached) out of {len(slugs)} total")

    fetched = 0
    errors = 0
    for i, slug in enumerate(remaining):
        cards = fetch_average_deck(slug)
        if cards is None:
            errors += 1
            if errors % 10 == 0:
                print(f"  {errors} errors so far...")
            time.sleep(1)
            continue

        for name, category in cards:
            conn.execute(
                "INSERT OR IGNORE INTO edhrec_average_decks VALUES (?,?,?)",
                (slug, name, category)
            )
        conn.commit()
        fetched += 1

        if (i + 1) % 50 == 0:
            print(f"  Fetched {fetched}/{len(remaining)} ({errors} errors)...")

        time.sleep(1)

    print(f"\nDone: {fetched} fetched, {errors} errors")
    return fetched


def show_stats(conn):
    ensure_avg_deck_schema(conn)
    total = conn.execute("SELECT COUNT(DISTINCT commander_slug) FROM edhrec_average_decks").fetchone()[0]
    cards = conn.execute("SELECT COUNT(*) FROM edhrec_average_decks").fetchone()[0]
    print(f"EDHREC average decks: {total} commanders, {cards} total card entries")
    if total > 0:
        avg = cards / total
        print(f"Average cards per deck: {avg:.0f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=1000)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    if args.stats:
        show_stats(conn)
    else:
        fetch_all(conn, max_slugs=args.max)
    conn.close()


if __name__ == "__main__":
    main()
