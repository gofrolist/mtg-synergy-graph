"""
prompt_builder.py
Assembles the LLM tagger prompt dynamically from:
  1. Base instructions + schema
  2. Provides/wants vocabulary (derived from tag_registry.json)
  3. Category vocabulary
  4. Corrections (injected as rules the model must follow)
  5. Few-shot examples (best tagged cards from golden dataset)
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
REGISTRY_PATH = BASE_DIR / "tag_registry.json"

SCHEMA = """{
  "name": "card name",
  "role": "ramp | draw | removal | protection | enabler | threat | utility | land",
  "provides": ["what this card gives to the deck — abstract capabilities, e.g. mana-acceleration, counter-amplification, trigger-doubling"],
  "wants": ["what other cards or conditions make this card better — abstract tags, e.g. counter-placement-events, creature-etb-triggers"]
}"""

def _load_tag_vocab() -> tuple[list[str], list[str]]:
    """Load canonical provides/wants tags from tag_registry.json."""
    with open(REGISTRY_PATH) as f:
        registry = json.load(f)
    provides = sorted(t for t, d in registry["tags"].items() if d["kind"] in ("provides", "both"))
    wants = sorted(t for t, d in registry["tags"].items() if d["kind"] in ("wants", "both"))
    return provides, wants


def _build_tag_vocab_block() -> str:
    """Build the provides/wants vocabulary block for the LLM prompt."""
    provides, wants = _load_tag_vocab()
    lines = [
        "PROVIDES TAGS (use these for 'provides' field — extend only if truly needed):",
        "  " + ", ".join(provides),
        "",
        "WANTS TAGS (use these for 'wants' field — extend only if truly needed):",
        "  " + ", ".join(wants),
    ]
    return "\n".join(lines)


# Cache at module level — registry doesn't change during a batch run
_TAG_VOCAB_BLOCK = _build_tag_vocab_block()



_SYSTEM_PROMPT_BASE = """You are an expert Magic: The Gathering card analyst building a synergy knowledge graph.
Your job is to analyze MTG cards and produce structured role/provides/wants tags that become directed edges in a synergy graph.

NOTE: Synergy tags (function tags) are provided separately from Scryfall's community tagger.
Your focus is on: role classification, provides (what the card gives), and wants (what makes it better).

Core principles:
- 'provides' = abstract capabilities this card contributes (e.g. 'mana-acceleration', 'counter-amplification', 'board-protection')
- 'wants' = abstract conditions/cards that make this card better (e.g. 'counter-placement-events', 'creature-etb-triggers'), NOT specific card names
- 'role' = the card's primary function in a deck — choose the single best fit
- 'permanent' in triggers = true only if the card stays on battlefield producing ongoing effect
- Be SPECIFIC in provides/wants — these are graph edges connecting cards
- Capture ALL key functionalities of a card. A card that untaps creatures provides 'untap'. A card with multiple abilities should have provides tags for each.
- The canonical tag lists below are preferred vocabulary. Use them when they fit, but invent a clear kebab-case tag if no canonical tag captures an important card function.

