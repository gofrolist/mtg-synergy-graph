"""
Retag all cards using the fine-tuned mtg-tagger model via Ollama.

Sends one card at a time with the minimal system prompt the model was
trained on. Saves results to a JSON file with resume support.

Usage:
    python3 retag_finetuned.py                                    # retag all 10k cards
    python3 retag_finetuned.py --model mtg-tagger-hf              # use different model
    python3 retag_finetuned.py --input data/top10000_candidates.json  # custom input
"""

import argparse
import json
import os
import time
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

SYSTEM_PROMPT = """You are an MTG card analyst. Analyze the card and return JSON with:
- role: one of enabler, threat, draw, removal, ramp, utility, protection, land
- provides: what this card gives (kebab-case tags, e.g. token-generation, counter-placement)
- wants: what conditions make this card better (e.g. creature-etb, attack-events)

Return ONLY valid JSON. No explanation."""

OLLAMA_URL = "http://localhost:11434/api/chat"


def format_card(card: dict) -> str:
    """Format a card for the fine-tuned model (matches training format)."""
    name = card.get("name", "")
    type_line = card.get("type_line", "")
    cmc = card.get("cmc", 0)
    keywords = ", ".join(card.get("keywords", [])) or "none"

    oracle_text = card.get("oracle_text", "")
    if "card_faces" in card:
        oracle_text = card["card_faces"][0].get("oracle_text", "")

    parts = [
        f"Name: {name}",
        f"Type: {type_line}",
        f"CMC: {cmc}",
        f"Keywords: {keywords}",
        f"Oracle text: {oracle_text}",
    ]

    power = card.get("power")
    toughness = card.get("toughness")
    if power is not None and toughness is not None:
        parts.append(f"Power/Toughness: {power}/{toughness}")

    loyalty = card.get("loyalty")
    if loyalty is not None:
        parts.append(f"Loyalty: {loyalty}")

    return "\n".join(parts)


def call_ollama(user: str, model: str) -> str | None:
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data["message"]["content"].strip()
    except Exception as e:
        print(f"  [ERROR] {e}")
        return None


def parse_response(raw: str) -> dict | None:
    import re
    # Strip thinking tags
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
        if isinstance(result, list):
            result = result[0]
        if isinstance(result, dict) and "role" in result:
            return result
    except json.JSONDecodeError:
        pass

    print(f"  [PARSE ERROR] {raw[:200]}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Retag cards with fine-tuned model")
    parser.add_argument("--input", default=os.path.join(DATA_DIR, "top10000_candidates.json"),
                        help="Input candidates file")
    parser.add_argument("--output", default=os.path.join(DATA_DIR, "top10000_tags_finetuned.json"),
                        help="Output tags file")
    parser.add_argument("--model", default="mtg-tagger", help="Ollama model name")
    args = parser.parse_args()

    with open(args.input) as f:
        candidates = json.load(f)
    print(f"Loaded {len(candidates)} candidates from {args.input}")

    # Resume support
    existing = {}
    if os.path.exists(args.output):
        with open(args.output) as f:
            for card in json.load(f):
                if "oracle_id" in card:
                    existing[card["oracle_id"]] = card
        print(f"Resuming: {len(existing)} already tagged")

    todo = [c for c in candidates if c["oracle_id"] not in existing]
    print(f"Cards to tag: {len(todo)}")

    if not todo:
        print("Nothing to tag!")
        return

    all_tagged = list(existing.values())
    tagged = 0
    failed = 0
    t0 = time.time()

    for i, card in enumerate(todo, 1):
        user_prompt = format_card(card)

        raw = None
        for attempt in range(3):
            raw = call_ollama(user_prompt, args.model)
            if raw:
                break
            time.sleep(1)

        if not raw:
            print(f"  [{i}/{len(todo)}] FAILED: {card['name']}")
            failed += 1
            continue

        parsed = parse_response(raw)
        if not parsed:
            failed += 1
            continue

        # Carry over metadata from candidate
        parsed["oracle_id"] = card["oracle_id"]
        parsed["filter_pass"] = card.get("filter_pass", 1)
        parsed["filter_reason"] = card.get("filter_reason", "")
        all_tagged.append(parsed)
        tagged += 1

        # Progress
        if i % 100 == 0 or i == len(todo):
            elapsed = time.time() - t0
            rate = tagged / elapsed if elapsed > 0 else 0
            eta = (len(todo) - i) / rate if rate > 0 else 0
            print(f"  [{i}/{len(todo)}] {card['name']} — "
                  f"{tagged} tagged, {failed} failed, "
                  f"{rate:.1f} cards/s, ETA {eta/60:.0f}m")

        # Save every 200 cards
        if tagged % 200 == 0:
            with open(args.output, "w") as f:
                json.dump(all_tagged, f, indent=2)

    # Final save
    with open(args.output, "w") as f:
        json.dump(all_tagged, f, indent=2)

    elapsed = time.time() - t0
    print(f"\nDone: {tagged} tagged, {failed} failed in {elapsed/60:.1f}m")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
