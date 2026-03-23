"""
Batch tagger — sends cards to LLM API in batches of 5.

Supports Anthropic (Claude), OpenAI (GPT), and Ollama (local) APIs.

Usage:
    python3 batch_tagger.py --deck kyler
    python3 batch_tagger.py --deck krenko --provider ollama --model phi4:14b
    python3 batch_tagger.py --deck kyler --output data/kyler_tags_phi4.json
    python3 batch_tagger.py --deck kyler --batch-size 3
    python3 batch_tagger.py --deck kyler --dry-run
"""

import argparse
import json
import os
import re
import time
import urllib.request
import urllib.error

from prompt_builder import build_batch_prompt, build_review_prompt, build_retag_prompt

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CANDIDATES_FILE = None  # set by --deck
OUTPUT_FILE = None  # set by --deck or --output

MAX_TOKENS = 2048  # 10 cards × ~100 tokens/card output + safety margin
MAX_REVIEW_ITERATIONS = 2


OLLAMA_URL = "http://localhost:11434/api/chat"


def detect_provider(cli_provider=None, cli_model=None) -> tuple[str, str, str]:
    """Detect API provider from CLI args or environment. Returns (provider, api_key, model)."""
    if cli_provider == "ollama":
        return "ollama", "", cli_model or "phi4:14b"

    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if openai_key:
        return "openai", openai_key, cli_model or "gpt-4o"
    elif anthropic_key:
        return "anthropic", anthropic_key, cli_model or "claude-sonnet-4-20250514"
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
        if e.code == 429:
            retry_after = e.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else 10
            print(f"  [RATE LIMITED] Waiting {wait}s...")
            time.sleep(wait)
            # Single retry — no recursion to avoid infinite loops
            try:
                with urllib.request.urlopen(req) as resp2:
                    data2 = json.loads(resp2.read())
                    return data2["choices"][0]["message"]["content"].strip()
            except Exception:
                return None
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


