"""
Prepare training data for fine-tuning a card tagger model.

Uses gpt-4.1-mini tagged cards as ground truth for provides/wants.
Excludes golden set cards (reserved for evaluation).

Usage:
    python3 prepare_training.py                    # generate train/val split
    python3 prepare_training.py --val-ratio 0.1    # 10% validation
    python3 prepare_training.py --stats            # show data quality stats
"""

import argparse
import json
import os
import random

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TAGS_FILE = os.path.join(DATA_DIR, "top10000_tags_gpt41mini.json")
SCRYFALL_FILE = os.path.join(DATA_DIR, "oracle_cards.json")
GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "golden_cards.json")

TRAIN_FILE = os.path.join(DATA_DIR, "train.jsonl")
VAL_FILE = os.path.join(DATA_DIR, "val.jsonl")

SYSTEM_PROMPT = """You are an MTG card analyst. Analyze the card and return JSON with:
- name: card name
- role: the card's primary function (ramp, draw, removal, protection, enabler, threat, utility, land)
- provides: what this card GIVES to the deck (e.g. card-draw, targeted-removal, counter-placement)
- wants: what conditions make this card BETTER (e.g. creature-death, wide-board, spell-cast)

Select tags from the controlled vocabulary used in training. Return ONLY valid JSON. No explanation."""


def load_oracle_texts() -> dict[str, dict]:
    """Load oracle texts from Scryfall data, keyed by oracle_id."""
    if not os.path.exists(SCRYFALL_FILE):
        return {}
    with open(SCRYFALL_FILE) as f:
        cards = json.load(f)

    result = {}
    for card in cards:
        oid = card.get("oracle_id")
        if not oid:
            continue
        text = card.get("oracle_text", "")
        if not text:
            faces = card.get("card_faces", [])
            if faces:
                text = " // ".join(f.get("oracle_text", "") for f in faces)
        result[oid] = {
            "name": card.get("name", ""),
            "type_line": card.get("type_line", ""),
            "oracle_text": text,
            "keywords": card.get("keywords", []),
            "cmc": card.get("cmc", 0),
        }
    return result


def card_to_user_prompt(card_info: dict) -> str:
    """Build the user prompt for a single card."""
    keywords = ", ".join(card_info.get("keywords", [])) or "none"
    parts = [
        f"Name: {card_info['name']}",
        f"Type: {card_info['type_line']}",
        f"CMC: {card_info.get('cmc', 0)}",
        f"Keywords: {keywords}",
        f"Oracle text: {card_info['oracle_text']}",
    ]
    return "\n".join(parts)


def card_to_response(name: str, role: str, provides: list, wants: list) -> str:
    """Build the expected JSON response."""
    output = {
        "name": name,
        "role": role,
        "provides": provides,
        "wants": wants,
    }
    return json.dumps(output, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description="Prepare fine-tuning training data")
    parser.add_argument("--val-ratio", type=float, default=0.05,
                        help="Validation split ratio (default: 0.05)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--stats", action="store_true",
                        help="Show stats on generated data")
    args = parser.parse_args()

    # Load tagged cards (gpt-4.1-mini output)
    with open(TAGS_FILE) as f:
        tagged_cards = json.load(f)

    # Golden cards to exclude (reserved for evaluation)
    golden_names = set()
    if os.path.exists(GOLDEN_FILE):
        with open(GOLDEN_FILE) as f:
            golden_data = json.load(f)
            golden_names = {c["name"] for c in golden_data["cards"]}
        print(f"Golden cards to exclude: {len(golden_names)}")

    # Oracle texts for card input prompts
    oracle = load_oracle_texts()
    print(f"Tagged cards: {len(tagged_cards)}")
    print(f"Oracle texts: {len(oracle)}")

    # Build training examples
    examples = []
    skipped = 0
    excluded_golden = 0

    for tagged in tagged_cards:
        oid = tagged.get("oracle_id", "")
        card_info = oracle.get(oid)
        if not card_info or not card_info.get("oracle_text"):
            skipped += 1
            continue

        name = tagged.get("name", card_info.get("name", ""))
        if name in golden_names:
            excluded_golden += 1
            continue

        provides = tagged.get("provides", [])
        wants = tagged.get("wants", [])
        role = tagged.get("role", "utility")

        # Skip cards with no tags
        if not provides and not wants:
            skipped += 1
            continue

        example = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": card_to_user_prompt(card_info)},
                {"role": "assistant", "content": card_to_response(
                    name, role, provides, wants)},
            ]
        }
        examples.append(example)

    print(f"Valid examples: {len(examples)} (skipped {skipped}, "
          f"excluded {excluded_golden} golden)")

    # Shuffle and split
    random.seed(args.seed)
    random.shuffle(examples)

    val_size = int(len(examples) * args.val_ratio)
    val_examples = examples[:val_size]
    train_examples = examples[val_size:]

    # Write JSONL
    with open(TRAIN_FILE, "w") as f:
        for ex in train_examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    with open(VAL_FILE, "w") as f:
        for ex in val_examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    print(f"\nTrain: {len(train_examples)} examples → {TRAIN_FILE}")
    print(f"Val:   {len(val_examples)} examples → {VAL_FILE}")

    # File sizes
    train_size = os.path.getsize(TRAIN_FILE) / 1024 / 1024
    val_size_mb = os.path.getsize(VAL_FILE) / 1024 / 1024
    print(f"Train file: {train_size:.1f}MB")
    print(f"Val file: {val_size_mb:.1f}MB")

    # Sample
    print(f"\nSample training example:")
    sample = train_examples[0]
    print(f"  User: {sample['messages'][1]['content'][:100]}...")
    print(f"  Asst: {sample['messages'][2]['content'][:200]}...")

    if args.stats:
        from collections import Counter
        all_provides = Counter()
        all_wants = Counter()
        roles = Counter()
        for ex in examples:
            resp = json.loads(ex["messages"][2]["content"])
            all_provides.update(resp.get("provides", []))
            all_wants.update(resp.get("wants", []))
            roles[resp.get("role", "")] += 1

        print(f"\n--- Stats ---")
        print(f"Unique provides: {len(all_provides)}")
        print(f"Unique wants:    {len(all_wants)}")
        print(f"Roles: {dict(roles.most_common())}")
        print(f"Avg provides/card: {sum(all_provides.values())/len(examples):.1f}")
        print(f"Avg wants/card:    {sum(all_wants.values())/len(examples):.1f}")


if __name__ == "__main__":
    main()
