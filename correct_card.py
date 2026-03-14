"""
Correct a card's tags and propagate the fix to all data sources.

Updates three places:
  1. Tags JSON file (top10000_tags.json or _finetuned.json) — immediate fix
  2. Training data (train.jsonl / val.jsonl) — model learns on next fine-tune
  3. Golden cards (golden_cards.json) — regression test catches future regressions

Usage:
    # Fix a role
    python3 correct_card.py "Ghostly Flicker" --role utility

    # Fix role + provides + wants
    python3 correct_card.py "Maester Seymour" --role enabler \
        --provides counter-placement,board-wide-counter-placement \
        --wants counter-placement-events,creature-power

    # Fix and add to golden dataset
    python3 correct_card.py "Chaos Warp" --role removal --golden

    # Use fine-tuned tags file
    python3 correct_card.py "Ghostly Flicker" --role utility \
        --tags-file data/top10000_tags_finetuned.json

    # Show current tags without changing anything
    python3 correct_card.py "Ghostly Flicker" --show

    # Batch corrections from a JSON file
    python3 correct_card.py --batch corrections_batch.json
"""

import argparse
import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
GOLDEN_FILE = os.path.join(os.path.dirname(__file__), "golden_cards.json")
TRAIN_FILE = os.path.join(DATA_DIR, "train.jsonl")
VAL_FILE = os.path.join(DATA_DIR, "val.jsonl")
SCRYFALL_FILE = os.path.join(DATA_DIR, "oracle_cards.json")

SYSTEM_PROMPT = """You are an MTG card analyst. Analyze the card and return JSON with:
- role: one of enabler, threat, draw, removal, ramp, utility, protection, land
- provides: what this card gives (kebab-case tags, e.g. token-generation, counter-placement)
- wants: what conditions make this card better (e.g. creature-etb, attack-events)

Return ONLY valid JSON. No explanation."""

VALID_ROLES = {"enabler", "threat", "draw", "removal", "ramp", "utility", "protection", "land"}


