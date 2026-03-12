"""
prompt_builder.py
Assembles the LLM tagger prompt dynamically from:
  1. Base instructions + schema
  2. Category vocabulary
  3. Corrections (injected as rules the model must follow)
  4. Few-shot examples (best tagged cards from golden dataset)
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).parent

SCHEMA = """{
  "name": "card name",
  "categories": ["list of functional categories this card belongs to"],
  "mechanics": ["list of MTG mechanics this card uses or references"],
  "provides": ["what this card gives to the deck when it's on the battlefield or in hand"],
  "wants": ["abstract tags — what other cards or conditions make this card better"],
  "triggers": [
    {
      "condition": "when X happens",
      "effect": "Y happens",
      "scope": "self | board | targeted | opponent",
      "permanent": true
    }
  ],
  "synergy_tags": ["reusable abstract tags for graph edges, e.g. counter-doubler, human-tribal"],
  "role": "ramp | draw | removal | protection | enabler | threat | utility | land",
  "notes": "one sentence: card's role in a deck"
}"""

CATEGORY_VOCAB = """
FUNCTIONAL CATEGORIES (use these, extend only if truly needed):
  Counter mechanics:
    counter-doubler, counter-placer, board-wide-counter-placer, single-target-counter-placer
    counter-payoff, counter-mover, counter-consolidator, proliferate-effect
    replacement-effect (for Hardened Scales / Pir style)

  Tribal:
    human-tribal, elf-tribal, dragon-tribal, wizard-tribal
    tribal-lord, tribal-enabler, can-become-chosen-type

  Triggers:
    etb-trigger, attack-trigger, death-trigger, cast-trigger
    triggered-ability-doubler, activated-ability-copier
    human-etb-payoff (specific: fires when a Human enters)

  Card advantage:
    card-draw-engine, conditional-draw, looting, top-of-library-access
    tutor-creature, tutor-spell, tutor-land

  Ramp:
    staple-mana-rock, colorless-ramp, land-fetch, mana-dork, mana-sink

  Protection:
    board-protection, reactive-protection, targeted-protection
    hexproof-granter, indestructible-granter, ward-granter
    wrath-counter, staple-protection

  Removal:
    removal-exile, removal-destroy, board-wipe, artifact-removal, enchantment-removal

  Combat:
    evasion-granter, combat-enabler, pump-spell, anthem, finisher

  Other:
    passive-permanent (always-on effect, no activation cost)
    utility-land, staple-land
    mana-rock, equipment
"""

SYSTEM_PROMPT = """You are an expert Magic: The Gathering card analyst building a knowledge graph.
Your job is to analyze MTG cards and produce structured tags that will become edges in a synergy graph.

Core principles:
- Tags must be REUSABLE across many cards — they are graph edges, not card descriptions
- Be SPECIFIC: 'human-tribal' not 'tribal-synergy', 'board-wide-counter-placer' not 'counter-placer'
- 'permanent' in triggers = true only if the card stays on battlefield producing ongoing effect
- 'wants' = abstract tags (e.g. 'counter-placement-events'), NOT specific card names
- 'provides' = what the card contributes to a deck's game plan

Return ONLY valid JSON. No preamble, no explanation, no markdown fences."""


def load_corrections() -> list[dict]:
    path = BASE_DIR / "corrections.json"
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def load_golden_examples(n: int = 3) -> list[dict]:
    """Load n cards from golden dataset as few-shot examples."""
    path = BASE_DIR / "golden_cards.json"
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f)
    # Return cards that have the richest expected tags (most must_include items)
    cards = data.get("cards", [])
    ranked = sorted(
        cards,
        key=lambda c: len(c["expected"].get("synergy_tags_must_include", [])),
        reverse=True,
    )
    return ranked[:n]


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
            "categories": ["counter-doubler", "permanent-amplifier"],
            "mechanics": ["replacement effect", "+1/+1 counters"],
            "provides": ["counter-amplification", "passive-counter-boost"],
            "wants": ["counter-placement-events", "counter-distribution"],
            "triggers": [],
            "synergy_tags": ["counter-doubler", "replacement-effect", "counter-amplifier", "passive-permanent"],
            "role": "enabler",
            "notes": "Passive enchantment adding one extra counter to every +1/+1 placement — stacks multiplicatively."
        },
        "Roaming Throne": {
            "name": "Roaming Throne",
            "categories": ["triggered-ability-doubler", "tribal-enabler", "ward-granter", "can-become-chosen-type"],
            "mechanics": ["triggered ability doubler", "ward", "type-selection on ETB"],
            "provides": ["trigger-doubling", "tribal-ward-protection", "flexible-type-identity"],
            "wants": ["triggered-ability-heavy-commanders", "human-tribal"],
            "triggers": [{"condition": "creature of chosen type triggers an ability", "effect": "that ability triggers again", "scope": "board", "permanent": True}],
            "synergy_tags": ["triggered-ability-doubler", "tribal-enabler", "can-become-chosen-type", "passive-permanent"],
            "role": "enabler",
            "notes": "Doubles triggered abilities of chosen type — choosing Human makes it a Human itself and doubles Kyler's trigger."
        },
        "Sol Ring": {
            "name": "Sol Ring",
            "categories": ["staple-mana-rock", "ramp"],
            "mechanics": ["mana ability", "tap ability"],
            "provides": ["mana-acceleration", "two-colorless-mana"],
            "wants": [],
            "triggers": [],
            "synergy_tags": ["staple-mana-rock", "colorless-ramp", "passive-permanent"],
            "role": "ramp",
            "notes": "Universal EDH staple producing 2 colorless mana for 1 investment."
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


def build_prompt(card: dict) -> tuple[str, str]:
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

    user_parts.append(f"""
Now analyze this card using the schema and rules above.

SCHEMA:
{SCHEMA}

CATEGORY VOCABULARY:
{CATEGORY_VOCAB}

CARD TO TAG:
Name: {card['name']}
Type: {card['type_line']}
CMC: {card.get('cmc', 0)}
Keywords: {', '.join(card.get('keywords', [])) or 'none'}
Oracle text: {card['oracle_text']}

Return JSON only.""")

    user = "\n".join(user_parts)

    return system, user


def build_batch_prompt(cards: list[dict]) -> tuple[str, str]:
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

    cards_block = []
    for i, card in enumerate(cards, 1):
        cards_block.append(f"""CARD {i}:
Name: {card['name']}
Type: {card['type_line']}
CMC: {card.get('cmc', 0)}
Keywords: {', '.join(card.get('keywords', [])) or 'none'}
Oracle text: {card.get('oracle_text', '')}""")

    user_parts.append(f"""
Analyze each of the following {len(cards)} cards using the schema and rules above.
Return a JSON ARRAY with one object per card, in the same order.

SCHEMA (for each card):
{SCHEMA}

CATEGORY VOCABULARY:
{CATEGORY_VOCAB}

{chr(10).join(cards_block)}

Return a JSON array of {len(cards)} objects. JSON only, no other text.""")

    user = "\n".join(user_parts)
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
