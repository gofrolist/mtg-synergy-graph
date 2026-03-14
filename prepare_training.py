"""
Prepare training data for fine-tuning a card tagger model.

Converts the 10k tagged cards into instruction-following format for
supervised fine-tuning (SFT) with unsloth/LoRA.

Usage:
    python3 prepare_training.py                    # generate train/val split
    python3 prepare_training.py --format chat      # chat format (default)
    python3 prepare_training.py --format alpaca    # alpaca format
    python3 prepare_training.py --val-ratio 0.1    # 10% validation
"""

import argparse
import json
import os
import random

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TAGS_FILE = os.path.join(DATA_DIR, "top10000_tags.json")
SCRYFALL_FILE = os.path.join(DATA_DIR, "oracle_cards.json")

TRAIN_FILE = os.path.join(DATA_DIR, "train.jsonl")
VAL_FILE = os.path.join(DATA_DIR, "val.jsonl")

# Compact system prompt for fine-tuning (shorter than production prompt)
SYSTEM_PROMPT = """You are an MTG card analyst. Analyze the card and return JSON with:
- role: one of enabler, threat, draw, removal, ramp, utility, protection, land
- provides: what this card gives (kebab-case tags, e.g. token-generation, counter-placement)
- wants: what conditions make this card better (e.g. creature-etb, attack-events)

Return ONLY valid JSON. No explanation."""


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
    return (
        f"Name: {card_info['name']}\n"
        f"Type: {card_info['type_line']}\n"
        f"CMC: {card_info.get('cmc', 0)}\n"
        f"Keywords: {keywords}\n"
        f"Oracle text: {card_info['oracle_text']}"
    )


def card_to_response(tagged: dict) -> str:
    """Build the expected JSON response."""
    output = {
        "name": tagged["name"],
        "role": tagged.get("role", ""),
        "provides": tagged.get("provides", []),
        "wants": tagged.get("wants", []),
    }
    return json.dumps(output, separators=(",", ":"))


def prepare_chat_format(tagged: dict, card_info: dict) -> dict:
    """Convert to chat/conversational format for SFT."""
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": card_to_user_prompt(card_info)},
            {"role": "assistant", "content": card_to_response(tagged)},
        ]
    }


def prepare_alpaca_format(tagged: dict, card_info: dict) -> dict:
    """Convert to alpaca instruction format."""
    return {
        "instruction": SYSTEM_PROMPT,
        "input": card_to_user_prompt(card_info),
        "output": card_to_response(tagged),
    }


def main():
    parser = argparse.ArgumentParser(description="Prepare fine-tuning training data")
    parser.add_argument("--format", choices=["chat", "alpaca"], default="chat",
                        help="Output format (default: chat)")
    parser.add_argument("--val-ratio", type=float, default=0.05,
                        help="Validation split ratio (default: 0.05)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    # Load tagged cards
    with open(TAGS_FILE) as f:
        tagged_cards = json.load(f)

    # Load oracle texts (needed for input prompts)
    oracle = load_oracle_texts()
    print(f"Tagged cards: {len(tagged_cards)}")
    print(f"Oracle texts: {len(oracle)}")

    # Match tagged cards with oracle texts
    examples = []
    skipped = 0
    for tagged in tagged_cards:
        oid = tagged.get("oracle_id", "")
        card_info = oracle.get(oid)
        if not card_info or not card_info.get("oracle_text"):
            skipped += 1
            continue

        if not tagged.get("provides") and not tagged.get("wants"):
            skipped += 1
            continue

        if args.format == "chat":
            example = prepare_chat_format(tagged, card_info)
        else:
            example = prepare_alpaca_format(tagged, card_info)
        examples.append(example)

    print(f"Valid examples: {len(examples)} (skipped {skipped})")

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

    # Stats
    train_size = os.path.getsize(TRAIN_FILE) / 1024 / 1024
    val_size_mb = os.path.getsize(VAL_FILE) / 1024 / 1024
    print(f"Train file: {train_size:.1f}MB")
    print(f"Val file: {val_size_mb:.1f}MB")

    # Sample
    print(f"\nSample training example:")
    sample = train_examples[0]
    if args.format == "chat":
        print(f"  User: {sample['messages'][1]['content'][:100]}...")
        print(f"  Asst: {sample['messages'][2]['content'][:150]}...")
    else:
        print(f"  Input: {sample['input'][:100]}...")
        print(f"  Output: {sample['output'][:150]}...")


if __name__ == "__main__":
    main()