def load_tags_file(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def save_tags_file(path: str, cards: list[dict]):
    with open(path, "w") as f:
        json.dump(cards, f, indent=2)


def find_card(cards: list[dict], name: str) -> dict | None:
    """Find a card by name (case-insensitive)."""
    name_lower = name.lower()
    for card in cards:
        if card.get("name", "").lower() == name_lower:
            return card
    return None


def get_scryfall_info(oracle_id: str) -> dict | None:
    """Get card info from Scryfall bulk data."""
    if not os.path.exists(SCRYFALL_FILE):
        return None
    with open(SCRYFALL_FILE) as f:
        for card in json.load(f):
            if card.get("oracle_id") == oracle_id:
                text = card.get("oracle_text", "")
                if not text:
                    faces = card.get("card_faces", [])
                    if faces:
                        text = " // ".join(f.get("oracle_text", "") for f in faces)
                return {
                    "name": card.get("name", ""),
                    "type_line": card.get("type_line", ""),
                    "oracle_text": text,
                    "keywords": card.get("keywords", []),
                    "cmc": card.get("cmc", 0),
                    "color_identity": card.get("color_identity", []),
                    "power": card.get("power"),
                    "toughness": card.get("toughness"),
                }
    return None


def card_to_user_prompt(card_info: dict) -> str:
    """Build the user prompt matching training format."""
    keywords = ", ".join(card_info.get("keywords", [])) or "none"
    parts = [
        f"Name: {card_info['name']}",
        f"Type: {card_info['type_line']}",
        f"CMC: {card_info.get('cmc', 0)}",
        f"Keywords: {keywords}",
        f"Oracle text: {card_info['oracle_text']}",
    ]
    if card_info.get("power") is not None:
        parts.append(f"Power/Toughness: {card_info['power']}/{card_info['toughness']}")
    return "\n".join(parts)


def card_to_response(tagged: dict) -> str:
    """Build the expected JSON response matching training format."""
    output = {
        "name": tagged["name"],
        "role": tagged.get("role", ""),
        "provides": tagged.get("provides", []),
        "wants": tagged.get("wants", []),
    }
    return json.dumps(output, separators=(",", ":"))


# ── Step 1: Update tags file ──

def update_tags_file(tags_path: str, name: str, corrections: dict) -> dict:
    """Apply corrections to the tags file. Returns the corrected card."""
    cards = load_tags_file(tags_path)
    card = find_card(cards, name)
    if not card:
        print(f"  Card '{name}' not found in {tags_path}")
        return None

    changed = []
    if "role" in corrections:
        old = card.get("role")
        card["role"] = corrections["role"]
        changed.append(f"role: {old} → {corrections['role']}")
    if "provides" in corrections:
        old = card.get("provides", [])
        card["provides"] = corrections["provides"]
        changed.append(f"provides: {old} → {corrections['provides']}")
    if "wants" in corrections:
        old = card.get("wants", [])
        card["wants"] = corrections["wants"]
        changed.append(f"wants: {old} → {corrections['wants']}")

    if changed:
        save_tags_file(tags_path, cards)
        print(f"  [tags] Updated {tags_path}")
        for c in changed:
            print(f"         {c}")
    else:
        print(f"  [tags] No changes needed")

    return card


# ── Step 2: Update training data ──

def update_training_data(card: dict, card_info: dict):
    """Update or insert the card in train.jsonl."""
    if not card_info:
        print(f"  [train] Skipped — no Scryfall data for oracle_id")
        return

    # Build the corrected training example
    new_example = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": card_to_user_prompt(card_info)},
            {"role": "assistant", "content": card_to_response(card)},
        ]
    }

    # Load existing training data
    train_examples = []
    replaced = False
    with open(TRAIN_FILE) as f:
        for line in f:
            ex = json.loads(line)
            # Match by card name in the user message
            user_msg = ex["messages"][1]["content"]
            if user_msg.startswith(f"Name: {card['name']}\n"):
                train_examples.append(new_example)
                replaced = True
            else:
                train_examples.append(ex)

    if not replaced:
        # Also check val.jsonl and move to train if found there
        val_examples = []
        with open(VAL_FILE) as f:
            for line in f:
                ex = json.loads(line)
                user_msg = ex["messages"][1]["content"]
                if user_msg.startswith(f"Name: {card['name']}\n"):
                    # Move corrected version to train instead
                    train_examples.append(new_example)
                    replaced = True
                else:
                    val_examples.append(ex)

        if replaced:
            with open(VAL_FILE, "w") as f:
                for ex in val_examples:
                    f.write(json.dumps(ex, ensure_ascii=False) + "\n")
            print(f"  [train] Moved from val.jsonl to train.jsonl (corrected)")
        else:
            # Card not in either file — append to train
            train_examples.append(new_example)
            print(f"  [train] Added new example to train.jsonl")

    if replaced and not any("Moved" in line for line in []):
        print(f"  [train] Updated existing example in train.jsonl")

    with open(TRAIN_FILE, "w") as f:
        for ex in train_examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    print(f"  [train] train.jsonl: {len(train_examples)} examples")


# ── Step 3: Update golden cards ──

def update_golden(card: dict, card_info: dict):
    """Add or update the card in golden_cards.json."""
    with open(GOLDEN_FILE) as f:
        golden = json.load(f)

    # Check if card already exists
    existing = None
    for i, g in enumerate(golden["cards"]):
        if g["name"] == card["name"]:
            existing = i
            break

    # Get scryfall tags from DB
    scryfall_tags = []
    try:
        from tag_db import get_scryfall_tags
        from card_db import NAME_INDEX
        oid = card.get("oracle_id") or NAME_INDEX.get(card["name"].lower())
        if oid:
            scryfall_tags = [t["tag"] for t in get_scryfall_tags(oid)]
    except Exception:
        pass

    golden_entry = {
        "name": card["name"],
        "type_line": card_info.get("type_line", "") if card_info else "",
        "oracle_text": card_info.get("oracle_text", "") if card_info else "",
        "keywords": card_info.get("keywords", []) if card_info else [],
        "cmc": card_info.get("cmc", 0) if card_info else 0,
        "scryfall_tags": scryfall_tags,
        "expected": {
            "role": card.get("role", ""),
            "synergy_tags_must_include": [],
            "synergy_tags_must_exclude": [],
            "provides_must_include": card.get("provides", []),
            "wants_must_include": card.get("wants", []),
        },
    }

    if existing is not None:
        golden["cards"][existing] = golden_entry
        print(f"  [golden] Updated existing entry in golden_cards.json")
    else:
        golden["cards"].append(golden_entry)
        print(f"  [golden] Added new entry to golden_cards.json ({len(golden['cards'])} total)")

    with open(GOLDEN_FILE, "w") as f:
        json.dump(golden, f, indent=2)


