# Kyler Card Filter + Batch Tagger Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter ~500 Kyler-relevant cards from Scryfall bulk data and batch-tag them via Claude Sonnet API.

**Architecture:** Two new scripts — `card_filter.py` (two-pass filter producing `data/kyler_candidates.json`) and `batch_tagger.py` (reads candidates, sends 5-card batches to Claude API, outputs `data/kyler_tags.json`). Reuses existing `prompt_builder.py` for system prompt, corrections, and few-shot examples. Adds `build_batch_prompt()` to `prompt_builder.py` for multi-card user prompts.

**Tech Stack:** Python 3, `urllib.request` for API calls, existing `prompt_builder.py` module.

---

## Chunk 1: Card Filter

### Task 1: Create `card_filter.py` — Two-Pass Filter

**Files:**
- Create: `card_filter.py`
- Read: `data/oracle_cards.json` (input)
- Write: `data/kyler_candidates.json` (output)

- [ ] **Step 1: Write `card_filter.py` with pass 1 (core synergy)**

```python
"""
Filter Kyler-relevant cards from Scryfall bulk data.

Pass 1 (core): Humans with oracle text, +1/+1 counter cards, Human-referencing cards
Pass 2 (extended): ETB triggers, token generators, tribal enablers, proliferate

Usage:
    python3 card_filter.py
"""

import json
import os
import re

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_FILE = os.path.join(DATA_DIR, "oracle_cards.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "kyler_candidates.json")

# Max cards from pass 2 to stay within budget
PASS2_CAP = 250


def is_gw_legal(card: dict) -> bool:
    """Card's color identity is a subset of {G, W}."""
    return set(card.get("color_identity", [])) <= {"G", "W"}


def has_oracle_text(card: dict, min_len: int = 20) -> bool:
    """Card has meaningful oracle text (not vanilla/french-vanilla)."""
    text = card.get("oracle_text", "")
    if not text:
        # Check card_faces for multi-face cards
        faces = card.get("card_faces", [])
        if faces:
            text = faces[0].get("oracle_text", "")
    return len(text) > min_len


def get_oracle_text(card: dict) -> str:
    """Get oracle text, handling multi-face cards."""
    text = card.get("oracle_text", "")
    if not text:
        faces = card.get("card_faces", [])
        if faces:
            text = faces[0].get("oracle_text", "")
    return text


def pass1_core(cards: list[dict]) -> dict[str, dict]:
    """Core synergy: Humans, +1/+1 counters, Human-referencing cards."""
    result = {}

    for card in cards:
        if not is_gw_legal(card):
            continue
        oracle_id = card.get("oracle_id")
        if not oracle_id or card.get("layout") == "reversible_card":
            continue

        type_line = card.get("type_line", "")
        oracle_text = get_oracle_text(card).lower()

        picked = False
        reason = ""

        # 1. Human creatures with non-trivial oracle text
        if "Human" in type_line and "Creature" in type_line and has_oracle_text(card):
            picked = True
            reason = "human-creature"

        # 2. Any card with +1/+1 counter in oracle text
        elif "+1/+1 counter" in oracle_text:
            picked = True
            reason = "counter-card"

        # 3. Any card mentioning "human" in oracle text (non-Human cards that care about Humans)
        elif re.search(r"\bhuman\b", oracle_text):
            picked = True
            reason = "human-reference"

        if picked:
            result[oracle_id] = {
                "name": card.get("name", ""),
                "oracle_id": oracle_id,
                "type_line": type_line,
                "oracle_text": get_oracle_text(card),
                "keywords": card.get("keywords", []),
                "cmc": card.get("cmc", 0),
                "color_identity": card.get("color_identity", []),
                "filter_pass": 1,
                "filter_reason": reason,
            }

    return result


def pass2_extended(cards: list[dict], already_picked: set[str]) -> dict[str, dict]:
    """Extended synergy: ETB, tokens, tribal enablers, proliferate."""
    result = {}

    for card in cards:
        if not is_gw_legal(card):
            continue
        oracle_id = card.get("oracle_id")
        if not oracle_id or oracle_id in already_picked:
            continue
        if card.get("layout") == "reversible_card":
            continue
        if not has_oracle_text(card):
            continue

        type_line = card.get("type_line", "")
        oracle_text = get_oracle_text(card).lower()

        # Skip basic lands
        if type_line.startswith("Basic Land"):
            continue

        picked = False
        reason = ""

        # 1. ETB triggers
        if re.search(r"enters(?:\s+the\s+battlefield)?", oracle_text) and \
           ("Creature" in type_line or "Enchantment" in type_line or "Artifact" in type_line):
            picked = True
            reason = "etb-trigger"

        # 2. Token generators
        elif re.search(r"create\s+(?:a|an|one|two|three|four|five|x|\d+)\s+", oracle_text):
            picked = True
            reason = "token-generator"

        # 3. Tribal enablers
        elif re.search(r"choose a creature type|changeling", oracle_text):
            picked = True
            reason = "tribal-enabler"

        # 4. Counter interaction (not +1/+1 specifically — those are in pass 1)
        elif re.search(r"proliferate|counter on|double.*counter|move.*counter", oracle_text):
            picked = True
            reason = "counter-interaction"

        if picked:
            result[oracle_id] = {
                "name": card.get("name", ""),
                "oracle_id": oracle_id,
                "type_line": type_line,
                "oracle_text": get_oracle_text(card),
                "keywords": card.get("keywords", []),
                "cmc": card.get("cmc", 0),
                "color_identity": card.get("color_identity", []),
                "filter_pass": 2,
                "filter_reason": reason,
            }

        if len(result) >= PASS2_CAP:
            break

    return result


def run():
    print("Loading oracle_cards.json...")
    with open(INPUT_FILE) as f:
        cards = json.load(f)
    print(f"Loaded {len(cards)} cards")

    print("\nPass 1: Core synergy (Humans, counters, Human-referencing)...")
    core = pass1_core(cards)
    print(f"  Found {len(core)} cards")

    # Breakdown
    reasons = {}
    for c in core.values():
        r = c["filter_reason"]
        reasons[r] = reasons.get(r, 0) + 1
    for r, count in sorted(reasons.items()):
        print(f"    {r}: {count}")

    print(f"\nPass 2: Extended synergy (ETB, tokens, tribal, proliferate)...")
    extended = pass2_extended(cards, set(core.keys()))
    print(f"  Found {len(extended)} cards (capped at {PASS2_CAP})")

    reasons2 = {}
    for c in extended.values():
        r = c["filter_reason"]
        reasons2[r] = reasons2.get(r, 0) + 1
    for r, count in sorted(reasons2.items()):
        print(f"    {r}: {count}")

    # Merge
    all_candidates = {**core, **extended}
    candidates_list = list(all_candidates.values())

    print(f"\nTotal candidates: {len(candidates_list)}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(candidates_list, f, indent=2)
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Run the filter and verify output**

Run: `python3 card_filter.py`
Expected: ~400-600 cards total, saved to `data/kyler_candidates.json`

- [ ] **Step 3: Spot-check output**

Run:
```bash
python3 -c "
import json
with open('data/kyler_candidates.json') as f:
    cards = json.load(f)
