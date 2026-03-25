#!/usr/bin/env python3
"""Import Forge card scripts as structured effect data.

Usage:
    python3 import_forge.py --download    # Clone Forge repo (sparse)
    python3 import_forge.py --import      # Parse and import to DB
    python3 import_forge.py --stats       # Show import stats
"""
import argparse
import os
import subprocess

from mtg_synergy.db import get_connection
from mtg_synergy.parse.forge_fallback import (
    parse_forge_ability_line, map_forge_verb, ensure_forge_schema,
)

FORGE_REPO = "https://github.com/Card-Forge/forge.git"
FORGE_DIR = "data/forge"
CARDS_DIR = os.path.join(FORGE_DIR, "forge-gui", "res", "cardsfolder")


def download_forge():
    """Clone the Forge repo with sparse checkout (cardsfolder only)."""
    if os.path.exists(FORGE_DIR):
        print(f"Forge repo already exists at {FORGE_DIR}")
        return
    print("Cloning Forge repo (sparse, cardsfolder only)...")
    subprocess.run([
        "git", "clone", "--depth", "1", "--filter=blob:none",
        "--sparse", FORGE_REPO, FORGE_DIR
    ], check=True)
    subprocess.run([
        "git", "-C", FORGE_DIR, "sparse-checkout", "set",
        "forge-gui/res/cardsfolder"
    ], check=True)
    print("Done.")


def parse_forge_card(filepath):
    """Parse a single Forge card script file.

    Returns:
        Tuple of (card_name, list_of_parsed_abilities).
    """
    name = None
    abilities = []
    ab_idx = 0
    with open(filepath, "r", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Name:"):
                name = line[5:].strip()
            elif line.startswith(("A:", "T:", "S:")):
                parsed = parse_forge_ability_line(line)
                if parsed and parsed["forge_verb"]:
                    our_verb = map_forge_verb(
                        parsed["forge_verb"],
                        origin=parsed.get("origin"),
                        destination=parsed.get("destination"),
                    )
                    parsed["our_verb"] = our_verb
                    parsed["ability_index"] = ab_idx
                    abilities.append(parsed)
                    ab_idx += 1
    return name, abilities


def import_all(conn):
    """Parse all Forge card scripts and import effects into the DB."""
    ensure_forge_schema(conn)
    conn.execute("DELETE FROM forge_effects")
    if not os.path.exists(CARDS_DIR):
        print(f"Forge cards not found at {CARDS_DIR}")
        print("Run: python3 import_forge.py --download")
        return 0
    imported = 0
    errors = 0
    for root, dirs, files in os.walk(CARDS_DIR):
        for fname in files:
            if not fname.endswith(".txt"):
                continue
            try:
                filepath = os.path.join(root, fname)
                name, abilities = parse_forge_card(filepath)
                if not name or not abilities:
                    continue
                for ab in abilities:
                    conn.execute(
                        "INSERT OR IGNORE INTO forge_effects VALUES (?,?,?,?,?,?,?)",
                        (name, ab["ability_index"], ab["forge_verb"],
                         ab.get("our_verb"), ab.get("target"), ab.get("amount"),
                         ab.get("trigger_type")),
                    )
                imported += 1
            except Exception:
                errors += 1
    conn.commit()
    print(f"Imported {imported} cards, {errors} errors")
    return imported


def show_stats(conn):
    """Show summary statistics for imported Forge effects."""
    ensure_forge_schema(conn)
    cards = conn.execute("SELECT COUNT(DISTINCT card_name) FROM forge_effects").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM forge_effects").fetchone()[0]
    mapped = conn.execute("SELECT COUNT(*) FROM forge_effects WHERE our_verb IS NOT NULL").fetchone()[0]
    print(f"Forge effects: {cards} cards, {total} abilities, {mapped} mapped to our verbs")


def main():
    parser = argparse.ArgumentParser(description="Import Forge card scripts as structured effect data.")
    parser.add_argument("--download", action="store_true", help="Clone Forge repo (sparse checkout)")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Parse and import to DB")
    parser.add_argument("--stats", action="store_true", help="Show import stats")
    args = parser.parse_args()
    if args.download:
        download_forge()
    elif args.do_import:
        conn = get_connection()
        import_all(conn)
        conn.close()
    elif args.stats:
        conn = get_connection()
        show_stats(conn)
        conn.close()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
