"""Activation/casting cost parsing for MTG oracle text."""
from __future__ import annotations

import re
from typing import Optional

from mtg_synergy.parse.ast_types import Cost, ManaAmount, ObjectFilter

# Mana symbol patterns
_MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")
_GENERIC_RE = re.compile(r"^\d+$")
_COLOR_RE = re.compile(r"^[WUBRGC]$")
_HYBRID_RE = re.compile(r"^([WUBRG])/([WUBRG])$")
_PHYREXIAN_RE = re.compile(r"^([WUBRG])/P$")

# Sacrifice pattern
_SACRIFICE_RE = re.compile(
    r"[Ss]acrifice\s+(?:an?\s+)?(.*)", re.IGNORECASE
)

# Pay life pattern
_PAY_LIFE_RE = re.compile(r"[Pp]ay\s+(\d+)\s+life", re.IGNORECASE)

# Discard pattern
_DISCARD_RE = re.compile(r"[Dd]iscard\s+(.*)", re.IGNORECASE)

# Exile pattern
_EXILE_RE = re.compile(
    r"[Ee]xile\s+(?:an?\s+)?(.+?)(?:\s+card)?\s+from\s+(?:your\s+)?(\w+)",
    re.IGNORECASE,
)

# Loyalty pattern (handles +N, -N, 0, and unicode minus)
_LOYALTY_RE = re.compile(r"^([+\u2212\-])(\d+)$")
_LOYALTY_ZERO_RE = re.compile(r"^0$")

# Mana production patterns
_ADD_MANA_RE = re.compile(r"[Aa]dd\s+(.*?)\.?$")
_ANY_COLOR_RE = re.compile(
    r"(?:one|two|three|\d+)\s+mana\s+of\s+any\s+color", re.IGNORECASE
)
_ANY_COLOR_COUNT_RE = re.compile(
    r"(one|two|three|\d+)\s+mana\s+of\s+any\s+color", re.IGNORECASE
)

# Common creature types for sacrifice parsing
_CARD_TYPES = {
    "creature", "artifact", "enchantment", "land", "planeswalker",
    "instant", "sorcery",
}


def _parse_mana_symbols(text: str) -> Optional[ManaAmount]:
    """Parse mana symbols like {2}{G}{W} into a ManaAmount."""
    symbols = _MANA_SYMBOL_RE.findall(text)
    if not symbols:
        return None

    colors: dict[str, int] = {}
    total = 0

    for sym in symbols:
        if sym == "T" or sym == "Q":
            # Tap/untap symbols are not mana
            continue
        elif sym == "X":
            colors["X"] = colors.get("X", 0) + 1
            # X does not count toward total
        elif _GENERIC_RE.match(sym):
            generic = int(sym)
            colors["generic"] = colors.get("generic", 0) + generic
            total += generic
        elif _COLOR_RE.match(sym):
            colors[sym] = colors.get(sym, 0) + 1
            total += 1
        elif _HYBRID_RE.match(sym):
            # Hybrid mana like {W/U} - counts as 1 total
            colors[sym] = colors.get(sym, 0) + 1
            total += 1
        elif _PHYREXIAN_RE.match(sym):
            # Phyrexian mana like {B/P} - counts as 1 total
            colors[sym] = colors.get(sym, 0) + 1
            total += 1
        else:
            # Unknown symbol, count as 1
            colors[sym] = colors.get(sym, 0) + 1
            total += 1

    if not colors:
        return None

    return ManaAmount(total=total, colors=colors)


def _parse_sacrifice(text: str) -> Optional[ObjectFilter]:
    """Parse sacrifice cost text into an ObjectFilter."""
    text = text.strip()
    text_lower = text.lower()

    if text_lower in _CARD_TYPES:
        return ObjectFilter(card_type=text_lower)

    # Check if it's a known subtype (capitalized, not a card type)
    # e.g., "Goblin", "Human", "Zombie"
    if text_lower not in _CARD_TYPES and text[0].isupper():
        return ObjectFilter(card_type="creature", subtype=text)

    return ObjectFilter(card_type=text_lower)


