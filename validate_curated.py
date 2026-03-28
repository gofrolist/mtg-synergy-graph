#!/usr/bin/env python3
"""Validate recommendations against hand-curated synergy pairs.

Unlike EDHREC (community inclusion rates), this measures against
expert-curated synergy pairs defined in each deck config.

Usage:
    python3 validate_curated.py              # all decks
    python3 validate_curated.py --deck krenko # single deck
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

RECOMMEND_CACHE = Path("data/recommend_cache")


def get_recommendations(deck_name: str) -> list[str]:
    """Get top-30 recommendations, with caching."""
    cache = RECOMMEND_CACHE / f"{deck_name}.json"
    if cache.exists():
        return json.loads(cache.read_text())

    result = subprocess.run(
        [sys.executable, "synergy_graph.py", "--deck", deck_name, "--recommend"],
        capture_output=True, text=True, timeout=120
    )
    cards = []
    for line in result.stdout.split("\n"):
        line = line.strip()
        if "\u2588" in line or "\u2591" in line:
            rest = re.sub(r'[\u2588\u2591]+', '', line)
            match = re.match(r'[\d.]+%\s+(.*)', rest)
            if match:
                rest = match.group(1).strip()
                if "[" in rest:
                    card_name = rest[:rest.index("[")].strip()
                else:
                    card_name = rest.strip()
                if card_name:
                    cards.append(card_name)
    return cards


def normalize(name: str) -> str:
    name = name.lower().strip()
    if " // " in name:
        name = name.split(" // ")[0].strip()
    return name


def validate_deck(deck_name: str, verbose: bool = True) -> dict:
    """Validate one deck against curated synergy pairs."""
    try:
        from decks import load_deck
    except ImportError:
        print("decks/ folder not found")
        return {}
    deck = load_deck(deck_name)

    pairs = getattr(deck, 'SYNERGY_PAIRS', [])
    if not pairs:
        if verbose:
            print(f"  {deck_name}: No curated synergy pairs, skipping")
        return {}

    # Extract unique non-deck cards from synergy pairs
    decklist = {normalize(c) for c in deck.DECKLIST} | {normalize(deck.COMMANDER)}
    curated_cards = set()
    for pair in pairs:
        a, b = pair[0], pair[1]
        if normalize(a) not in decklist:
            curated_cards.add(normalize(a))
        if normalize(b) not in decklist:
            curated_cards.add(normalize(b))

    # Get our recommendations
    recs = get_recommendations(deck_name)
    recs_set = {normalize(r) for r in recs[:30]}

    # How many curated cards are in our top 30?
    hits = curated_cards & recs_set
    total = len(curated_cards)

    if verbose:
        hit_rate = len(hits) * 100 // max(total, 1)
        print(f"  {deck_name:<15} {deck.COMMANDER:<35} {len(hits):>2}/{total} curated cards in top 30 ({hit_rate}%)")
        if verbose and curated_cards - recs_set:
            missed = sorted(curated_cards - recs_set)
            for m in missed[:5]:
                print(f"    MISSED: {m}")

    return {
        "commander": deck.COMMANDER,
        "curated_total": total,
        "curated_hits": len(hits),
        "curated_missed": sorted(curated_cards - recs_set),
    }


def main():
    parser = argparse.ArgumentParser(description="Validate against curated synergy pairs")
    parser.add_argument("--deck", help="Single deck to validate")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show missed cards")
    args = parser.parse_args()

    try:
        from decks import list_decks
    except ImportError:
        print("decks/ folder not found")
        return
    decks = [args.deck] if args.deck else list_decks()

    print(f"Validating {len(decks)} deck(s) against curated synergy pairs\n")

    results = {}
    for deck_name in decks:
        r = validate_deck(deck_name, verbose=True)
        if r:
            results[deck_name] = r

    if len(results) > 1:
        total_hits = sum(r["curated_hits"] for r in results.values())
        total_cards = sum(r["curated_total"] for r in results.values())
        print(f"\n  {'AVERAGE':<15} {'':<35} {total_hits}/{total_cards} ({total_hits * 100 // max(total_cards, 1)}%)")


if __name__ == "__main__":
    main()
