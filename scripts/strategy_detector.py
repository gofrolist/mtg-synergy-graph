#!/usr/bin/env python3
"""Strategy detection: maps cards to strategy themes based on tags and EDHREC data.

Usage:
    python3 strategy_detector.py --commander "Kyler, Sigardian Emissary"
    python3 strategy_detector.py --populate      # populate card_strategies for all cards
    python3 strategy_detector.py --stats         # show strategy distribution
"""

import json
import re
import sqlite3
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
EDHREC_PATH = os.path.join(DATA_DIR, "edhrec_theme_cards.json")

# Strategy mapping rules: (forge_verb_or_keyword_set, strategy_name, base_confidence)
# A card with any of these Forge verbs/keywords/triggers maps to the strategy.
# Forge verbs are PascalCase: Token, PutCounter, Mill, Draw, etc.
# Forge trigger_modes are PascalCase: ChangesZone, Attacks, SpellCast, etc.
# Forge keywords are mixed case: Flying, Lifelink, Equip, etc.
STRATEGY_RULES = [
    # Token strategies
    ({"Token"}, "tokens", 1.0),

    # Counter strategies
    ({"PutCounter", "PutCounterAll"}, "+1/+1-counters", 1.0),
    ({"Proliferate"}, "proliferate", 1.0),

    # Aristocrats / sacrifice
    ({"Sacrifice", "SacrificeAll"}, "aristocrats", 0.9),
    ({"Sacrificed"}, "aristocrats", 0.8),             # trigger_mode

    # Spellslinger
    ({"CopySpellAbility"}, "spellslinger", 1.0),
    ({"ReduceCost"}, "spellslinger", 0.8),
    ({"Storm"}, "storm", 1.0),

    # Graveyard / Reanimator
    ({"ChangeZone"}, "reanimator", 0.6),              # broad — covers reanimate + bounce
    ({"Mill"}, "self-mill", 0.9),
    ({"Dredge"}, "dredge", 1.0),

    # Artifacts
    ({"Equip"}, "artifacts", 0.7),

    # Enchantments
    ({"Enchant"}, "enchantress", 0.7),

    # Combat / Voltron
    ({"Flying", "Menace"}, "voltron", 0.5),
    ({"Trample"}, "voltron", 0.4),

    # Equipment
    ({"Equip"}, "equipment", 0.9),

    # Control / Stax
    ({"Counter"}, "control", 0.7),
    ({"Tap"}, "stax", 0.7),

    # Card advantage
    ({"Draw"}, "card-draw", 0.5),
    ({"Dig"}, "card-draw", 0.5),

    # Life
    ({"GainLife"}, "lifegain", 0.9),
    ({"Lifelink"}, "lifegain", 0.7),
    ({"LoseLife"}, "lifedrain", 0.9),

    # Lands
    ({"Mana"}, "lands-matter", 0.4),                  # very broad

    # Mill
    ({"Mill"}, "mill", 1.0),

    # Go wide
    ({"PumpAll"}, "go-wide", 0.7),

    # Infect / poison
    ({"Infect", "Toxic"}, "infect", 0.9),

    # Burn
    ({"DealDamage", "DamageAll"}, "burn", 0.6),

    # Lifedrain: life-gain also enables drain combos
    ({"GainLife"}, "lifedrain", 0.5),

    # Treasure: token generation of artifact tokens → artifacts
    ({"Token"}, "artifacts", 0.3),                     # weak — only some tokens are artifacts
]


# Wants-based strategy rules: (forge_trigger_mode_set, strategy_name, base_confidence)
# These match on trigger_mode (what event the card triggers on).
WANTS_STRATEGY_RULES = [
    ({"Attacks"}, "voltron", 0.5),
    ({"Attacks", "AttackersDeclared"}, "equipment", 0.5),
    ({"DamageDone", "DamageDoneOnce"}, "voltron", 0.5),
    ({"SpellCast"}, "spellslinger", 0.7),
    ({"Sacrificed"}, "aristocrats", 0.7),
    ({"LifeGained"}, "lifegain", 0.7),
    ({"Discarded"}, "wheels", 0.6),
    ({"Drawn"}, "wheels", 0.6),
    ({"Drawn"}, "card-draw", 0.6),
]



