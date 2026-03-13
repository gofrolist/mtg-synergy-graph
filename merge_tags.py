"""
Merge Scryfall tagger function tags with LLM-generated role/provides/wants.

Combines two data sources into unified card profiles for the synergy graph:
  - Scryfall tagger: synergy_tags (community-curated function tags)
  - LLM output: role, provides, wants, categories, mechanics, triggers, notes

Usage:
    python3 merge_tags.py --deck kyler
    python3 merge_tags.py --deck krenko --llm data/krenko_tags_phi4.json
    python3 merge_tags.py --deck kyler --stats
"""

import argparse
import json
import os
from collections import Counter

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

DEFAULT_SCRYFALL = os.path.join(DATA_DIR, "scryfall_function_tags.json")


def load_scryfall(path: str) -> dict[str, dict]:
    """Load Scryfall tags, keyed by oracle_id."""
    if not os.path.exists(path):
        print(f"WARNING: Scryfall tags not found at {path}")
        print(f"Run: python3 scryfall_tagger.py")
        return {}
    with open(path) as f:
        items = json.load(f)
    return {item["oracle_id"]: item for item in items}


def load_llm(path: str) -> dict[str, dict]:
    """Load LLM tags, keyed by oracle_id."""
    if not os.path.exists(path):
        print(f"WARNING: LLM tags not found at {path}")
        print(f"Run: python3 batch_tagger.py")
        return {}
    with open(path) as f:
        items = json.load(f)
    return {item["oracle_id"]: item for item in items if "oracle_id" in item}


def _front_face(name: str) -> str:
    """Normalize card name to front face only. 'Horn of Gondor // Horn of Gondor' → 'Horn of Gondor'."""
    if " // " in name:
        return name.split(" // ")[0].strip()
    return name


def merge(scryfall: dict[str, dict], llm: dict[str, dict]) -> list[dict]:
    """Merge Scryfall synergy_tags with LLM role/provides/wants.

    After merging by oracle_id, consolidates entries that share the same
    front-face name (e.g. 'Horn of Gondor' and 'Horn of Gondor // Horn of Gondor'
    are different oracle_ids but the same card).
    """
    all_oracle_ids = set(scryfall.keys()) | set(llm.keys())
    by_oid = []

    for oid in sorted(all_oracle_ids):
        sf = scryfall.get(oid, {})
        lm = llm.get(oid, {})

        name = lm.get("name") or sf.get("name", "")

        card = {
            "name": name,
            "oracle_id": oid,
            # From Scryfall tagger (canonical)
            "synergy_tags": sf.get("scryfall_tags", []),
            # From LLM
            "role": lm.get("role", ""),
            "provides": lm.get("provides", []),
            "wants": lm.get("wants", []),
            "categories": lm.get("categories", []),
            "mechanics": lm.get("mechanics", []),
            "triggers": lm.get("triggers", []),
            "notes": lm.get("notes", ""),
            # Provenance
            "tag_source": {
                "synergy_tags": "scryfall" if sf else "none",
                "role_provides_wants": "llm" if lm else "none",
            },
        }

        # Carry forward filter metadata if present
        if lm.get("filter_pass"):
            card["filter_pass"] = lm["filter_pass"]
        if lm.get("filter_reason"):
            card["filter_reason"] = lm["filter_reason"]

        by_oid.append(card)

    # Consolidate entries sharing the same front-face name.
    # "Horn of Gondor // Horn of Gondor" and "Horn of Gondor" become one entry.
    by_name = {}
    for card in by_oid:
        card["_orig_name"] = card["name"]
        front = _front_face(card["name"])
        if front not in by_name:
            card["name"] = front
            by_name[front] = card
        else:
            existing = by_name[front]
            # Merge synergy_tags (union, deduplicated)
            merged_tags = list(dict.fromkeys(existing["synergy_tags"] + card["synergy_tags"]))
            existing["synergy_tags"] = merged_tags
            # Keep richer LLM data (more provides/wants)
            existing_richness = len(existing.get("provides", [])) + len(existing.get("wants", []))
            card_richness = len(card.get("provides", [])) + len(card.get("wants", []))
            # When equal richness, prefer the non-DFC entry (original name without //)
            existing_is_dfc = " // " in existing.get("_orig_name", "")
            card_is_dfc = " // " in card.get("_orig_name", "")
            if card_richness > existing_richness or (card_richness == existing_richness and existing_is_dfc and not card_is_dfc):
                for field in ("oracle_id", "role", "provides", "wants", "categories",
                              "mechanics", "triggers", "notes", "filter_pass", "filter_reason"):
                    if card.get(field):
                        existing[field] = card[field]
            # Update provenance
            if card["tag_source"]["synergy_tags"] == "scryfall":
                existing["tag_source"]["synergy_tags"] = "scryfall"
            if card["tag_source"]["role_provides_wants"] == "llm":
                existing["tag_source"]["role_provides_wants"] = "llm"

    # Clean up internal field
    for card in by_name.values():
        card.pop("_orig_name", None)
    return list(by_name.values())


