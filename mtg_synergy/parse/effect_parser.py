"""Effect verb + target + amount extraction from MTG oracle text."""
from __future__ import annotations

import re
from typing import Optional

from mtg_synergy.parse.ast_types import Effect, Amount, ObjectFilter, TokenDef, ScalesWith

# Known artifact token subtypes (no P/T)
_ARTIFACT_TOKENS = {"Treasure", "Clue", "Food", "Blood", "Map", "Gold"}

# Number words
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# MTG keywords that can appear on tokens or be granted
_KEYWORDS = {
    "deathtouch", "defender", "double strike", "first strike", "flash",
    "flying", "haste", "hexproof", "indestructible", "lifelink", "menace",
    "reach", "trample", "vigilance", "ward", "shroud", "fear", "intimidate",
    "protection", "prowess",
}

# Card types for target parsing
_CARD_TYPES = {
    "creature", "artifact", "enchantment", "instant", "sorcery",
    "planeswalker", "land", "permanent", "nonland permanent", "spell",
}


def parse_effects(effect_text: str) -> list[Effect]:
    """Parse effect text into a list of Effect AST nodes."""
    text = effect_text.strip().rstrip(".")

    # Try multi-keyword grant first ("has haste and shroud", "has flying, haste, and trample")
    # This avoids _split_effects breaking "haste and shroud" into separate non-parseable parts
    multi = _try_grant_multiple_keywords(text)
    if len(multi) >= 2:
        return multi

    parts = _split_effects(text)
    results = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        effect = _parse_single_effect(part)
        if effect:
            results.append(effect)
    return results


def _split_effects(text: str) -> list[str]:
    """Split on ' and ' only when both sides look like separate effects."""
    # Don't split on "and" inside card type references like "instant and sorcery"
    parts = re.split(r'\band\b', text)
    if len(parts) <= 1:
        return [text]

    # Try to merge parts that aren't standalone effects
    merged = []
    i = 0
    while i < len(parts):
        candidate = parts[i].strip()
        # Check if next part looks like an effect start
        if i + 1 < len(parts):
            next_part = parts[i + 1].strip()
            # If both sides look like effects, keep them split
            if _looks_like_effect(candidate) and _looks_like_effect(next_part):
                merged.append(candidate)
                i += 1
                continue
            else:
                # Rejoin with "and"
                candidate = candidate + " and " + next_part
                i += 2
                # Keep merging if needed
                while i < len(parts):
                    next_part = parts[i].strip()
                    if _looks_like_effect(next_part):
                        merged.append(candidate)
                        candidate = next_part
                        i += 1
                        break
                    else:
                        candidate = candidate + " and " + next_part
                        i += 1
                merged.append(candidate)
                continue
        merged.append(candidate)
        i += 1
    return merged if merged else [text]


def _looks_like_effect(text: str) -> bool:
    """Check if text looks like a standalone MTG effect."""
    t = text.strip().lower()
    effect_starts = [
        "you ", "target ", "each ", "draw ", "create ", "destroy ", "exile ",
        "return ", "put ", "search ", "add ", "scry ", "untap ", "tap ",
        "sacrifice ", "mill ", "discard ", "counter ",
    ]
    for start in effect_starts:
        if t.startswith(start):
            return True
    # "X loses/gains" pattern
    if re.match(r'\w+\s+(loses?|gains?)\s+\d', t):
        return True
    return False


def _parse_single_effect(text: str) -> Optional[Effect]:
    """Try each verb pattern, most specific first."""
    parsers = [
        _try_create_token,
        _try_deal_damage,
        _try_draw,
        _try_destroy,
        _try_exile,
        _try_return,
        _try_put_counter,
        _try_gain_life,
        _try_lose_life,
        _try_sacrifice,
        _try_discard,
        _try_search,
        _try_mill,
        _try_add_mana,
        _try_pump,
        _try_grant_keyword,
        _try_prevent,
        _try_counter,
        _try_tap,
        _try_scry,
        _try_untap,
    ]
    for parser in parsers:
        result = parser(text)
        if result:
            return result
    return None


# ---- Token creation ----

