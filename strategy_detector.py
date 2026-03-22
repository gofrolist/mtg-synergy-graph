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
    ({"sacrifice-payoff"}, "aristocrats", 0.8),  # Payoff for sacrificing creatures

    # Spellslinger
    ({"spell-copy"}, "spellslinger", 1.0),
    ({"spell-cost-reduction"}, "spellslinger", 0.8),
    ({"storm-count"}, "storm", 1.0),

    # Graveyard
    ({"graveyard-recursion"}, "reanimator", 0.9),
    ({"self-mill"}, "self-mill", 0.9),
    ({"dredge"}, "dredge", 1.0),
    ({"graveyard-payoff"}, "reanimator", 0.8),  # Payoff for graveyard filling

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
    ({"card-draw-payoff"}, "card-draw", 0.7),  # Payoff specifically for drawing cards
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

    # Infect / poison
    ({"infect", "poison-counter-placement", "toxic-ability", "toxic-1", "toxic-deal"}, "infect", 0.9),

    # Payoff patterns — cards that benefit from a strategy's output
    # etb-payoff: cards like Impact Tremors that fire on every creature ETB (tokens spam ETBs)
    ({"etb-payoff"}, "tokens", 0.7),
    # damage-dealing + direct: also flags burn/spellslinger payoffs like Guttersnipe
    ({"damage-dealing"}, "burn", 0.6),
]