Return ONLY valid JSON. No preamble, no explanation, no markdown fences."""


def _build_system_prompt() -> str:
    """Build system prompt with tag vocabulary baked in."""
    return _SYSTEM_PROMPT_BASE + "\n\n" + _TAG_VOCAB_BLOCK


SYSTEM_PROMPT = _build_system_prompt()


_golden_cache = None


def load_corrections() -> list[dict]:
    """Load corrections — not cached since corrections.json may be updated during review loops."""
    path = BASE_DIR / "corrections.json"
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def load_golden_examples(n: int = 3) -> list[dict]:
    """Load n cards from golden dataset as few-shot examples. Cached after first call."""
    global _golden_cache
    if _golden_cache is not None:
        return _golden_cache[:n]
    path = BASE_DIR / "golden_cards.json"
    if not path.exists():
        _golden_cache = []
        return _golden_cache
    with open(path) as f:
        data = json.load(f)
    cards = data.get("cards", [])
    _golden_cache = sorted(
        cards,
        key=lambda c: len(c["expected"].get("provides_must_include", []))
                     + len(c["expected"].get("wants_must_include", [])),
        reverse=True,
    )
    return _golden_cache[:n]


def build_corrections_block(corrections: list[dict]) -> str:
    if not corrections:
        return ""

    lines = ["RULES YOU MUST FOLLOW (learned from previous errors):"]
    for i, c in enumerate(corrections, 1):
        scope = f"[{c['scope'].upper()}]"
        if c.get("wrong"):
            lines.append(f"  {i}. {scope} NEVER use '{c['wrong']}' — {c['rule']}")
        else:
            lines.append(f"  {i}. {scope} {c['rule']}")
        if c.get("example_card"):
            lines.append(f"     (Example: {c['example_card']})")

    return "\n".join(lines)


def build_examples_block(examples: list[dict]) -> str:
    """Build few-shot examples block from golden cards."""
    if not examples:
        return ""

    # We show what CORRECT output looks like for a subset of golden cards
    # These are pre-verified answers the model can learn from
    VERIFIED_OUTPUTS = {
        "Hardened Scales": {
            "name": "Hardened Scales",
            "role": "enabler",
            "provides": ["counter-amplification", "passive-counter-boost"],
            "wants": ["counter-placement-events", "counter-distribution"],
        },
        "Roaming Throne": {
            "name": "Roaming Throne",
            "role": "enabler",
            "provides": ["trigger-doubling", "tribal-ward-protection", "flexible-type-identity"],
            "wants": ["triggered-ability-heavy-commanders", "human-tribal"],
        },
        "Sol Ring": {
            "name": "Sol Ring",
            "role": "ramp",
            "provides": ["mana-acceleration", "two-colorless-mana"],
            "wants": [],
        },
    }

    lines = ["FEW-SHOT EXAMPLES (correct output format):"]
    shown = 0
    for card in examples:
        name = card["name"]
        if name in VERIFIED_OUTPUTS:
            lines.append(f"\nExample {shown+1} — Input:")
            lines.append(f"  Name: {name}")
            lines.append(f"  Type: {card['type_line']}")
            lines.append(f"  Oracle: {card['oracle_text']}")
            lines.append(f"  Expected output:")
            lines.append(f"  {json.dumps(VERIFIED_OUTPUTS[name], indent=2)}")
            shown += 1
        if shown >= 2:  # Cap at 2 examples to save tokens
            break

    return "\n".join(lines) if shown > 0 else ""


def build_prompt(card: dict, rules_context: str = "") -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) ready to send to the API.
    """
    corrections = load_corrections()
    examples = load_golden_examples(n=3)

    corrections_block = build_corrections_block(corrections)
    examples_block = build_examples_block(examples)

    # Assemble system prompt
    system_parts = [SYSTEM_PROMPT]
    if corrections_block:
        system_parts.append("\n" + corrections_block)

    system = "\n\n".join(system_parts)

    # Assemble user prompt
    user_parts = []

    if examples_block:
        user_parts.append(examples_block)

    if rules_context:
        user_parts.append(f"""
RELEVANT MTG RULES (use these to inform your tagging decisions):
{rules_context}
""")

    user_parts.append(f"""
Now analyze this card using the schema and rules above.

SCHEMA:
{SCHEMA}

CARD TO TAG:
Name: {card['name']}
Type: {card['type_line']}
CMC: {card.get('cmc', 0)}
Keywords: {', '.join(card.get('keywords', [])) or 'none'}
Oracle text: {card['oracle_text']}

Return JSON only.""")

    user = "\n".join(user_parts)

    return system, user


def build_batch_prompt(cards: list[dict], rules_context: str = "") -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) for a batch of cards.
    System prompt + corrections + examples are shared across all cards in the batch.
    """
    corrections = load_corrections()
    examples = load_golden_examples(n=3)

    corrections_block = build_corrections_block(corrections)
    examples_block = build_examples_block(examples)

    # System prompt — same as single-card
    system_parts = [SYSTEM_PROMPT]
    if corrections_block:
        system_parts.append("\n" + corrections_block)
    system = "\n\n".join(system_parts)

    # User prompt — multiple cards
    user_parts = []
    if examples_block:
        user_parts.append(examples_block)

    if rules_context:
        user_parts.append(f"""
RELEVANT MTG RULES (use these to inform your tagging decisions):
{rules_context}
""")

    # Pre-tag cards with rule-based tagger for hints
    try:
        from rule_tagger import tag_card
        use_hints = True
    except ImportError:
        use_hints = False

    cards_block = []
    for i, card in enumerate(cards, 1):
        hint = ""
        if use_hints:
            rule_tags = tag_card(card)
            hints = []
            if rule_tags.get("role"):
                hints.append(f"role={rule_tags['role']}")
            if rule_tags.get("provides"):
                hints.append(f"provides={rule_tags['provides']}")
            if rule_tags.get("wants"):
                hints.append(f"wants={rule_tags['wants']}")
            if hints:
                hint = f"\nPre-analysis hints (validate and extend): {', '.join(hints)}"

        cards_block.append(f"""CARD {i}:
Name: {card['name']}
Type: {card['type_line']}
CMC: {card.get('cmc', 0)}
Keywords: {', '.join(card.get('keywords', [])) or 'none'}
Oracle text: {card.get('oracle_text', '')}{hint}""")

    user_parts.append(f"""
Analyze each of the following {len(cards)} cards using the schema and rules above.
Return a JSON ARRAY with one object per card, in the same order.

SCHEMA (for each card):
{SCHEMA}

{chr(10).join(cards_block)}

