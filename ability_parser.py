"""Oracle text parser: extracts structured abilities from MTG card text.

Three phases:
1. Keyword extraction (from Scryfall keywords field)
2. Pattern matching (triggered, activated, static, replacement, mana)
3. Effect tagging (maps effect text to provides/wants vocabulary)
"""

import re
import json

# MTG keywords recognized by Scryfall (subset — the keywords field is authoritative)
KEYWORD_ZONES = {
    "cycling": "hand",
    "unearth": "graveyard",
    "escape": "graveyard",
    "flashback": "graveyard",
    "retrace": "graveyard",
    "jump-start": "graveyard",
    "embalm": "graveyard",
    "eternalize": "graveyard",
    "disturb": "graveyard",
    "encore": "graveyard",
    "scavenge": "graveyard",
    "channel": "hand",
    "ninjutsu": "hand",
    "foretell": "hand",
    "forecast": "hand",
    "miracle": "hand",
    "madness": "hand",
    "transmute": "hand",
}


def _strip_reminder_text(text):
    """Remove reminder text in parentheses."""
    return re.sub(r'\([^)]*\)', '', text).strip()


def _split_faces(oracle_text):
    """Split double-faced/adventure cards on ' // ' separator.

    Returns list of (face_index, text) tuples.
    """
    if " // " in oracle_text:
        faces = oracle_text.split(" // ")
        return list(enumerate(faces))
    return [(0, oracle_text)]


def _extract_keywords(card):
    """Phase 1: Extract keyword abilities from the card's keywords field."""
    abilities = []
    keywords = card.get("keywords") or []
    for kw in keywords:
        kw_lower = kw.lower()
        zone = KEYWORD_ZONES.get(kw_lower, "battlefield")
        abilities.append({
            "ability_type": "keyword",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": None,
            "effect": kw_lower,
            "effect_tags": None,
            "zone": zone,
            "targets": None,
            "is_mana_ability": False,
        })
    return abilities


def _is_keyword_only_paragraph(para, keywords):
    """Check if a paragraph is just a list of keywords."""
    kw_lower = {kw.lower() for kw in keywords}
    # Handle "Flying, vigilance" or "Flying" or "Haste, trample, lifelink"
    parts = [p.strip().lower().rstrip('.') for p in re.split(r'[,\n]', para)]
    return all(p in kw_lower or p == "" for p in parts)


def _parse_paragraph(para):
    """Parse a single paragraph into an ability dict. Phase 2 placeholder."""
    return {
        "ability_type": "static",
        "trigger_condition": None,
        "trigger_tags": None,
        "cost": None,
        "effect": para,
        "effect_tags": None,
        "zone": "battlefield",
        "targets": None,
        "is_mana_ability": False,
    }


def parse_card(card):
    """Parse a single card's oracle text into structured abilities.

    Args:
        card: dict with oracle_id, name, oracle_text, keywords

    Returns:
        list of ability dicts, each with ability_index set
    """
    oracle_text = card.get("oracle_text") or ""
    if not oracle_text:
        return []

    all_abilities = []

    # Phase 1: Keywords from Scryfall field
    all_abilities.extend(_extract_keywords(card))

    # Split faces for DFC/adventure cards
    faces = _split_faces(oracle_text)

    for face_idx, face_text in faces:
        # Strip reminder text
        clean = _strip_reminder_text(face_text)

        # Split into paragraphs (each = one ability in MTG rules)
        paragraphs = [p.strip() for p in clean.split("\n") if p.strip()]

        for para in paragraphs:
            # Skip if this paragraph is just keywords we already extracted
            if _is_keyword_only_paragraph(para, card.get("keywords") or []):
                continue

            ability = _parse_paragraph(para)
            if ability:
                all_abilities.append(ability)

    # Assign sequential indices
    for i, ab in enumerate(all_abilities):
        ab["ability_index"] = i

    return all_abilities
