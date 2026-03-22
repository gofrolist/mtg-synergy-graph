"""Oracle text parser: extracts structured abilities from MTG card text.

Three phases:
1. Keyword extraction (from Scryfall keywords field)
2. Pattern matching (triggered, activated, static, replacement, mana)
3. Effect tagging (maps effect text to provides/wants vocabulary)
"""

import re
import json
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")

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


# Keywords that map to specific effect tags
KEYWORD_EFFECT_TAGS = {
    "toxic": ["poison-counter-placement"],
    "infect": ["infect", "poison-counter-placement"],
    "proliferate": ["proliferate", "counter-placement"],
    "lifelink": ["life-gain"],
    "deathtouch": ["spot-removal"],
    "haste": ["evasion"],
    "flying": ["evasion"],
    "trample": ["evasion"],
    "menace": ["evasion"],
    "vigilance": ["board-protection"],
    "hexproof": ["board-protection"],
    "indestructible": ["board-protection"],
    "ward": ["board-protection"],
}


def _extract_keywords(card):
    """Phase 1: Extract keyword abilities from the card's keywords field."""
    abilities = []
    keywords = card.get("keywords") or []
    for kw in keywords:
        kw_lower = kw.lower()
        zone = KEYWORD_ZONES.get(kw_lower, "battlefield")
        effect_tags = KEYWORD_EFFECT_TAGS.get(kw_lower)
        abilities.append({
            "ability_type": "keyword",
            "trigger_condition": None,
            "trigger_tags": None,
            "cost": None,
            "effect": kw_lower,
            "effect_tags": effect_tags,
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


# ── Phase 3: Effect text -> tag mappings ──

EFFECT_TAG_PATTERNS = [
    (re.compile(r'create.*token', re.I), "token-generation"),
    (re.compile(r'draw.*card|draws.*card', re.I), "card-draw"),
    (re.compile(r'destroy.*(?:creature|permanent|artifact|enchantment)', re.I), "spot-removal"),
    (re.compile(r'deals?\s+\d+\s+damage|damage to', re.I), "direct-damage"),
    (re.compile(r'return.*from.*graveyard|return.*to the battlefield', re.I), "graveyard-recursion"),
    (re.compile(r'\+1/\+1 counter', re.I), "counter-placement"),
    (re.compile(r'gain.*life|gains?\s+\d+\s+life', re.I), "life-gain"),
    (re.compile(r'lose.*life|loses?\s+\d+\s+life', re.I), "life-drain"),
    (re.compile(r'exile.*(?:creature|permanent|card)', re.I), "exile-removal"),
    (re.compile(r'search.*library', re.I), "tutor"),
    (re.compile(r'add \{', re.I), "mana-acceleration"),
    (re.compile(r'scry|look at the top', re.I), "card-filtering"),
    (re.compile(r'mill|put.*from.*library into.*graveyard', re.I), "mill"),
    (re.compile(r'discard', re.I), "discard"),
    (re.compile(r'counter target.*spell', re.I), "counterspell"),
    (re.compile(r'tap.*(?:creature|permanent)|doesn\'t untap', re.I), "tap-control"),
    (re.compile(r'untap', re.I), "untap"),
    (re.compile(r'copy.*(?:spell|creature|permanent)', re.I), "copy-effect"),
    (re.compile(r'each opponent|all opponents', re.I), "group-damage"),
    (re.compile(r'get[s]?\s+[+\-]\d+/[+\-]\d+', re.I), "creature-pump"),
    (re.compile(r'additional combat', re.I), "extra-combat"),
    (re.compile(r'extra turn', re.I), "extra-turn"),
    (re.compile(r'can\'t be blocked|unblockable', re.I), "evasion"),
    (re.compile(r'indestructible|hexproof|shroud', re.I), "board-protection"),
    (re.compile(r'treasure token', re.I), "treasure-generation"),
    (re.compile(r'food token', re.I), "food-generation"),
    (re.compile(r'clue token', re.I), "clue-generation"),
    (re.compile(r'equip|reconfigure', re.I), "equipment-synergy"),
    (re.compile(r'enchant|aura', re.I), "aura-synergy"),
    (re.compile(r'proliferate', re.I), "proliferate"),
    (re.compile(r'transform|flip', re.I), "transform"),
]

# Trigger condition -> tag mappings
TRIGGER_TAG_PATTERNS = [
    (re.compile(r'creature.*enters|enters the battlefield', re.I), "creature-etb"),
    (re.compile(r'creature.*dies|a creature.*is put into a graveyard', re.I), "creature-death"),
    (re.compile(r'you gain life|whenever you gain', re.I), "life-gain-events"),
    (re.compile(r'you cast.*spell|whenever you cast', re.I), "spell-cast"),
    (re.compile(r'deals.*combat damage|whenever.*deals damage', re.I), "combat-damage-events"),
    (re.compile(r'attacks|declared as an attacker', re.I), "attack-events"),
    (re.compile(r'becomes? the target|target.*you control', re.I), "targeting-events"),
    (re.compile(r'draw.*card|whenever you draw', re.I), "draw-events"),
    (re.compile(r'discard|whenever.*discard', re.I), "discard-events"),
    (re.compile(r'sacrifice|whenever.*sacrifice', re.I), "sacrifice-events"),
    (re.compile(r'counter.*is.*placed|counter.*is.*put', re.I), "counter-placement-events"),
    (re.compile(r'token.*created|token.*enters', re.I), "token-events"),
    (re.compile(r'beginning of your upkeep', re.I), "upkeep-trigger"),
    (re.compile(r'beginning of your end step', re.I), "end-step-trigger"),
    (re.compile(r'land.*enters|play a land', re.I), "landfall"),
    (re.compile(r'leaves the battlefield', re.I), "leaves-battlefield"),
    (re.compile(r'from.*graveyard|put into.*graveyard from', re.I), "graveyard-events"),
]

# Cost -> tag mappings (for activated abilities)
COST_TAG_PATTERNS = [
    (re.compile(r'[Ss]acrifice', re.I), "sacrifice-outlet"),
    (re.compile(r'[Dd]iscard', re.I), "discard-outlet"),
    (re.compile(r'[Ee]xile.*from.*graveyard', re.I), "graveyard-exile-cost"),
    (re.compile(r'[Pp]ay.*life|\{[WUBRG]/P\}', re.I), "life-payment"),
    (re.compile(r'\{[Tt]\}|[Tt]ap', re.I), "tap-cost"),
]


def _tag_effect(text):
    """Map effect text to tags using pattern matching."""
    if not text:
        return []
    tags = []
    for pattern, tag in EFFECT_TAG_PATTERNS:
        if pattern.search(text):
            tags.append(tag)
    return tags


def _tag_trigger(text):
    """Map trigger condition text to tags."""
    if not text:
        return []
    tags = []
    for pattern, tag in TRIGGER_TAG_PATTERNS:
        if pattern.search(text):
            tags.append(tag)
    return tags


def _tag_cost(text):
    """Map activation cost text to tags."""
    if not text:
        return []
    tags = []
    for pattern, tag in COST_TAG_PATTERNS:
        if pattern.search(text):
            tags.append(tag)
    return tags


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

    # Phase 3: Tag effects and triggers
    for ab in all_abilities:
        # Tag effects (merge with any pre-existing tags from keyword extraction)
        effect_tags = _tag_effect(ab.get("effect") or "")
        cost_tags = _tag_cost(ab.get("cost") or "")
        effect_tags.extend(cost_tags)
        # Preserve keyword-derived effect_tags (set in Phase 1)
        existing = ab.get("effect_tags") or []
        if existing:
            effect_tags = list(set(existing + effect_tags))
        ab["effect_tags"] = effect_tags if effect_tags else None

        # Tag trigger conditions
        if ab.get("trigger_condition"):
            trigger_tags = _tag_trigger(ab["trigger_condition"])
            ab["trigger_tags"] = trigger_tags if trigger_tags else None

    # Assign sequential indices
    for i, ab in enumerate(all_abilities):
        ab["ability_index"] = i

    return all_abilities


def save_abilities_to_db(parsed_cards, db_path=None):
    """Save parsed abilities to the abilities table.

    Args:
        parsed_cards: list of (oracle_id, abilities_list) tuples
        db_path: optional DB path override
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)

    for oracle_id, abilities in parsed_cards:
        # Clear old abilities for this card
        conn.execute("DELETE FROM abilities WHERE oracle_id = ?", (oracle_id,))

        for ab in abilities:
            conn.execute("""
                INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, cost, effect, effect_tags, zone, targets, is_mana_ability)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                oracle_id,
                ab["ability_index"],
                ab["ability_type"],
                ab.get("trigger_condition"),
                json.dumps(ab["trigger_tags"]) if ab.get("trigger_tags") else None,
                ab.get("cost"),
                ab.get("effect"),
                json.dumps(ab["effect_tags"]) if ab.get("effect_tags") else None,
                ab.get("zone", "battlefield"),
                ab.get("targets"),
                1 if ab.get("is_mana_ability") else 0,
            ))

    conn.commit()
    conn.close()


