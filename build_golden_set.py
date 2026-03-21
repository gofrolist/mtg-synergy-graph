"""
Build a golden evaluation set from EDHREC + Scryfall ground truth.

Cross-references EDHREC theme memberships, Scryfall function tags, and
oracle card data to create verified card tags without LLM involvement.

Samples cards balanced by card type, theme coverage, and synergy level.

Usage:
    python3 build_golden_set.py                    # build 500-card golden set
    python3 build_golden_set.py --size 1000        # larger set
    python3 build_golden_set.py --stats            # analyze existing golden set
"""

import argparse
import json
import os
import random

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
EDHREC_FILE = os.path.join(DATA_DIR, "edhrec_theme_cards.json")
SCRYFALL_TAGS_FILE = os.path.join(DATA_DIR, "scryfall_function_tags.json")
ORACLE_FILE = os.path.join(DATA_DIR, "oracle_cards.json")
REGISTRY_FILE = os.path.join(os.path.dirname(__file__), "synergy_tag_registry.json")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "golden_cards.json")


def card_category(type_line: str) -> str:
    """Classify card by type for balanced sampling."""
    if "Land" in type_line:
        return "land"
    elif "Creature" in type_line:
        return "creature"
    elif "Instant" in type_line:
        return "instant"
    elif "Sorcery" in type_line:
        return "sorcery"
    elif "Artifact" in type_line:
        return "artifact"
    elif "Enchantment" in type_line:
        return "enchantment"
    elif "Planeswalker" in type_line:
        return "planeswalker"
    return "other"


def build_function_map(registry: dict) -> dict:
    """Map Scryfall tag names → our function tag names."""
    func_map = {}
    for category, tags in registry["function_tags"].items():
        if category.startswith("_"):
            continue
        for our_tag, info in tags.items():
            for sf_tag in info.get("scryfall", []):
                func_map[sf_tag] = our_tag
    return func_map


