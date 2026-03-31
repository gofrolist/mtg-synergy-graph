"""Strategy detection, candidate filtering, and commander-based deck building."""
import sqlite3
from collections import Counter

from mtg_synergy.config import DATA_DIR


def _detect_deck_types(cards: list[dict], deck_cards: set[str],
                       threshold: float = 0.3) -> set[str]:
    """Auto-detect dominant creature types in the deck.

    If >30% of creatures share a type, it's a tribal deck for that type.
    Returns set of dominant types (e.g. {'Human'}) or empty set.
    """
    type_counts = Counter()
    creature_count = 0

    for c in cards:
        if c["name"] not in deck_cards:
            continue
        type_line = c.get("type_line", "")
        if "Creature" not in type_line:
            continue
        creature_count += 1
        if " — " in type_line:
            subtypes = type_line.split(" — ")[1].split()
            for st in subtypes:
                type_counts[st.strip(",")] += 1

    if creature_count == 0:
        return set()

    dominant = set()
    for t, count in type_counts.items():
        if count / creature_count >= threshold:
            dominant.add(t)

    if dominant:
        print(f"  Detected tribal types: {', '.join(sorted(dominant))} "
              f"(>{threshold:.0%} of {creature_count} creatures)")

    return dominant