def print_stats(merged: list[dict]):
    """Print merge statistics."""
    total = len(merged)
    both = sum(1 for c in merged
               if c["tag_source"]["synergy_tags"] == "scryfall"
               and c["tag_source"]["role_provides_wants"] == "llm")
    scryfall_only = sum(1 for c in merged
                        if c["tag_source"]["synergy_tags"] == "scryfall"
                        and c["tag_source"]["role_provides_wants"] == "none")
    llm_only = sum(1 for c in merged
                   if c["tag_source"]["synergy_tags"] == "none"
                   and c["tag_source"]["role_provides_wants"] == "llm")
    neither = sum(1 for c in merged
                  if c["tag_source"]["synergy_tags"] == "none"
                  and c["tag_source"]["role_provides_wants"] == "none")

    print(f"\nMerge Statistics:")
    print(f"  Total cards:       {total}")
    print(f"  Both sources:      {both}")
    print(f"  Scryfall only:     {scryfall_only}")
    print(f"  LLM only:          {llm_only}")
    print(f"  Neither:           {neither}")

    # Tag coverage
    all_synergy = [t for c in merged for t in c["synergy_tags"]]
    unique_synergy = set(all_synergy)
    print(f"\n  Synergy tags:      {len(all_synergy)} total, {len(unique_synergy)} unique")

    # Role distribution
    roles = Counter(c["role"] for c in merged if c["role"])
    print(f"\n  Role distribution:")
    for role, count in roles.most_common():
        print(f"    {role:12s}  {count:4d}")

    # Provides/wants coverage
    has_provides = sum(1 for c in merged if c["provides"])
    has_wants = sum(1 for c in merged if c["wants"])
    print(f"\n  Cards with provides: {has_provides}/{total}")
    print(f"  Cards with wants:    {has_wants}/{total}")

    # Top synergy tags
    tag_counts = Counter(all_synergy)
    print(f"\n  Top 15 synergy tags:")
    for tag, count in tag_counts.most_common(15):
        print(f"    {tag:35s}  {count:4d}")


def run():
    from decks import list_decks

    parser = argparse.ArgumentParser(description="Merge Scryfall + LLM tags")
    parser.add_argument("--deck", required=True, choices=list_decks(),
                        help="Deck config to use")
    parser.add_argument("--scryfall", default=DEFAULT_SCRYFALL,
                        help="Scryfall function tags JSON")
    parser.add_argument("--llm", type=str,
                        help="LLM-generated tags JSON (default: data/<deck>_tags.json)")
    parser.add_argument("--output", type=str,
                        help="Output merged JSON (default: data/<deck>_merged.json)")
    parser.add_argument("--stats", action="store_true",
                        help="Show merge statistics")
    args = parser.parse_args()

    llm_path = args.llm or os.path.join(DATA_DIR, f"{args.deck}_tags.json")
    output_path = args.output or os.path.join(DATA_DIR, f"{args.deck}_merged.json")

    scryfall = load_scryfall(args.scryfall)
    llm = load_llm(llm_path)

    print(f"Scryfall tags: {len(scryfall)} cards")
    print(f"LLM tags:      {len(llm)} cards")

    merged = merge(scryfall, llm)

    # Normalize provides/wants to controlled vocabulary
    from normalize_tags import normalize_cards
    normalize_cards(merged)
    print("Normalized provides/wants to controlled vocabulary")

    with open(output_path, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"\nMerged {len(merged)} cards → {output_path}")

    if args.stats or True:  # always show stats for now
        print_stats(merged)


if __name__ == "__main__":
    run()
