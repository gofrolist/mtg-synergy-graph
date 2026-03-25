"""Commander archetype inference from oracle text + parsed abilities + type line.

Detects strategies, tribal type, and event profiles for any of the 3,141 legal
commanders without needing EDHREC data. Profiles are precomputed and stored in
the commander_profiles table for O(1) lookup at recommendation time.
"""
import json
import re
from dataclasses import dataclass, field
from mtg_synergy.recommend.scoring import STRATEGY_KEYWORDS


@dataclass
class CommanderProfile:
    strategies: set = field(default_factory=set)
    tribal_type: str | None = None
    key_events_produced: set = field(default_factory=set)
    key_events_consumed: set = field(default_factory=set)
    key_effects: set = field(default_factory=set)


# Event -> strategy mapping
_EVENT_TO_STRATEGY = {
    "dies": "aristocrats",
    "enters_graveyard": "reanimator",
    "life_gained": "lifegain",
    "creature_enters": "tokens",
    "attacks": "voltron",
}

# Effect -> strategy mapping
_EFFECT_TO_STRATEGY = {
    "create": "tokens",
    "put_counter": "+1/+1-counters",
    "deal_damage": "burn",
    "mill": "mill",
    "gain_life": "lifegain",
}

# Subtypes that indicate tribal deck potential
_TRIBAL_SUBTYPES = {
    "goblin", "elf", "human", "zombie", "vampire", "dragon", "angel",
    "merfolk", "wizard", "warrior", "knight", "dinosaur", "elemental",
    "demon", "cat", "spirit", "beast", "pirate", "faerie", "rat",
    "sliver", "ally", "cleric", "rogue", "shaman", "soldier", "bird",
    "insect", "fungus", "skeleton", "sphinx", "giant", "werewolf",
}


def infer_profile(
    oracle_text: str,
    type_line: str,
    parsed_events_produced: set[str] | None = None,
    parsed_events_consumed: set[str] | None = None,
    parsed_effects: set[str] | None = None,
) -> CommanderProfile:
    """Infer commander archetype from text and parsed data."""
    strategies = set()
    events_produced = parsed_events_produced or set()
    events_consumed = parsed_events_consumed or set()
    effects = parsed_effects or set()

    # 1. Strategy keywords from oracle text
    oracle_lower = oracle_text.lower()
    for strat, keywords in STRATEGY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in oracle_lower)
        if hits >= 2:
            strategies.add(strat)

    # 2. Event-based strategy detection
    for event, strat in _EVENT_TO_STRATEGY.items():
        if event in events_consumed:
            strategies.add(strat)

    # 3. Effect-based strategy detection
    for eff, strat in _EFFECT_TO_STRATEGY.items():
        if eff in effects:
            strategies.add(strat)

    # 4. Tribal detection from type line
    tribal_type = None
    if type_line and "\u2014" in type_line:
        try:
            subtypes = type_line.split("\u2014")[1].strip().split()
            for st in subtypes:
                if st.lower() in _TRIBAL_SUBTYPES:
                    tribal_type = st
                    break
        except (IndexError, AttributeError):
            pass

    # Also check oracle text for tribal references
    if tribal_type is None:
        for st in _TRIBAL_SUBTYPES:
            pattern = rf"\b{st}\b.*\b(you control|enters|dies|gets)\b"
            if re.search(pattern, oracle_lower):
                tribal_type = st.title()
                strategies.add(f"tribal-{st}")
                break

    if tribal_type:
        strategies.add(f"tribal-{tribal_type.lower()}")

    return CommanderProfile(
        strategies=strategies,
        tribal_type=tribal_type,
        key_events_produced=events_produced,
        key_events_consumed=events_consumed,
        key_effects=effects,
    )


def ensure_profile_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commander_profiles (
            oracle_id TEXT PRIMARY KEY,
            strategies TEXT NOT NULL,
            tribal_type TEXT,
            events_produced TEXT NOT NULL,
            events_consumed TEXT NOT NULL,
            key_effects TEXT NOT NULL
        )
    """)
    conn.commit()


def save_profile(conn, oracle_id: str, profile: CommanderProfile):
    conn.execute(
        "INSERT OR REPLACE INTO commander_profiles VALUES (?,?,?,?,?,?)",
        (oracle_id,
         json.dumps(sorted(profile.strategies)),
         profile.tribal_type,
         json.dumps(sorted(profile.key_events_produced)),
         json.dumps(sorted(profile.key_events_consumed)),
         json.dumps(sorted(profile.key_effects))),
    )
    conn.commit()


def load_profile(conn, oracle_id: str) -> CommanderProfile | None:
    row = conn.execute(
        "SELECT strategies, tribal_type, events_produced, events_consumed, key_effects "
        "FROM commander_profiles WHERE oracle_id = ?",
        (oracle_id,)
    ).fetchone()
    if not row:
        return None
    return CommanderProfile(
        strategies=set(json.loads(row[0])),
        tribal_type=row[1],
        key_events_produced=set(json.loads(row[2])),
        key_events_consumed=set(json.loads(row[3])),
        key_effects=set(json.loads(row[4])),
    )
