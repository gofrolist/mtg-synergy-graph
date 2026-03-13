"""
Normalize provides/wants tags to a controlled vocabulary.

Canonical vocabulary is defined in tag_registry.json (single source of truth).
This module loads the registry and maps freeform LLM tags to canonical terms.

Usage:
    python3 normalize_tags.py                           # normalize default merged file
    python3 normalize_tags.py --input data/merged.json  # normalize specific file
    python3 normalize_tags.py --stats                   # show normalization stats
    python3 normalize_tags.py --unmapped                # show tags that didn't map
"""

import argparse
import json
import os
import re
from collections import Counter, defaultdict

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "tag_registry.json")


# ── LOAD CANONICAL VOCABULARY FROM REGISTRY ──────────────────────────────────

def _load_maps_from_registry() -> tuple[dict, dict]:
    """Build PROVIDES_MAP and WANTS_MAP from tag_registry.json."""
    with open(REGISTRY_PATH) as f:
        registry = json.load(f)

    provides_map = {}
    wants_map = {}

    for tag_name, tag_data in registry["tags"].items():
        kind = tag_data.get("kind")
        if kind in ("provides", "both"):
            provides_map[tag_name] = tag_name  # identity mapping
            for alias in tag_data.get("aliases", []):
                provides_map[alias] = tag_name
        if kind in ("wants", "both"):
            wants_map[tag_name] = tag_name
            for alias in tag_data.get("aliases", []):
                wants_map[alias] = tag_name

    return provides_map, wants_map


PROVIDES_MAP, WANTS_MAP = _load_maps_from_registry()


# ── NORMALIZATION FUNCTIONS ──────────────────────────────────────────────────

def normalize_provides(tags: list[str]) -> list[str]:
    """Map provides tags to canonical vocabulary, deduplicating."""
    seen = set()
    result = []
    for tag in tags:
        canonical = PROVIDES_MAP.get(tag, tag)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def normalize_wants(tags: list[str]) -> list[str]:
    """Map wants tags to canonical vocabulary, deduplicating."""
    seen = set()
    result = []
    for tag in tags:
        canonical = WANTS_MAP.get(tag, tag)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def infer_wants(card: dict) -> list[str]:
    """Infer missing wants from card properties.

    Cards with triggered abilities benefit from trigger-doubling.
    Cards that trigger on creature ETB should want creature-etb.
    Cards that trigger on creature death should want creature-death.
    """
    inferred = []
    provides = set(card.get("provides", []))
    wants = set(card.get("wants", []))
    synergy_tags = set(card.get("synergy_tags", []))
    triggers = card.get("triggers", [])
    notes = card.get("notes", "").lower()
    role = card.get("role", "")

    # Cards with repeatable triggered abilities benefit from trigger-doubling.
    # Check for permanent triggers, repeatable effects, or key synergy tags.
    has_repeatable_triggers = (
        any(t.get("permanent", False) for t in triggers)
        or any(t in synergy_tags for t in [
            "attack trigger", "death trigger", "cast trigger",
            "triggered ability matters", "repeatable removal",
            "creaturefall",
        ])
        # Multi-trigger permanents (>1 trigger = likely a permanent with ongoing effects)
        or (len(triggers) > 1 and role not in ("removal",))
    )
    if has_repeatable_triggers and "trigger-doubling" not in wants and "trigger-doubling" not in provides:
        inferred.append("trigger-doubling")

    # Cards that care about creatures entering (ETB payoffs)
    etb_signals = [
        "etb-payoff" in provides,
        "token-generation" in provides,
        any("etb" in str(t.get("condition", "")).lower() for t in triggers),
        any("enters" in str(t.get("condition", "")).lower() for t in triggers),
        "creature enters" in notes or "enters the battlefield" in notes,
    ]
    if any(etb_signals) and "creature-etb" not in wants:
        inferred.append("creature-etb")

    # Cards that care about creatures dying (death payoffs)
    death_signals = [
        "sacrifice-payoff" in provides,
        any("dies" in str(t.get("condition", "")).lower() for t in triggers),
        any("death" in str(t.get("condition", "")).lower() for t in triggers),
        "creature dies" in notes or "leaves the battlefield" in notes,
        "counter-mover" in provides,  # Ozolith-style: saves counters on death
    ]
    if any(death_signals) and "creature-death" not in wants:
        inferred.append("creature-death")

    # Cards that pump creatures want creature-board presence
    pump_signals = [
        "creature-pump" in provides,
        "board-wide-counter-placement" in provides,
        "counter-distribution" in provides,
    ]
    if any(pump_signals) and "creature-board" not in wants:
        inferred.append("creature-board")

    # Cards that grow from counters or care about counters want counter-placement-events
    counter_signals = [
        "counter-payoff" in provides,
        "combat-enabler" in provides and "counter-placement" in provides,
        any("counter" in str(t.get("condition", "")).lower() for t in triggers),
        "grows" in notes and "counter" in notes,
    ]
    if any(counter_signals) and "counter-placement-events" not in wants:
        inferred.append("counter-placement-events")

    return inferred