Return a JSON array of {len(cards)} objects. JSON only, no other text.""")

    user = "\n".join(user_parts)
    return system, user


REVIEW_SYSTEM_PROMPT = """You are a senior Magic: The Gathering analyst reviewing card tags produced by a junior tagger.
Your job is to verify that the tags are accurate, complete, and follow the project conventions.

You will receive:
1. The original card data (name, type, oracle text)
2. The tags produced by the tagger

Check each card for these issues:
- WRONG ROLE: Does the role accurately reflect the card's primary function?
- MISSING PROVIDES: Are there capabilities the card clearly offers that are missing from provides?
  Pay special attention to: untap effects, mana generation, counter placement, tribal types, protection.
- WRONG PROVIDES: Are there provides tags that don't match what the card actually does?
- MISSING WANTS: Are there conditions that clearly make this card better that are missing from wants?
- WRONG WANTS: Are there wants tags that don't apply to this card?
- TAG SPECIFICITY: Are tags too vague (e.g. 'tribal-synergy' instead of 'goblin-tribal')?
- SCOPE ERRORS: Is board-wide vs single-target counter placement distinguished correctly?

Return a JSON object with this structure:
{
  "cards": [
    {
      "name": "card name",
      "verdict": "APPROVE" or "REVISE",
      "issues": ["list of specific issues found — empty if APPROVE"],
      "suggested_fixes": {"field": "corrected value"}
    }
  ]
}

Be strict but fair. Only flag genuine errors, not stylistic preferences.
Return JSON only, no other text."""


def build_review_prompt(cards: list[dict], tagged: list[dict]) -> tuple[str, str]:
    """Build prompt for the reviewer agent to check tagged output."""
    corrections = load_corrections()
    corrections_block = build_corrections_block(corrections)

    system = REVIEW_SYSTEM_PROMPT
    if corrections_block:
        system += "\n\n" + corrections_block

    # Build user prompt with card data + tagger output side by side
    review_blocks = []
    for i, (card, tags) in enumerate(zip(cards, tagged), 1):
        review_blocks.append(f"""CARD {i}:
  Name: {card['name']}
  Type: {card['type_line']}
  Oracle text: {card.get('oracle_text', '')}
  Keywords: {', '.join(card.get('keywords', [])) or 'none'}

  TAGGER OUTPUT:
  {json.dumps(tags, indent=2)}""")

    user = f"""Review the following {len(cards)} tagged cards for accuracy.

{_TAG_VOCAB_BLOCK}

{chr(10).join(review_blocks)}

Return your review as JSON. For each card, verdict is APPROVE or REVISE."""

    return system, user


def build_retag_prompt(cards: list[dict], tagged: list[dict],
                       review: list[dict], rules_context: str = "") -> tuple[str, str]:
    """Build a re-tagging prompt that includes reviewer feedback for flagged cards."""
    # Only re-tag cards that got REVISE verdict
    revise_indices = []
    feedback_map = {}
    for i, r in enumerate(review):
        if r.get("verdict") == "REVISE":
            revise_indices.append(i)
            feedback_map[i] = r

    if not revise_indices:
        return "", ""

    cards_to_retag = [cards[i] for i in revise_indices]

    # Build the base batch prompt
    system, user = build_batch_prompt(cards_to_retag, rules_context=rules_context)

    # Inject reviewer feedback into the user prompt
    feedback_lines = ["\nREVIEWER FEEDBACK (fix these issues in your re-tagging):"]
    for idx in revise_indices:
        r = feedback_map[idx]
        card_name = cards[idx]["name"]
        issues = r.get("issues", [])
        fixes = r.get("suggested_fixes", {})
        feedback_lines.append(f"\n  {card_name}:")
        for issue in issues:
            feedback_lines.append(f"    - {issue}")
        if fixes:
            feedback_lines.append(f"    Suggested fixes: {json.dumps(fixes)}")

    user = "\n".join(feedback_lines) + "\n\n" + user
    return system, user


def prompt_stats() -> dict:
    """Return info about current prompt state — useful for debugging."""
    corrections = load_corrections()
    examples = load_golden_examples()
    system, user = build_prompt({
        "name": "TEST", "type_line": "Creature", "oracle_text": "test", "keywords": [], "cmc": 1
    })
    return {
        "corrections_count": len(corrections),
        "global_rules": sum(1 for c in corrections if c["scope"] == "global"),
        "card_rules": sum(1 for c in corrections if c["scope"] == "card"),
        "golden_examples_available": len(examples),
        "system_prompt_chars": len(system),
        "user_prompt_chars_approx": len(user),
    }


if __name__ == "__main__":
    stats = prompt_stats()
    print("Prompt Builder Stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print("\nSample system prompt (first 500 chars):")
    system, user = build_prompt({
        "name": "Hardened Scales",
        "type_line": "Enchantment",
        "oracle_text": "If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead.",
        "keywords": [],
        "cmc": 1,
    })
    print(system[:500])
    print("...")
