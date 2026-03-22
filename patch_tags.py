#!/usr/bin/env python3
"""Targeted tag patches: fix known gaps in LLM-assigned tags using oracle text rules.

No LLM needed — deterministic fixes based on keywords and oracle text patterns.

Usage:
    python3 patch_tags.py              # apply all patches
    python3 patch_tags.py --dry-run    # show what would change
    python3 patch_tags.py --stats      # show patch coverage
"""

import json
import os
import re
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")

# Common creature types for tribal provides detection
TRIBAL_TYPES = {
    "human": "human-tribal", "goblin": "goblin-tribal", "elf": "elf-tribal",
    "zombie": "zombie-tribal", "vampire": "vampire-tribal", "dragon": "dragon-tribal",
    "angel": "angel-tribal", "demon": "demon-tribal", "sliver": "sliver-tribal",
    "dinosaur": "dinosaur-tribal", "pirate": "pirate-tribal", "wizard": "wizard-tribal",
    "knight": "knight-tribal", "merfolk": "merfolk-tribal", "elemental": "elemental-tribal",
    "spirit": "spirit-tribal", "soldier": "soldier-tribal", "cat": "cat-tribal",
    "warrior": "warrior-tribal", "rogue": "rogue-tribal", "cleric": "cleric-tribal",
    "rat": "rat-tribal", "faerie": "faerie-tribal", "bird": "bird-tribal",
    "beast": "beast-tribal",
}

# Tribal-relevant oracle text patterns (the card must reference the type meaningfully)
TRIBAL_ORACLE_PATTERNS = [
    r"other {type}s? you control",
    r"{type}s? you control get",
    r"{type}s? you control have",
    r"whenever (?:a|another) {type}",
    r"each {type} you control",
    r"number of {type}s?",
    r"for each {type}",
    r"all {type}s",
    r"target {type}",
    r"create .* {type}",
    r"search .* {type}",
    r"{type} creature (?:cards?|spells?) you",
    r"{type} creatures? enter",
]


