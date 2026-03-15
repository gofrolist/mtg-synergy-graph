"""
New-set update workflow — detect new and errata'd cards, tag, and update DB.

Compares fresh Scryfall data against the existing tag database to find:
  1. New cards not yet tagged (from new set releases)
  2. Errata'd cards whose oracle text has changed (need re-tagging)
  3. Newly popular cards that crossed into the top N by EDHREC rank

Usage:
    python3 update_cards.py --check              # dry run: show what changed
    python3 update_cards.py --update             # full pipeline: download, diff, tag, import
    python3 update_cards.py --update --top 15000 # expand DB to top 15k cards
    python3 update_cards.py --tag-only           # tag pending candidates without downloading
    python3 update_cards.py --errata-only        # show errata changes without tagging
"""

import argparse
import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SCRYFALL_FILE = os.path.join(DATA_DIR, "oracle_cards.json")
TAGS_FILE = os.path.join(DATA_DIR, "top10000_tags.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending_candidates.json")


def load_existing_tags() -> dict[str, dict]:
    """Load existing tagged cards, keyed by oracle_id."""
    if not os.path.exists(TAGS_FILE):
        return {}
    with open(TAGS_FILE) as f:
        return {c["oracle_id"]: c for c in json.load(f) if "oracle_id" in c}


def load_scryfall() -> list[dict]:
    """Load Scryfall oracle cards."""
    if not os.path.exists(SCRYFALL_FILE):
        print(f"ERROR: {SCRYFALL_FILE} not found. Run: python3 download_cards.py")
        return []
    with open(SCRYFALL_FILE) as f:
        return json.load(f)


def get_oracle_text(card: dict) -> str:
    """Get oracle text, handling multi-face cards."""
    text = card.get("oracle_text", "")
    if not text:
        faces = card.get("card_faces", [])
        if faces:
            text = " // ".join(f.get("oracle_text", "") for f in faces)
    return text


def is_taggable(card: dict) -> bool:
    """Check if a card should be tagged (not basic land, not token, has text)."""
    type_line = card.get("type_line", "")
    if "Basic Land" in type_line:
        return False
    layout = card.get("layout", "")
    if layout in ("token", "emblem", "art_series", "double_faced_token"):
        return False
    oracle_text = get_oracle_text(card)
    if not oracle_text and "Land" not in type_line:
        return False
    return True


def to_candidate(card: dict, reason: str) -> dict:
    """Convert a Scryfall card to candidates format."""
    return {
        "name": card["name"],
        "oracle_id": card["oracle_id"],
        "type_line": card.get("type_line", ""),
        "oracle_text": get_oracle_text(card),
        "keywords": card.get("keywords", []),
        "cmc": card.get("cmc", 0),
        "color_identity": card.get("color_identity", []),
        "edhrec_rank": card.get("edhrec_rank"),
    }


def diff_cards(top_n: int = 10000) -> dict:
    """Compare Scryfall data against existing tags to find changes.

    Returns dict with:
      - new_cards: cards in top N not yet tagged
      - errata: cards whose oracle_text changed since tagging
      - removed: cards that fell out of top N (informational)
      - summary: human-readable stats
    """
    existing = load_existing_tags()
    scryfall = load_scryfall()
    if not scryfall:
        return {"new_cards": [], "errata": [], "removed": [], "summary": "No Scryfall data"}

    # Build Scryfall index
    scryfall_by_oid = {}
    for card in scryfall:
        oid = card.get("oracle_id")
        if oid and is_taggable(card):
            scryfall_by_oid[oid] = card

    # Get top N by EDHREC rank
    ranked = [c for c in scryfall if "edhrec_rank" in c and is_taggable(c)]
    ranked.sort(key=lambda c: c["edhrec_rank"])
    top_n_oids = {c["oracle_id"] for c in ranked[:top_n]}

    # Also include any card in current DB (don't lose coverage)
    target_oids = top_n_oids | set(existing.keys())

    # Find new cards (in target set but not tagged)
    new_cards = []
    for oid in target_oids:
        if oid not in existing and oid in scryfall_by_oid:
            card = scryfall_by_oid[oid]
            rank = card.get("edhrec_rank")
            reason = f"new-card-rank-{rank}" if rank else "new-card-unranked"
            new_cards.append(to_candidate(card, reason))

    # Sort new cards by EDHREC rank
    new_cards.sort(key=lambda c: c.get("edhrec_rank") or 999999)

    # Find errata'd cards (oracle text changed)
    errata = []
    for oid, tagged in existing.items():
        if oid not in scryfall_by_oid:
            continue
        scry_card = scryfall_by_oid[oid]
        new_text = get_oracle_text(scry_card)
        old_text = tagged.get("oracle_text", "")

        if new_text != old_text and new_text and old_text:
            errata.append({
                "name": tagged.get("name", scry_card.get("name", "")),
                "oracle_id": oid,
                "old_text": old_text,
                "new_text": new_text,
                "candidate": to_candidate(scry_card, "errata"),
            })

    # Cards that fell out of top N
    removed = []
    for oid in existing:
        if oid not in top_n_oids and oid in scryfall_by_oid:
            card = scryfall_by_oid[oid]
            rank = card.get("edhrec_rank")
            if rank and rank > top_n:
                removed.append({
                    "name": card.get("name", ""),
                    "edhrec_rank": rank,
                })

    summary = (
        f"Scryfall: {len(scryfall)} cards, {len(ranked)} with EDHREC rank\n"
        f"Existing DB: {len(existing)} tagged cards\n"
        f"Target pool: top {top_n} + existing = {len(target_oids)} cards\n"
        f"New cards to tag: {len(new_cards)}\n"
        f"Errata'd cards (text changed): {len(errata)}\n"
        f"Cards that dropped out of top {top_n}: {len(removed)}"
    )

    return {
        "new_cards": new_cards,
        "errata": errata,
        "removed": removed,
        "summary": summary,
    }


