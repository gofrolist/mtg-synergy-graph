"""
Fix LLM tag quality issues in the 10k tags file.

Applies three automated fixes:
  1. Normalize roles to 8 canonical values
  2. Patch missing provides/wants based on scryfall tag correlations
  3. Collapse rare singleton LLM tags to canonical vocabulary

Usage:
    python3 fix_tags.py --check          # show what would change
    python3 fix_tags.py --apply          # apply fixes to top10000_tags.json
    python3 fix_tags.py --apply --reimport  # apply + rebuild SQLite DB
"""

import argparse
import json
import os
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TAGS_FILE = os.path.join(DATA_DIR, "top10000_tags.json")

# ── Fix 1: Role normalization ──

ROLE_MAP = {
    # Draw variants
    "card-draw": "draw",
    "card-draw-payoff": "draw",
    "card-draw-engine": "draw",
    "draw engine": "draw",
    "draw-enabler": "draw",
    "draw-control": "draw",
    "draw-mill": "draw",
    "card advantage": "draw",
    "draw | removal": "draw",
    # Token/creature generation → enabler
    "token-generation": "enabler",
    "token-generator": "enabler",
    "tribal-enabler": "enabler",
    "combo": "enabler",
    # Combat → threat
    "combat": "threat",
    "combat-enabler": "threat",
    "combat-trigger": "threat",
    "board-pressure": "threat",
    "damage-dealing": "threat",
    "damage-amplifier": "threat",
    "pump": "threat",
    "finisher": "threat",
    "win-condition": "threat",
    "win-con": "threat",
    "prowess": "threat",
    "creature": "threat",
    # Mana → ramp
    "mana-acceleration": "ramp",
    "mana-sink": "ramp",
    "mana": "ramp",
    "mana-generator": "ramp",
    # Removal variants
    "board-wipe": "removal",
    "artifact-enchantment-removal": "removal",
    "counter": "removal",
    # Protection variants
    "board-protection": "protection",
    "resilience": "protection",
    # Utility catch-all
    "disruption": "utility",
    "stax": "utility",
    "control": "utility",
    "tutor": "utility",
    "equipment": "utility",
    "life-gain": "utility",
    "life-drain": "utility",
    "sacrifice-payoff": "utility",
    "card-discard": "utility",
    "graveyard-recursion": "utility",
    "graveyard-hate": "utility",
    "resurrection": "utility",
    "recursion": "utility",
    "mill": "utility",
    "planeswalker": "utility",
    "transformation": "utility",
    "library-manipulation": "utility",
    "spell-casting": "utility",
    "untap": "utility",
    "card-retrieval": "utility",
    "partner": "utility",
    "crew": "utility",
    "dungeon": "utility",
    "saga": "utility",
}

# ── Fix 2: Scryfall-based provides/wants patches ──
# When a card has a scryfall tag, ensure it has the corresponding LLM tag.
# Only high-confidence mappings (>70% correlation from analysis).

SCRYFALL_TO_PROVIDES = {
    "repeatable creature tokens": "token-generation",
    "power boost to all": "creature-pump",
    "toughness boost to all": "creature-pump",
    "spot removal": "targeted-removal",
    "removal-destroy": "targeted-removal",
    "removal-exile": "targeted-removal",
    "removal-creature": "targeted-removal",
    "draw engine": "card-draw",
    "repeatable pure draw": "card-draw",
    "pure draw": "card-draw",
    "hand-positive": "card-draw",
    "utility land": "mana-acceleration",
    "ramp": "land-search",
    "opponent loses life": "life-drain",
    "group slug": "life-drain",
    "repeatable lifegain": "life-gain",
    "gains pp counters": "counter-placement",
    "gives pp counters": "counter-placement",
    "gives pp counters to all": "board-wide-counter-placement",
    "pp counters matter": "counter-amplification",
    "sacrifice outlet-creature": "sacrifice-payoff",
    "sacrifice outlet-artifact": "sacrifice-payoff",
    "sacrifice outlet-permanent": "sacrifice-payoff",
}

SCRYFALL_TO_WANTS = {
    "attack trigger": "attack-events",
    "attacking matters": "attack-events",
    "attacking matters-self": "attack-events",
    "cast trigger-you": "spell-casting",
    "sacrifice outlet-creature": "sacrifice-events",
}

# ── Fix 3: Collapse rare provides tags ──
# Map freeform LLM tags to canonical vocabulary.

PROVIDES_COLLAPSE = {
    "creature-removal": "targeted-removal",
    "enchantment-removal": "targeted-removal",
    "artifact-removal": "targeted-removal",
    "planeswalker-removal": "targeted-removal",
    "permanent-removal": "targeted-removal",
    "board-wide-creature-destruction": "board-wipe",
    "board-wide-removal": "board-wipe",
    "mass-removal": "board-wipe",
    "colorless-ramp": "mana-acceleration",
    "board-wide-counter-placer": "board-wide-counter-placement",
    "passive-counter-boost": "counter-amplification",
    "counter-distribution": "counter-placement",
    "token-doubling": "token-generation",
    "creature-token-generation": "token-generation",
    "haste-granter": "haste-grant",
    "hexproof-granter": "hexproof-grant",
    "indestructible-granter": "indestructible-grant",
    "lifelink-grant": "life-gain",
    "library-search": "card-draw",
    "card-selection": "card-draw",
    "ability-doubling": "trigger-doubling",
}

