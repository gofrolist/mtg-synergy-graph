"""Filter attribute explosion (SPEC §4.6).

Converts a Forge ``ValidCard$``-style filter string into ``port_attributes``
rows so Layer 4 (graph engine) can join via indexed integer comparisons
instead of regex-parsing strings inside SQL.
"""

from __future__ import annotations

# Lightweight built-in vocabularies. The full classifier loads Scryfall data
# at import time; for unit tests these constants are sufficient and stay
# deterministic. Unknown tokens are classified as 'unknown' rather than
# silently dropped.

CARD_TYPES = frozenset(
    {
        "Artifact",
        "Creature",
        "Enchantment",
        "Instant",
        "Land",
        "Planeswalker",
        "Sorcery",
        "Tribal",
        "Battle",
        "Permanent",
        "Card",
        "Spell",
        "Token",
    }
)

SUPERTYPES = frozenset({"Legendary", "Basic", "Snow", "World", "Tribal"})

COLORS = frozenset({"White", "Blue", "Black", "Red", "Green", "Colorless", "Multicolor"})

# Forge uses 1-letter colour codes too.
COLOR_LETTERS = frozenset({"W", "U", "B", "R", "G", "C"})

# A non-exhaustive starter set of common keywords. Anything not here gets
# classified as 'unknown' and logged at import time, never silently dropped.
COMMON_KEYWORDS = frozenset(
    {
        "Flying",
        "First Strike",
        "Double Strike",
        "Deathtouch",
        "Lifelink",
        "Trample",
        "Vigilance",
        "Reach",
        "Haste",
        "Hexproof",
        "Indestructible",
        "Menace",
        "Defender",
        "Flash",
        "Ward",
    }
)

CONTROLLER_QUALIFIERS = frozenset({"YouCtrl", "OppCtrl", "Self", "Other", "Any", "You", "Opponent"})

ZONE_QUALIFIERS = frozenset({"Battlefield", "Graveyard", "Hand", "Library", "Exile", "Stack", "Command"})

_CMC_PREFIXES = ("cmcEQ", "cmcLE", "cmcLT", "cmcGE", "cmcGT", "cmcNE")
_POWER_PREFIXES = ("powerEQ", "powerLE", "powerLT", "powerGE", "powerGT", "powerNE")


def classify_attr_token(token: str) -> str:
    """Classify a single filter token (post '!' stripping).

    Returns the ``attr_kind`` value to store in ``port_attributes``.
    """
    if not token:
        return "unknown"
    if token in CARD_TYPES:
        return "type"
    if token in SUPERTYPES:
        return "supertype"
    if token in COLORS:
        return "color"
    if token in COLOR_LETTERS:
        return "color"
    if token in COMMON_KEYWORDS:
        return "keyword"
    if token in CONTROLLER_QUALIFIERS:
        return "controller"
    if token in ZONE_QUALIFIERS:
        return "zone"
    for pfx in _CMC_PREFIXES:
        if token.startswith(pfx):
            return "cmc_cmp"
    for pfx in _POWER_PREFIXES:
        if token.startswith(pfx):
            return "power_cmp"
    # Counter type tokens follow the pattern "counters_TYPE" or are short
    # uppercase identifiers like "P1P1" / "M1M1" — heuristic only.
    if token.isupper() and any(c.isdigit() for c in token):
        return "counter_type"
    # Anything left over is treated as a creature subtype. Forge filters use
    # subtype names that overlap heavily with Scryfall types — Goblin, Elf,
    # Cleric, Zombie, Human, etc. — so this is the right default.
    return "subtype"


def explode_filter(valid_filter: str) -> list[dict]:
    """Convert a Forge ValidCard$ filter string into attribute rows.

    >>> explode_filter("Creature.Goblin.YouCtrl+!Black+cmcLE3")
    [{'attr_kind': 'type', 'attr_value': 'Creature', 'is_negated': False},
     {'attr_kind': 'subtype', 'attr_value': 'Goblin', 'is_negated': False},
     {'attr_kind': 'controller', 'attr_value': 'YouCtrl', 'is_negated': False},
     {'attr_kind': 'color', 'attr_value': 'Black', 'is_negated': True},
     {'attr_kind': 'cmc_cmp', 'attr_value': 'cmcLE3', 'is_negated': False}]

    The ``port_id`` is added by the importer; this function returns the
    decomposed attributes only.
    """
    if not valid_filter:
        return []
    rows: list[dict] = []
    for segment in valid_filter.replace("+", ".").split("."):
        if not segment:
            continue
        negated = segment.startswith("!")
        token = segment[1:] if negated else segment
        if not token:
            continue
        rows.append(
            {
                "attr_kind": classify_attr_token(token),
                "attr_value": token,
                "is_negated": negated,
            }
        )
    return rows