def show_diff(diff: dict, verbose: bool = False):
    """Display the diff results."""
    print(diff["summary"])

    if diff["new_cards"]:
        print(f"\nNew cards to tag ({len(diff['new_cards'])}):")
        for c in diff["new_cards"][:20]:
            rank = c.get("edhrec_rank")
            rank_str = f"#{rank}" if rank else "unranked"
            print(f"  {rank_str:>8}  {c['name']}")
        if len(diff["new_cards"]) > 20:
            print(f"  ... and {len(diff['new_cards']) - 20} more")

    if diff["errata"]:
        print(f"\nErrata'd cards ({len(diff['errata'])}):")
        for e in diff["errata"][:10]:
            print(f"  {e['name']}:")
            if verbose:
                # Show first line that differs
                old_lines = e["old_text"].split("\n")
                new_lines = e["new_text"].split("\n")
                for i, (old, new) in enumerate(zip(old_lines, new_lines)):
                    if old != new:
                        print(f"    - {old[:80]}")
                        print(f"    + {new[:80]}")
                        break
                else:
                    # Length difference
                    print(f"    old: {len(e['old_text'])} chars → new: {len(e['new_text'])} chars")
            else:
                print(f"    old: {e['old_text'][:60]}...")
                print(f"    new: {e['new_text'][:60]}...")
        if len(diff["errata"]) > 10:
            print(f"  ... and {len(diff['errata']) - 10} more")

    if diff["removed"] and verbose:
        print(f"\nDropped out of top N ({len(diff['removed'])}):")
        for r in diff["removed"][:10]:
            print(f"  #{r['edhrec_rank']:>6}  {r['name']}")


def save_pending(diff: dict):
    """Save new + errata'd cards as pending candidates for tagging."""
    candidates = list(diff["new_cards"])
    for e in diff["errata"]:
        candidates.append(e["candidate"])

    if not candidates:
        print("No pending cards to save.")
        return 0

    with open(PENDING_FILE, "w") as f:
        json.dump(candidates, f, indent=2)
    print(f"\nSaved {len(candidates)} pending candidates to {PENDING_FILE}")
    return len(candidates)


def merge_new_tags(pending_tags_file: str):
    """Merge newly tagged cards into the main tags file."""
    existing = load_existing_tags()

    with open(pending_tags_file) as f:
        new_tags = json.load(f)

    added = 0
    updated = 0
    for card in new_tags:
        oid = card.get("oracle_id")
        if not oid:
            continue
        if oid in existing:
            updated += 1
        else:
            added += 1
        existing[oid] = card

    # Write back
    all_cards = list(existing.values())
    with open(TAGS_FILE, "w") as f:
        json.dump(all_cards, f, indent=2)

    print(f"Merged: {added} new, {updated} updated → {len(all_cards)} total in {TAGS_FILE}")
    return added, updated


