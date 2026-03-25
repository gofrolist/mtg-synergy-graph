#!/usr/bin/env python3
"""Fetch EDHREC synergy scores + average decklists for commanders.

Fetches both data sources in one pass:
1. Synergy scores: per-card synergy values from commander page
2. Average decklists: consensus 75-card deck

Usage:
    python3 fetch_edhrec_all.py                  # Fetch next 500 commanders
    python3 fetch_edhrec_all.py --max 1000       # Fetch up to 1000
    python3 fetch_edhrec_all.py --stats          # Show coverage
"""
import argparse
import json
import re
import time
import urllib.request
import urllib.error

from mtg_synergy.db import get_connection

BASIC_LANDS = {"Plains", "Island", "Swamp", "Mountain", "Forest",
               "Wastes", "Snow-Covered Plains", "Snow-Covered Island",
               "Snow-Covered Swamp", "Snow-Covered Mountain", "Snow-Covered Forest"}


def name_to_slug(name):
    """Convert card name to EDHREC slug format."""
    name = name.split(" // ")[0]
    slug = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    slug = re.sub(r"\s+", "-", slug.strip())
    return slug


def get_commander_slugs(conn, max_slugs=1000):
    """Get commander slugs to fetch, ordered by EDHREC rank."""
    existing_syn = set(r[0] for r in conn.execute(
        "SELECT DISTINCT commander_slug FROM edhrec_card_synergy"))
    existing_avg = set(r[0] for r in conn.execute(
        "SELECT DISTINCT commander_slug FROM edhrec_average_decks"))
    already_done = existing_syn & existing_avg

    commanders = conn.execute("""
        SELECT name, oracle_id, edhrec_rank FROM cards
        WHERE (type_line LIKE '%Legendary%Creature%'
               OR type_line LIKE '%Legendary%Planeswalker%'
               OR oracle_text LIKE '%can be your commander%'
               OR type_line LIKE '%Vehicle%' AND oracle_text LIKE '%crew%')
        AND edhrec_rank IS NOT NULL
        ORDER BY edhrec_rank ASC
        LIMIT ?
    """, (max_slugs + len(already_done),)).fetchall()

    slugs = []
    for name, oid, rank in commanders:
        slug = name_to_slug(name)
        if slug not in already_done and len(slugs) < max_slugs:
            slugs.append((slug, name))

    return slugs, len(already_done)


def fetch_synergy_cards(slug):
    """Fetch synergy scores from commander page."""
    url = f"https://json.edhrec.com/pages/commanders/{slug}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MTG-Synergy-Graph/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    cards = []
    for section in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        for cv in section.get("cardviews", []):
            name = cv.get("name", "")
            synergy = cv.get("synergy")
            if name and synergy is not None:
                cards.append((name, synergy))
    return cards


def fetch_average_deck(slug):
    """Fetch average decklist."""
    url = f"https://json.edhrec.com/pages/average-decks/{slug}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MTG-Synergy-Graph/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    cards = []
    for section in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        category = section.get("header", "unknown")
        for cv in section.get("cardviews", []):
            name = cv.get("name", "")
            if name and name not in BASIC_LANDS:
                cards.append((name, category))
    return cards


def ensure_schemas(conn):
    """Ensure both tables exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edhrec_card_synergy (
            commander_slug TEXT NOT NULL,
            card_name TEXT NOT NULL,
            synergy REAL NOT NULL,
            PRIMARY KEY (commander_slug, card_name)
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


def fetch_all(conn, max_slugs=500):
    """Fetch both synergy scores and average decklists."""
    ensure_schemas(conn)
    slugs, already = get_commander_slugs(conn, max_slugs)
    print(f"Fetching: {len(slugs)} commanders ({already} already done)")

    fetched_syn = 0
    fetched_avg = 0
    errors = 0

    for i, (slug, name) in enumerate(slugs):
        # Fetch synergy scores
        syn_cards = fetch_synergy_cards(slug)
        if syn_cards:
            for card_name, synergy in syn_cards:
                conn.execute(
                    "INSERT OR IGNORE INTO edhrec_card_synergy VALUES (?,?,?)",
                    (slug, card_name, synergy))
            fetched_syn += 1
        time.sleep(0.5)

        # Fetch average decklist
        avg_cards = fetch_average_deck(slug)
        if avg_cards:
            for card_name, category in avg_cards:
                conn.execute(
                    "INSERT OR IGNORE INTO edhrec_average_decks VALUES (?,?,?)",
                    (slug, card_name, category))
            fetched_avg += 1
        else:
            errors += 1
        time.sleep(0.5)

        conn.commit()

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(slugs)}: syn={fetched_syn} avg={fetched_avg} errors={errors}")

    print(f"\nDone: {fetched_syn} synergy + {fetched_avg} avg decks ({errors} errors)")


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
    print(f"EDHREC synergy: {syn} commanders, {syn_pairs:,} pairs")
    print(f"EDHREC avg decks: {avg} commanders, {avg_cards:,} cards")
    print(f"Both available: {both} commanders")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=500)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    if args.stats:
        show_stats(conn)
    else:
        fetch_all(conn, max_slugs=args.max)
        show_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