def call_ollama(system: str, user: str, model: str) -> str | None:
    """Send a message to Ollama API. Returns raw text response."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
            return data["message"]["content"].strip()
    except Exception as e:
        print(f"  [OLLAMA ERROR] {e}")
        return None


def call_api(system: str, user: str, api_key: str, provider: str, model: str) -> str | None:
    if provider == "openai":
        return call_openai(system, user, api_key, model)
    elif provider == "ollama":
        return call_ollama(system, user, model)
    else:
        return call_anthropic(system, user, api_key, model)


def review_batch(batch: list[dict], tagged: list[dict], api_key: str,
                  provider: str, model: str) -> list[dict] | None:
    """Send tagged cards to reviewer agent. Returns list of review verdicts."""
    system, user = build_review_prompt(batch, tagged)
    raw = call_api(system, user, api_key, provider, model)
    if not raw:
        return None
    parsed = parse_response(raw, 1)  # Single JSON object with "cards" array
    if not parsed:
        return None
    # Handle both {"cards": [...]} and [{...}] formats
    result = parsed[0] if isinstance(parsed, list) else parsed
    if isinstance(result, dict) and "cards" in result:
        return result["cards"]
    # If the model returned a flat list of card reviews
    return parsed


def retag_with_feedback(batch: list[dict], tagged: list[dict], reviews: list[dict],
                        api_key: str, provider: str, model: str,
                        rules_context: str = "") -> list[dict] | None:
    """Re-tag cards that the reviewer flagged, injecting feedback into the prompt."""
    system, user = build_retag_prompt(batch, tagged, reviews, rules_context=rules_context)
    if not system:
        return tagged  # Nothing to revise

    # Find which cards need re-tagging
    revise_indices = [i for i, r in enumerate(reviews) if r.get("verdict") == "REVISE"]

    raw = call_api(system, user, api_key, provider, model)
    if not raw:
        return tagged  # Keep original on failure

    retagged = parse_response(raw, len(revise_indices))
    if not retagged:
        return tagged  # Keep original on failure

    # Merge re-tagged cards back into the full batch
    result = list(tagged)
    for new_tags, idx in zip(retagged, revise_indices):
        result[idx] = new_tags
    return result


def parse_response(raw: str, expected_count: int) -> list[dict] | None:
    """Parse JSON array from API response. Returns list of tagged card dicts."""
    # Strip thinking tags (e.g. qwen3)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # Strip markdown fences if present
    raw = re.sub(r"^```json\s*", "", raw.strip())
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
    from decks import list_decks

    parser = argparse.ArgumentParser(description="Batch card tagger")
    parser.add_argument("--deck", choices=list_decks(), help="Deck config to use")
    parser.add_argument("--candidates", type=str, help="Custom candidates JSON file (alternative to --deck)")
    parser.add_argument("--batch-size", type=int, default=10, help="Cards per API call")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without calling API")
    parser.add_argument("--provider", choices=["openai", "anthropic", "ollama"], help="API provider (auto-detected from env if not set)")
    parser.add_argument("--model", type=str, help="Model name override")
    parser.add_argument("--output", type=str, help="Output file path override")
    parser.add_argument("--rag", action="store_true", help="Enable RAG — inject relevant MTG rules into prompts")
    parser.add_argument("--review", action="store_true", help="Enable multi-agent review loop — a reviewer LLM checks tags and triggers re-tagging on errors")
    args = parser.parse_args()

    if not args.deck and not args.candidates:
        parser.error("Either --deck or --candidates is required")

    global CANDIDATES_FILE, OUTPUT_FILE
    if args.candidates:
        CANDIDATES_FILE = args.candidates
        # Derive output from candidates filename: top5000_candidates.json → top5000_tags.json
        base = os.path.basename(args.candidates).replace("_candidates", "_tags")
        OUTPUT_FILE = os.path.join(os.path.dirname(args.candidates) or DATA_DIR, base)
    else:
        CANDIDATES_FILE = os.path.join(DATA_DIR, f"{args.deck}_candidates.json")
        OUTPUT_FILE = os.path.join(DATA_DIR, f"{args.deck}_tags.json")
    if args.output:
        OUTPUT_FILE = args.output

    provider, api_key, model = detect_provider(args.provider, args.model)
    if provider == "none" and not args.dry_run:
        print("ERROR: Set OPENAI_API_KEY or ANTHROPIC_API_KEY, or use --provider ollama")
        return

    retrieve_rules = None
    if args.rag:
        try:
            from rules_retriever import retrieve_rules_for_cards
            retrieve_rules = retrieve_rules_for_cards
            print("RAG enabled — will inject relevant MTG rules into prompts")
        except Exception as e:
            print(f"WARNING: Could not load RAG pipeline: {e}")
            print("Run: python3 rules_fetcher.py && python3 rules_index.py")
            print("Continuing without RAG...")

    review_enabled = args.review
    if review_enabled:
        print("Review loop enabled — reviewer agent will check tags after each batch")

    if provider != "none":
        print(f"Using {provider} (model: {model})")

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

        rules_context = ""
        if retrieve_rules:
            rules_context = retrieve_rules(batch)

        system, user = build_batch_prompt(batch, rules_context=rules_context)

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

        # Multi-agent review loop
        if review_enabled:
            final_tags = parsed
            for review_iter in range(MAX_REVIEW_ITERATIONS):
                print(f"  Review pass {review_iter + 1}/{MAX_REVIEW_ITERATIONS}...")
                reviews = review_batch(batch, final_tags, api_key, provider, model)
                if not reviews:
                    print(f"  Review failed — keeping current tags")
                    break

                revise_count = sum(1 for r in reviews if r.get("verdict") == "REVISE")
                approve_count = len(reviews) - revise_count
                print(f"  Review: {approve_count} approved, {revise_count} need revision")

                if revise_count == 0:
                    break

                # Show what the reviewer flagged
                for r in reviews:
                    if r.get("verdict") == "REVISE":
                        issues = r.get("issues", [])
                        print(f"    {r.get('name', '?')}: {'; '.join(issues[:3])}")

                # Re-tag with feedback
                print(f"  Re-tagging {revise_count} cards with reviewer feedback...")
                final_tags = retag_with_feedback(
                    batch, final_tags, reviews, api_key, provider, model,
                    rules_context=rules_context,
                )

                if provider != "ollama":
                    time.sleep(0.5)
            parsed = final_tags

        # Attach oracle_id from candidates to tagged results
        for card_data, tagged in zip(batch, parsed):
            tagged["oracle_id"] = card_data["oracle_id"]
            all_tagged.append(tagged)
            tagged_count += 1

        # Save after each batch (crash-safe)
        save_tags(all_tagged)
        print(f"  Tagged {len(parsed)} cards (total: {len(all_tagged)})")

        # Rate limiting (skip for local models)
        if provider != "ollama":
            time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"DONE: {tagged_count} tagged, {failed_count} failed, {len(existing)} skipped (already tagged)")
    print(f"Total in {OUTPUT_FILE}: {len(all_tagged)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    run()
