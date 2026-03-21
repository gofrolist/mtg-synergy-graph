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


# Planeswalker loyalty cost pattern: +N, -N, 0
_LOYALTY_RE = re.compile(r'^([+\-\u2212]?\d+): (.+)$')

# Saga chapter pattern: I, II, III, IV, etc.
_SAGA_RE = re.compile(r'^(I{1,3}V?|IV|V|VI{0,3}) \u2014 (.+)$')

# Triggered ability: When/Whenever/At ...
_TRIGGERED_RE = re.compile(r'^(When(?:ever)?|At) (.+)')

# Activated ability: cost: effect (cost must contain mana symbol, tap, or sacrifice-like word)
_ACTIVATED_RE = re.compile(r'^(.+?): (.+)$')
_COST_INDICATORS = re.compile(r'\{|(?:^|\W)[Tt]ap\b|Sacrifice|Remove|Discard|Pay|Exile .* from')

# Replacement effect
_REPLACEMENT_RE = re.compile(r'\bwould\b.*\binstead\b', re.IGNORECASE)

# Mana ability: effect produces mana
_MANA_EFFECT_RE = re.compile(r'[Aa]dd \{')


def _split_trigger_effect(text):
    """Split a triggered ability into trigger_condition and effect.

    Handles 'if' clauses by greedily including them in the trigger.
    'Whenever X, if Y, effect' -> trigger='X, if Y', effect='effect'
    'Whenever X, effect' -> trigger='X', effect='effect'
    """
    match = _TRIGGERED_RE.match(text)
    if not match:
        return text, text

    trigger_word = match.group(1)
    rest = match.group(2)

    # Split on commas, looking for the main effect
    effect_verbs = r'(?:put|draw|create|destroy|exile|return|deal|add|gain|lose|sacrifice|search|discard|counter|tap|untap|each|that|it|you|target|all|choose)'

    parts = rest.split(', ')
    for i in range(len(parts) - 1, 0, -1):
        candidate = parts[i].strip()
        if re.match(effect_verbs, candidate, re.IGNORECASE):
            trigger = ', '.join(parts[:i])
            effect = ', '.join(parts[i:])
            return f"{trigger_word} {trigger}", effect

    # Fallback: first comma split
    if ', ' in rest:
        idx = rest.index(', ')
        return f"{trigger_word} {rest[:idx]}", rest[idx + 2:]

    return f"{trigger_word} {rest}", rest


def _extract_targets(text):
    """Extract target description from effect text."""
    match = re.search(r'target ([\w\s]+?)(?:\.|,|$)', text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _infer_zone(text):
    """Infer which zone an ability operates from."""
    text_lower = text.lower()
    if 'from your graveyard' in text_lower or 'from a graveyard' in text_lower:
        return 'graveyard'
    if 'from your hand' in text_lower:
        return 'hand'
    if 'from exile' in text_lower:
        return 'exile'
    return 'battlefield'


def _parse_paragraph(para):
    """Parse a single oracle text paragraph into a structured ability."""

    # Check for saga chapters
    saga_match = _SAGA_RE.match(para)
    if saga_match:
        chapter = saga_match.group(1)
        effect = saga_match.group(2)
        return {
            "ability_type": "triggered",
            "trigger_condition": f"chapter {chapter}",
            "trigger_tags": None,
            "cost": None,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": False,
        }

    # Check for planeswalker loyalty abilities
    loyalty_match = _LOYALTY_RE.match(para)
    if loyalty_match:
        cost = loyalty_match.group(1)
        effect = loyalty_match.group(2)
        return {
            "ability_type": "activated",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": cost,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": False,
        }

    # Check for replacement effects
    if _REPLACEMENT_RE.search(para):
        return {
            "ability_type": "replacement",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": None,
            "effect": para,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": None,
            "is_mana_ability": False,
        }

    # Check for triggered abilities
    if _TRIGGERED_RE.match(para):
        trigger, effect = _split_trigger_effect(para)
        return {
            "ability_type": "triggered",
            "trigger_condition": trigger,
            "trigger_tags": None,
            "cost": None,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": False,
        }

    # Check for activated abilities
    activated_match = _ACTIVATED_RE.match(para)
    if activated_match and _COST_INDICATORS.search(activated_match.group(1)):
        cost = activated_match.group(1)
        effect = activated_match.group(2)
        is_mana = bool(_MANA_EFFECT_RE.search(effect))
        return {
            "ability_type": "mana" if is_mana else "activated",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": cost,
            "effect": effect,
            "effect_tags": None,
            "zone": "battlefield",
            "targets": _extract_targets(effect),
            "is_mana_ability": is_mana,
        }

    # Default: static ability
    return {
        "ability_type": "static",
        "trigger_condition": None,
        "trigger_tags": None,
        "cost": None,
        "effect": para,
        "effect_tags": None,
        "zone": _infer_zone(para),
        "targets": _extract_targets(para),
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