def normalize_cards(cards: list[dict]) -> list[dict]:
    """Normalize provides/wants on all cards. Modifies in place and returns."""
    for card in cards:
        card["provides"] = normalize_provides(card.get("provides", []))
        card["wants"] = normalize_wants(card.get("wants", []))
        # Infer missing wants from card properties
        inferred = infer_wants(card)
        existing = set(card["wants"])
        for tag in inferred:
            if tag not in existing:
                card["wants"].append(tag)
    return cards


# ── CLI ──────────────────────────────────────────────────────────────────────

def print_stats(cards: list[dict], original_cards: list[dict]):
    """Show before/after normalization statistics."""
    # Original
    orig_p = Counter(t for c in original_cards for t in c.get("provides", []))
    orig_w = Counter(t for c in original_cards for t in c.get("wants", []))
    # Normalized
    norm_p = Counter(t for c in cards for t in c.get("provides", []))
    norm_w = Counter(t for c in cards for t in c.get("wants", []))

    print(f"\nNormalization Statistics:")
    print(f"  Provides: {len(orig_p)} unique → {len(norm_p)} unique")
    print(f"  Wants:    {len(orig_w)} unique → {len(norm_w)} unique")

    # Rare tag reduction
    orig_p_rare = sum(1 for v in orig_p.values() if v <= 2)
    norm_p_rare = sum(1 for v in norm_p.values() if v <= 2)
    orig_w_rare = sum(1 for v in orig_w.values() if v <= 2)
    norm_w_rare = sum(1 for v in norm_w.values() if v <= 2)
    print(f"\n  Provides used ≤2 times: {orig_p_rare}/{len(orig_p)} → {norm_p_rare}/{len(norm_p)}")
    print(f"  Wants used ≤2 times:    {orig_w_rare}/{len(orig_w)} → {norm_w_rare}/{len(norm_w)}")

    print(f"\n  Top 20 provides (after):")
    for tag, cnt in norm_p.most_common(20):
        print(f"    {cnt:4d}  {tag}")

    print(f"\n  Top 20 wants (after):")
    for tag, cnt in norm_w.most_common(20):
        print(f"    {cnt:4d}  {tag}")


def print_unmapped(cards: list[dict]):
    """Show tags that didn't map to any canonical term."""
    unmapped_p = Counter()
    unmapped_w = Counter()
    for card in cards:
        for t in card.get("provides", []):
            if t not in PROVIDES_MAP:
                unmapped_p[t] += 1
        for t in card.get("wants", []):
            if t not in WANTS_MAP:
                unmapped_w[t] += 1

    if unmapped_p:
        print(f"\n  Unmapped provides ({len(unmapped_p)}):")
        for tag, cnt in unmapped_p.most_common():
            print(f"    {cnt:4d}  {tag}")
    else:
        print(f"\n  All provides tags mapped!")

    if unmapped_w:
        print(f"\n  Unmapped wants ({len(unmapped_w)}):")
        for tag, cnt in unmapped_w.most_common():
            print(f"    {cnt:4d}  {tag}")
    else:
        print(f"\n  All wants tags mapped!")


def run():
    from decks import list_decks

    parser = argparse.ArgumentParser(description="Normalize provides/wants tags")
    parser.add_argument("--deck", type=str, choices=list_decks(), help="Deck config (sets default input)")
    parser.add_argument("--input", type=str, help="Merged tags JSON (default: data/<deck>_merged.json)")
    parser.add_argument("--output", help="Output file (default: overwrite input)")
    parser.add_argument("--stats", action="store_true", help="Show normalization stats")
    parser.add_argument("--unmapped", action="store_true", help="Show unmapped tags")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output")
    args = parser.parse_args()

    input_path = args.input
    if not input_path:
        deck_name = args.deck or "kyler"
        input_path = os.path.join(DATA_DIR, f"{deck_name}_merged.json")

    with open(input_path) as f:
        cards = json.load(f)

    import copy
    original = copy.deepcopy(cards)

    # Check unmapped before normalizing
    if args.unmapped:
        print_unmapped(cards)

    normalize_cards(cards)

    if args.stats or True:  # always show stats
        print_stats(cards, original)

    if args.unmapped:
        print("\n  After normalization:")
        # Check what's still unmapped (shouldn't be anything mapped now,
        # but original unmapped tags pass through as-is)
        remaining_p = set()
        remaining_w = set()
        for card in original:
            for t in card.get("provides", []):
                if t not in PROVIDES_MAP:
                    remaining_p.add(t)
            for t in card.get("wants", []):
                if t not in WANTS_MAP:
                    remaining_w.add(t)
        if remaining_p:
            print(f"    {len(remaining_p)} provides tags pass through unchanged")
        if remaining_w:
            print(f"    {len(remaining_w)} wants tags pass through unchanged")

    if not args.dry_run:
        output = args.output or input_path
        with open(output, "w") as f:
            json.dump(cards, f, indent=2)
        print(f"\nWrote normalized tags to {output}")


if __name__ == "__main__":
    run()