def patch_all(db_path=None, dry_run=False):
    """Apply all targeted tag patches. Returns dict of patch results."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    results = {
        "poison": _patch_poison(conn, dry_run),
        "tribal": _patch_tribal(conn, dry_run),
        "stax": _patch_stax(conn, dry_run),
        "wheel": _patch_wheel(conn, dry_run),
        "blink": _patch_blink(conn, dry_run),
        "sacrifice": _patch_sacrifice(conn, dry_run),
        "high_power": _patch_high_power(conn, dry_run),
        "cost_reduction": _patch_cost_reduction(conn, dry_run),
    }

    if not dry_run:
        conn.commit()
    conn.close()
    return results


def _add_provides(conn, oracle_id, tag, dry_run):
    """Add a provides tag if not already present."""
    exists = conn.execute(
        "SELECT 1 FROM provides WHERE oracle_id = ? AND tag = ?",
        (oracle_id, tag)
    ).fetchone()
    if exists:
        return False
    if not dry_run:
        conn.execute("INSERT INTO provides (oracle_id, tag) VALUES (?, ?)", (oracle_id, tag))
    return True


def _add_wants(conn, oracle_id, tag, dry_run):
    """Add a wants tag if not already present."""
    exists = conn.execute(
        "SELECT 1 FROM wants WHERE oracle_id = ? AND tag = ?",
        (oracle_id, tag)
    ).fetchone()
    if exists:
        return False
    if not dry_run:
        conn.execute("INSERT INTO wants (oracle_id, tag) VALUES (?, ?)", (oracle_id, tag))
    return True


def _patch_poison(conn, dry_run):
    """Patch 1: Cards with Toxic/Infect keyword must provide poison-counter-placement."""
    cards = conn.execute("""
        SELECT oracle_id, name, keywords FROM cards
        WHERE keywords LIKE '%Toxic%' OR keywords LIKE '%Infect%'
    """).fetchall()

    patched = 0
    for oid, name, kw_json in cards:
        keywords = json.loads(kw_json) if kw_json else []
        kw_lower = {k.lower() for k in keywords}

        if "toxic" in kw_lower or "infect" in kw_lower:
            if _add_provides(conn, oid, "poison-counter-placement", dry_run):
                patched += 1
        if "infect" in kw_lower:
            if _add_provides(conn, oid, "infect", dry_run):
                patched += 1
        if "proliferate" in kw_lower:
            if _add_provides(conn, oid, "proliferate", dry_run):
                patched += 1

    return {"patched": patched, "description": "poison/infect/proliferate keyword → provides tag"}


def _patch_tribal(conn, dry_run):
    """Patch 2: Cards referencing creature types in oracle text must provide X-tribal."""
    cards = conn.execute(
        "SELECT oracle_id, name, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ).fetchall()

    patched = 0
    for oid, name, oracle_text in cards:
        oracle_lower = oracle_text.lower()
        for ctype, tribal_tag in TRIBAL_TYPES.items():
            # Check if oracle text mentions the type in a tribal-relevant context
            for pattern_template in TRIBAL_ORACLE_PATTERNS:
                pattern = pattern_template.replace("{type}", ctype)
                if re.search(pattern, oracle_lower):
                    if _add_provides(conn, oid, tribal_tag, dry_run):
                        patched += 1
                    break  # One match per type is enough

    return {"patched": patched, "description": "oracle text creature type reference → X-tribal provides"}


def _patch_stax(conn, dry_run):
    """Patch 3: Cards with tax/denial effects must provide stax-tax."""
    cards = conn.execute(
        "SELECT oracle_id, name, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ).fetchall()

    tax_patterns = [
        re.compile(r"can't .* unless .* pay", re.I),
        re.compile(r"costs? \{?\d+\}? more", re.I),
        re.compile(r"pay \{?\d+\}? (?:for each|to)", re.I),
        re.compile(r"an additional \{?\d", re.I),
    ]

    denial_patterns = [
        re.compile(r"don't untap", re.I),
        re.compile(r"can't untap", re.I),
        re.compile(r"skip .* untap", re.I),
        re.compile(r"players can't .* more than", re.I),
    ]

    patched = 0
    for oid, name, oracle_text in cards:
        for p in tax_patterns:
            if p.search(oracle_text):
                if _add_provides(conn, oid, "stax-tax", dry_run):
                    patched += 1
                break

        for p in denial_patterns:
            if p.search(oracle_text):
                if _add_provides(conn, oid, "resource-denial", dry_run):
                    patched += 1
                break

    return {"patched": patched, "description": "tax/denial oracle text → stax-tax/resource-denial provides"}


def _patch_wheel(conn, dry_run):
    """Patch 4: Cards that make everyone discard and draw must provide wheel."""
    cards = conn.execute(
        "SELECT oracle_id, name, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ).fetchall()

    wheel_patterns = [
        re.compile(r"each player .* discard.* hand.* draw", re.I),
        re.compile(r"each player shuffles .* hand .* library.* draws", re.I),
        re.compile(r"each player draws .* cards? .* discards", re.I),
    ]

    patched = 0
    for oid, name, oracle_text in cards:
        for p in wheel_patterns:
            if p.search(oracle_text):
                if _add_provides(conn, oid, "wheel", dry_run):
                    patched += 1
                break

    return {"patched": patched, "description": "wheel oracle text → wheel provides"}


def _patch_blink(conn, dry_run):
    """Patch 5: Cards that exile and return permanents must provide blink."""
    cards = conn.execute(
        "SELECT oracle_id, name, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ).fetchall()

    blink_patterns = [
        re.compile(r"exile .* then return .* to the battlefield", re.I),
        re.compile(r"exile .* return .* under .* control", re.I),
        re.compile(r"flicker", re.I),
    ]

    patched = 0
    for oid, name, oracle_text in cards:
        for p in blink_patterns:
            if p.search(oracle_text):
                if _add_provides(conn, oid, "blink", dry_run):
                    patched += 1
                break

    return {"patched": patched, "description": "blink oracle text → blink provides"}


def _patch_sacrifice(conn, dry_run):
    """Patch 6: Cards with sacrifice costs must provide sacrifice-outlet."""
    cards = conn.execute(
        "SELECT oracle_id, name, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ).fetchall()

    sac_patterns = [
        re.compile(r"sacrifice (?:a|an|another) (?:creature|artifact|permanent|token)", re.I),
        re.compile(r"sacrifice (?:a|an) .* creature:", re.I),
    ]

    patched = 0
    for oid, name, oracle_text in cards:
        for p in sac_patterns:
            if p.search(oracle_text):
                if _add_provides(conn, oid, "sacrifice-outlet", dry_run):
                    patched += 1
                break

    return {"patched": patched, "description": "sacrifice cost oracle text → sacrifice-outlet provides"}


def _patch_high_power(conn, dry_run):
    """Patch 7: Creatures with power >= 5 provide creature-power."""
    cards = conn.execute("""
        SELECT oracle_id, name, power FROM cards
        WHERE type_line LIKE '%Creature%' AND power IS NOT NULL
    """).fetchall()

    patched = 0
    for oid, name, power in cards:
        try:
            p = int(power)
        except (ValueError, TypeError):
            continue
        if p >= 5:
            if _add_provides(conn, oid, "creature-power", dry_run):
                patched += 1

    return {"patched": patched, "description": "creatures with power >= 5 → creature-power provides"}


def _patch_cost_reduction(conn, dry_run):
    """Patch 8: Cards with cost reduction provide cost-reduction."""
    cards = conn.execute(
        "SELECT oracle_id, name, oracle_text FROM cards WHERE oracle_text IS NOT NULL"
    ).fetchall()

    patterns = [
        re.compile(r'costs? \{?\d+\}? less', re.I),
        re.compile(r'cost \{?\d+\}? less', re.I),
        re.compile(r'reduce the cost', re.I),
        re.compile(r'without paying .* mana cost', re.I),
    ]

    patched = 0
    for oid, name, oracle_text in cards:
        for p in patterns:
            if p.search(oracle_text):
                if _add_provides(conn, oid, "cost-reduction", dry_run):
                    patched += 1
                break

    return {"patched": patched, "description": "cost reduction oracle text → cost-reduction provides"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Targeted tag patches")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    db = args.db or DB_PATH
    results = patch_all(db, dry_run=args.dry_run)

    action = "Would patch" if args.dry_run else "Patched"
    total = sum(r["patched"] for r in results.values())
    print(f"{action} {total} tags total:")
    for name, result in results.items():
        if result["patched"] > 0:
            print(f"  {name}: +{result['patched']} ({result['description']})")
