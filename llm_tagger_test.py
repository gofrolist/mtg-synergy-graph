"""
LLM Tagger Test — v0.1
Sends 10 cards from the Kyler deck to Claude API and asks for structured tags.
Goal: validate tag quality before building the full graph.

Output: card_tags.json — one tagged object per card
"""

import json
import re
import requests

# ── TEST CARDS ────────────────────────────────────────────────────────────────
# Deliberately varied: Humans, non-Humans, counters, protection, ramp, utility
# Includes some edge cases (Roaming Throne, Slippery Bogbonder)

TEST_CARDS = [
    {
        "name": "Kyler, Sigardian Emissary",
        "type_line": "Legendary Creature — Human Cleric",
        "oracle_text": "When another Human you control enters, put a +1/+1 counter on each other Human you control. When Kyler, Sigardian Emissary enters, put a +1/+1 counter on it for each other Human you control.",
        "keywords": [],
        "cmc": 5,
    },
    {
        "name": "Hardened Scales",
        "type_line": "Enchantment",
        "oracle_text": "If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead.",
        "keywords": [],
        "cmc": 1,
    },
    {
        "name": "Roaming Throne",
        "type_line": "Artifact Creature — Construct",
        "oracle_text": "As Roaming Throne enters, choose a creature type. This creature is the chosen type in addition to its other types. Creatures you control of the chosen type have ward {2}. Whenever a creature you control of the chosen type triggers an ability, that ability triggers an additional time.",
        "keywords": [],
        "cmc": 4,
    },
    {
        "name": "Thalia's Lieutenant",
        "type_line": "Creature — Human Soldier",
        "oracle_text": "When Thalia's Lieutenant enters, put a +1/+1 counter on each other Human you control. Whenever another Human enters under your control, put a +1/+1 counter on Thalia's Lieutenant.",
        "keywords": [],
        "cmc": 2,
    },
    {
        "name": "Pir, Imaginative Rascal",
        "type_line": "Legendary Creature — Human",
        "oracle_text": "If one or more counters would be put on a permanent your team controls, that many plus one of each of those kinds of counters are put on that permanent instead.",
        "keywords": [],
        "cmc": 3,
    },
    {
        "name": "Heroic Intervention",
        "type_line": "Instant",
        "oracle_text": "Permanents you control gain hexproof and indestructible until end of turn.",
        "keywords": [],
        "cmc": 2,
    },
    {
        "name": "Sol Ring",
        "type_line": "Artifact",
        "oracle_text": "{T}: Add {C}{C}.",
        "keywords": [],
        "cmc": 1,
    },
    {
        "name": "Champion of Lambholt",
        "type_line": "Creature — Human Warrior",
        "oracle_text": "Creatures with power less than Champion of Lambholt's power can't block creatures you control. Whenever another creature enters under your control, put a +1/+1 counter on Champion of Lambholt.",
        "keywords": [],
        "cmc": 3,
    },
    {
        "name": "Slippery Bogbonder",
        "type_line": "Creature — Human Scout",
        "oracle_text": "Flash. When Slippery Bogbonder enters, move all counters from target creature onto another target creature. That creature gains hexproof until end of turn.",
        "keywords": ["Flash"],
        "cmc": 4,
    },
    {
        "name": "Gavony Township",
        "type_line": "Land",
        "oracle_text": "{2}{G}{W}, {T}: Put a +1/+1 counter on each creature you control.",
        "keywords": [],
        "cmc": 0,
    },
]

# ── SCHEMA ────────────────────────────────────────────────────────────────────

SCHEMA = """
{
  "name": "card name",
  "categories": ["list of functional categories this card belongs to"],
  "mechanics": ["list of MTG mechanics this card uses or references"],
  "provides": ["what this card gives to the deck when it's on the battlefield"],
  "wants": ["what other cards or conditions make this card better"],
  "triggers": [
    {
      "condition": "when X happens",
      "effect": "Y happens",
      "scope": "self | board | opponent",
      "permanent": true | false
    }
  ],
  "synergy_tags": ["abstract tags for finding synergies, e.g. 'counter-doubler', 'human-tribal', 'etb-payoff'"],
  "role": "primary role: ramp | draw | removal | protection | enabler | threat | utility | land",
  "notes": "one sentence explaining the card's place in a deck"
}
"""

# ── CATEGORY VOCABULARY ───────────────────────────────────────────────────────
# Give the LLM a vocabulary to work with — reduces hallucination and inconsistency

