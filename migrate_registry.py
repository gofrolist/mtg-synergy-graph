"""One-time migration: build unified tag_registry.json from normalize_tags.py maps.

Merges the hardcoded PROVIDES_MAP/WANTS_MAP dictionaries and the existing
tag_registry.json into a single registry where each canonical tag has:
  - kind: "provides", "wants", or "synergy"
  - definition: one-line description
  - aliases: all input variations that map to this tag
  - inherits/amplifies/requires: graph edges (preserved from existing registry)

Usage:
    python3 migrate_registry.py              # write new tag_registry.json
    python3 migrate_registry.py --dry-run    # show stats without writing
"""

import json
import os
from collections import defaultdict

from normalize_tags import PROVIDES_MAP, WANTS_MAP

REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "tag_registry.json")


def migrate():
    # Load existing registry
    with open(REGISTRY_PATH) as f:
        old_registry = json.load(f)
    old_tags = old_registry.get("tags", {})

    # Invert maps: canonical → [aliases]
    provides_aliases = defaultdict(list)
    for alias, canonical in PROVIDES_MAP.items():
        if alias != canonical:
            provides_aliases[canonical].append(alias)

    wants_aliases = defaultdict(list)
    for alias, canonical in WANTS_MAP.items():
        if alias != canonical:
            wants_aliases[canonical].append(alias)

    # Build new registry
    new_tags = {}

    # Add all canonical provides tags
    for canonical in sorted(set(PROVIDES_MAP.values())):
        aliases = sorted(set(provides_aliases.get(canonical, [])))
        existing = old_tags.get(canonical, {})
        new_tags[canonical] = {
            "kind": "provides",
            "definition": existing.get("definition", ""),
            "aliases": aliases,
            "inherits": existing.get("inherits", []),
            "amplifies": existing.get("amplifies", []),
            "requires": existing.get("requires", []),
        }

    # Add all canonical wants tags
    for canonical in sorted(set(WANTS_MAP.values())):
        aliases = sorted(set(wants_aliases.get(canonical, [])))
        existing = old_tags.get(canonical, {})
        new_tags[canonical] = {
            "kind": "wants",
            "definition": existing.get("definition", ""),
            "aliases": aliases,
            "inherits": existing.get("inherits", []),
            "amplifies": existing.get("amplifies", []),
            "requires": existing.get("requires", []),
        }

    # Add existing registry tags not in provides/wants as "synergy"
    for tag_name, tag_data in old_tags.items():
        if tag_name not in new_tags and tag_name != "_meta":
            new_tags[tag_name] = {
                "kind": "synergy",
                "definition": tag_data.get("definition", ""),
                "aliases": tag_data.get("aliases", []),
                "inherits": tag_data.get("inherits", []),
                "amplifies": tag_data.get("amplifies", []),
                "requires": tag_data.get("requires", []),
            }

    registry = {
        "_meta": {
            "description": "Single source of truth for all synergy tags. "
                           "Canonical provides/wants vocabulary + aliases for LLM normalization.",
            "version": "1.0",
            "schema_version": "1.0",
            "edge_types": {
                "inherits": "is-a relationship",
                "amplifies": "synergy edge — having this tag increases value of target",
                "requires": "dependency — this tag needs target tag present to be effective",
            },
            "kind_types": {
                "provides": "What a card gives to the deck (graph edge source)",
                "wants": "What conditions make a card better (graph edge target)",
                "synergy": "Descriptive tag for Scryfall tagger / golden validation",
            },
        },
        "tags": new_tags,
    }

    return registry


def print_stats(registry):
    tags = registry["tags"]
    provides = [t for t, d in tags.items() if d["kind"] == "provides"]
    wants = [t for t, d in tags.items() if d["kind"] == "wants"]
    synergy = [t for t, d in tags.items() if d["kind"] == "synergy"]

    total_aliases = sum(len(d["aliases"]) for d in tags.values())
    no_def = sum(1 for d in tags.values() if not d.get("definition"))

    print(f"Registry stats:")
    print(f"  provides: {len(provides)} canonical tags")
    print(f"  wants:    {len(wants)} canonical tags")
    print(f"  synergy:  {len(synergy)} tags")
    print(f"  total:    {len(tags)} tags, {total_aliases} aliases")
    print(f"  missing definitions: {no_def}")


if __name__ == "__main__":
    import sys

    registry = migrate()
    print_stats(registry)

    if "--dry-run" in sys.argv:
        print("\nDry run — not writing.")
    else:
        with open(REGISTRY_PATH, "w") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
        print(f"\nWritten to {REGISTRY_PATH}")
