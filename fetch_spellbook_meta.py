#!/usr/bin/env python3
"""Fetch Commander Spellbook features and templates metadata.

Features are combo outcome categories (e.g., "Infinite mana", "Infinite tokens").
Templates are abstract combo patterns with Scryfall queries.

Usage:
    python3 fetch_spellbook_meta.py              # fetch + import all
    python3 fetch_spellbook_meta.py --features    # features only
    python3 fetch_spellbook_meta.py --templates   # templates only
    python3 fetch_spellbook_meta.py --stats       # show stats from DB
"""

import json
import sqlite3
import os
import time
import urllib.request
import urllib.error

BASE_URL = "https://backend.commanderspellbook.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FEATURES_PATH = os.path.join(DATA_DIR, "spellbook_features.json")
TEMPLATES_PATH = os.path.join(DATA_DIR, "spellbook_templates.json")
DB_PATH = os.path.join(DATA_DIR, "tags.db")


def fetch_paginated(endpoint, label, limit_per_page=100):
    """Fetch all records from a paginated Spellbook API endpoint.

    Returns list of raw dicts from the API.
    """
    results = []
    url = f"{BASE_URL}/{endpoint}/?format=json&limit={limit_per_page}&offset=0"
    page = 0

    while url:
        print(f"  Fetching {label} page {page + 1}... ({len(results)} so far)")

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

        for item in data.get("results", []):
            results.append(item)

        url = data.get("next")
        page += 1
        time.sleep(0.1)

    return results


def fetch_features():
    """Fetch all features (combo outcome categories).

    Returns list of dicts with keys: id, name, uncountable, status.
    """
    raw = fetch_paginated("features", "features")
    features = []
    for item in raw:
        features.append({
            "id": item["id"],
            "name": item.get("name", ""),
            "uncountable": item.get("uncountable", False),
            "status": item.get("status", ""),
        })
    return features


def fetch_templates():
    """Fetch all templates (abstract combo patterns).

    Returns list of dicts with keys: id, name, scryfallQuery, scryfallApi.
    """
    raw = fetch_paginated("templates", "templates")
    templates = []
    for item in raw:
        templates.append({
            "id": item["id"],
            "name": item.get("name", ""),
            "scryfall_query": item.get("scryfallQuery", ""),
            "scryfall_api": item.get("scryfallApi", ""),
        })
    return templates


def ensure_tables(conn):
    """Create metadata tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spellbook_features (
            id INTEGER PRIMARY KEY,
            name TEXT,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spellbook_templates (
            id INTEGER PRIMARY KEY,
            name TEXT,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spellbook_combo_features (
            combo_id TEXT NOT NULL,
            feature_id INTEGER NOT NULL,
            UNIQUE(combo_id, feature_id),
            FOREIGN KEY (combo_id) REFERENCES spellbook_combos(combo_id),
            FOREIGN KEY (feature_id) REFERENCES spellbook_features(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_combo_features_combo
        ON spellbook_combo_features(combo_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_combo_features_feature
        ON spellbook_combo_features(feature_id)
    """)
    conn.commit()


def import_features_to_db(features, db_path=None):
    """Import features into spellbook_features table."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    ensure_tables(conn)

    conn.execute("DELETE FROM spellbook_features")
    for feat in features:
        # Store status/uncountable info in description for reference
        desc_parts = []
        if feat.get("status"):
            desc_parts.append(f"status={feat['status']}")
        if feat.get("uncountable"):
            desc_parts.append("uncountable")
        description = ", ".join(desc_parts) if desc_parts else None

        conn.execute("""
            INSERT OR REPLACE INTO spellbook_features (id, name, description)
            VALUES (?, ?, ?)
        """, (feat["id"], feat["name"], description))

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM spellbook_features").fetchone()[0]
    conn.close()
    print(f"Imported {count} features into DB")


def import_templates_to_db(templates, db_path=None):
    """Import templates into spellbook_templates table."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    ensure_tables(conn)

    conn.execute("DELETE FROM spellbook_templates")
    for tmpl in templates:
        # Store scryfall query as description
        description = tmpl.get("scryfall_query", "")

        conn.execute("""
            INSERT OR REPLACE INTO spellbook_templates (id, name, description)
            VALUES (?, ?, ?)
        """, (tmpl["id"], tmpl["name"], description))

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM spellbook_templates").fetchone()[0]
    conn.close()
    print(f"Imported {count} templates into DB")