CATEGORY_VOCAB = """
Functional categories (use these where applicable, add new ones if needed):
  counter-doubler, counter-placer, counter-payoff, counter-mover
  human-tribal, elf-tribal, dragon-tribal, token-generator
  etb-trigger, attack-trigger, death-trigger, cast-trigger
  ramp-spell, ramp-creature, land-fetch
  board-protection, targeted-protection, counterspell
  card-draw-engine, conditional-draw, looting
  removal-exile, removal-destroy, board-wipe
  tutor-creature, tutor-spell, tutor-land
  pump-spell, anthem, finisher
  tap-ability, mana-ability
  ward-protection, hexproof-granter, indestructible-granter
  triggered-ability-doubler, activated-ability-copier
  staple-mana-rock, staple-land, utility-land
"""

# ── PROMPT ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Magic: The Gathering card analyst building a knowledge graph.
Your job is to analyze MTG cards and produce structured tags that capture their mechanical identity.

Rules:
- Be precise about MTG terminology
- "permanent" in triggers means the effect is ongoing (enchantment/creature on battlefield), not one-time
- Distinguish between board-wide effects and targeted/single effects
- "wants" should name abstract tags, not specific card names (e.g. "counter-placement-events" not "Luminarch Aspirant")
- "provides" should name what the card contributes to the deck's game plan
- synergy_tags should be reusable across many cards — they are the edges in a knowledge graph

Return ONLY valid JSON. No preamble, no explanation, no markdown fences."""

def build_user_prompt(card: dict) -> str:
    return f"""Analyze this MTG card and return structured tags matching this schema exactly:

SCHEMA:
{SCHEMA}

CATEGORY VOCABULARY (use these, extend if needed):
{CATEGORY_VOCAB}

CARD:
Name: {card['name']}
Type: {card['type_line']}
CMC: {card['cmc']}
Keywords: {', '.join(card['keywords']) if card['keywords'] else 'none'}
Oracle text: {card['oracle_text']}

Return JSON only."""


def call_claude(prompt_user: str) -> dict | None:
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json"},
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt_user}],
        },
    )
    if response.status_code != 200:
        print(f"  [API ERROR] {response.status_code}: {response.text[:200]}")
        return None

    data = response.json()
    raw = data["content"][0]["text"].strip()

    # Strip markdown fences if model added them anyway
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [JSON ERROR] {e}")
        print(f"  Raw: {raw[:300]}")
        return None


def run():
    print("═" * 60)
    print("LLM TAGGER TEST — 10 cards from Kyler deck")
    print("═" * 60)

    results = []
    failures = []

    for i, card in enumerate(TEST_CARDS, 1):
        print(f"\n[{i:2d}/10] {card['name']}...")
        prompt = build_user_prompt(card)
        tagged = call_claude(prompt)

        if tagged:
            tagged["_input_name"] = card["name"]  # sanity check
            results.append(tagged)
            print(f"  ✓ categories: {tagged.get('categories', [])}")
            print(f"  ✓ synergy_tags: {tagged.get('synergy_tags', [])}")
            print(f"  ✓ role: {tagged.get('role', '?')}")
            print(f"  ✓ provides: {tagged.get('provides', [])}")
            print(f"  ✓ wants: {tagged.get('wants', [])}")
        else:
            failures.append(card["name"])
            print(f"  ✗ FAILED")

    # Save results
    output_path = "/home/claude/mtg_optimizer/card_tags.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n{'═'*60}")
    print(f"RESULTS: {len(results)}/10 tagged successfully")
    if failures:
        print(f"FAILURES: {failures}")
    print(f"Saved to: {output_path}")
    print(f"{'═'*60}")

    # ── CONSISTENCY CHECK ──────────────────────────────────────────────────
    # Look for shared synergy_tags — this is the key validation
    # Cards that should be connected should share tags
    if len(results) >= 2:
        print("\n── SYNERGY TAG OVERLAP (should connect related cards) ──")
        all_tags = {}
        for r in results:
            for tag in r.get("synergy_tags", []):
                if tag not in all_tags:
                    all_tags[tag] = []
                all_tags[tag].append(r["name"])

        # Show tags shared by 2+ cards
        shared = {t: cards for t, cards in all_tags.items() if len(cards) >= 2}
        if shared:
            for tag, cards in sorted(shared.items()):
                print(f"  [{tag}]")
                for c in cards:
                    print(f"    → {c}")
        else:
            print("  No shared tags found — vocabulary may need refinement")


if __name__ == "__main__":
    run()