def _try_create_token(text: str) -> Optional[Effect]:
    m = re.match(r'[Cc]reate\s+(.+?)(?:\s+tokens?(?:\s+.*)?)', text, re.IGNORECASE)
    if not m:
        m = re.match(r'[Cc]reate\s+(.+?\s+token(?:\s+.*)?)', text, re.IGNORECASE)
    if not m:
        return None

    # Pass full text after "Create" for keyword extraction, but use
    # the part before "token(s)" for amount/subtype parsing
    full_after_create = re.sub(r'^[Cc]reate\s+', '', text)
    amount = _parse_amount_prefix(full_after_create)
    token = _parse_token_def(full_after_create)
    scaling = _extract_scaling(text)

    if scaling and amount.value == "X":
        amount.scales_with = scaling

    return Effect(verb="create", amount=amount, token=token)


def _parse_token_def(text: str) -> TokenDef:
    """Extract token definition: P/T, color, subtype, keywords."""
    token = TokenDef()

    # Check for keywords via "with ..."
    kw_match = re.search(r'\bwith\s+(.+)', text, re.IGNORECASE)
    if kw_match:
        kw_text = kw_match.group(1).lower().strip().rstrip(".")
        found_kws = []
        for kw in _KEYWORDS:
            if kw in kw_text:
                found_kws.append(kw)
        token.keywords = found_kws
        # Remove keyword clause for further parsing
        text = text[:kw_match.start()]

    # P/T
    pt_match = re.search(r'(\d+)/(\d+)', text)
    if pt_match:
        token.power = int(pt_match.group(1))
        token.toughness = int(pt_match.group(2))

    # Check for known artifact tokens
    for art in _ARTIFACT_TOKENS:
        if art in text:
            token.subtype = art
            token.card_type = "artifact"
            return token

    # Find subtype: capitalized word before "creature" or at end
    # Pattern: optional color words, then Subtype, then optional "creature" / "token"
    sub_match = re.search(
        r'(?:\d+/\d+\s+)?(?:(?:white|blue|black|red|green|colorless)\s+)?'
        r'([A-Z][a-z]+)\s+(?:creature|token)',
        text
    )
    if not sub_match:
        # Try: just a capitalized word after P/T and color (no creature/token suffix)
        sub_match = re.search(
            r'(?:\d+/\d+\s+)?(?:(?:white|blue|black|red|green|colorless)\s+)?'
            r'([A-Z][a-z]+)\s*$',
            text.strip()
        )
    if sub_match:
        token.subtype = sub_match.group(1)
        token.card_type = "creature"

    # Color
    colors = {"white", "blue", "black", "red", "green", "colorless"}
    for color in colors:
        if color in text.lower():
            token.color = color
            break

    return token


# ---- Amount parsing ----

def _parse_amount_prefix(text: str) -> Amount:
    """Extract amount from the beginning of text."""
    t = text.strip().lower()

    # "X" amount
    if re.match(r'^x\b', t):
        return Amount(value="X")

    # Number word at start
    for word, val in _NUMBER_WORDS.items():
        if re.match(rf'^{re.escape(word)}\b', t):
            return Amount(value=val)

    # Digit at start
    m = re.match(r'^(\d+)\b', t)
    if m:
        return Amount(value=int(m.group(1)))

    return Amount(value=1)


def _parse_amount_in_text(text: str) -> Amount:
    """Find a number anywhere in the text."""
    t = text.lower()

    # Digits
    m = re.search(r'(\d+)', t)
    if m:
        return Amount(value=int(m.group(1)))

    # Number words
    for word, val in _NUMBER_WORDS.items():
        if word != "a" and word != "an":  # too common
            if re.search(rf'\b{re.escape(word)}\b', t):
                return Amount(value=val)

    return Amount(value=1)


# ---- Target parsing ----

def _parse_target(text: str) -> Optional[ObjectFilter]:
    """Parse target specification from text."""
    t = text.lower()

    # "each opponent" / "each player"
    if "each opponent" in t:
        return ObjectFilter(controller="opponent")
    if "target opponent" in t:
        return ObjectFilter(controller="opponent")
    if "target player" in t:
        return ObjectFilter(controller="any")

    # "each creature you control"
    m = re.search(r'(?:each|all)\s+(\w+(?:\s+\w+)?)\s+you\s+control', t)
    if m:
        card_type = _normalize_card_type(m.group(1))
        return ObjectFilter(card_type=card_type, controller="you")

    # "creatures you control"
    m = re.search(r'(\w+)s?\s+you\s+control', t)
    if m:
        card_type = _normalize_card_type(m.group(1))
        if card_type:
            return ObjectFilter(card_type=card_type, controller="you")

    # "target creature/permanent/artifact/etc."
    m = re.search(r'target\s+([\w\s]+?)(?:\s+card|\s+to\b|\s+from\b|\s*$|\s*,)', t)
    if m:
        card_type = _normalize_card_type(m.group(1).strip())
        if card_type:
            return ObjectFilter(card_type=card_type)

    return None