WANTS_COLLAPSE = {
    "creature-entry-trigger": "creature-etb",
    "creature-etb-triggers": "creature-etb",
    "creature-entering": "creature-etb",
    "creature-death-events": "creature-death",
    "death-trigger": "creature-death",
    "mana-intensive-decks": "mana-needs",
    "mana-needs": "mana-needs",
    "counter-distribution": "counter-placement-events",
}


def load_scryfall_tags_for_card(conn, oracle_id: str) -> set[str]:
    """Get scryfall tag names for a card."""
    rows = conn.execute(
        "SELECT tag FROM scryfall_tags WHERE oracle_id = ?", (oracle_id,)
    ).fetchall()
    return {r[0] for r in rows}


def fix_tags(check_only: bool = True) -> dict:
    """Apply all fixes. Returns stats dict."""
    with open(TAGS_FILE) as f:
        cards = json.load(f)

    from tag_db import get_connection, DB_PATH
    conn = get_connection(DB_PATH)

    stats = {
        "roles_fixed": 0,
        "provides_added": 0,
        "wants_added": 0,
        "provides_collapsed": 0,
        "wants_collapsed": 0,
        "total_cards": len(cards),
    }

    for card in cards:
        oid = card.get("oracle_id", "")

        # Fix 1: Role normalization
        role = card.get("role", "")
        if role in ROLE_MAP:
            card["role"] = ROLE_MAP[role]
            stats["roles_fixed"] += 1

        # Fix 2: Scryfall-based patches
        scryfall = load_scryfall_tags_for_card(conn, oid)
        provides = set(card.get("provides", []))
        wants = set(card.get("wants", []))

        for stag in scryfall:
            if stag in SCRYFALL_TO_PROVIDES:
                expected = SCRYFALL_TO_PROVIDES[stag]
                if expected not in provides:
                    provides.add(expected)
                    stats["provides_added"] += 1

            if stag in SCRYFALL_TO_WANTS:
                expected = SCRYFALL_TO_WANTS[stag]
                if expected not in wants:
                    wants.add(expected)
                    stats["wants_added"] += 1

        # Fix 3: Collapse rare tags
        new_provides = set()
        for p in provides:
            if p in PROVIDES_COLLAPSE:
                new_provides.add(PROVIDES_COLLAPSE[p])
                stats["provides_collapsed"] += 1
            else:
                new_provides.add(p)

        new_wants = set()
        for w in wants:
            if w in WANTS_COLLAPSE:
                new_wants.add(WANTS_COLLAPSE[w])
                stats["wants_collapsed"] += 1
            else:
                new_wants.add(w)

        card["provides"] = sorted(new_provides)
        card["wants"] = sorted(new_wants)

    conn.close()

    if not check_only:
        with open(TAGS_FILE, "w") as f:
            json.dump(cards, f, indent=2)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Fix LLM tag quality issues")
    parser.add_argument("--check", action="store_true", help="Show what would change")
    parser.add_argument("--apply", action="store_true", help="Apply fixes")
    parser.add_argument("--reimport", action="store_true",
                        help="Rebuild SQLite DB after applying fixes")
    args = parser.parse_args()

    if args.check:
        stats = fix_tags(check_only=True)
        print("DRY RUN — changes that would be applied:")
        print(f"  Roles normalized:     {stats['roles_fixed']}/{stats['total_cards']}")
        print(f"  Provides added:       {stats['provides_added']} (from scryfall correlations)")
        print(f"  Wants added:          {stats['wants_added']} (from scryfall correlations)")
        print(f"  Provides collapsed:   {stats['provides_collapsed']} (rare → canonical)")
        print(f"  Wants collapsed:      {stats['wants_collapsed']} (rare → canonical)")
        total_changes = sum(v for k, v in stats.items() if k != "total_cards")
        print(f"\n  Total changes: {total_changes}")

    elif args.apply:
        stats = fix_tags(check_only=False)
        print("Applied fixes:")
        print(f"  Roles normalized:     {stats['roles_fixed']}")
        print(f"  Provides added:       {stats['provides_added']}")
        print(f"  Wants added:          {stats['wants_added']}")
        print(f"  Provides collapsed:   {stats['provides_collapsed']}")
        print(f"  Wants collapsed:      {stats['wants_collapsed']}")

        if args.reimport:
            print("\nRebuilding SQLite DB...")
            from tag_db import import_file
            os.remove(os.path.join(DATA_DIR, "tags.db"))
            import_file(TAGS_FILE)
            # Re-import scryfall tags
            from tag_db import import_scryfall_file
            scryfall_file = os.path.join(DATA_DIR, "scryfall_function_tags.json")
            if os.path.exists(scryfall_file):
                import_scryfall_file(scryfall_file)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
