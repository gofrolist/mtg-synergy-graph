#!/usr/bin/env python3
"""Strategy detection: maps cards to strategy themes based on tags and EDHREC data.

Usage:
    python3 strategy_detector.py --commander "Kyler, Sigardian Emissary"
    python3 strategy_detector.py --populate      # populate card_strategies for all cards
    python3 strategy_detector.py --stats         # show strategy distribution
"""

import json
import sqlite3
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
EDHREC_PATH = os.path.join(DATA_DIR, "edhrec_theme_cards.json")

# Strategy mapping rules: (provides_tag_or_set, strategy_name, base_confidence)
# A card with any of these provides tags maps to the strategy.
STRATEGY_RULES = [
    # Token strategies
    ({"token-generation"}, "tokens", 1.0),
    ({"treasure-generation"}, "treasure", 1.0),
    ({"food-generation"}, "food", 0.8),
    ({"clue-generation"}, "clues", 0.8),

    # Counter strategies
    ({"counter-placement", "board-wide-counter-placement"}, "+1/+1-counters", 1.0),
    ({"counter-amplification"}, "+1/+1-counters", 0.9),
    ({"proliferate"}, "proliferate", 1.0),

    # Tribal/typal
    ({"human-tribal"}, "humans", 1.0),
    ({"goblin-tribal"}, "goblins", 1.0),
    ({"elf-tribal"}, "elves", 1.0),
    ({"sliver-tribal"}, "slivers", 1.0),
    ({"tribal-enabler"}, "tribal", 0.7),

    # Aristocrats / sacrifice
    ({"sacrifice-outlet"}, "aristocrats", 0.9),
    ({"death-trigger"}, "aristocrats", 0.8),

    # Spellslinger
    ({"spell-copy"}, "spellslinger", 1.0),
    ({"spell-cost-reduction"}, "spellslinger", 0.8),
    ({"storm-count"}, "storm", 1.0),

    # Graveyard
    ({"graveyard-recursion"}, "reanimator", 0.9),
    ({"self-mill"}, "self-mill", 0.9),
    ({"dredge"}, "dredge", 1.0),

    # Artifacts
    ({"artifact-enabler", "artifact-presence"}, "artifacts", 0.8),

    # Enchantments
    ({"enchantment-synergy", "aura-synergy"}, "enchantress", 0.8),

    # Combat
    ({"extra-combat"}, "extra-combats", 1.0),
    ({"evasion"}, "voltron", 0.6),
    ({"equipment-synergy"}, "equipment", 0.9),

    # Control
    ({"counterspell"}, "control", 0.7),
    ({"board-control"}, "control", 0.6),
    ({"tap-control"}, "stax", 0.7),

    # Card advantage
    ({"card-draw"}, "card-draw", 0.5),  # Low confidence — almost everything draws
    ({"tutor"}, "toolbox", 0.7),

    # Life
    ({"life-gain"}, "lifegain", 0.9),
    ({"life-drain"}, "lifedrain", 0.9),

    # Lands
    ({"landfall-trigger"}, "landfall", 1.0),
    ({"land-ramp"}, "lands-matter", 0.7),

    # Mill
    ({"mill"}, "mill", 1.0),

    # Blink
    ({"blink"}, "blink", 1.0),

    # Wheels
    ({"wheel"}, "wheels", 1.0),

    # Go wide
    ({"board-wide-creature-pump", "creature-pump"}, "go-wide", 0.7),
]


