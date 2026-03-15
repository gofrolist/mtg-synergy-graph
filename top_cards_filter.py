"""
Extract top N cards by EDHREC rank from Scryfall bulk data.

Produces a candidates file compatible with batch_tagger.py.

Usage:
    python3 top_cards_filter.py --top 5000
    python3 top_cards_filter.py --top 5000 --output data/top5000_candidates.json
"""

import argparse
import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SCRYFALL_FILE = os.path.join(DATA_DIR, "oracle_cards.json")


def get_oracle_text(card: dict) -> str:
    """Get oracle text, handling multi-face cards."""
    text = card.get("oracle_text", "")
    if not text:
        faces = card.get("card_faces", [])
        if faces:
            text = " // ".join(f.get("oracle_text", "") for f in faces)
    return text


def get_type_line(card: dict) -> str:
    return card.get("type_line", "")


def extract_top_cards(n: int) -> list[dict]:
    """Extract top N cards by edhrec_rank from Scryfall data."""
    print(f"Loading Scryfall data from {SCRYFALL_FILE}...")
    with open(SCRYFALL_FILE) as f:
        all_cards = json.load(f)

    # Filter: must have edhrec_rank, oracle_text, and not be a basic land
    eligible = []
    for card in all_cards:
        if "edhrec_rank" not in card:
            continue
        type_line = get_type_line(card)
        if "Basic Land" in type_line:
            continue
        # Skip tokens, emblems, etc.
        layout = card.get("layout", "")
        if layout in ("token", "emblem", "art_series", "double_faced_token"):
            continue
        # Must have some text to tag
        oracle_text = get_oracle_text(card)
        if not oracle_text and "Land" not in type_line:
            continue
        eligible.append(card)

    # Sort by edhrec_rank (lower = more popular)
    eligible.sort(key=lambda c: c["edhrec_rank"])

    # Take top N
    top = eligible[:n]
    print(f"Selected {len(top)} cards from {len(eligible)} eligible (out of {len(all_cards)} total)")

    # Convert to candidates format
    candidates = []
    for card in top:
        candidates.append({
            "name": card["name"],
            "oracle_id": card["oracle_id"],
            "type_line": get_type_line(card),
            "oracle_text": get_oracle_text(card),
            "keywords": card.get("keywords", []),
            "cmc": card.get("cmc", 0),
            "color_identity": card.get("color_identity", []),
            "edhrec_rank": card["edhrec_rank"],
        })

    return candidates


def main():
    parser = argparse.ArgumentParser(description="Extract top EDH cards by EDHREC rank")
    parser.add_argument("--top", type=int, default=5000, help="Number of top cards to extract")
    parser.add_argument("--output", type=str, help="Output file path")
    args = parser.parse_args()

    output = args.output or os.path.join(DATA_DIR, f"top{args.top}_candidates.json")
    candidates = extract_top_cards(args.top)

    with open(output, "w") as f:
        json.dump(candidates, f, indent=2)

    print(f"\nSaved {len(candidates)} candidates to {output}")
    print(f"\nTop 10:")
    for c in candidates[:10]:
        print(f"  #{c['edhrec_rank']:>5} {c['name']}")
    print(f"\nBottom 10:")
    for c in candidates[-10:]:
        print(f"  #{c['edhrec_rank']:>5} {c['name']}")


if __name__ == "__main__":
    main()
