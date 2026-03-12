"""
Batch tagger — sends cards to LLM API in batches of 5.

Supports both Anthropic (Claude) and OpenAI (GPT) APIs.

Reads: data/kyler_candidates.json (from card_filter.py)
Writes: data/kyler_tags.json

Usage:
    OPENAI_API_KEY=sk-... python3 batch_tagger.py
    ANTHROPIC_API_KEY=sk-... python3 batch_tagger.py
    python3 batch_tagger.py --batch-size 3
    python3 batch_tagger.py --dry-run
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

MAX_TOKENS = 4096


def detect_provider() -> tuple[str, str, str]:
    """Detect API provider from environment. Returns (provider, api_key, model)."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if openai_key:
        return "openai", openai_key, "gpt-4o"
    elif anthropic_key:
        return "anthropic", anthropic_key, "claude-sonnet-4-20250514"
    else:
        return "none", "", ""


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


def call_openai(system: str, user: str, api_key: str, model: str) -> str | None:
    """Send a message to OpenAI API. Returns raw text response."""
    payload = json.dumps({
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  [API ERROR] {e.code}: {body[:200]}")
        return None


def call_anthropic(system: str, user: str, api_key: str, model: str) -> str | None:
    """Send a message to Anthropic API. Returns raw text response."""
    payload = json.dumps({
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  [API ERROR] {e.code}: {body[:200]}")
        return None


def call_api(system: str, user: str, api_key: str, provider: str, model: str) -> str | None:
    if provider == "openai":
        return call_openai(system, user, api_key, model)
    else:
        return call_anthropic(system, user, api_key, model)


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

    provider, api_key, model = detect_provider()
    if provider == "none" and not args.dry_run:
        print("ERROR: Set OPENAI_API_KEY or ANTHROPIC_API_KEY environment variable")
        return

    if provider != "none":
        print(f"Using {provider} API (model: {model})")

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
        print(f"Provider: {provider} ({model})")
        print(f"System prompt: {len(system)} chars")
        print(f"User prompt (batch 1): {len(user)} chars")
        print(f"First batch cards: {[c['name'] for c in batches[0]]}")
        print(f"Estimated API calls: {len(batches)}")
        est_input = (len(system) + len(user)) * len(batches) // 4
        est_output = 300 * args.batch_size * len(batches)
        print(f"Estimated input tokens: ~{est_input:,}")
        print(f"Estimated output tokens: ~{est_output:,}")
        if provider == "openai":
            # gpt-4o pricing: $2.50/M input, $10/M output
            cost = est_input * 2.5 / 1_000_000 + est_output * 10 / 1_000_000
        else:
            # Sonnet pricing: $3/M input, $15/M output
            cost = est_input * 3 / 1_000_000 + est_output * 15 / 1_000_000
        print(f"Estimated cost: ~${cost:.2f}")
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
            raw = call_api(system, user, api_key, provider, model)
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

        # Rate limiting
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"DONE: {tagged_count} tagged, {failed_count} failed, {len(existing)} skipped (already tagged)")
    print(f"Total in {OUTPUT_FILE}: {len(all_tagged)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    run()