# ── Show card ──

def show_card(name: str, tags_path: str):
    """Display current tags for a card."""
    cards = load_tags_file(tags_path)
    card = find_card(cards, name)
    if not card:
        print(f"Card '{name}' not found in {tags_path}")
        return

    print(f"  Name:     {card['name']}")
    print(f"  Role:     {card.get('role', '?')}")
    print(f"  Provides: {card.get('provides', [])}")
    print(f"  Wants:    {card.get('wants', [])}")

    oid = card.get("oracle_id")
    if oid:
        info = get_scryfall_info(oid)
        if info:
            print(f"  Type:     {info['type_line']}")
            print(f"  Text:     {info['oracle_text'][:200]}")


# ── Batch corrections ──

def load_batch(path: str) -> list[dict]:
    """Load batch corrections from JSON file.

    Format: [{"name": "Card Name", "role": "enabler", "provides": [...], "wants": [...]}, ...]
    """
    with open(path) as f:
        return json.load(f)


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description="Correct card tags and propagate to all data sources")
    parser.add_argument("card_name", nargs="?", help="Card name to correct")
    parser.add_argument("--role", help="Corrected role")
    parser.add_argument("--provides", help="Corrected provides (comma-separated)")
    parser.add_argument("--wants", help="Corrected wants (comma-separated)")
    parser.add_argument("--golden", action="store_true",
                        help="Also add/update in golden_cards.json")
    parser.add_argument("--tags-file",
                        default=os.path.join(DATA_DIR, "top10000_tags.json"),
                        help="Tags file to update (default: top10000_tags.json)")
    parser.add_argument("--show", action="store_true",
                        help="Show current tags without making changes")
    parser.add_argument("--batch", help="Batch corrections from JSON file")
    args = parser.parse_args()

    if args.batch:
        corrections_list = load_batch(args.batch)
        print(f"Batch: {len(corrections_list)} corrections from {args.batch}")
        for corr in corrections_list:
            name = corr.pop("name")
            print(f"\n{'─' * 50}")
            print(f"Correcting: {name}")
            _apply_correction(name, corr, args.tags_file, golden=args.golden)
        print(f"\nDone: {len(corrections_list)} cards corrected")
        return

    if not args.card_name:
        parser.error("card_name is required (or use --batch)")

    if args.show:
        show_card(args.card_name, args.tags_file)
        return

    # Build corrections dict
    corrections = {}
    if args.role:
        if args.role not in VALID_ROLES:
            parser.error(f"Invalid role '{args.role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}")
        corrections["role"] = args.role
    if args.provides is not None:
        corrections["provides"] = [t.strip() for t in args.provides.split(",") if t.strip()]
    if args.wants is not None:
        corrections["wants"] = [t.strip() for t in args.wants.split(",") if t.strip()]

    if not corrections:
        parser.error("Specify at least one of: --role, --provides, --wants")

    _apply_correction(args.card_name, corrections, args.tags_file, golden=args.golden)


def _apply_correction(name: str, corrections: dict, tags_file: str, golden: bool = False):
    """Apply a single card correction to all data sources."""
    print(f"\nCorrecting '{name}':")

    # Step 1: Update tags file
    card = update_tags_file(tags_file, name, corrections)
    if not card:
        return

    # Step 2: Update training data
    oid = card.get("oracle_id")
    card_info = get_scryfall_info(oid) if oid else None
    update_training_data(card, card_info)

    # Step 3: Update golden cards (if requested)
    if golden:
        update_golden(card, card_info)

    print(f"  Done. Correction will take effect on next fine-tune run.")


if __name__ == "__main__":
    main()
