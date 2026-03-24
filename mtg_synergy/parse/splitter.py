"""Pass 1-2: Split oracle text into individual abilities and classify each by kind."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class RawAbility:
    kind: str  # "triggered", "activated", "static", "replacement", "keyword", "trigger_modifier", "modal"
    raw_text: str  # full original text of this ability
    cost_text: str | None = None  # for activated: the cost portion
    effect_text: str | None = None  # for activated/triggered: the effect portion
    trigger_text: str | None = None  # for triggered: the trigger clause
    loyalty_cost: int | None = None  # for planeswalker abilities
    chapter: int | None = None  # for saga chapters
    reminder_text: str | None = None  # extracted from parentheses
    restrictions_text: str | None = None  # "Activate only once each turn." etc.
    modes: list[str] | None = None  # for modal abilities: list of mode texts


# Common MTG keywords (case-insensitive matching done via set lookup)
_KEYWORDS = {
    k.lower()
    for k in [
        "Flying",
        "First strike",
        "Double strike",
        "Deathtouch",
        "Trample",
        "Haste",
        "Lifelink",
        "Vigilance",
        "Hexproof",
        "Indestructible",
        "Menace",
        "Reach",
        "Flash",
        "Defender",
        "Fear",
        "Intimidate",
        "Shroud",
        "Protection",
        "Infect",
        "Wither",
        "Persist",
        "Undying",
        "Exalted",
        "Extort",
        "Prowess",
        "Convoke",
        "Delve",
        "Dredge",
        "Cascade",
        "Changeling",
        "Devoid",
        "Afflict",
        "Afterlife",
        "Annihilator",
        "Ascend",
        "Bushido",
        "Flanking",
        "Horsemanship",
        "Landwalk",
        "Mountainwalk",
        "Forestwalk",
        "Islandwalk",
        "Swampwalk",
        "Plainswalk",
        "Shadow",
        "Skulk",
        "Storm",
        "Soulbond",
        "Suspend",
        "Totem armor",
        "Ward",
        "Ninjutsu",
        "Riot",
        "Spectacle",
        "Escape",
        "Mutate",
        "Companion",
        "Foretell",
        "Madness",
        "Miracle",
        "Morph",
        "Megamorph",
        "Cycling",
        "Equip",
        "Reconfigure",
        "Living weapon",
        "Bestow",
        "Embalm",
        "Eternalize",
        "Unearth",
        "Encore",
        "Blitz",
        "Dash",
        "Evoke",
        "Offering",
        "Overload",
        "Craft",
        "Fabricate",
        "Modular",
        "Exploit",
        "Myriad",
        "Partner",
        "Crew",
        "Entwine",
        "Kicker",
        "Multikicker",
        "Buyback",
        "Flashback",
        "Retrace",
        "Jump-start",
        "Splice",
        "Aftermath",
        "Fuse",
        "Discover",
        "Amass",
        "Adapt",
        "Monstrosity",
        "Renown",
        "Connive",
        "Investigate",
        "Populate",
        "Proliferate",
        "Scry",
        "Surveil",
        "Venture",
        "Mill",
        "Bolster",
        "Manifest",
        "Cloak",
        "Detain",
        "Fateseal",
        "Goad",
        "Treasure",
    ]
}

# Keyword pattern: "Keyword N" (e.g., "Discover 5", "Afflict 3", "Ward {2}")
_KEYWORD_WITH_VALUE_RE = re.compile(
    r"^([A-Z][a-z]+(?:\s[a-z]+)?)\s+(\d+|\{[^}]+\})$"
)

# Patterns to skip (flavor rules text)
_SKIP_PATTERNS = [
    re.compile(r"can be your commander", re.IGNORECASE),
    re.compile(r"^partner with\b", re.IGNORECASE),
]

# Restriction patterns
_RESTRICTION_RE = re.compile(
    r"(Activate (?:this ability )?only .*?\.)$"
)

# Planeswalker loyalty cost pattern: +N:, -N:, 0:, −N: (Unicode minus)
_LOYALTY_RE = re.compile(r"^([+\u2212-]?\d+):\s*(.*)$")

# Saga chapter pattern: I —, II —, III —, IV —, etc.
_CHAPTER_RE = re.compile(r"^([IVX]+)\s*\u2014\s*(.*)$")

# Roman numeral conversion
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

# Trigger modifier pattern (Panharmonicon-style)
_TRIGGER_MODIFIER_RE = re.compile(r"triggers?\s+an\s+additional\s+time", re.IGNORECASE)

# Replacement effect pattern
_REPLACEMENT_RE = re.compile(r"\bwould\b.*\binstead\b", re.IGNORECASE | re.DOTALL)

# Triggered ability patterns
_TRIGGERED_RE = re.compile(r"^(When(ever)?|At the beginning|At end)\b", re.IGNORECASE)

# Modal pattern
_MODAL_RE = re.compile(r"^Choose\s+(one|two|three|four|five)\s*\u2014", re.IGNORECASE)

# Reminder text pattern: text in parentheses at end
_REMINDER_RE = re.compile(r"\s*\(([^)]+)\)\s*$")

# Cost indicators for activated abilities
_COST_INDICATORS = ["{", "Sacrifice", "Pay", "Discard", "Exile", "Tap", "Remove"]


def _roman_to_int(roman: str) -> int:
    """Convert roman numeral string to int."""
    return _ROMAN.get(roman, 0)


def _strip_reminder(text: str) -> tuple[str, str | None]:
    """Strip reminder text in parentheses from the end. Returns (cleaned, reminder)."""
    m = _REMINDER_RE.search(text)
    if m:
        reminder = m.group(1)
        cleaned = text[: m.start()].strip()
        return cleaned, reminder
    return text, None


def _extract_restriction(text: str) -> tuple[str, str | None]:
    """Extract restriction sentence from end of text."""
    m = _RESTRICTION_RE.search(text)
    if m:
        restriction = m.group(1)
        cleaned = text[: m.start()].strip()
        return cleaned, restriction
    return text, None


def _is_keyword(text: str) -> bool:
    """Check if text is a keyword or keyword with value (e.g., 'Discover 5')."""
    lower = text.strip().lower()
    if lower in _KEYWORDS:
        return True
    # Check "Keyword N" pattern
    m = _KEYWORD_WITH_VALUE_RE.match(text.strip())
    if m and m.group(1).lower() in _KEYWORDS:
        return True
    # Comma-separated keywords: "Flying, haste"
    parts = [p.strip() for p in text.split(",")]
    if len(parts) > 1 and all(p.lower() in _KEYWORDS for p in parts):
        return True
    return False


def _has_cost_indicator(text: str) -> bool:
    """Check if text before a colon looks like an activated ability cost."""
    for indicator in _COST_INDICATORS:
        if indicator in text:
            return True
    return False


def _find_activated_colon(text: str) -> int | None:
    """Find the index of the colon separating cost from effect in an activated ability.

    Returns the index of the colon, or None if not found.
    Skips colons inside mana symbols like {C}.
    """
    i = 0
    while i < len(text):
        if text[i] == "{":
            # Skip entire mana symbol
            close = text.find("}", i)
            if close != -1:
                i = close + 1
                continue
        if text[i] == ":":
            return i
        i += 1
    return None


def _find_trigger_effect_split(text: str) -> int:
    """Find the comma index that separates trigger clause from effect clause.

    For simple triggers like "Whenever X, do Y", returns the first comma.
    For multi-clause triggers like "Whenever X, or Y, or Z, EFFECT",
    returns the last comma before the effect starts.

    Heuristic: scan commas from left. If the text after the comma starts with
    'or ' (continuation of trigger), skip it. Otherwise, it's the effect split.
    If all commas are followed by 'or ', use the last comma as fallback.
    """
    last_comma = -1
    i = 0
    while i < len(text):
        if text[i] == ",":
            after = text[i + 1:].lstrip()
            if after.lower().startswith("or "):
                # This comma is part of a multi-clause trigger, skip
                last_comma = i
                i += 1
                continue
            else:
                return i
        i += 1
    # All commas were followed by "or" — use the last one
    return last_comma


def _classify_line(text: str) -> RawAbility:
    """Classify a single line of oracle text into a RawAbility."""
    original = text

    # Strip reminder text
    text, reminder = _strip_reminder(text)

    # Strip restriction
    text, restriction = _extract_restriction(text)

    # Check for planeswalker loyalty
    m = _LOYALTY_RE.match(text)
    if m:
        cost_str = m.group(1)
        effect = m.group(2).strip()
        # Parse loyalty cost (handle Unicode minus −)
        cost_str_normalized = cost_str.replace("\u2212", "-")
        loyalty = int(cost_str_normalized)
        return RawAbility(
            kind="activated",
            raw_text=original,
            cost_text=cost_str,
            effect_text=effect,
            loyalty_cost=loyalty,
            reminder_text=reminder,
            restrictions_text=restriction,
        )

    # Check for saga chapter
    m = _CHAPTER_RE.match(text)
    if m:
        roman = m.group(1)
        effect = m.group(2).strip()
        chapter = _roman_to_int(roman)
        return RawAbility(
            kind="triggered",
            raw_text=original,
            effect_text=effect,
            chapter=chapter,
            reminder_text=reminder,
            restrictions_text=restriction,
        )

    # Check for trigger modifier (Panharmonicon)
    if _TRIGGER_MODIFIER_RE.search(text):
        return RawAbility(
            kind="trigger_modifier",
            raw_text=original,
            reminder_text=reminder,
            restrictions_text=restriction,
        )

    # Check for replacement effect (must come before triggered check
    # because some start with "If")
    if _REPLACEMENT_RE.search(text):
        return RawAbility(
            kind="replacement",
            raw_text=original,
            reminder_text=reminder,
            restrictions_text=restriction,
        )

    # Check for triggered ability
    if _TRIGGERED_RE.match(text):
        # Split trigger clause from effect at the right comma.
        # For multi-clause triggers like "Whenever X, or Y, or Z, EFFECT"
        # we need the last comma before the effect, not the first comma.
        trigger_text = None
        effect_text = None
        comma_idx = _find_trigger_effect_split(text)
        if comma_idx != -1:
            trigger_text = text[: comma_idx].strip()
            effect_text = text[comma_idx + 1 :].strip()
        else:
            trigger_text = text
            effect_text = text
        return RawAbility(
            kind="triggered",
            raw_text=original,
            trigger_text=trigger_text,
            effect_text=effect_text,
            reminder_text=reminder,
            restrictions_text=restriction,
        )

    # Check for activated ability (cost: effect)
    colon_idx = _find_activated_colon(text)
    if colon_idx is not None:
        cost_part = text[:colon_idx].strip()
        effect_part = text[colon_idx + 1 :].strip()
        if _has_cost_indicator(cost_part):
            return RawAbility(
                kind="activated",
                raw_text=original,
                cost_text=cost_part,
                effect_text=effect_part,
                reminder_text=reminder,
                restrictions_text=restriction,
            )

    # Check for keyword
    if _is_keyword(text):
        return RawAbility(
            kind="keyword",
            raw_text=original,
            reminder_text=reminder,
            restrictions_text=restriction,
        )

    # Default: static
    return RawAbility(
        kind="static",
        raw_text=original,
        reminder_text=reminder,
        restrictions_text=restriction,
    )


def split_abilities(oracle_text: str) -> list[RawAbility]:
    """Split oracle text into individual classified abilities.

    Pass 1: Split text into individual ability strings.
    Pass 2: Classify each ability by kind.

    Handles:
    - Double-faced cards (split on ' // ')
    - Modal abilities ('Choose one —' with '•' bullets)
    - Planeswalker loyalty abilities (+N:, -N:, 0:)
    - Saga chapters (I —, II —, etc.)
    - Newline-separated abilities
    - Reminder text stripping
    - Restriction detection
    - Flavor rules text skipping
    """
    if not oracle_text or not oracle_text.strip():
        return []

    results: list[RawAbility] = []

    # Handle DFC: split on ' // '
    faces = oracle_text.split(" // ")

    for face in faces:
        face = face.strip()
        if not face:
            continue

        # Check for modal ability
        m = _MODAL_RE.match(face)
        if m:
            # Extract bullet points
            bullet_lines = re.findall(r"\u2022\s*(.+?)(?=\n\u2022|\Z)", face, re.DOTALL)
            modes = [line.strip() for line in bullet_lines if line.strip()]
            if modes:
                results.append(
                    RawAbility(
                        kind="modal",
                        raw_text=face,
                        modes=modes,
                    )
                )
                continue

        # Split on newlines
        lines = face.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Skip flavor rules text
            skip = False
            for pattern in _SKIP_PATTERNS:
                if pattern.search(line):
                    skip = True
                    break
            if skip:
                continue

            ability = _classify_line(line)
            results.append(ability)

    return results