def _parse_exile(text: str) -> Optional[ObjectFilter]:
    """Parse exile cost like 'Exile a creature card from your graveyard'."""
    m = _EXILE_RE.match(text.strip())
    if m:
        what = m.group(1).strip().lower()
        zone = m.group(2).strip().lower()
        card_type = what if what in _CARD_TYPES else None
        return ObjectFilter(card_type=card_type, zone=zone)
    return None


def parse_cost(cost_text: str, is_loyalty: bool = False) -> Cost:
    """Parse a cost string into a Cost dataclass.

    Args:
        cost_text: The cost string, e.g. "{2}{G}, {T}, Sacrifice a creature"
        is_loyalty: If True, parse as a planeswalker loyalty cost (+1, -3, etc.)

    Returns:
        A Cost dataclass with parsed components.
    """
    cost_text = cost_text.strip()

    # Handle loyalty costs
    if is_loyalty:
        m = _LOYALTY_RE.match(cost_text)
        if m:
            sign = m.group(1)
            value = int(m.group(2))
            if sign in ("\u2212", "-"):
                value = -value
            return Cost(loyalty=value)
        if _LOYALTY_ZERO_RE.match(cost_text):
            return Cost(loyalty=0)
        # Fall through to normal parsing if not a loyalty pattern

    # Split on comma to get individual cost segments
    segments = [s.strip() for s in cost_text.split(",")]

    mana: Optional[ManaAmount] = None
    tap = False
    sacrifice: Optional[ObjectFilter] = None
    pay_life: Optional[int] = None
    discard: Optional[ObjectFilter] = None
    exile: Optional[ObjectFilter] = None

    # Collect all mana symbols across segments
    mana_segments = []

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        # Check for tap symbol
        if seg == "{T}":
            tap = True
            continue

        # Check for untap symbol
        if seg == "{Q}":
            continue

        # Check for sacrifice
        sac_m = _SACRIFICE_RE.match(seg)
        if sac_m:
            sacrifice = _parse_sacrifice(sac_m.group(1).strip())
            continue

        # Check for pay life
        life_m = _PAY_LIFE_RE.match(seg)
        if life_m:
            pay_life = int(life_m.group(1))
            continue

        # Check for discard
        disc_m = _DISCARD_RE.match(seg)
        if disc_m:
            what = disc_m.group(1).strip().lower()
            if what == "a card":
                discard = ObjectFilter()
            else:
                # Try to extract card type
                card_type = None
                for ct in _CARD_TYPES:
                    if ct in what:
                        card_type = ct
                        break
                discard = ObjectFilter(card_type=card_type)
            continue

        # Check for exile
        exile_m = _EXILE_RE.match(seg)
        if exile_m:
            exile = _parse_exile(seg)
            continue

        # Check for mana symbols
        if _MANA_SYMBOL_RE.search(seg):
            mana_segments.append(seg)
            continue

    # Combine all mana segments
    if mana_segments:
        combined = "".join(mana_segments)
        mana = _parse_mana_symbols(combined)

    return Cost(
        mana=mana,
        tap=tap,
        sacrifice=sacrifice,
        pay_life=pay_life,
        discard=discard,
        exile=exile,
    )


def parse_mana_production(text: str) -> ManaAmount:
    """Parse mana production text like 'Add {R}{R}' or 'Add one mana of any color'.

    Args:
        text: The production text.

    Returns:
        A ManaAmount describing what's produced.
    """
    text = text.strip()

    # Check for "any color" pattern
    any_m = _ANY_COLOR_RE.search(text)
    if any_m:
        # Extract count
        count_m = _ANY_COLOR_COUNT_RE.search(text)
        if count_m:
            count_str = count_m.group(1).lower()
            word_to_num = {"one": 1, "two": 2, "three": 3}
            count = word_to_num.get(count_str)
            if count is None:
                count = int(count_str)
        else:
            count = 1
        return ManaAmount(total=count, is_any_color=True)

    # Check for mana symbols after "Add"
    add_m = _ADD_MANA_RE.match(text)
    if add_m:
        remainder = add_m.group(1)
        result = _parse_mana_symbols(remainder)
        if result:
            return result

    # Fallback: try parsing the whole text for mana symbols
    result = _parse_mana_symbols(text)
    if result:
        return result

    return ManaAmount()
