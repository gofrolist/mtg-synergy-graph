#!/usr/bin/env python3
"""Strategy detection — VESTIGIAL.

This script populates the `card_strategies` table from EDHREC theme data and
tribal oracle text matching. It is **no longer used by the inference pipeline**:
the forge model (commit cd278d3) eliminated the `strategy_cosine` feature in
favor of `mech_cosine` (auto-derived from Forge mechanics vectors), and the
strategy table is now read only by the `compare_strategy_vs_mech.py` analysis
script for offline comparisons.

In production deployments the `data/edhrec_theme_cards.json` file is gitignored
and absent, so `_load_edhrec_strategies()` returns `{}` and only the tribal
oracle-text path produces output. The oracle-text path is itself deprecated
(see project rule `feedback_oracle_text_bad`: oracle text features cause wrong
recommendations).

Kept for back-compat with `compare_strategy_vs_mech.py` and historical
reference. Safe to delete in a future cleanup pass that also removes the
`card_strategies` schema, the comparison script, and the related tests.

Usage (analysis only — no longer part of the inference pipeline):
    python3 strategy_detector.py --commander "Kyler, Sigardian Emissary"
    python3 strategy_detector.py --populate      # populate card_strategies for all cards
    python3 strategy_detector.py --stats         # show strategy distribution
"""

import json
import os
import re
import sqlite3

from mtg_synergy.config import DATA_DIR, DB_PATH

EDHREC_PATH = DATA_DIR / "edhrec_theme_cards.json"

# Common creature types for oracle text tribal detection
CREATURE_TYPE_STRATEGIES = {
    "human": "humans", "goblin": "goblins", "elf": "elves", "zombie": "zombies",
    "vampire": "vampires", "dragon": "dragons", "angel": "angels", "demon": "demons",
    "sliver": "slivers", "dinosaur": "dinosaurs", "pirate": "pirates", "wizard": "wizards",
    "knight": "knights", "merfolk": "merfolk", "elemental": "elementals", "spirit": "spirits",
    "soldier": "soldiers", "cat": "cats", "bird": "birds", "warrior": "warriors",
    "rogue": "rogues", "cleric": "clerics", "rat": "rats", "faerie": "faeries",
}

# Tribal context patterns — card must reference the type in a synergy context
_TRIBAL_PATTERNS = [
    "you control get", "you control have", "enter", "die", "whenever",
    "each other", "all ", "other ", "among ", "number of",
]


def _detect_tribal(oracle_text: str) -> dict[str, float]:
    """Detect tribal strategies from oracle text."""
    strategies = {}
    if not oracle_text:
        return strategies
    oracle_lower = oracle_text.lower()
    for ctype, strat_name in CREATURE_TYPE_STRATEGIES.items():
        if re.search(r'\b' + re.escape(ctype) + r's?\b', oracle_lower):
            if any(p in oracle_lower for p in _TRIBAL_PATTERNS):
                strategies[strat_name] = 0.8
    return strategies


def _load_edhrec_strategies() -> dict[str, dict[str, float]]:
    """Load EDHREC theme data. Returns {card_name: {theme: synergy_score}}."""
    if not os.path.exists(EDHREC_PATH):
        return {}
    with open(EDHREC_PATH) as f:
        data = json.load(f)
    card_themes: dict[str, dict[str, float]] = {}
    for theme, cards in data.items():
        for card in cards:
            name = card["name"]
            synergy = card.get("synergy", 0)
            if synergy > 0.10:
                if name not in card_themes:
                    card_themes[name] = {}
                card_themes[name][theme] = synergy
    return card_themes


def detect_strategies(oracle_id, db_path=None):
    """Detect strategies for a single card from EDHREC themes + tribal oracle text.

    Returns list of {"name": str, "confidence": float, "signals": [str]}.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Card name + oracle text
    row = conn.execute(
        "SELECT name, oracle_text FROM cards WHERE oracle_id = ?", (oracle_id,)
    ).fetchone()
    conn.close()

    if not row:
        return []

    name, oracle_text = row[0], (row[1] or "")
    strategies = {}

    # EDHREC themes
    edhrec = _load_edhrec_strategies()
    if name in edhrec:
        for theme, synergy in edhrec[name].items():
            strategies[theme] = {"name": theme, "confidence": synergy, "signals": ["edhrec"]}

    # Tribal from oracle text
    for strat_name, conf in _detect_tribal(oracle_text).items():
        if strat_name not in strategies or strategies[strat_name]["confidence"] < conf:
            strategies[strat_name] = {
                "name": strat_name, "confidence": conf, "signals": ["oracle:tribal"],
            }

    return sorted(strategies.values(), key=lambda s: -s["confidence"])


def populate_card_strategies(db_path=None):
    """Populate card_strategies table from EDHREC themes + tribal oracle text.

    Returns number of strategy assignments.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    conn.execute("DELETE FROM card_strategies")

    cards = conn.execute("SELECT oracle_id, name, oracle_text FROM cards").fetchall()
    edhrec = _load_edhrec_strategies()

    count = 0
    for oracle_id, name, oracle_text in cards:
        strategies = {}

        # EDHREC themes (primary source)
        if name in edhrec:
            for theme, synergy in edhrec[name].items():
                strategies[theme] = synergy

        # Tribal from oracle text
        for strat_name, conf in _detect_tribal(oracle_text or "").items():
            if strat_name not in strategies or strategies[strat_name] < conf:
                strategies[strat_name] = conf

        for strategy, confidence in strategies.items():
            conn.execute(
                "INSERT OR REPLACE INTO card_strategies (oracle_id, strategy, confidence) VALUES (?, ?, ?)",
                (oracle_id, strategy, confidence),
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
