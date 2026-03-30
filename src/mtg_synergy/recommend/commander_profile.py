"""Commander archetype inference from oracle text + parsed abilities + type line.

Detects strategies, tribal type, and event profiles for any of the 3,141 legal
commanders without needing EDHREC data. Profiles are precomputed and stored in
the commander_profiles table for O(1) lookup at recommendation time.
"""
from dataclasses import dataclass, field
# Strategy keyword maps: oracle text patterns that indicate real synergy
# with each strategy.  Moved here from scoring.py after the scoring pipeline
# stopped using them (superseded by forge + causal graph).
STRATEGY_KEYWORDS = {
    "+1/+1-counters": ["+1/+1 counter", "proliferate", "+1/+1 counters on",
                       "counter equal", "counter among", "modify", "modified",
                       "twice that many", "additional +1"],
    "tokens": ["create a", "create two", "create x", "token creature", "populate", "amass"],
    "spellslinger": ["instant or sorcery", "noncreature spell", "magecraft",
                     "prowess", "storm", "copy a spell", "copies of"],
    "aristocrats": ["whenever a creature dies", "whenever another creature",
                    "sacrifice a creature", "sacrifice a permanent", "blood artist"],
    "equipment": ["equip", "equipment enters", "equipped creature", "attach"],
    "auras": ["enchant creature", "enchanted creature", "constellation", "aura"],
    "enchantress": ["enchantment enters", "constellation", "whenever you cast an enchantment"],
    "landfall": ["landfall", "whenever a land enters", "land you control enters"],
    "reanimator": ["return target creature card from your graveyard",
                   "put a creature card from a graveyard", "unearth", "reanimate"],
    "mill": ["mill", "cards from the top of", "into your graveyard from your library"],
    "self-mill": ["mill", "cards from the top of your library"],
    "artifacts": ["artifact enters", "affinity for artifacts", "metalcraft", "improvise"],
    "voltron": ["equipped creature gets", "enchanted creature gets", "hexproof",
                "indestructible", "double strike", "commander damage"],
    "lifegain": ["gain life", "lifelink", "whenever you gain life"],
    "lifedrain": ["each opponent loses", "drain", "deals damage to each opponent"],
    "go-wide": ["create a 1/1", "create two", "create x", "for each creature you control"],
    "blink": ["exile target creature you control, then return",
              "exile any number of target creatures you control",
              "flicker", "enters, you may"],
    "wheels": ["each player discards", "wheel", "each player draws"],
    "proliferate": ["proliferate", "+1/+1 counter"],
    "burn": ["deals damage to any target", "deals damage to each opponent",
             "whenever.*deals damage"],
    "stax": ["can't cast", "costs .* more to cast", "each opponent sacrifices",
             "opponents can't"],
}


# Forge verb → strategy mapping (replaces STRATEGY_KEYWORDS oracle text matching)
_FORGE_VERB_STRATEGIES = {
    "Token": "tokens",
    "PutCounter": "+1/+1-counters",
    "PutCounterAll": "+1/+1-counters",
    "Proliferate": "+1/+1-counters",
    "Sacrifice": "aristocrats",
    "CopySpellAbility": "spellslinger",
    "GainLife": "lifegain",
    "LoseLife": "lifedrain",
    "DealDamage": "burn",
    "DamageAll": "burn",
    "Mill": "mill",
    "Draw": "card-draw",
    "Dig": "card-draw",
    "Equip": "equipment",
    "Enchant": "enchantress",
    "PumpAll": "go-wide",
    "Mana": "ramp",
}

_FORGE_TRIGGER_STRATEGIES = {
    "Attacks": "voltron",
    "AttackersDeclared": "go-wide",
    "SpellCast": "spellslinger",
    "Sacrificed": "aristocrats",
    "LifeGained": "lifegain",
    "Discarded": "wheels",
    "Drawn": "card-draw",
    "ChangesZone": "blink",
}


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


