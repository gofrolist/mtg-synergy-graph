"""
tags.py — Tag Registry CLI

Commands:
  python tags.py list                    # all tags sorted by usage
  python tags.py search protection       # find tags matching a keyword  
  python tags.py show board-protection   # full details for one tag
  python tags.py similar protection      # find potentially duplicate/related tags
  python tags.py sync                    # rebuild counts from top10000_tags.json
  python tags.py add <tag> <definition>  # add a new tag to registry
"""

import json
import sys
import argparse
from pathlib import Path
from difflib import SequenceMatcher

BASE_DIR = Path(__file__).parent
REGISTRY_PATH = BASE_DIR / "tag_registry.json"
TAGS_PATH = BASE_DIR / "data" / "top10000_tags.json"


def load_registry() -> dict:
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def save_registry(data: dict):
    with open(REGISTRY_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_tagged_cards() -> list[dict]:
    if not TAGS_PATH.exists():
        return []
    with open(TAGS_PATH) as f:
        return json.load(f)


# ── COMMANDS ──────────────────────────────────────────────────────────────────

def cmd_list(args):
    """List all tags sorted by usage count."""
    registry = load_registry()
    tags = registry["tags"]

    sort_by = getattr(args, "sort", "count")
    if sort_by == "alpha":
        items = sorted(tags.items())
    else:
        items = sorted(tags.items(), key=lambda x: x[1].get("count", 0), reverse=True)

    print(f"\n{'TAG':<35} {'COUNT':>6}  DEFINITION (truncated)")
    print("─" * 80)
    for tag, data in items:
        count = data.get("count", 0)
        defn = data.get("definition", "")[:45]
        if len(data.get("definition", "")) > 45:
            defn += "..."
        marker = " ⚠" if count == 0 else ""
        print(f"  {tag:<33} {count:>6}  {defn}{marker}")

    print(f"\nTotal: {len(tags)} tags")
    unused = [t for t, d in tags.items() if d.get("count", 0) == 0]
    if unused:
        print(f"Unused tags (⚠): {', '.join(unused)}")


def cmd_search(args):
    """Find tags matching a keyword."""
    query = args.query.lower()
    registry = load_registry()
    tags = registry["tags"]

    matches = []
    for tag, data in tags.items():
        score = 0
        if query in tag:
            score += 3
        if query in data.get("definition", "").lower():
            score += 2
        if any(query in a for a in data.get("aliases", [])):
            score += 2
        if any(query in r for r in data.get("related", [])):
            score += 1
        if score > 0:
            matches.append((score, tag, data))

    matches.sort(reverse=True)

    if not matches:
        print(f"\nNo tags found matching '{query}'")
        print(f"Run 'python tags.py list' to see all tags")
        return

    print(f"\nTags matching '{query}':")
    print("─" * 70)
    for score, tag, data in matches:
        count = data.get("count", 0)
        print(f"\n  [{tag}]  (used by {count} cards)")
        print(f"  Definition: {data.get('definition', 'no definition')}")
        if data.get("aliases"):
            print(f"  Aliases: {', '.join(data['aliases'])}")
        if data.get("related"):
            print(f"  Related: {', '.join(data['related'])}")
        if data.get("cards"):
            print(f"  Cards: {', '.join(data['cards'][:5])}")


def cmd_show(args):
    """Show full details for a specific tag."""
    tag_name = args.tag
    registry = load_registry()
    tags = registry["tags"]

    if tag_name not in tags:
        # Try partial match
        partial = [t for t in tags if tag_name.lower() in t.lower()]
        if partial:
            print(f"\nTag '{tag_name}' not found. Did you mean:")
            for t in partial:
                print(f"  {t}")
        else:
            print(f"\nTag '{tag_name}' not found.")
            print(f"Run 'python tags.py search {tag_name}' to search")
        return

    data = tags[tag_name]
    print(f"\n{'═'*60}")
    print(f"  {tag_name}")
    print(f"{'═'*60}")
    print(f"\n  Definition:")
    print(f"    {data.get('definition', 'no definition')}")

    if data.get("aliases"):
        print(f"\n  Aliases (same meaning, use {tag_name} instead):")
        for a in data["aliases"]:
            print(f"    → {a}")

    if data.get("related"):
        print(f"\n  Related tags (different but connected):")
        for r in data["related"]:
            r_data = tags.get(r, {})
            r_count = r_data.get("count", "?")
            r_defn = r_data.get("definition", "")[:50]
            print(f"    → {r} ({r_count} cards) — {r_defn}")

    print(f"\n  Used by {data.get('count', 0)} cards:")
    for c in data.get("cards", []):
        print(f"    · {c}")

    print()


def cmd_similar(args):
    """Find tags that might be duplicates or overly similar."""
    query = args.tag.lower().replace("-", " ")
    registry = load_registry()
    tags = registry["tags"]

    print(f"\nTags similar to '{args.tag}':")
    print("─" * 60)

    scored = []
    for tag, data in tags.items():
        if tag == args.tag:
            continue
        tag_words = tag.lower().replace("-", " ")
        ratio = SequenceMatcher(None, query, tag_words).ratio()
        # Also check word overlap
        q_words = set(query.split())
        t_words = set(tag_words.split())
        overlap = len(q_words & t_words) / max(len(q_words), len(t_words), 1)
        score = max(ratio, overlap)
        if score > 0.3:
            scored.append((score, tag, data))

    scored.sort(reverse=True)

    if not scored:
        print(f"  No similar tags found.")
    else:
        for score, tag, data in scored[:8]:
            count = data.get("count", 0)
            similarity = f"{score:.0%}"
            print(f"\n  [{tag}]  similarity: {similarity}  ({count} cards)")
            print(f"  {data.get('definition', '')[:70]}")

    print(f"\nIf these are duplicates, add an alias in tag_registry.json")
    print(f"If one is wrong, use: python regression_test.py --add-correction ...")


def cmd_sync(args):
    """Rebuild tag counts and card lists from top10000_tags.json."""
    registry = load_registry()
    tags = registry["tags"]
    cards = load_tagged_cards()

    if not cards:
        print("No tags file found — nothing to sync")
        return

    # Reset counts
    for tag in tags:
        tags[tag]["count"] = 0
        tags[tag]["cards"] = []

    # New tags discovered in tags file but not in registry
    new_tags = {}

    for card in cards:
        name = card.get("name") or card.get("_input_name", "unknown")
        all_tags = card.get("provides", []) + card.get("wants", []) + card.get("synergy_tags", [])
        for tag in all_tags:
            if tag in tags:
                tags[tag]["count"] += 1
                if name not in tags[tag]["cards"]:
                    tags[tag]["cards"].append(name)
            else:
                if tag not in new_tags:
                    new_tags[tag] = {"count": 0, "cards": []}
                new_tags[tag]["count"] += 1
                if name not in new_tags[tag]["cards"]:
                    new_tags[tag]["cards"].append(name)

    # Add new tags to registry with placeholder definitions
    added = []
    for tag, data in new_tags.items():
        tags[tag] = {
            "definition": "⚠ AUTO-DISCOVERED — add definition manually",
            "aliases": [],
            "related": [],
            "cards": data["cards"],
            "count": data["count"],
        }
        added.append(tag)

    save_registry(registry)

    print(f"\nSync complete:")
    print(f"  Updated counts for {len(tags) - len(added)} existing tags")
    if added:
        print(f"  Added {len(added)} new tags (need definitions):")
        for t in added:
            print(f"    ⚠ {t}  ({new_tags[t]['count']} cards: {new_tags[t]['cards']})")
        print(f"\n  Add definitions with: python tags.py add <tag> \"<definition>\"")


def cmd_add(args):
    """Add a new tag to the registry."""
    registry = load_registry()
    tags = registry["tags"]

    tag = args.tag
    definition = args.definition

    if tag in tags:
        existing = tags[tag].get("definition", "")
        if "AUTO-DISCOVERED" not in existing:
            print(f"Tag '{tag}' already exists:")
            print(f"  {existing}")
            print(f"Use 'python tags.py show {tag}' to see full details")
            return
        else:
            # Update auto-discovered tag
            tags[tag]["definition"] = definition
            print(f"Updated definition for '{tag}'")
    else:
        tags[tag] = {
            "definition": definition,
            "aliases": [],
            "related": [],
            "cards": [],
            "count": 0,
        }
        print(f"Added tag '{tag}'")

    save_registry(registry)
    print(f"Run 'python tags.py show {tag}' to verify")


# ── MAIN ──────────────────────────────────────────────────────────────────────



def cmd_tree(args):
    """Show inheritance tree for a tag — what it inherits from and what inherits from it."""
    registry = load_registry()
    tags = registry["tags"]

    root = args.tag
    if root not in tags:
        partial = [t for t in tags if args.tag.lower() in t.lower()]
        if partial:
            print(f"Tag '{root}' not found. Did you mean: {', '.join(partial)}")
        else:
            print(f"Tag '{root}' not found.")
        return

    def get_children(tag):
        return [t for t, d in tags.items() if tag in d.get("inherits", [])]

    def get_ancestors(tag, visited=None):
        if visited is None:
            visited = set()
        parents = tags[tag].get("inherits", [])
        result = []
        for p in parents:
            if p not in visited:
                visited.add(p)
                result.append(p)
                result.extend(get_ancestors(p, visited))
        return result

    def print_children(tag, indent=0, visited=None):
        if visited is None:
            visited = set()
        if tag in visited:
            return
        visited.add(tag)
        data = tags.get(tag, {})
        count = data.get("count", 0)
        abstract = " (abstract)" if data.get("abstract") else ""
        marker = "◆" if tag == root else ("○" if data.get("abstract") else "●")
        print(f"{'  ' * indent}{marker} {tag}  [{count} cards]{abstract}")
        for child in get_children(tag):
            print_children(child, indent + 1, visited)

    # Show ancestors (what this tag inherits from)
    ancestors = get_ancestors(root)
    if ancestors:
        print(f"\n  Inherits from (ancestors):")
        for a in ancestors:
            data = tags.get(a, {})
            abstract = " (abstract)" if data.get("abstract") else ""
            print(f"    ↑ {a}{abstract}")

    # Show the tree rooted at this tag
    print(f"\n  Inheritance tree:")
    print_children(root)

    # Amplifies
    amplifies = tags[root].get("amplifies", [])
    if amplifies:
        print(f"\n  Amplifies (synergy — increases value of):")
        for a in amplifies:
            data = tags.get(a, {})
            count = data.get("count", 0)
            print(f"    → {a}  [{count} cards]")

    # Requires
    requires = tags[root].get("requires", [])
    if requires:
        print(f"\n  Requires in deck (card is weak without):")
        for r in requires:
            data = tags.get(r, {})
            count = data.get("count", 0)
            print(f"    ⚡ {r}  [{count} cards]")

    # What amplifies THIS tag
    amplified_by = [t for t, d in tags.items() if root in d.get("amplifies", [])]
    if amplified_by:
        print(f"\n  Amplified by (these tags make {root} more valuable):")
        for a in amplified_by:
            data = tags.get(a, {})
            count = data.get("count", 0)
            print(f"    ← {a}  [{count} cards]")
    print()


def cmd_graph(args):
    """Show the full tag graph summary — all edges."""
    registry = load_registry()
    tags = registry["tags"]

    print(f"\n{'═'*60}")
    print(f"TAG GRAPH SUMMARY")
    print(f"{'═'*60}\n")

    # Count edge types
    inherits_edges = []
    amplifies_edges = []
    requires_edges = []

    for tag, data in tags.items():
        for parent in data.get("inherits", []):
            inherits_edges.append((tag, parent))
        for target in data.get("amplifies", []):
            amplifies_edges.append((tag, target))
        for dep in data.get("requires", []):
            requires_edges.append((tag, dep))

    print(f"  Nodes: {len(tags)} tags ({sum(1 for d in tags.values() if not d.get('abstract'))} concrete, {sum(1 for d in tags.values() if d.get('abstract'))} abstract)")
    print(f"  Edges: {len(inherits_edges)} inherits + {len(amplifies_edges)} amplifies + {len(requires_edges)} requires = {len(inherits_edges)+len(amplifies_edges)+len(requires_edges)} total\n")

    print(f"  INHERITS edges (is-a hierarchy):")
    for child, parent in sorted(inherits_edges):
        print(f"    {child}  →inherits→  {parent}")

    print(f"\n  AMPLIFIES edges (synergy):")
    for source, target in sorted(amplifies_edges):
        print(f"    {source}  →amplifies→  {target}")

    print(f"\n  REQUIRES edges (dependency):")
    for source, dep in sorted(requires_edges):
        print(f"    {source}  →requires→  {dep}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MTG Tag Registry CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tags.py list
  python tags.py search protection
  python tags.py show board-protection
  python tags.py similar protection
  python tags.py sync
  python tags.py add equipment-hexproof "Equipment granting hexproof to equipped creature"
        """
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List all tags")
    p_list.add_argument("--sort", choices=["count", "alpha"], default="count")

    p_search = sub.add_parser("search", help="Search tags by keyword")
    p_search.add_argument("query")

    p_show = sub.add_parser("show", help="Show details for a tag")
    p_show.add_argument("tag")

    p_similar = sub.add_parser("similar", help="Find similar/duplicate tags")
    p_similar.add_argument("tag")

    p_sync = sub.add_parser("sync", help="Rebuild counts from top10000_tags.json")

    p_tree = sub.add_parser("tree", help="Show inheritance tree for a tag")
    p_tree.add_argument("tag")

    p_graph = sub.add_parser("graph", help="Show full tag graph edges")

    p_add = sub.add_parser("add", help="Add a new tag")
    p_add.add_argument("tag")
    p_add.add_argument("definition")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "similar":
        cmd_similar(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "tree":
        cmd_tree(args)
    elif args.command == "graph":
        cmd_graph(args)
    elif args.command == "add":
        cmd_add(args)
    else:
        parser.print_help()