# Wants-based strategy rules: (wants_tag_set, strategy_name, base_confidence)
# Lower confidence than provides-based rules since wanting something doesn't mean you enable it.
WANTS_STRATEGY_RULES = [
    ({"counter-placement-events", "counter-distribution"}, "+1/+1-counters", 0.7),
    # NOTE: creature-etb, creature-death, creature-board are too generic for strategy
    # mapping. Any creature-heavy deck wants these (humans, counters, tribal, etc.).
    # Blink only detects from provides:blink. Tokens only from provides:token-generation
    # or token-specific wants (token-events, wide-board).
    ({"creature-death", "sacrifice-events"}, "aristocrats", 0.7),
    ({"spell-casting", "instant-sorcery-casting", "noncreature-spells", "cast-spell-events",
      "second-spell-casting", "instant-or-sorcery-spells"}, "spellslinger", 0.7),
    ({"token-events", "wide-board"}, "tokens", 0.6),
    # creature-board is too generic — any creature deck wants it. Only wide-board maps to go-wide.
    ({"wide-board"}, "go-wide", 0.6),
    ({"life-gain-events"}, "lifegain", 0.7),
    ({"graveyard-events", "graveyard-fill"}, "reanimator", 0.6),
    # graveyard-filling: cards wanting graveyard fill benefit from self-mill strategy
    ({"graveyard-filling"}, "self-mill", 0.6),
    ({"artifact-etb", "artifact-presence", "artifact-casting"}, "artifacts", 0.6),
    ({"enchantment-presence", "enchantment-casting-events"}, "enchantress", 0.6),
    ({"landfall", "land-play"}, "landfall", 0.7),
    ({"attack-events", "combat-damage-events"}, "voltron", 0.5),
    ({"commander-casting"}, "commander-matters", 0.7),
    # opponent-life-loss wants: lifedrain strategy payoffs
    ({"opponent-life-loss"}, "lifedrain", 0.6),
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
    """Detect strategies for a single card based on provides tags + oracle text.

    Also scans oracle text for creature type references to detect tribal strategies
    even when the LLM tagger didn't assign X-tribal provides tags.

    Returns list of {"name": str, "confidence": float, "signals": [str]}.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    provides = {row[0] for row in conn.execute(
        "SELECT tag FROM provides WHERE oracle_id = ?", (oracle_id,)
    ).fetchall()}

    wants = {row[0] for row in conn.execute(
        "SELECT tag FROM wants WHERE oracle_id = ?", (oracle_id,)
    ).fetchall()}

    oracle_text = conn.execute(
        "SELECT oracle_text FROM cards WHERE oracle_id = ?", (oracle_id,)
    ).fetchone()
    oracle_text = (oracle_text[0] or "").lower() if oracle_text else ""

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

    for tag_set, strategy, confidence in WANTS_STRATEGY_RULES:
        matching = wants & tag_set
        if matching:
            if strategy not in strategies or strategies[strategy]["confidence"] < confidence:
                strategies[strategy] = {
                    "name": strategy,
                    "confidence": confidence,
                    "signals": [f"wants:{t}" for t in matching],
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

    # Ability-based strategy derivation: use parsed abilities to infer strategies
    # that the provides/wants tags missed
    if db_path:
        ab_strategies = _strategies_from_abilities(oracle_id, db_path)
        for strat_name, conf in ab_strategies.items():
            if strat_name not in strategies or strategies[strat_name]["confidence"] < conf:
                strategies[strat_name] = {
                    "name": strat_name,
                    "confidence": conf,
                    "signals": [f"ability-derived"],
                }

    return sorted(strategies.values(), key=lambda s: -s["confidence"])


# Maps ability effect_tags/trigger_tags to strategies.
# Only includes tags that are SPECIFIC to a strategy (not generic creature tags).
ABILITY_STRATEGY_MAP = {
    # Effect tags → strategies
    "token-generation": ("tokens", 0.9),
    "counter-placement": ("+1/+1-counters", 0.8),
    "life-gain": ("lifegain", 0.7),
    "life-drain": ("lifedrain", 0.7),
    "mill": ("mill", 0.8),
    "graveyard-recursion": ("reanimator", 0.7),
    "copy-effect": ("spellslinger", 0.6),
    "untap": ("combo", 0.5),
    "treasure-generation": ("treasure", 0.8),
    "proliferate": ("proliferate", 0.8),
    # Trigger tags → strategies (these are triggers, so the card CARES about the event)
    "counter-placement-events": ("+1/+1-counters", 0.7),
    "life-gain-events": ("lifegain", 0.6),
    "sacrifice-events": ("aristocrats", 0.6),
    "draw-events": ("card-draw", 0.5),
    "landfall": ("landfall", 0.7),
}


def _strategies_from_abilities(oracle_id, db_path):
    """Derive strategies from a card's parsed abilities.

    Returns dict of {strategy_name: confidence}.
    """
    conn = sqlite3.connect(db_path)
    abilities = conn.execute(
        "SELECT trigger_tags, effect_tags FROM abilities WHERE oracle_id = ?",
        (oracle_id,)
    ).fetchall()
    conn.close()

    strategies = {}
    for trigger_tags_json, effect_tags_json in abilities:
        for tags_json in (trigger_tags_json, effect_tags_json):
            if not tags_json:
                continue
            tags = json.loads(tags_json)
            for tag in tags:
                if tag in ABILITY_STRATEGY_MAP:
                    strat_name, conf = ABILITY_STRATEGY_MAP[tag]
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

    # Get all cards with their provides tags, wants tags, and oracle text
    cards = conn.execute("SELECT oracle_id, name, oracle_text FROM cards").fetchall()
    provides_by_card = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM provides").fetchall():
        provides_by_card.setdefault(oid, set()).add(tag)

    wants_by_card = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM wants").fetchall():
        wants_by_card.setdefault(oid, set()).add(tag)

    # Load parsed abilities (trigger_tags, effect_tags per card)
    abilities_by_card = {}
    for oid, ttags, etags in conn.execute(
        "SELECT oracle_id, trigger_tags, effect_tags FROM abilities"
    ).fetchall():
        abilities_by_card.setdefault(oid, []).append((ttags, etags))

    # Load EDHREC data
    edhrec = _load_edhrec_strategies()

    # Tribal context patterns for oracle text scanning
    _TRIBAL_PATTERNS = [
        "you control get", "you control have", "enter", "die", "whenever",
        "each other", "all ", "other ", "among ", "number of",
    ]

    count = 0
    for oracle_id, name, oracle_text in cards:
        card_provides = provides_by_card.get(oracle_id, set())

        # Rule-based strategies (provides-based)
        strategies = {}
        for tag_set, strategy, confidence in STRATEGY_RULES:
            if card_provides & tag_set:
                if strategy not in strategies or strategies[strategy] < confidence:
                    strategies[strategy] = confidence

        # Wants-based strategies
        card_wants = wants_by_card.get(oracle_id, set())
        for tag_set, strategy, confidence in WANTS_STRATEGY_RULES:
            if card_wants & tag_set:
                if strategy not in strategies or strategies[strategy] < confidence:
                    strategies[strategy] = confidence

        # Ability-based strategy derivation
        card_abilities = abilities_by_card.get(oracle_id, [])
        for trigger_tags, effect_tags in card_abilities:
            for tags_json in (trigger_tags, effect_tags):
                if not tags_json:
                    continue
                tags = json.loads(tags_json)
                for tag in tags:
                    if tag in ABILITY_STRATEGY_MAP:
                        strat_name, conf = ABILITY_STRATEGY_MAP[tag]
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
