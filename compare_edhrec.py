#!/usr/bin/env python3
"""Compare our --recommend output against EDHREC synergy data from the DB.

Usage:
    python3 compare_edhrec.py --commander "Krenko, Mob Boss"   # single commander
    python3 compare_edhrec.py --all                            # all commanders with synergy data
    python3 compare_edhrec.py --all --quiet                    # summary table only
    python3 compare_edhrec.py --commander "Krenko, Mob Boss" --fast  # use cached output
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DB_PATH = Path("data/tags.db")
RECOMMEND_CACHE_DIR = Path("data/recommend_cache")
RECOMMEND_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def parse_recommend_output(output: str) -> list[str]:
    """Parse card names from --recommend output."""
    cards = []
    for line in output.split("\n"):
        line = line.strip()
        if "%" in line and ("█" in line or "░" in line):
            rest = re.sub(r'[█░]+', '', line)
            match = re.match(r'[\d.]+%\s+(.*)', rest)
            if match:
                rest = match.group(1).strip()
                # Strip OSC 8 hyperlink escape sequences
                rest = re.sub(r'\033\]8;;[^\033]*\033\\', '', rest)
                # Strip diagnostic tags: [brackets], T=x.x, EDH=x.xx, ⚠ HIGH CMC
                if "[" in rest:
                    rest = rest[:rest.index("[")].strip()
                rest = re.sub(r'\s+T=[\d.]+', '', rest)
                rest = re.sub(r'\s+EDH=[\d.-]+', '', rest)
                rest = re.sub(r'\s+⚠.*$', '', rest)
                card_name = rest.strip()
                if card_name:
                    cards.append(card_name)
    return cards


def normalize(name: str) -> str:
    """Normalize card name for comparison. Handles DFC cards (Front // Back -> front)."""
    name = name.lower().strip()
    if " // " in name:
        name = name.split(" // ")[0].strip()
    return name


def get_recommendations(commander_name: str, use_cache: bool = False) -> list[str]:
    """Get recommendations for a commander, with caching."""
    cache_key = commander_name.replace(" ", "_").replace(",", "").replace("'", "").lower()
    cache_path = RECOMMEND_CACHE_DIR / f"{cache_key}.json"

    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())

    cmd = ["uv", "run", "python3", "synergy_graph.py", "--commander", commander_name,
           "--recommend", "--top", "30"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        cards = parse_recommend_output(result.stdout)
        if cards:
            cache_path.write_text(json.dumps(cards, indent=2))
        return cards
    except Exception as e:
        print(f"  ERROR running recommend for {commander_name}: {e}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        return []


def get_edhrec_data(conn: sqlite3.Connection, slug: str) -> dict:
    """Get EDHREC synergy data from DB for a commander slug.

    Returns dict with keys:
        all_cards: dict[normalized_name] -> {name, synergy, section}
        high_syn: dict[normalized_name] -> {name, synergy, section}
        top_cards: set of normalized names in 'Top Cards' section
    """
    rows = conn.execute(
        "SELECT card_name, synergy, inclusion, num_decks, section "
        "FROM edhrec_card_synergy WHERE commander_slug = ?",
        (slug,)
    ).fetchall()

    all_cards = {}
    high_syn = {}
    top_cards = set()

    for card_name, synergy, inclusion, num_decks, section in rows:
        nname = normalize(card_name)
        entry = {"name": card_name, "synergy": synergy or 0.0,
                 "inclusion": inclusion or 0, "section": section or ""}
        all_cards[nname] = entry
        if section == "High Synergy Cards":
            high_syn[nname] = entry
        if section == "Top Cards":
            top_cards.add(nname)

    return {"all_cards": all_cards, "high_syn": high_syn, "top_cards": top_cards}


def build_slug_to_name(conn: sqlite3.Connection) -> dict[str, str]:
    """Build mapping from edhrec slug -> card name using the cards table."""
    slug_to_name = {}
    slugs = [r[0] for r in conn.execute(
        "SELECT DISTINCT commander_slug FROM edhrec_card_synergy"
    ).fetchall()]

    for slug in slugs:
        # Convert slug to approximate name for matching
        # "krenko-mob-boss" -> "krenko mob boss"
        name_approx = slug.replace("-", " ")
        # Try matching against cards table (case-insensitive, strip commas/apostrophes)
        result = conn.execute(
            "SELECT name FROM cards "
            "WHERE LOWER(REPLACE(REPLACE(REPLACE(name, ',', ''), '''', ''), '-', '')) = LOWER(?)",
            (name_approx.replace(",", "").replace("'", "").replace("-", ""),)
        ).fetchone()
        if result:
            slug_to_name[slug] = result[0]
        else:
            # Try fuzzy: just check if all slug words appear in the name
            # This handles cases like "y'shtola" where apostrophe is tricky
            words = slug.split("-")
            result = conn.execute(
                "SELECT name FROM cards WHERE type_line LIKE '%Legendary%' "
                "AND LOWER(name) LIKE ? LIMIT 1",
                (f"%{words[0]}%",)
            ).fetchone()
            if result and all(w in result[0].lower().replace(",", "").replace("'", "")
                             for w in words):
                slug_to_name[slug] = result[0]

    return slug_to_name


def name_to_slug(name: str) -> str:
    """Convert commander name to EDHREC-style slug."""
    slug = name.lower()
    # Remove special chars, replace spaces with hyphens
    slug = slug.replace(",", "").replace("'", "").replace("'", "")
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug.strip())
    return slug


def compare_commander(commander_name: str, slug: str, conn: sqlite3.Connection,
                      use_cache: bool = False, verbose: bool = True) -> dict | None:
    """Compare our recommendations against EDHREC for one commander."""
    edhrec = get_edhrec_data(conn, slug)
    if not edhrec["all_cards"]:
        if verbose:
            print(f"  No EDHREC data for {commander_name} (slug: {slug})")
        return None

    if verbose:
        print(f"\n{'='*70}")
        print(f"COMMANDER: {commander_name} (slug: {slug})")
        print(f"{'='*70}")

    our_recs = get_recommendations(commander_name, use_cache=use_cache)
    if not our_recs:
        if verbose:
            print("  No recommendations generated")
        return None

    our_recs_norm = {normalize(c) for c in our_recs[:30]}

    hi_syn_overlap = our_recs_norm & set(edhrec["high_syn"].keys())
    top_overlap = our_recs_norm & edhrec["top_cards"]
    on_page = our_recs_norm & set(edhrec["all_cards"].keys())
    not_edh = [n for n in our_recs[:30] if normalize(n) not in edhrec["all_cards"]]

    if verbose:
        # Show EDHREC high synergy cards
        hi_syn_sorted = sorted(edhrec["high_syn"].values(),
                               key=lambda x: x["synergy"], reverse=True)
        print(f"\n  EDHREC High Synergy Cards ({len(hi_syn_sorted)}):")
        for i, c in enumerate(hi_syn_sorted[:15], 1):
            in_ours = "OUR REC" if normalize(c["name"]) in our_recs_norm else "MISSED"
            print(f"    {i:2}. {c['name']:<40} syn={c['synergy']:.2f}  [{in_ours}]")

        # Show our recommendations
        print(f"\n  OUR top 30 recommendations:")
        for i, name in enumerate(our_recs[:30], 1):
            edh = edhrec["all_cards"].get(normalize(name))
            if edh:
                print(f"    {i:2}. {name:<40} EDHREC syn={edh['synergy']:.2f}  [{edh['section']}]")
            else:
                print(f"    {i:2}. {name:<40} NOT ON EDHREC PAGE")

        # Summary
        print(f"\n  SUMMARY:")
        print(f"    Hi-Syn (in High Synergy Cards):  {len(hi_syn_overlap)}/30")
        print(f"    Top (in Top Cards):              {len(top_overlap)}/30")
        print(f"    OnPage (anywhere on EDHREC):     {len(on_page)}/30")
        print(f"    NotEDH (not on EDHREC at all):   {len(not_edh)}/30")

        # Show missed high-syn cards
        missed_hi = set(edhrec["high_syn"].keys()) - our_recs_norm
        if missed_hi:
            missed_sorted = sorted(missed_hi,
                                   key=lambda x: edhrec["high_syn"][x]["synergy"],
                                   reverse=True)
            print(f"\n  TOP MISSED High Synergy Cards:")
            for nname in missed_sorted[:10]:
                c = edhrec["high_syn"][nname]
                print(f"    - {c['name']:<40} syn={c['synergy']:.2f}")

    return {
        "commander": commander_name,
        "slug": slug,
        "hi_syn": len(hi_syn_overlap),
        "top": len(top_overlap),
        "on_page": len(on_page),
        "not_edh": len(not_edh),
    }


def main():
    parser = argparse.ArgumentParser(description="Compare recommendations vs EDHREC synergy data")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--commander", type=str, help="Commander name (e.g. 'Krenko, Mob Boss')")
    group.add_argument("--all", action="store_true", help="All commanders with EDHREC data")
    parser.add_argument("--fast", action="store_true", help="Use cached recommend output")
    parser.add_argument("--quiet", action="store_true", help="Summary table only")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH))

    if args.commander:
        # Single commander mode
        slug = name_to_slug(args.commander)
        # Verify commander exists in cards table
        cmdr_row = conn.execute(
            "SELECT name FROM cards WHERE LOWER(name) = LOWER(?)",
            (args.commander,)
        ).fetchone()
        if not cmdr_row:
            print(f"Commander not found in cards DB: {args.commander}")
            conn.close()
            sys.exit(1)
        commander_name = cmdr_row[0]  # canonical name

        result = compare_commander(commander_name, slug, conn,
                                   use_cache=args.fast, verbose=not args.quiet)
        if result:
            if args.quiet:
                print(f"{'Commander':<40} {'Hi-Syn':>7} {'Top':>5} {'OnPage':>7} {'NotEDH':>7}")
                print("-" * 70)
                r = result
                print(f"{r['commander']:<40} {r['hi_syn']:>4}/30 {r['top']:>2}/30 {r['on_page']:>4}/30 {r['not_edh']:>4}/30")

    elif args.all:
        # All commanders mode
        print("Building slug-to-name mapping...")
        slug_to_name = build_slug_to_name(conn)
        print(f"Resolved {len(slug_to_name)} of "
              f"{conn.execute('SELECT COUNT(DISTINCT commander_slug) FROM edhrec_card_synergy').fetchone()[0]} slugs\n")

        all_results = {}
        for i, (slug, name) in enumerate(sorted(slug_to_name.items()), 1):
            if not args.quiet:
                print(f"[{i}/{len(slug_to_name)}] {name}")
            else:
                # Progress indicator
                if i % 50 == 0:
                    print(f"  Progress: {i}/{len(slug_to_name)}...", file=sys.stderr)

            result = compare_commander(name, slug, conn,
                                       use_cache=args.fast, verbose=not args.quiet)
            if result:
                all_results[slug] = result

        # Summary table
        print(f"\n\n{'='*80}")
        print("OVERALL SUMMARY")
        print(f"{'='*80}")
        print(f"{'Slug':<30} {'Commander':<35} {'Hi-Syn':>7} {'Top':>5} {'OnPage':>7} {'NotEDH':>7}")
        print("-" * 95)

        totals = {"hi_syn": 0, "top": 0, "on_page": 0, "not_edh": 0}
        for slug in sorted(all_results):
            r = all_results[slug]
            print(f"{slug:<30} {r['commander']:<35} {r['hi_syn']:>4}/30 {r['top']:>2}/30 "
                  f"{r['on_page']:>4}/30 {r['not_edh']:>4}/30")
            for k in totals:
                totals[k] += r[k]

        n = len(all_results)
        if n > 1:
            print("-" * 95)
            print(f"{'AVERAGE':<30} {'':<35} {totals['hi_syn']/n:>4.1f}/30 "
                  f"{totals['top']/n:>2.1f}/30 {totals['on_page']/n:>4.1f}/30 "
                  f"{totals['not_edh']/n:>4.1f}/30")
            print(f"\nCommanders evaluated: {n}")

    conn.close()


if __name__ == "__main__":
    main()