def detect_strategies(oracle_id, db_path=None):
    """Detect strategies for a single card based on its provides/wants tags.

    Returns list of {"name": str, "confidence": float, "signals": [str]}.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    provides = {row[0] for row in conn.execute(
        "SELECT tag FROM provides WHERE oracle_id = ?", (oracle_id,)
    ).fetchall()}

    conn.close()

    strategies = {}
    for tag_set, strategy, confidence in STRATEGY_RULES:
        matching = provides & tag_set
        if matching:
            if strategy not in strategies or strategies[strategy]["confidence"] < confidence:
                strategies[strategy] = {
                    "name": strategy,
                    "confidence": confidence,
                    "signals": [f"provides:{t}" for t in matching],
                }

    return sorted(strategies.values(), key=lambda s: -s["confidence"])


def _load_edhrec_strategies():
    """Load EDHREC theme data for strategy enrichment."""
    if not os.path.exists(EDHREC_PATH):
        return {}
    with open(EDHREC_PATH) as f:
        data = json.load(f)
    # Build name -> {theme: synergy_score}
    card_themes = {}
    for theme, cards in data.items():
        for card in cards:
            name = card["name"]
            synergy = card.get("synergy", 0)
            if synergy > 0.10:  # Only significant synergy
                if name not in card_themes:
                    card_themes[name] = {}
                card_themes[name][theme] = synergy
    return card_themes


def populate_card_strategies(db_path=None):
    """Populate card_strategies table for all cards in DB.

    Uses STRATEGY_RULES + EDHREC theme data.
    Returns number of strategy assignments.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Clear existing
    conn.execute("DELETE FROM card_strategies")

    # Get all cards with their provides tags
    cards = conn.execute("SELECT oracle_id, name FROM cards").fetchall()
    provides_by_card = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM provides").fetchall():
        provides_by_card.setdefault(oid, set()).add(tag)

    # Load EDHREC data
    edhrec = _load_edhrec_strategies()

    count = 0
    for oracle_id, name in cards:
        card_provides = provides_by_card.get(oracle_id, set())

        # Rule-based strategies
        strategies = {}
        for tag_set, strategy, confidence in STRATEGY_RULES:
            if card_provides & tag_set:
                if strategy not in strategies or strategies[strategy] < confidence:
                    strategies[strategy] = confidence

        # EDHREC-based strategies
        if name in edhrec:
            for theme, synergy in edhrec[name].items():
                if theme not in strategies or strategies[theme] < synergy:
                    strategies[theme] = synergy

        # Insert all strategies
        for strategy, confidence in strategies.items():
            conn.execute(
                "INSERT OR REPLACE INTO card_strategies (oracle_id, strategy, confidence) VALUES (?, ?, ?)",
                (oracle_id, strategy, confidence)
            )
            count += 1

    conn.commit()
    conn.close()
    return count


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Strategy detection for MTG cards")
    parser.add_argument("--commander", help="Detect strategies for a commander by name")
    parser.add_argument("--populate", action="store_true", help="Populate strategies for all cards")
    parser.add_argument("--stats", action="store_true", help="Show strategy distribution")
    parser.add_argument("--db", default=None, help="DB path")
    args = parser.parse_args()

    db = args.db or DB_PATH

    if args.commander:
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (args.commander,)).fetchone()
        conn.close()
        if not row:
            print(f"Commander not found: {args.commander}")
            exit(1)
        strategies = detect_strategies(row[0], db)
        print(f"\nStrategies for {args.commander}:")
        for s in strategies:
            active = "ACTIVE" if s["confidence"] >= 0.3 else "weak"
            print(f"  {s['name']}: {s['confidence']:.2f} [{active}] — {', '.join(s['signals'])}")

    elif args.populate:
        count = populate_card_strategies(db)
        print(f"Populated {count} strategy assignments")

    elif args.stats:
        conn = sqlite3.connect(db)
        top = conn.execute("""
            SELECT strategy, COUNT(*) as cnt, AVG(confidence) as avg_conf
            FROM card_strategies
            GROUP BY strategy ORDER BY cnt DESC LIMIT 30
        """).fetchall()
        conn.close()
        print("Top strategies by card count:")
        for strategy, cnt, avg_conf in top:
            print(f"  {strategy}: {cnt} cards (avg confidence: {avg_conf:.2f})")