def _normalize_card_type(text: str) -> Optional[str]:
    """Normalize a card type string."""
    t = text.strip().lower().rstrip("s")
    # Map plurals and variants
    if t in ("creature", "creatures"):
        return "creature"
    for ct in _CARD_TYPES:
        if t == ct or t == ct + "s" or t.rstrip("s") == ct:
            return ct
    return t if t else None


# ---- Scaling ----

def _extract_scaling(text: str) -> Optional[ScalesWith]:
    """Extract 'where X is the number of...' or 'for each...' scaling."""
    m = re.search(r'where X is the number of\s+(.+?)(?:\.|$)', text, re.IGNORECASE)
    if m:
        return ScalesWith(what=m.group(1).strip(), how="linear")

    m = re.search(r'for each\s+(.+?)(?:\.|$)', text, re.IGNORECASE)
    if m:
        return ScalesWith(what=m.group(1).strip(), how="linear")

    return None


# ---- Verb parsers ----

def _try_deal_damage(text: str) -> Optional[Effect]:
    # "X deals N damage to ..." — subject can be multi-word (e.g. "Syr Konrad, the Grim")
    m = re.match(r'(?:.*?\s+)?deals\s+(\d+|X)\s+damage\s+to\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    raw_amount = m.group(1)
    amount_val = "X" if raw_amount == "X" else int(raw_amount)
    target = _parse_target(m.group(2))
    if not target:
        target = ObjectFilter()
    return Effect(verb="deal_damage", amount=Amount(value=amount_val), target=target)


def _try_draw(text: str) -> Optional[Effect]:
    m = re.match(r'(?:you\s+(?:may\s+)?)?[Dd]raw\s+(.+?)(?:\s+cards?|\s+unless\b|\s*$)', text, re.IGNORECASE)
    if not m:
        return None
    amount = _parse_amount_prefix(m.group(1))
    return Effect(verb="draw", amount=amount)


def _try_destroy(text: str) -> Optional[Effect]:
    m = re.match(r'[Dd]estroy\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    target = _parse_target(m.group(1))
    return Effect(verb="destroy", target=target)


def _try_exile(text: str) -> Optional[Effect]:
    m = re.match(r'[Ee]xile\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    target = _parse_target(m.group(1))
    return Effect(verb="exile", target=target)


def _try_return(text: str) -> Optional[Effect]:
    m = re.match(r'[Rr]eturn\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None

    body = m.group(1)
    target = _parse_target(body)

    dest = None
    if "to the battlefield" in body.lower() or "onto the battlefield" in body.lower():
        dest = "battlefield"
    elif "to its owner's hand" in body.lower() or "to your hand" in body.lower() or "to their owner's hand" in body.lower():
        dest = "hand"

    return Effect(verb="return", target=target, destination=dest)


def _try_put_counter(text: str) -> Optional[Effect]:
    m = re.match(r'[Pp]ut\s+(?:a|an|\d+)\s+[+\-]\d+/[+\-]\d+\s+counters?\s+on\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    target = _parse_target(m.group(1))
    if not target:
        # Try parsing "each creature you control" directly
        rest = m.group(1).lower()
        if "each" in rest or "all" in rest:
            target = _parse_target(rest)
        if not target:
            target = ObjectFilter()
    return Effect(verb="put_counter", target=target)


def _try_gain_life(text: str) -> Optional[Effect]:
    m = re.search(r'gains?\s+(\d+|X)\s+life', text, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1)
    val = "X" if raw == "X" else int(raw)
    return Effect(verb="gain_life", amount=Amount(value=val))


def _try_lose_life(text: str) -> Optional[Effect]:
    m = re.search(r'loses?\s+(\d+|X)\s+life', text, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1)
    val = "X" if raw == "X" else int(raw)
    target = _parse_target(text)
    return Effect(verb="lose_life", amount=Amount(value=val), target=target)


def _try_sacrifice(text: str) -> Optional[Effect]:
    m = re.search(r'sacrifices?\s+', text, re.IGNORECASE)
    if not m:
        return None
    # Who sacrifices?
    pre = text[:m.start()].strip().lower()
    target = None
    if "each opponent" in pre or "opponent" in pre:
        target = ObjectFilter(controller="opponent")
    elif "you" in pre:
        target = ObjectFilter(controller="you")
    return Effect(verb="sacrifice", target=target)


def _try_discard(text: str) -> Optional[Effect]:
    m = re.match(r'[Dd]iscard\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    amount = _parse_amount_prefix(m.group(1))
    return Effect(verb="discard", amount=amount)


def _try_search(text: str) -> Optional[Effect]:
    if re.match(r'[Ss]earch\s+', text):
        return Effect(verb="search")
    return None


def _try_mill(text: str) -> Optional[Effect]:
    m = re.search(r'mills?\s+(.+?)(?:\s+cards?)', text, re.IGNORECASE)
    if not m:
        return None
    amount = _parse_amount_prefix(m.group(1))
    return Effect(verb="mill", amount=amount)


def _try_add_mana(text: str) -> Optional[Effect]:
    if re.match(r'[Aa]dd\s+\{', text):
        return Effect(verb="add_mana")
    # Word-form mana: "Add one mana of any color"
    if re.match(r'[Aa]dd\s+(?:one|two|three|\d+)\s+mana', text):
        return Effect(verb="add_mana")
    return None


def _try_pump(text: str) -> Optional[Effect]:
    m = re.search(r'gets?\s+[+\-]\d+/[+\-]\d+', text, re.IGNORECASE)
    if not m:
        return None
    target = _parse_target(text)
    return Effect(verb="pump", target=target)


def _try_grant_keyword(text: str) -> Optional[Effect]:
    m = re.search(r'(?:have|has|gains?)\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    rest = m.group(1).lower().strip().rstrip(".")
    target = _parse_target(text)
    # Check if any keyword matches (single keyword, first match)
    for kw in _KEYWORDS:
        if rest.startswith(kw):
            return Effect(verb="grant_keyword", keyword=kw, target=target)
    return None


def _try_grant_multiple_keywords(text: str) -> list[Effect]:
    """Parse 'has/have/gains KW1 and KW2 [and KW3 ...]' into multiple grant_keyword effects."""
    m = re.search(r'(?:have|has|gains?)\s+(.+)', text, re.IGNORECASE)
    if not m:
        return []
    rest = m.group(1).lower().strip().rstrip(".")
    target = _parse_target(text)
    # Split on " and " and "," to find multiple keywords
    parts = re.split(r'\s*(?:,\s*and\s+|,\s+|\s+and\s+)\s*', rest)
    found = []
    for part in parts:
        part = part.strip()
        for kw in _KEYWORDS:
            if part.startswith(kw):
                found.append(Effect(verb="grant_keyword", keyword=kw, target=target))
                break
    return found


def _try_prevent(text: str) -> Optional[Effect]:
    """Parse 'can't' / 'cannot' abilities into prevent effects."""
    m = re.search(r"(?:can'?t|cannot)\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    action_text = m.group(1).strip().rstrip(".")
    # Normalize the blocked action to a compact keyword
    action_lower = action_text.lower()
    kw = action_lower.replace(" ", "_")
    # Clean up common suffixes
    kw = re.sub(r'_unless.*$', '', kw)
    kw = re.sub(r'_except.*$', '', kw)
    # Truncate overly long keywords
    if len(kw) > 40:
        kw = kw[:40]
    target = _parse_target(text)
    return Effect(verb="prevent", keyword=kw, target=target)


def _try_counter(text: str) -> Optional[Effect]:
    """Parse 'Counter target spell' effects."""
    m = re.match(r'[Cc]ounter\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    target = _parse_target(m.group(1))
    return Effect(verb="counter", target=target)


def _try_tap(text: str) -> Optional[Effect]:
    """Parse 'Tap target creature' effects."""
    m = re.match(r'[Tt]ap\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    target = _parse_target(m.group(1))
    if target:
        return Effect(verb="tap", target=target)
    return None


def _try_scry(text: str) -> Optional[Effect]:
    m = re.match(r'[Ss]cry\s+(\d+|X)', text, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1)
    val = "X" if raw == "X" else int(raw)
    return Effect(verb="scry", amount=Amount(value=val))


def _try_untap(text: str) -> Optional[Effect]:
    m = re.match(r'[Uu]ntap\s+(.+)', text, re.IGNORECASE)
    if not m:
        return None
    target = _parse_target(m.group(1))
    return Effect(verb="untap", target=target)