# Check known cards are present
known = ['Hardened Scales', 'Roaming Throne', 'Thalia\\'s Lieutenant', 'Sol Ring', 'Gavony Township']
for name in known:
    found = any(c['name'] == name for c in cards)
    print(f'  {name}: {\"FOUND\" if found else \"MISSING\"}')"
```
Expected: All 5 known cards found.

---

## Chunk 2: Batch Prompt Builder

### Task 2: Add `build_batch_prompt()` to `prompt_builder.py`

**Files:**
- Modify: `prompt_builder.py`

- [ ] **Step 1: Add `build_batch_prompt()` function**

Add this function after `build_prompt()` in `prompt_builder.py`:

```python
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
```

- [ ] **Step 2: Verify prompt builds correctly**

Run:
```bash
python3 -c "
from prompt_builder import build_batch_prompt
cards = [
    {'name': 'Sol Ring', 'type_line': 'Artifact', 'oracle_text': '{T}: Add {C}{C}.', 'keywords': [], 'cmc': 1},
    {'name': 'Hardened Scales', 'type_line': 'Enchantment', 'oracle_text': 'If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead.', 'keywords': [], 'cmc': 1},
]
system, user = build_batch_prompt(cards)
print(f'System: {len(system)} chars')
print(f'User: {len(user)} chars')
print('CARD 1:' in user and 'CARD 2:' in user)
"
```
Expected: `True`, reasonable char counts.

---

## Chunk 3: Batch Tagger

### Task 3: Create `batch_tagger.py`

**Files:**
- Create: `batch_tagger.py`
- Read: `data/kyler_candidates.json` (input)
- Write: `data/kyler_tags.json` (output)

- [ ] **Step 1: Write `batch_tagger.py`**

```python
"""
Batch tagger — sends cards to Claude API in batches of 5.

Reads: data/kyler_candidates.json (from card_filter.py)
Writes: data/kyler_tags.json

Usage:
    ANTHROPIC_API_KEY=sk-... python3 batch_tagger.py
    ANTHROPIC_API_KEY=sk-... python3 batch_tagger.py --batch-size 3
    ANTHROPIC_API_KEY=sk-... python3 batch_tagger.py --dry-run
"""

import argparse
import json
import os
import re
import time
import urllib.request
import urllib.error

from prompt_builder import build_batch_prompt

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CANDIDATES_FILE = os.path.join(DATA_DIR, "kyler_candidates.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "kyler_tags.json")

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 4096


def load_candidates() -> list[dict]:
    with open(CANDIDATES_FILE) as f:
        return json.load(f)


def load_existing_tags() -> dict[str, dict]:
    """Load already-tagged cards for resume support. Returns oracle_id -> tagged dict."""
    if not os.path.exists(OUTPUT_FILE):
        return {}
    with open(OUTPUT_FILE) as f:
        tags = json.load(f)
    return {t["oracle_id"]: t for t in tags if "oracle_id" in t}


def save_tags(tags: list[dict]):
    with open(OUTPUT_FILE, "w") as f:
        json.dump(tags, f, indent=2)


def call_api(system: str, user: str, api_key: str) -> str | None:
    """Send a message to Claude API. Returns raw text response."""
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  [API ERROR] {e.code}: {body[:200]}")
        return None


def parse_response(raw: str, expected_count: int) -> list[dict] | None:
    """Parse JSON array from API response. Returns list of tagged card dicts."""
    # Strip markdown fences if present
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [JSON ERROR] {e}")
        print(f"  Raw (first 300 chars): {raw[:300]}")
        return None

    if isinstance(result, dict):
        # Single card returned instead of array
        result = [result]

    if not isinstance(result, list):
        print(f"  [PARSE ERROR] Expected list, got {type(result)}")
        return None

    if len(result) != expected_count:
        print(f"  [WARN] Expected {expected_count} cards, got {len(result)}")

    return result


def run():
    parser = argparse.ArgumentParser(description="Batch card tagger")
    parser.add_argument("--batch-size", type=int, default=5, help="Cards per API call")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without calling API")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        return

    # Load candidates
    candidates = load_candidates()
    print(f"Loaded {len(candidates)} candidates from {CANDIDATES_FILE}")

    # Resume support
    existing = load_existing_tags()
    if existing:
        print(f"Found {len(existing)} already-tagged cards — skipping them")
    candidates = [c for c in candidates if c["oracle_id"] not in existing]
    print(f"Cards to tag: {len(candidates)}")

    if not candidates:
        print("Nothing to tag!")
        return

    # Build batches
    batches = []
    for i in range(0, len(candidates), args.batch_size):
        batches.append(candidates[i:i + args.batch_size])

    print(f"Batches: {len(batches)} (batch size: {args.batch_size})")

    if args.dry_run:
        system, user = build_batch_prompt(batches[0])
        print(f"\n--- DRY RUN ---")
        print(f"System prompt: {len(system)} chars")
        print(f"User prompt (batch 1): {len(user)} chars")
        print(f"First batch cards: {[c['name'] for c in batches[0]]}")
        print(f"Estimated API calls: {len(batches)}")
        print(f"Estimated input tokens: ~{(len(system) + len(user)) * len(batches) // 4:,}")
        return

    # Tag batches
    all_tagged = list(existing.values())
    tagged_count = 0
    failed_count = 0
    total_batches = len(batches)

    for batch_idx, batch in enumerate(batches, 1):
        names = [c["name"] for c in batch]
        print(f"\n[batch {batch_idx}/{total_batches}] {', '.join(names)}")

        system, user = build_batch_prompt(batch)

        # Retry with exponential backoff
        raw = None
        for attempt in range(3):
            if attempt > 0:
                wait = 2 ** attempt
                print(f"  Retry {attempt + 1}/3 (waiting {wait}s)...")
                time.sleep(wait)
            raw = call_api(system, user, api_key)
            if raw:
                break

        if not raw:
            print(f"  FAILED after 3 attempts — skipping batch")
            failed_count += len(batch)
            continue

        parsed = parse_response(raw, len(batch))
        if not parsed:
            failed_count += len(batch)
            continue

        # Attach oracle_id from candidates to tagged results
        for card_data, tagged in zip(batch, parsed):
            tagged["oracle_id"] = card_data["oracle_id"]
            tagged["filter_pass"] = card_data["filter_pass"]
            tagged["filter_reason"] = card_data["filter_reason"]
            all_tagged.append(tagged)
            tagged_count += 1

        # Save after each batch (crash-safe)
        save_tags(all_tagged)
        print(f"  Tagged {len(parsed)} cards (total: {len(all_tagged)})")

        # Rate limiting — be polite to the API
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"DONE: {tagged_count} tagged, {failed_count} failed, {len(existing)} skipped (already tagged)")
    print(f"Total in {OUTPUT_FILE}: {len(all_tagged)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Test with `--dry-run` to verify prompt construction**

Run: `python3 batch_tagger.py --dry-run`
Expected: Shows batch stats, prompt sizes, first batch card names. No API call made.

- [ ] **Step 3: Test with a single small batch**

Run: `ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python3 batch_tagger.py --batch-size 2`
Wait for first batch to complete, then Ctrl+C. Check `data/kyler_tags.json` has 2 tagged cards with valid structure.

- [ ] **Step 4: Run full tagger**

Run: `ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python3 batch_tagger.py`
Expected: ~100 batches, ~5 min, all candidates tagged. Resume support means you can Ctrl+C and restart safely.

- [ ] **Step 5: Add `data/kyler_candidates.json` and `data/kyler_tags.json` to `.gitignore`**

Append to `.gitignore`:
```
data/kyler_candidates.json
data/kyler_tags.json
```

---

## Chunk 4: Verification

### Task 4: Verify tagged output quality

- [ ] **Step 1: Run summary stats on tagged output**

Run:
```bash
python3 -c "
import json
with open('data/kyler_tags.json') as f:
    tags = json.load(f)
print(f'Total tagged: {len(tags)}')

# Collect all synergy_tags
all_st = {}
for t in tags:
    for st in t.get('synergy_tags', []):
        all_st[st] = all_st.get(st, 0) + 1

print(f'Unique synergy_tags: {len(all_st)}')
print(f'\\nTop 20 synergy_tags:')
for tag, count in sorted(all_st.items(), key=lambda x: -x[1])[:20]:
    print(f'  {tag}: {count}')

# Role distribution
roles = {}
for t in tags:
    r = t.get('role', 'unknown')
    roles[r] = roles.get(r, 0) + 1
print(f'\\nRole distribution:')
for r, count in sorted(roles.items(), key=lambda x: -x[1]):
    print(f'  {r}: {count}')
"
```

- [ ] **Step 2: Validate golden cards are correctly tagged in batch output**

Run:
```bash
python3 -c "
import json
with open('data/kyler_tags.json') as f:
    tags = json.load(f)
with open('golden_cards.json') as f:
    golden = json.load(f)

tag_by_name = {t['name']: t for t in tags}
for gc in golden['cards']:
    name = gc['name']
    tagged = tag_by_name.get(name)
    if not tagged:
        print(f'MISSING: {name}')
        continue
    exp = gc['expected']
    st = set(tagged.get('synergy_tags', []))
    must_have = set(exp.get('synergy_tags_must_include', []))
    must_not = set(exp.get('synergy_tags_must_exclude', []))
    missing = must_have - st
    bad = must_not & st
    status = 'PASS' if not missing and not bad else 'FAIL'
    print(f'{status}: {name}')
    if missing:
        print(f'  Missing: {missing}')
    if bad:
        print(f'  Unwanted: {bad}')
"
```
Expected: All golden cards PASS.