def main():
    parser = argparse.ArgumentParser(description="Build golden evaluation set")
    parser.add_argument("--size", type=int, default=500,
                        help="Target golden set size (default: 500)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--stats", action="store_true",
                        help="Show stats on existing golden set")
    args = parser.parse_args()

    if args.stats:
        with open(OUTPUT_FILE) as f:
            golden = json.load(f)
        cards = golden["cards"]
        print(f"Golden set: {len(cards)} cards")
        from collections import Counter
        cats = Counter(card_category(c.get("type_line", "")) for c in cards)
        print("\nBy type:")
        for cat, count in cats.most_common():
            print(f"  {cat:<15} {count:>4} ({count/len(cards)*100:.1f}%)")

        theme_counts = [len(c["expected"]["themes"]) for c in cards]
        func_counts = [len(c["expected"]["function"]) for c in cards]
        prov_counts = [len(c["expected"].get("provides_must_include", [])) for c in cards]
        wants_counts = [len(c["expected"].get("wants_must_include", [])) for c in cards]
        print(f"\nThemes per card:   avg={sum(theme_counts)/len(theme_counts):.1f}, "
              f"min={min(theme_counts)}, max={max(theme_counts)}")
        print(f"Functions per card: avg={sum(func_counts)/len(func_counts):.1f}, "
              f"min={min(func_counts)}, max={max(func_counts)}")
        print(f"Provides per card:  avg={sum(prov_counts)/len(prov_counts):.1f}")
        print(f"Wants per card:     avg={sum(wants_counts)/len(wants_counts):.1f}")

        all_themes = Counter()
        for c in cards:
            all_themes.update(c["expected"]["themes"])
        print(f"\nUnique themes covered: {len(all_themes)}")
        print("Top 20 themes:")
        for theme, count in all_themes.most_common(20):
            print(f"  {theme:<30} {count:>4}")
        return

    # Load data
    with open(EDHREC_FILE) as f:
        edhrec = json.load(f)
    with open(SCRYFALL_TAGS_FILE) as f:
        scryfall_raw = json.load(f)
    with open(ORACLE_FILE) as f:
        oracle_raw = json.load(f)
    with open(REGISTRY_FILE) as f:
        registry = json.load(f)

    # Build lookups
    scryfall = {}
    for card in scryfall_raw:
        name = card.get("name", "")
        tags = card.get("tags", card.get("function_tags", card.get("scryfall_tags", [])))
        if name and tags:
            scryfall[name] = set(tags)

    oracle_by_name = {}
    for c in oracle_raw:
        name = c.get("name", "")
        if name and name not in oracle_by_name:
            oracle_by_name[name] = c

    edhrec_cards = {}
    for theme, cards in edhrec.items():
        for card in cards:
            name = card["name"]
            if name not in edhrec_cards:
                edhrec_cards[name] = []
            edhrec_cards[name].append({
                "theme": theme,
                "synergy": card.get("synergy", 0),
                "inclusion": card.get("inclusion", 0),
                "section": card.get("section", ""),
            })

    func_map = build_function_map(registry)
    valid_themes = set(registry["themes"]["archetype"] + registry["themes"]["typal"])

    # Build candidate pool
    candidates = []
    for name in set(edhrec_cards.keys()) & set(scryfall.keys()) & set(oracle_by_name.keys()):
        oracle_card = oracle_by_name[name]
        themes_data = edhrec_cards[name]
        sf_tags = scryfall[name]

        # Map scryfall → function tags
        functions = sorted({func_map[t] for t in sf_tags if t in func_map})

        # Get themes (only ones in our registry)
        all_themes = sorted({t["theme"] for t in themes_data if t["theme"] in valid_themes})
        positive_themes = sorted({t["theme"] for t in themes_data
                                  if t["synergy"] > 0.05 and t["theme"] in valid_themes})
        high_synergy_themes = sorted({t["theme"] for t in themes_data
                                      if t["synergy"] > 0.15 and t["theme"] in valid_themes})

        # Must have at least 1 function and 1 positive theme
        if not functions or not positive_themes:
            continue

        cat = card_category(oracle_card.get("type_line", ""))

        # Quality score: prefer cards with more high-synergy themes + more functions
        quality = len(high_synergy_themes) * 3 + len(positive_themes) + len(functions)

        oracle_text = oracle_card.get("oracle_text", "")
        if not oracle_text:
            faces = oracle_card.get("card_faces", [])
            if faces:
                oracle_text = " // ".join(f.get("oracle_text", "") for f in faces)

        candidates.append({
            "name": name,
            "category": cat,
            "quality": quality,
            "oracle_card": oracle_card,
            "oracle_text": oracle_text,
            "functions": functions,
            "all_themes": all_themes,
            "positive_themes": positive_themes,
            "high_synergy_themes": high_synergy_themes,
            "scryfall_tags": sorted(sf_tags),
        })

    print(f"Total candidates: {len(candidates)}")

    # Force-include iconic EDH staples that must be in any golden set
    # These are format-defining cards every tagger must handle correctly
    MUST_INCLUDE = [
        "Sol Ring", "Skullclamp", "Cyclonic Rift", "Rhystic Study",
        "Smothering Tithe", "Swords to Plowshares", "Path to Exile",
        "Blood Artist", "Zulaport Cutthroat", "Grave Pact",
        "Craterhoof Behemoth", "Avenger of Zendikar", "Seedborn Muse",
        "Dockside Extortionist", "Consecrated Sphinx", "Aura Shards",
        "Demonic Tutor", "Cultivate", "Kodama's Reach", "Beast Within",
        "Blasphemous Act", "Toxic Deluge", "Heroic Intervention",
        "Lightning Greaves", "Swiftfoot Boots", "Ashnod's Altar",
        "Phyrexian Altar", "Viscera Seer", "Eternal Witness",
        "Sun Titan", "Reanimate", "Animate Dead",
        "Doubling Season", "Parallel Lives", "Anointed Procession",
        "Hardened Scales", "The Great Henge", "Guardian Project",
        "Mystic Remora", "Necropotence", "Bolas's Citadel",
        "Urborg, Tomb of Yawgmoth", "Cabal Coffers", "Gaea's Cradle",
        "Command Tower", "Bojuka Bog", "Reliquary Tower",
        "Impact Tremors", "Purphoros, God of the Forge", "Terror of the Peaks",
    ]

    # Balanced sampling: proportional to card type distribution, biased toward quality
    random.seed(args.seed)
    target = args.size

    candidate_map = {c["name"]: c for c in candidates}

    # Start with must-include cards
    selected = []
    must_included = 0
    for name in MUST_INCLUDE:
        if name in candidate_map:
            selected.append(candidate_map[name])
            must_included += 1
    print(f"  must-include:  {must_included:>4} / {len(MUST_INCLUDE)}")

    selected_names = {c["name"] for c in selected}
    remaining_target = target - len(selected)

    # Target distribution (roughly matches EDH card pools)
    type_ratios = {
        "creature": 0.38,
        "instant": 0.12,
        "sorcery": 0.12,
        "artifact": 0.12,
        "enchantment": 0.12,
        "land": 0.08,
        "planeswalker": 0.04,
        "other": 0.02,
    }

    # Count must-includes per type, adjust targets
    must_by_type = {}
    for c in selected:
        cat = c["category"]
        must_by_type[cat] = must_by_type.get(cat, 0) + 1

    for cat, ratio in type_ratios.items():
        pool = [c for c in candidates
                if c["category"] == cat and c["name"] not in selected_names]
        already = must_by_type.get(cat, 0)
        n = max(0, min(int(target * ratio) - already, len(pool)))
        if not pool or n <= 0:
            if already:
                print(f"  {cat:<15} {already:>4} (from must-include)")
            continue
        # Weight by quality score
        pool.sort(key=lambda x: -x["quality"])
        # Take top 60% by quality, randomly sample from those
        top_pool = pool[:max(int(len(pool) * 0.6), n)]
        sampled = random.sample(top_pool, min(n, len(top_pool)))
        selected.extend(sampled)
        selected_names.update(c["name"] for c in sampled)
        print(f"  {cat:<15} {len(sampled) + already:>4} ({already} must + {len(sampled)} sampled)")

    # Fill remaining slots from any category
    remaining = target - len(selected)
    if remaining > 0:
        leftover = [c for c in candidates if c["name"] not in selected_names]
        leftover.sort(key=lambda x: -x["quality"])
        top_leftover = leftover[:max(int(len(leftover) * 0.5), remaining)]
        extra = random.sample(top_leftover, min(remaining, len(top_leftover)))
        selected.extend(extra)
        print(f"  {'fill':<15} {len(extra):>4}")

    print(f"\nSelected: {len(selected)} cards")

    # Build golden set
    golden_cards = []
    for c in selected:
        oracle = c["oracle_card"]

        entry = {
            "name": c["name"],
            "type_line": oracle.get("type_line", ""),
            "oracle_text": c["oracle_text"],
            "keywords": oracle.get("keywords", []),
            "cmc": oracle.get("cmc", 0),
            "scryfall_tags": c["scryfall_tags"],
            "expected": {
                "function": c["functions"],
                "themes": c["positive_themes"],
                "high_synergy_themes": c["high_synergy_themes"],
                "provides_must_include": [],
                "wants_must_include": [],
            },
        }

        # Add optional fields
        power = oracle.get("power")
        toughness = oracle.get("toughness")
        if power is not None and toughness is not None:
            entry["power"] = power
            entry["toughness"] = toughness
        loyalty = oracle.get("loyalty")
        if loyalty is not None:
            entry["loyalty"] = loyalty

        golden_cards.append(entry)

    # Sort by name for readability
    golden_cards.sort(key=lambda x: x["name"])

    golden = {
        "_meta": {
            "description": "Golden evaluation set built from EDHREC theme data + Scryfall community tags. No LLM involvement in label generation.",
            "version": "2.0",
            "created": "2026-03-14",
            "sources": {
                "edhrec_themes": len(edhrec),
                "scryfall_tagged_cards": len(scryfall),
                "oracle_cards": len(oracle_by_name),
            },
            "notes": "function tags mapped from Scryfall tagger via synergy_tag_registry.json. "
                     "themes from EDHREC with >5% synergy score. "
                     "provides/wants left empty — to be filled by LLM tagger evaluation.",
        },
        "cards": golden_cards,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(golden, f, indent=2)
    print(f"\nSaved: {OUTPUT_FILE}")

    # Summary stats
    from collections import Counter
    all_themes = Counter()
    all_functions = Counter()
    for c in golden_cards:
        all_themes.update(c["expected"]["themes"])
        all_functions.update(c["expected"]["function"])

    print(f"\nThemes covered: {len(all_themes)}")
    print(f"Functions covered: {len(all_functions)}")
    theme_counts = [len(c["expected"]["themes"]) for c in golden_cards]
    func_counts = [len(c["expected"]["function"]) for c in golden_cards]
    print(f"Avg themes/card:    {sum(theme_counts)/len(theme_counts):.1f}")
    print(f"Avg functions/card: {sum(func_counts)/len(func_counts):.1f}")

    print(f"\nTop 15 themes:")
    for theme, count in all_themes.most_common(15):
        print(f"  {theme:<30} {count:>4}")

    print(f"\nTop 15 functions:")
    for func, count in all_functions.most_common(15):
        print(f"  {func:<30} {count:>4}")


if __name__ == "__main__":
    main()
