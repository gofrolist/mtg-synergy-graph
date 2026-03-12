import re
from dataclasses import dataclass, field
from typing import Optional
from card_db import CARD_DB, NAME_INDEX


@dataclass
class CardFeatures:
    name: str
    cmc: float
    type_line: str
    oracle_text: str
    keywords: list
    color_identity: list

    is_human: bool = False
    mentions_human: bool = False
    places_counters_on_others: bool = False
    places_counter_on_self: bool = False
    doubles_counters: bool = False
    doubles_counters_scope: str = "none"   # "board", "single", "none"
    interacts_with_counters: bool = False
    role: str = "unknown"

    tribal_score: float = 0
    counter_score: float = 0
    role_score: float = 0
    synergy_bonus: float = 0
    commander_multiplier: float = 1.0
    cmc_penalty: float = 0.0
    is_permanent: bool = True          # False for instants/sorceries
    can_become_human: bool = False     # e.g. Roaming Throne, Realmwalker (changeling counts too)
    doubles_triggered_abilities: bool = False  # e.g. Roaming Throne, Panharmonicon
    final_score: float = 0
    explanation: list = field(default_factory=list)


def fetch_card(name_or_id: str) -> Optional[dict]:
    """Fetch a card by name or oracle_id."""
    # Try direct oracle_id lookup
    if name_or_id in CARD_DB:
        return dict(CARD_DB[name_or_id])

    # Try name lookup via index
    oracle_id = NAME_INDEX.get(name_or_id.lower())
    if oracle_id:
        return dict(CARD_DB[oracle_id])

    return None


def extract_features(card_data: dict) -> CardFeatures:
    name = card_data.get("name", "Unknown")
    cmc = card_data.get("cmc", 0)
    type_line = card_data.get("type_line", "")
    if "card_faces" in card_data:
        oracle_text = card_data["card_faces"][0].get("oracle_text", "")
    else:
        oracle_text = card_data.get("oracle_text", "")
    keywords = card_data.get("keywords", [])
    color_identity = card_data.get("color_identity", [])

    oracle_lower = oracle_text.lower()
    type_lower = type_line.lower()

    is_human = "human" in type_lower
    mentions_human = bool(re.search(r"\bhuman\b", oracle_lower))

    places_counters_on_others = bool(
        re.search(r"put[s]? (?:a |one |two |three |\+1/\+1 )?(?:\+1/\+1 )?counter[s]? on (?:each|all|another|target|other)", oracle_lower)
        or re.search(r"distribut\w+ .*counter", oracle_lower)
        or re.search(r"put \w+ \+1/\+1 counter[s]? on each", oracle_lower)
    )

    places_counter_on_self = bool(
        re.search(r"put[s]? (?:a |\+1/\+1 )counter on (?:it|itself|kyler|this)", oracle_lower)
        or re.search(r"enters.*with.*counter", oracle_lower)
        or re.search(r"gets \+1/\+1 counter", oracle_lower)
    )

    doubles_counters = bool(
        re.search(r"that many plus one", oracle_lower)
        or re.search(r"twice that many.*counter", oracle_lower)
        or re.search(r"twice as many.*counter", oracle_lower)
        or re.search(r"doubl\w+ .*counter", oracle_lower)
        or re.search(r"two counters? instead", oracle_lower)
        or re.search(r"additional.*counter", oracle_lower)
    )

    # FIX 2: scope of doubling — board-wide vs single-target
    doubles_counters_scope = "none"
    if doubles_counters:
        # Board-wide: affects "each", "all", "any number", "permanents you control"
        if re.search(r"each|all creatures|permanents you control|any number of target", oracle_lower):
            doubles_counters_scope = "board"
        else:
            doubles_counters_scope = "single"

    interacts_with_counters = bool(
        re.search(r"proliferate", oracle_lower)
        or re.search(r"counter[s]? (?:from|on|among|into)", oracle_lower)
        or re.search(r"move[s]? .*counter", oracle_lower)
        or re.search(r"remove.*counter", oracle_lower)
        or name in ["The Ozolith", "Ozolith, the Shattered Spire"]
    )

    is_permanent = not any(t in type_lower for t in ["instant", "sorcery"])

    # Cards that can become Human by choice (e.g. Roaming Throne choosing Human type)
    # or are already every type (Changeling)
    can_become_human = bool(
        re.search(r"this creature is the chosen type in addition", oracle_lower)
        or "changeling" in oracle_lower
        or "Changeling" in keywords
    )

    doubles_triggered_abilities = bool(
        re.search(r"triggers? an additional time", oracle_lower)
        or re.search(r"that ability triggers? an additional time", oracle_lower)
        or re.search(r"copy target triggered ability", oracle_lower)
    )

    role = classify_role(type_lower, oracle_lower, keywords, cmc)

    return CardFeatures(
        name=name, cmc=cmc, type_line=type_line, oracle_text=oracle_text,
        keywords=keywords, color_identity=color_identity,
        is_human=is_human, mentions_human=mentions_human,
        places_counters_on_others=places_counters_on_others,
        places_counter_on_self=places_counter_on_self,
        doubles_counters=doubles_counters,
        doubles_counters_scope=doubles_counters_scope,
        interacts_with_counters=interacts_with_counters,
        role=role,
        is_permanent=is_permanent,
        can_become_human=can_become_human,
        doubles_triggered_abilities=doubles_triggered_abilities,
    )


def classify_role(type_lower, oracle_lower, keywords, cmc):
    if "land" in type_lower:
        return "land"
    if re.search(r"add \{[wugbr]", oracle_lower) or re.search(r"search.*library.*land", oracle_lower):
        return "ramp"
    if re.search(r"draw (?:a|two|three|\w+) card", oracle_lower):
        return "draw"
    if re.search(r"destroy target|exile target|deal[s]? \d+ damage to target|return target.*to.*hand", oracle_lower):
        return "removal"
    if re.search(r"(?:your creatures|creatures you control|permanents you control|target creature).*(?:gain|get|have).*(?:hexproof|indestructible|protection)", oracle_lower):
        return "protection"
    if any(k in ["Hexproof", "Indestructible"] for k in keywords):
        return "protection"
    if re.search(r"indestructible until end of turn|hexproof until end of turn|phase out", oracle_lower):
        return "protection"
    if re.search(r"counter|\+1/\+1|proliferate", oracle_lower):
        return "enabler"
    if "creature" in type_lower and cmc >= 4:
        return "threat"
    return "utility"
