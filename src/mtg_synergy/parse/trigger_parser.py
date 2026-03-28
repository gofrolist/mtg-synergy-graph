"""Trigger event + subject filter extraction from MTG oracle text.

Parses trigger phrases like "Whenever a creature enters the battlefield
under your control" into structured Trigger AST nodes.
"""
from __future__ import annotations

import re
from typing import Optional

from mtg_synergy.parse.ast_types import Trigger, ObjectFilter, Condition

# ---------------------------------------------------------------------------
# Card types recognised as subject nouns
# ---------------------------------------------------------------------------
CARD_TYPES = {
    "creature", "artifact", "enchantment", "land", "planeswalker",
    "instant", "sorcery", "spell",
}

# ---------------------------------------------------------------------------
# Event patterns — ordered most-specific first
# Each entry: (compiled regex, event_name)
# The regex is matched against the full (lowered) trigger text.
# ---------------------------------------------------------------------------
_EVENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Counters
    (re.compile(r"counters? (?:is|are) put on"), "counter_placed"),
    # Combat damage
    (re.compile(r"deals combat damage"), "deals_combat_damage"),
    # Deals (non-combat) damage
    (re.compile(r"deals? damage"), "deals_damage"),
    # Dies
    (re.compile(r"\bdies\b"), "dies"),
    # Is put into a graveyard from the battlefield (older wording for dies)
    (re.compile(r"is put into (?:a |the )?graveyard from the battlefield"), "dies"),
    # Enters the battlefield
    (re.compile(r"enters the battlefield"), "enters_the_battlefield"),
    # Enters (newer wording, post-2023)
    (re.compile(r"\benters\b(?! the battlefield)"), "enters_the_battlefield"),
    # Attacks
    (re.compile(r"\battacks\b"), "attacks"),
    # Blocks
    (re.compile(r"\bblocks\b"), "blocks"),
    # Cast
    (re.compile(r"\bcasts? "), "cast"),
    # Life gained
    (re.compile(r"\bgains? life\b"), "life_gained"),
    # Life lost
    (re.compile(r"\bloses? life\b"), "life_lost"),
    # Discard
    (re.compile(r"\bdiscards?\b"), "discard"),
    # Draw
    (re.compile(r"\bdraws? (?:a )?card"), "draw"),
    # Sacrifice
    (re.compile(r"\bsacrifices?\b"), "sacrifice"),
    # Exile
    (re.compile(r"\bexiled\b"), "exile"),
    # Upkeep
    (re.compile(r"beginning of (?:your |each )?upkeep"), "upkeep"),
    # End step
    (re.compile(r"beginning of (?:your |the |each )?end step"), "end_step"),
    # Combat phase
    (re.compile(r"beginning of (?:your |each )?combat"), "begin_combat"),
    # Untap step
    (re.compile(r"untap step"), "untap_step"),
    # Becomes tapped
    (re.compile(r"\bbecomes? tapped\b"), "becomes_tapped"),
    # Becomes the target
    (re.compile(r"becomes the target"), "becomes_target"),
    # Leaves the battlefield
    (re.compile(r"leaves the battlefield"), "leaves_the_battlefield"),
    # Transformed / turned face up
    (re.compile(r"\btransforms?\b"), "transforms"),
]


def parse_trigger(trigger_text: str) -> Trigger:
    """Parse a trigger phrase into a structured ``Trigger`` AST node."""
    text = trigger_text.strip()
    lower = text.lower()

    # Split off condition clause ("… , if …")
    condition = _extract_condition(text)

    # Remove condition part for event/subject parsing
    main_text = text
    if condition:
        # Cut at ", if" boundary
        if_idx = lower.find(", if ")
        if if_idx != -1:
            main_text = text[:if_idx]
            lower = main_text.lower()

    event: str = ""
    event_match_start: int = len(lower)

    for pattern, evt_name in _EVENT_PATTERNS:
        m = pattern.search(lower)
        if m:
            event = evt_name
            event_match_start = m.start()
            break

    if not event:
        # Fallback — return a bare trigger with raw text
        return Trigger(event="unknown", condition=condition)

    # --- Extract subject from the text preceding the event match ----------
    subject = _extract_subject(main_text, lower, event, event_match_start)

    # --- Controller from full text (often appears after event) ------------
    controller = _extract_controller(lower)
    if controller and subject:
        subject.controller = controller
    elif controller and subject is None:
        subject = ObjectFilter(controller=controller)

    return Trigger(event=event, subject=subject, condition=condition)


# ---------------------------------------------------------------------------
# Subject extraction
# ---------------------------------------------------------------------------

# Regex for the noun phrase right before the event keyword.
# Captures things like "a creature", "another Goblin creature", "an artifact",
# "a nontoken creature", "equipped creature", "this creature".
_SUBJECT_RE = re.compile(
    r"(?:(?:whenever|when|each time)\s+)"   # skip trigger word
    r"(.*?)"                                 # capture subject phrase
    r"\s*$",                                 # end
    re.IGNORECASE,
)


