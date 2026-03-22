#!/usr/bin/env python3
"""Refine generic tags into specific sub-tags using oracle text analysis.

Splits overly broad wants tags (creature-board, board-threats) into
specific sub-tags that enable precise semantic bridges.

No LLM needed — uses oracle text pattern matching.

Usage:
    python3 refine_tags.py                # refine all cards
    python3 refine_tags.py --dry-run      # show what would change
    python3 refine_tags.py --stats        # show current tag distribution
"""

import json
import os
import re
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")

# Patterns to detect what a card ACTUALLY wants when it says "creature-board"
# Each pattern: (regex on oracle_text, replacement_tags, description)
CREATURE_BOARD_REFINEMENTS = [
    # Sacrifice outlets — need creatures as fodder
    (re.compile(r'sacrifice (?:a|another) creature|sacrifice a .* creature', re.I),
     ["sacrifice-fodder"], "sacrifice outlet"),

    # Death triggers — need creatures dying
    (re.compile(r'whenever (?:a|another) creature (?:you control )?dies|creature.* is put into .* graveyard', re.I),
     ["creature-death-payoff"], "death trigger"),

    # ETB triggers — need creatures entering (already have creature-etb usually)
    (re.compile(r'whenever (?:a|another) (?:\w+ )?creature (?:you control )?enters', re.I),
     ["creature-etb-payoff"], "ETB trigger"),

    # Equipment/aura — need a creature to attach to
    (re.compile(r'equip|equipped creature|enchanted creature|reconfigure', re.I),
     ["equip-target"], "equipment/aura target"),

    # Power/toughness matters — need creatures with stats
    (re.compile(r'greatest power|equal to .* power|power among|toughness among|power \d+ or greater', re.I),
     ["creature-power-matters"], "power/toughness matters"),

    # Creature count matters — need many creatures
    (re.compile(r'for each creature|number of creatures|creatures you control get|each creature you control', re.I),
     ["creature-count-matters"], "creature count matters"),

    # Tap abilities — need creatures that can tap
    (re.compile(r'tap (?:an )?untapped creature|tap .* you control|convoke|\{T\}.*creature', re.I),
     ["tap-ability-creatures"], "tap creatures for value"),

    # Combat-focused — need creatures to attack
    (re.compile(r'attacking creature|whenever .* attacks|combat damage.*player|each creature .* attacks', re.I),
     ["combat-attackers"], "needs attackers"),

    # Type selection — cares about creature types
    (re.compile(r'choose a creature type|of the chosen type|creature type .* control|creature subtype', re.I),
     ["creature-type-matters"], "creature type matters"),

    # Copy/clone — needs a creature to copy
    (re.compile(r'copy of (?:target|any|a) (?:artifact or )?creature|enters? as a copy', re.I),
     ["creature-copy-target"], "copy/clone target"),
]

# Similar refinements for other generic tags
BOARD_THREATS_REFINEMENTS = [
    (re.compile(r'opponent.*controls? (?:a |no )?creature', re.I),
     ["opponent-creatures"], "cares about opponent creatures"),
    (re.compile(r'each opponent|opponents? lose|opponents? discard', re.I),
     ["opponent-targeting"], "targets opponents"),
]


def refine_card(oracle_text, current_wants):
    """Refine generic wants tags for a single card.

    Returns set of tags to ADD (doesn't remove the original generic tag).
    """
    if not oracle_text:
        return set()

    new_tags = set()

    if "creature-board" in current_wants:
        for pattern, tags, desc in CREATURE_BOARD_REFINEMENTS:
            if pattern.search(oracle_text):
                new_tags.update(tags)

    if "board-threats" in current_wants:
        for pattern, tags, desc in BOARD_THREATS_REFINEMENTS:
            if pattern.search(oracle_text):
                new_tags.update(tags)

    return new_tags


def refine_all(db_path=None, dry_run=False):
    """Refine generic tags for all cards in the DB.

    Adds specific sub-tags alongside the generic ones.
    Returns (cards_refined, tags_added).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Get all cards with their wants tags and oracle text
    cards = conn.execute(
        "SELECT oracle_id, name, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ).fetchall()
    wants_by_card = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM wants").fetchall():
        wants_by_card.setdefault(oid, set()).add(tag)

    cards_refined = 0
    tags_added = 0

    for oracle_id, name, oracle_text in cards:
        current_wants = wants_by_card.get(oracle_id, set())
        new_tags = refine_card(oracle_text, current_wants)

        # Only add tags that don't already exist
        new_tags -= current_wants

        if new_tags:
            cards_refined += 1
            tags_added += len(new_tags)

            if not dry_run:
                for tag in new_tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO wants (oracle_id, tag) VALUES (?, ?)",
                        (oracle_id, tag)
                    )

    if not dry_run:
        conn.commit()
    conn.close()

    return cards_refined, tags_added


def show_stats(db_path=None):
    """Show distribution of generic vs refined tags."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    generic_tags = ["creature-board", "board-threats", "opponent-threats", "mana-needs"]
    refined_tags = [
        "sacrifice-fodder", "creature-death-payoff", "creature-etb-payoff",
        "equip-target", "creature-power-matters", "creature-count-matters",
        "tap-ability-creatures", "combat-attackers", "creature-type-matters",
        "creature-copy-target", "opponent-creatures", "opponent-targeting",
    ]

    print("Generic tags (broad):")
    for tag in generic_tags:
        cnt = conn.execute("SELECT COUNT(*) FROM wants WHERE tag = ?", (tag,)).fetchone()[0]
        print(f"  {tag:<25} {cnt:>6} cards")

    print("\nRefined tags (specific):")
    for tag in refined_tags:
        cnt = conn.execute("SELECT COUNT(*) FROM wants WHERE tag = ?", (tag,)).fetchone()[0]
        if cnt > 0:
            print(f"  {tag:<25} {cnt:>6} cards")

    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Refine generic tags into specific sub-tags")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--stats", action="store_true", help="Show tag distribution")
    parser.add_argument("--db", default=None, help="DB path")
    args = parser.parse_args()

    db = args.db or DB_PATH

    if args.stats:
        show_stats(db)
    else:
        cards, tags = refine_all(db, dry_run=args.dry_run)
        action = "Would add" if args.dry_run else "Added"
        print(f"{action} {tags} refined tags across {cards} cards")
        if not args.dry_run:
            show_stats(db)