# Common creature types for oracle text scanning
CREATURE_TYPE_STRATEGIES = {
    "human": "humans", "goblin": "goblins", "elf": "elves", "zombie": "zombies",
    "vampire": "vampires", "dragon": "dragons", "angel": "angels", "demon": "demons",
    "sliver": "slivers", "dinosaur": "dinosaurs", "pirate": "pirates", "wizard": "wizards",
    "knight": "knights", "merfolk": "merfolk", "elemental": "elementals", "spirit": "spirits",
    "soldier": "soldiers", "cat": "cats", "bird": "birds", "warrior": "warriors",
    "rogue": "rogues", "cleric": "clerics", "rat": "rats", "faerie": "faeries",
}


def detect_strategies(oracle_id, db_path=None):
    """Detect strategies for a single card based on Forge abilities + oracle text.

    Also scans oracle text for creature type references to detect tribal strategies
    even when forge data doesn't cover the card.

    Returns list of {"name": str, "confidence": float, "signals": [str]}.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    # Get Forge verbs, trigger_modes, and keywords (what the card does / triggers on)
    forge_verbs = set()
    forge_triggers = set()
    for row in conn.execute(
        "SELECT fa.verb, fa.trigger_mode, fa.keyword FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fnm.oracle_id = ?", (oracle_id,)):
        if row[0]: forge_verbs.add(row[0])
        if row[1]: forge_triggers.add(row[1])
        if row[2]: forge_verbs.add(row[2])

    oracle_text = conn.execute(
        "SELECT oracle_text FROM cards WHERE oracle_id = ?", (oracle_id,)
    ).fetchone()
    oracle_text = (oracle_text[0] or "").lower() if oracle_text else ""

    conn.close()

    strategies = {}
    # Match verbs + keywords against STRATEGY_RULES
    forge_all = forge_verbs | forge_triggers
    for tag_set, strategy, confidence in STRATEGY_RULES:
        matching = forge_all & tag_set
        if matching:
            if strategy not in strategies or strategies[strategy]["confidence"] < confidence:
                strategies[strategy] = {
                    "name": strategy,
                    "confidence": confidence,
                    "signals": [f"forge:{t}" for t in matching],
                }

    # Match trigger_modes against WANTS_STRATEGY_RULES
    for tag_set, strategy, confidence in WANTS_STRATEGY_RULES:
        matching = forge_triggers & tag_set
        if matching:
            if strategy not in strategies or strategies[strategy]["confidence"] < confidence:
                strategies[strategy] = {
                    "name": strategy,
                    "confidence": confidence,
                    "signals": [f"trigger:{t}" for t in matching],
                }

    # Oracle text tribal detection: if oracle text references a creature type
    # with tribal-relevant verbs, infer the tribal strategy
    if oracle_text:
        _TRIBAL_PATTERNS = [
            "you control get", "you control have", "enter", "die", "whenever",
            "each other", "all ", "other ", "among ", "number of",
        ]
        for ctype, strat_name in CREATURE_TYPE_STRATEGIES.items():
            # Word boundary match to avoid "rat" matching "proliferate"
            if re.search(r'\b' + re.escape(ctype) + r's?\b', oracle_text):
                # Check if it's in a tribal-relevant context (not just mentioning the type)
                if any(p in oracle_text for p in _TRIBAL_PATTERNS):
                    if strat_name not in strategies or strategies[strat_name]["confidence"] < 0.8:
                        strategies[strat_name] = {
                            "name": strat_name,
                            "confidence": 0.8,
                            "signals": [f"oracle:{ctype}"],
                        }

    # Ability-based strategy derivation from Forge verbs
    ab_strategies = _strategies_from_forge_verbs(forge_all)
    for strat_name, conf in ab_strategies.items():
        if strat_name not in strategies or strategies[strat_name]["confidence"] < conf:
            strategies[strat_name] = {
                "name": strat_name,
                "confidence": conf,
                "signals": ["forge-derived"],
            }

    return sorted(strategies.values(), key=lambda s: -s["confidence"])


# Maps Forge verbs/trigger_modes to strategies.
# Only includes verbs that are SPECIFIC to a strategy (not generic).
ABILITY_STRATEGY_MAP = {
    # Forge verbs → strategies
    "Token": ("tokens", 0.9),
    "PutCounter": ("+1/+1-counters", 0.8),
    "PutCounterAll": ("+1/+1-counters", 0.8),
    "GainLife": ("lifegain", 0.7),
    "LoseLife": ("lifedrain", 0.7),
    "Mill": ("mill", 0.8),
    "CopySpellAbility": ("spellslinger", 0.6),
    "Untap": ("combo", 0.5),
    "Proliferate": ("proliferate", 0.8),
    "Equip": ("equipment", 0.7),
    "Enchant": ("enchantress", 0.5),
    "DealDamage": ("burn", 0.5),
    "DamageAll": ("burn", 0.6),
    "Draw": ("card-draw", 0.5),
    "Sacrifice": ("aristocrats", 0.6),
    # Forge trigger_modes → strategies
    "Sacrificed": ("aristocrats", 0.6),
    "LifeGained": ("lifegain", 0.6),
    "Drawn": ("card-draw", 0.5),
    "SpellCast": ("spellslinger", 0.5),
    "ChangesZone": ("blink", 0.3),
}


def _strategies_from_forge_verbs(forge_all):
    """Derive strategies from a card's Forge verbs/triggers/keywords.

    Args:
        forge_all: set of Forge verbs + trigger_modes + keywords

    Returns dict of {strategy_name: confidence}.
    """
    strategies = {}
    for verb in forge_all:
        if verb in ABILITY_STRATEGY_MAP:
            strat_name, conf = ABILITY_STRATEGY_MAP[verb]
            if strat_name not in strategies or strategies[strat_name] < conf:
                strategies[strat_name] = conf

    return strategies


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

    # Get all cards with oracle text
    cards = conn.execute("SELECT oracle_id, name, oracle_text FROM cards").fetchall()

    # Load Forge verbs, trigger_modes, and keywords for all cards (bulk)
    forge_verbs_by_card = {}    # oid -> set of verbs + keywords
    forge_triggers_by_card = {} # oid -> set of trigger_modes
    for row in conn.execute(
        "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.keyword "
        "FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"):
        oid = row[0]
        v = forge_verbs_by_card.setdefault(oid, set())
        t = forge_triggers_by_card.setdefault(oid, set())
        if row[1]: v.add(row[1])
        if row[2]: t.add(row[2])
        if row[3]: v.add(row[3])

    # Load EDHREC data
    edhrec = _load_edhrec_strategies()

    # Tribal context patterns for oracle text scanning
    _TRIBAL_PATTERNS = [
        "you control get", "you control have", "enter", "die", "whenever",
        "each other", "all ", "other ", "among ", "number of",
    ]

    count = 0
    for oracle_id, name, oracle_text in cards:
        card_verbs = forge_verbs_by_card.get(oracle_id, set())
        card_triggers = forge_triggers_by_card.get(oracle_id, set())
        card_all = card_verbs | card_triggers

        # Rule-based strategies (Forge verb/keyword matching)
        strategies = {}
        for tag_set, strategy, confidence in STRATEGY_RULES:
            if card_all & tag_set:
                if strategy not in strategies or strategies[strategy] < confidence:
                    strategies[strategy] = confidence

        # Trigger-mode-based strategies
        for tag_set, strategy, confidence in WANTS_STRATEGY_RULES:
            if card_triggers & tag_set:
                if strategy not in strategies or strategies[strategy] < confidence:
                    strategies[strategy] = confidence

        # Ability-based strategy derivation (from Forge verbs)
        for verb in card_all:
            if verb in ABILITY_STRATEGY_MAP:
                strat_name, conf = ABILITY_STRATEGY_MAP[verb]
                if strat_name not in strategies or strategies[strat_name] < conf:
                    strategies[strat_name] = conf

        # Oracle text tribal detection
        oracle_lower = (oracle_text or "").lower()
        if oracle_lower:
            for ctype, strat_name in CREATURE_TYPE_STRATEGIES.items():
                if re.search(r'\b' + re.escape(ctype) + r's?\b', oracle_lower) and any(p in oracle_lower for p in _TRIBAL_PATTERNS):
                    if strat_name not in strategies or strategies[strat_name] < 0.8:
                        strategies[strat_name] = 0.8

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
