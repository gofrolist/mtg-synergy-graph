"""Parse Forge filter grammar strings into ForgeFilter objects.

Forge filter format: 'CardType.modifier+modifier+modifier'
Examples:
  'Creature.YouCtrl+powerGE4+attacking'
  'Instant,Sorcery'
  'Card.Self'
  'Goblin.YouCtrl+Other'
"""
import re
from mtg_synergy.parse.forge_types import ForgeFilter

# Known card types in Forge
_CARD_TYPES = {
    "creature", "artifact", "enchantment", "instant", "sorcery",
    "planeswalker", "land", "permanent", "spell", "card",
    "tribal", "battle",
}

# Controller modifiers
_CONTROLLERS = {"youctrl", "oppctrl", "youown", "youdontctrl"}

# Boolean modifiers
_BOOL_MODIFIERS = {
    "attacking": ("is_attacking", True),
    "blocking": ("is_blocking", True),
    "tapped": ("is_tapped", True),
    "untapped": ("is_tapped", False),
    "token": ("is_token", True),
    "nontoken": ("is_token", False),
    "legendary": ("is_legendary", True),
    "other": ("is_other", True),
    "self": ("is_self", True),
    "isremembered": ("is_remembered", True),
}

# Numeric comparison patterns
_NUMERIC_RE = re.compile(r'^(power|toughness|cmc)(GE|LE|EQ)(\d+)$', re.IGNORECASE)

# Zone names
_ZONES = {"battlefield", "graveyard", "hand", "library", "exile", "command", "stack"}


def parse_forge_filter(filter_str: str) -> ForgeFilter:
    """Parse a Forge filter string into a ForgeFilter object."""
    if not filter_str or not filter_str.strip():
        return ForgeFilter()

    f = ForgeFilter(raw=filter_str)

    # Split on '+' to get top-level modifiers
    # But first handle '.' separator (type.modifier)
    # Forge format: 'Type.mod1+mod2' or 'Type1,Type2' or 'Type.mod1.mod2+mod3'
    parts = filter_str.replace(".", "+").split("+")

    unparsed = []
    for part in parts:
        part = part.strip()
        if not part:
            continue

        part_lower = part.lower()

        # Check for comma-separated type union
        if "," in part:
            for t in part.split(","):
                t = t.strip()
                if t.lower() in _CARD_TYPES:
                    f.card_types.append(t)
                else:
                    f.subtypes.append(t)
            continue

        # Check if it's a known card type
        if part_lower in _CARD_TYPES:
            f.card_types.append(part)
            continue

        # Check controller
        if part_lower in _CONTROLLERS:
            f.controller = part
            continue

        # Check boolean modifiers
        if part_lower in _BOOL_MODIFIERS:
            attr, val = _BOOL_MODIFIERS[part_lower]
            setattr(f, attr, val)
            continue

        # Check 'with' keyword prefix
        if part_lower.startswith("with"):
            kw = part[4:]  # strip "with"
            if kw:
                f.has_keyword = kw
            continue

        # Check 'without' keyword prefix
        if part_lower.startswith("without"):
            continue  # skip negative keyword filters for now

        # Check numeric comparisons (powerGE4, cmcLE3, etc.)
        m = _NUMERIC_RE.match(part)
        if m:
            stat, op, val = m.group(1).lower(), m.group(2).upper(), int(m.group(3))
            if op == "GE":
                setattr(f, f"{stat}_ge", val)
            elif op == "LE":
                setattr(f, f"{stat}_le", val)
            continue

        # Check for attached_by patterns
        if part_lower in ("attachedby", "enchantedby", "equippedby"):
            f.attached_by = part
            continue

        # Check for zone names
        if part_lower in _ZONES:
            f.zone = part
            continue

        # Not a known card type, controller, or zone — might be a creature subtype
        # Real subtypes are single capitalized words (Goblin, Elf, Human)
        # CamelCase words (EffectSource, RememberedCard) are Forge runtime tokens
        if (part[0].isupper()
                and not any(c.isupper() for c in part[1:])
                and part_lower not in _CARD_TYPES
                and part_lower not in _ZONES):
            f.subtypes.append(part)
            continue

        # Unparsed modifier
        unparsed.append(part)

    # If we have unparsed modifiers, store them in raw
    if unparsed:
        f.raw = filter_str  # keep full string for unparsed cases
    elif not unparsed:
        f.raw = None  # fully parsed — clear raw

    return f