def _extract_subject(
    original: str,
    lower: str,
    event: str,
    event_start: int,
) -> Optional[ObjectFilter]:
    """Parse the noun-phrase subject from text before the event keyword."""

    # For phase triggers there is no subject noun
    if event in ("upkeep", "end_step", "begin_combat", "untap_step"):
        # But we can still note controller for "your upkeep"
        if "your " in lower:
            return ObjectFilter(controller="you")
        return None

    # Text before the event match is where the subject lives
    before = original[:event_start].strip()
    # Also grab text after event for "counter_placed" etc.
    after_lower = lower[event_start:]

    # Strip leading "whenever" / "when" / "each time"
    before_clean = re.sub(
        r"^(?:whenever|when|each time)\s+", "", before, flags=re.IGNORECASE
    ).strip()

    # For "you cast/gain/discard" patterns the subject is implicit
    if event == "cast":
        return _parse_cast_subject(before_clean, lower)
    if event == "life_gained":
        ctrl = "you" if "you " in lower else None
        return ObjectFilter(controller=ctrl) if ctrl else ObjectFilter()
    if event == "discard":
        ctrl = None
        if "a player " in lower or "each player " in lower:
            ctrl = "any"
        elif "an opponent " in lower:
            ctrl = "opponent"
        elif "you " in lower:
            ctrl = "you"
        return ObjectFilter(controller=ctrl)

    # For counter_placed, subject is after "on"
    if event == "counter_placed":
        return _parse_counter_subject(after_lower)

    # For deals_combat_damage, subject is before "deals"
    obj = _parse_noun_phrase(before_clean)
    if obj is None and before_clean:
        # Might be "equipped creature" or "this creature"
        obj = _parse_noun_phrase(before_clean)

    return obj


def _parse_cast_subject(before: str, lower: str) -> ObjectFilter:
    """Parse cast-trigger subjects like 'you cast an instant or sorcery spell'."""
    obj = ObjectFilter()

    if re.search(r"\byou\b", before, re.IGNORECASE):
        obj.controller = "you"
    elif re.search(r"\ban opponent\b", before, re.IGNORECASE):
        obj.controller = "opponent"
    elif re.search(r"\ba player\b", before, re.IGNORECASE):
        obj.controller = "any"

    # Try to find card type in the full text
    for ct in CARD_TYPES:
        if ct in lower:
            obj.card_type = ct
            break

    return obj


def _parse_counter_subject(after_lower: str) -> Optional[ObjectFilter]:
    """Parse 'counters are put on a creature you control'."""
    m = re.search(r"on (?:a |an )?([\w\s]+?)(?:\s+you control|\s*$)", after_lower)
    if not m:
        return None
    noun = m.group(1).strip()
    obj = ObjectFilter()
    for ct in CARD_TYPES:
        if ct in noun:
            obj.card_type = ct
            break
    if "you control" in after_lower:
        obj.controller = "you"
    return obj


def _parse_noun_phrase(phrase: str) -> Optional[ObjectFilter]:
    """Parse a noun phrase like 'a nontoken creature', 'another Goblin',
    'equipped creature', 'this creature' into an ObjectFilter."""
    if not phrase:
        return None

    lower = phrase.lower().strip()
    # Strip leading article
    lower = re.sub(r"^(?:a|an|the)\s+", "", lower)

    obj = ObjectFilter()

    # "another"
    if lower.startswith("another "):
        obj.is_another = True
        lower = lower[len("another "):]

    # "nontoken"
    if lower.startswith("nontoken "):
        obj.is_token = False
        lower = lower[len("nontoken "):]

    # "equipped" / "enchanted" / "this"
    if lower.startswith(("equipped ", "enchanted ", "this ")):
        lower = re.sub(r"^(?:equipped|enchanted|this)\s+", "", lower)

    # Try to find card type at end
    words = lower.split()
    found_type = False
    for w in reversed(words):
        if w in CARD_TYPES:
            obj.card_type = w
            found_type = True
            break

    # Remaining words before the card type might be a subtype
    if found_type and len(words) > 1:
        # Everything before the card type is potential subtype
        type_idx = None
        for i, w in enumerate(words):
            if w == obj.card_type:
                type_idx = i
                break
        if type_idx and type_idx > 0:
            subtype_str = " ".join(words[:type_idx])
            # Clean up and capitalize
            subtype_str = subtype_str.strip()
            if subtype_str and subtype_str not in ("nontoken", "another", "other"):
                # Capitalize each word (subtypes are proper nouns in MTG)
                obj.subtype = " ".join(w.capitalize() for w in subtype_str.split())
    elif not found_type and words:
        # No card type found — the whole phrase might be a subtype
        # e.g. "a Goblin" without "creature"
        candidate = " ".join(w.capitalize() for w in words)
        if candidate.lower() not in CARD_TYPES and len(candidate) > 1:
            obj.subtype = candidate

    return obj


# ---------------------------------------------------------------------------
# Controller extraction
# ---------------------------------------------------------------------------

def _extract_controller(lower: str) -> Optional[str]:
    """Extract controller from phrases like 'under your control', 'you control',
    'an opponent'."""
    if "under your control" in lower or "you control" in lower:
        return "you"
    if "an opponent" in lower:
        return "opponent"
    return None


# ---------------------------------------------------------------------------
# Condition extraction
# ---------------------------------------------------------------------------

# Conditions that are hard to meet
_SEVERE_PATTERNS = [
    re.compile(r"ten or more", re.IGNORECASE),
    re.compile(r"seven or more", re.IGNORECASE),
    re.compile(r"no cards in", re.IGNORECASE),
    re.compile(r"no creatures", re.IGNORECASE),
    re.compile(r"20 or more", re.IGNORECASE),
]


def _extract_condition(text: str) -> Optional[Condition]:
    """Look for an 'if' clause and classify its restrictiveness."""
    lower = text.lower()

    # Pattern: ", if <condition>"
    m = re.search(r",\s*if\s+(.+?)\.?\s*$", text, re.IGNORECASE)
    if not m:
        return None

    raw = m.group(1).strip()
    restrictiveness = "mild"

    for pat in _SEVERE_PATTERNS:
        if pat.search(raw):
            restrictiveness = "severe"
            break

    return Condition(
        kind="if_clause",
        raw=raw,
        restrictiveness=restrictiveness,
    )