def link_combo_features(db_path=None):
    """Link existing combos to features by matching result text to feature names.

    The spellbook_combos.result column contains comma-separated feature names.
    This function populates spellbook_combo_features from that data.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    ensure_tables(conn)

    # Build feature name → id lookup
    features = conn.execute("SELECT id, name FROM spellbook_features").fetchall()
    name_to_id = {name.strip().lower(): fid for fid, name in features}

    if not name_to_id:
        print("No features in DB, skipping combo-feature linking")
        conn.close()
        return

    conn.execute("DELETE FROM spellbook_combo_features")

    combos = conn.execute(
        "SELECT combo_id, result FROM spellbook_combos WHERE result IS NOT NULL AND result != ''"
    ).fetchall()

    linked = 0
    for combo_id, result in combos:
        for part in result.split(","):
            part_clean = part.strip().lower()
            if part_clean in name_to_id:
                conn.execute("""
                    INSERT OR IGNORE INTO spellbook_combo_features (combo_id, feature_id)
                    VALUES (?, ?)
                """, (combo_id, name_to_id[part_clean]))
                linked += 1

    conn.commit()
    conn.close()
    print(f"Linked {linked} combo-feature associations")


def show_stats(db_path=None):
    """Print stats about imported Spellbook metadata."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Check if tables exist
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'spellbook_%'"
    ).fetchall()]

    if "spellbook_features" in tables:
        total_features = conn.execute("SELECT COUNT(*) FROM spellbook_features").fetchone()[0]
        print(f"Spellbook features: {total_features}")

        top = conn.execute(
            "SELECT name FROM spellbook_features ORDER BY id LIMIT 10"
        ).fetchall()
        for (name,) in top:
            print(f"  - {name}")
        if total_features > 10:
            print(f"  ... and {total_features - 10} more")
    else:
        print("No spellbook_features table")

    if "spellbook_templates" in tables:
        total_templates = conn.execute("SELECT COUNT(*) FROM spellbook_templates").fetchone()[0]
        print(f"\nSpellbook templates: {total_templates}")

        top = conn.execute(
            "SELECT name FROM spellbook_templates ORDER BY id LIMIT 10"
        ).fetchall()
        for (name,) in top:
            print(f"  - {name}")
        if total_templates > 10:
            print(f"  ... and {total_templates - 10} more")
    else:
        print("No spellbook_templates table")

    if "spellbook_combo_features" in tables:
        total_links = conn.execute("SELECT COUNT(*) FROM spellbook_combo_features").fetchone()[0]
        combos_with_features = conn.execute(
            "SELECT COUNT(DISTINCT combo_id) FROM spellbook_combo_features"
        ).fetchone()[0]
        print(f"\nCombo-feature links: {total_links}")
        print(f"Combos with linked features: {combos_with_features}")

        if total_links > 0:
            print("\nTop features by combo count:")
            top_features = conn.execute("""
                SELECT f.name, COUNT(*) as cnt
                FROM spellbook_combo_features cf
                JOIN spellbook_features f ON cf.feature_id = f.id
                GROUP BY cf.feature_id
                ORDER BY cnt DESC
                LIMIT 15
            """).fetchall()
            for name, cnt in top_features:
                print(f"  {cnt:6d}  {name}")
    else:
        print("No spellbook_combo_features table")

    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch Commander Spellbook features and templates")
    parser.add_argument("--features", action="store_true", help="Fetch features only")
    parser.add_argument("--templates", action="store_true", help="Fetch templates only")
    parser.add_argument("--stats", action="store_true", help="Show DB stats")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        exit(0)

    # Default: fetch both if neither flag is set
    do_features = args.features or (not args.features and not args.templates)
    do_templates = args.templates or (not args.features and not args.templates)

    os.makedirs(DATA_DIR, exist_ok=True)

    if do_features:
        print("Fetching features from Commander Spellbook...")
        features = fetch_features()
        print(f"Fetched {len(features)} features")

        with open(FEATURES_PATH, "w") as f:
            json.dump(features, f, indent=2)
        print(f"Cached to {FEATURES_PATH}")

        print("Importing features to DB...")
        import_features_to_db(features)

        print("Linking combos to features...")
        link_combo_features()

    if do_templates:
        print("\nFetching templates from Commander Spellbook...")
        templates = fetch_templates()
        print(f"Fetched {len(templates)} templates")

        with open(TEMPLATES_PATH, "w") as f:
            json.dump(templates, f, indent=2)
        print(f"Cached to {TEMPLATES_PATH}")

        print("Importing templates to DB...")
        import_templates_to_db(templates)

    print("\n--- Stats ---")
    show_stats()