def run_full_update(top_n: int, provider: str, model: str, skip_download: bool = False):
    """Run the complete update pipeline."""
    # Step 1: Download fresh Scryfall data
    if not skip_download:
        print("=" * 60)
        print("Step 1/4: Downloading fresh Scryfall data...")
        print("=" * 60)
        from download_cards import download
        download()
    else:
        print("Step 1/4: Skipping download (--skip-download)")

    # Step 2: Diff against existing DB
    print("\n" + "=" * 60)
    print("Step 2/4: Comparing against existing tags...")
    print("=" * 60)
    diff = diff_cards(top_n)
    show_diff(diff, verbose=True)

    # Step 3: Tag new/errata'd cards
    pending_count = save_pending(diff)
    if pending_count == 0:
        print("\nNothing to tag — DB is up to date!")
        return

    print("\n" + "=" * 60)
    print(f"Step 3/4: Tagging {pending_count} cards...")
    print("=" * 60)

    pending_tags = PENDING_FILE.replace("_candidates", "_tags")

    import subprocess
    cmd = [
        "python3", "batch_tagger.py",
        "--candidates", PENDING_FILE,
        "--output", pending_tags,
        "--provider", provider,
        "--model", model,
    ]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("ERROR: Tagging failed. Fix issues and run: python3 update_cards.py --tag-only")
        return

    # Step 4: Merge and import to DB
    print("\n" + "=" * 60)
    print("Step 4/4: Merging tags and updating DB...")
    print("=" * 60)
    merge_new_tags(pending_tags)

    # Rebuild SQLite DB
    from tag_db import import_file
    import_file(TAGS_FILE)

    # Cleanup
    for f in [PENDING_FILE, pending_tags]:
        if os.path.exists(f):
            os.remove(f)
    print("\nUpdate complete!")


def main():
    parser = argparse.ArgumentParser(
        description="New-set update workflow — detect, tag, and import new/changed cards"
    )
    parser.add_argument("--check", action="store_true",
                        help="Dry run: show what changed without tagging")
    parser.add_argument("--update", action="store_true",
                        help="Full pipeline: download, diff, tag, import")
    parser.add_argument("--tag-only", action="store_true",
                        help="Tag pending candidates (resume after failed tagging)")
    parser.add_argument("--errata-only", action="store_true",
                        help="Show only errata changes")
    parser.add_argument("--merge", type=str, metavar="FILE",
                        help="Merge a tags JSON file into the main DB")
    parser.add_argument("--top", type=int, default=10000,
                        help="Top N cards by EDHREC rank to target (default: 10000)")
    parser.add_argument("--provider", default="ollama",
                        help="LLM provider for tagging (default: ollama)")
    parser.add_argument("--model", default="phi4:14b",
                        help="LLM model for tagging (default: phi4:14b)")
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip Scryfall download (use existing oracle_cards.json)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed diff output")
    args = parser.parse_args()

    if args.check:
        diff = diff_cards(args.top)
        show_diff(diff, verbose=args.verbose)

    elif args.errata_only:
        diff = diff_cards(args.top)
        if diff["errata"]:
            print(f"Found {len(diff['errata'])} errata'd cards:\n")
            for e in diff["errata"]:
                print(f"  {e['name']} ({e['oracle_id'][:8]}...):")
                print(f"    OLD: {e['old_text']}")
                print(f"    NEW: {e['new_text']}")
                print()
        else:
            print("No errata detected.")

    elif args.tag_only:
        if not os.path.exists(PENDING_FILE):
            print(f"No pending candidates at {PENDING_FILE}")
            print("Run: python3 update_cards.py --check  (to generate pending list)")
            return
        import subprocess
        pending_tags = PENDING_FILE.replace("_candidates", "_tags")
        cmd = [
            "python3", "batch_tagger.py",
            "--candidates", PENDING_FILE,
            "--output", pending_tags,
            "--provider", args.provider,
            "--model", args.model,
        ]
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd)
        if result.returncode == 0:
            merge_new_tags(pending_tags)
            from tag_db import import_file
            import_file(TAGS_FILE)
            for f in [PENDING_FILE, pending_tags]:
                if os.path.exists(f):
                    os.remove(f)
            print("\nUpdate complete!")

    elif args.merge:
        merge_new_tags(args.merge)
        from tag_db import import_file
        import_file(TAGS_FILE)

    elif args.update:
        run_full_update(args.top, args.provider, args.model, args.skip_download)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