def parse_all_cards(db_path=None):
    """Parse all cards in the DB and save abilities.

    Returns (total_parsed, low_confidence_count).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cards = conn.execute("""
        SELECT oracle_id, name, type_line, oracle_text, keywords
        FROM cards WHERE oracle_text IS NOT NULL AND oracle_text != ''
    """).fetchall()
    conn.close()

    parsed = []
    low_confidence = 0

    for row in cards:
        card = {
            "oracle_id": row["oracle_id"],
            "name": row["name"],
            "type_line": row["type_line"],
            "oracle_text": row["oracle_text"],
            "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
        }
        abilities = parse_card(card)
        parsed.append((row["oracle_id"], abilities))

        # Check confidence: <50% of non-keyword abilities got tags
        non_kw = [a for a in abilities if a["ability_type"] != "keyword"]
        if non_kw:
            tagged = sum(1 for a in non_kw if a.get("effect_tags") or a.get("trigger_tags"))
            if tagged / len(non_kw) < 0.5:
                low_confidence += 1

    # Save in batches
    batch_size = 500
    for i in range(0, len(parsed), batch_size):
        save_abilities_to_db(parsed[i:i + batch_size], db_path)

    return len(parsed), low_confidence


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Parse oracle text into structured abilities")
    parser.add_argument("--db", default=None, help="DB path (default: data/tags.db)")
    parser.add_argument("--card", default=None, help="Parse a single card by name (for inspection)")
    args = parser.parse_args()

    db = args.db or DB_PATH

    if args.card:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM cards WHERE name = ?", (args.card,)).fetchone()
        conn.close()
        if not row:
            print(f"Card not found: {args.card}")
            exit(1)
        card = dict(row)
        card["keywords"] = json.loads(card["keywords"]) if card["keywords"] else []
        abilities = parse_card(card)
        print(f"\n{card['name']} ({card['type_line']})")
        print(f"Oracle: {card['oracle_text']}\n")
        for ab in abilities:
            print(f"  [{ab['ability_type']}] {ab.get('trigger_condition') or ''}")
            if ab.get('cost'):
                print(f"    Cost: {ab['cost']}")
            print(f"    Effect: {ab['effect']}")
            if ab.get('effect_tags'):
                print(f"    Effect tags: {ab['effect_tags']}")
            if ab.get('trigger_tags'):
                print(f"    Trigger tags: {ab['trigger_tags']}")
            print()
    else:
        print(f"Parsing all cards in {db}...")
        total, low_conf = parse_all_cards(db)
        print(f"Parsed {total} cards. Low confidence: {low_conf} ({low_conf*100//max(total,1)}%)")
