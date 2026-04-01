#!/usr/bin/env python3
"""Compare our --recommend output against EDHREC synergy data from the DB.

Usage:
    python3 compare_edhrec.py --commander "Krenko, Mob Boss"   # single commander
    python3 compare_edhrec.py --top 100                        # top 100 by popularity (quiet)
    python3 compare_edhrec.py --all                            # all 2700+ commanders (verbose)
    python3 compare_edhrec.py --all --quiet                    # all commanders (summary only)
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from mtg_synergy.config import DB_PATH


def normalize(name: str) -> str:
    """Normalize card name for comparison. Handles DFC cards (Front // Back -> front)."""
    name = name.lower().strip()
    if " // " in name:
        name = name.split(" // ")[0].strip()
    return name




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
    slugs = [r[0] for r in conn.execute(
        "SELECT DISTINCT commander_slug FROM edhrec_card_synergy"
    ).fetchall()]

    # Load ALL cards once (1 query) instead of 2 queries per slug
    all_cards = conn.execute(
        "SELECT name, type_line FROM cards"
    ).fetchall()

    # Build lookup: normalized name (no commas/apostrophes/hyphens, lowered) -> original name
    norm_lookup: dict[str, str] = {}
    # Build legendary cards list for fuzzy fallback
    legendary_cards: list[tuple[str, str]] = []  # (name, normalized_name)
    for name, type_line in all_cards:
        norm_key = name.lower().replace(",", "").replace("'", "").replace("-", "")
        norm_lookup[norm_key] = name
        if type_line and "Legendary" in type_line:
            legendary_cards.append((name, name.lower().replace(",", "").replace("'", "")))

    slug_to_name = {}
    for slug in slugs:
        # Convert slug to approximate name for matching
        # "krenko-mob-boss" -> "krenko mob boss"
        name_approx = slug.replace("-", " ")
        norm_key = name_approx.replace(",", "").replace("'", "").replace("-", "")
        result = norm_lookup.get(norm_key)
        if result:
            slug_to_name[slug] = result
        else:
            # Fuzzy fallback: check if all slug words appear in a legendary card name
            words = slug.split("-")
            for card_name, card_norm in legendary_cards:
                if words[0] in card_norm and all(w in card_norm for w in words):
                    slug_to_name[slug] = card_name
                    break

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
                      our_recs: list[str], verbose: bool = True) -> dict | None:
    """Compare our recommendations against EDHREC for one commander.

    Args:
        our_recs: list of card names from our recommendations (pre-computed).
    """
    edhrec = get_edhrec_data(conn, slug)
    if not edhrec["all_cards"]:
        return None

    if verbose:
        print(f"\n{'='*70}")
        print(f"COMMANDER: {commander_name} (slug: {slug})")
        print(f"{'='*70}")

    if not our_recs:
        if verbose:
            print("  No recommendations generated")
        return None

    our_recs_norm = {normalize(c) for c in our_recs[:50]}

    hi_syn_overlap = our_recs_norm & set(edhrec["high_syn"].keys())
    top_overlap = our_recs_norm & edhrec["top_cards"]
    on_page = our_recs_norm & set(edhrec["all_cards"].keys())
    not_edh = [n for n in our_recs[:50] if normalize(n) not in edhrec["all_cards"]]

    if verbose:
        # Show EDHREC high synergy cards
        hi_syn_sorted = sorted(edhrec["high_syn"].values(),
                               key=lambda x: x["synergy"], reverse=True)
        print(f"\n  EDHREC High Synergy Cards ({len(hi_syn_sorted)}):")
        for i, c in enumerate(hi_syn_sorted[:15], 1):
            in_ours = "OUR REC" if normalize(c["name"]) in our_recs_norm else "MISSED"
            print(f"    {i:2}. {c['name']:<40} syn={c['synergy']:.2f}  [{in_ours}]")

        # Show our recommendations
        print(f"\n  OUR top 50 recommendations:")
        for i, name in enumerate(our_recs[:50], 1):
            edh = edhrec["all_cards"].get(normalize(name))
            if edh:
                print(f"    {i:2}. {name:<40} EDHREC syn={edh['synergy']:.2f}  [{edh['section']}]")
            else:
                print(f"    {i:2}. {name:<40} NOT ON EDHREC PAGE")

        # Summary
        print(f"\n  SUMMARY:")
        print(f"    Hi-Syn (in High Synergy Cards):  {len(hi_syn_overlap)}/50")
        print(f"    Top (in Top Cards):              {len(top_overlap)}/50")
        print(f"    OnPage (anywhere on EDHREC):     {len(on_page)}/50")
        print(f"    NotEDH (not on EDHREC at all):   {len(not_edh)}/50")

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
    group.add_argument("--top", type=int, metavar="N",
                        help="Compare top N commanders by popularity (quiet summary)")
    group.add_argument("--all", action="store_true", help="All commanders with EDHREC data")
    parser.add_argument("--quiet", action="store_true", help="Summary table only")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH))

    from mtg_synergy.recommend.scoring import batch_recommend

    if args.commander:
        # Single commander
        cmdr_row = conn.execute(
            "SELECT name FROM cards WHERE LOWER(name) = LOWER(?)",
            (args.commander,)
        ).fetchone()
        if not cmdr_row:
            print(f"Commander not found: {args.commander}")
            conn.close()
            sys.exit(1)
        commander_name = cmdr_row[0]
        slug = name_to_slug(commander_name)

        t0 = time.time()
        recs = batch_recommend(conn, [commander_name], top_n=50, verbose=True)
        our_recs = [name for name, _ in recs.get(commander_name, [])]
        print(f"  Scored in {time.time()-t0:.1f}s")

        result = compare_commander(commander_name, slug, conn, our_recs,
                                   verbose=not args.quiet)
        if result and args.quiet:
            print(f"{'Commander':<40} {'Hi-Syn':>7} {'Top':>5} {'OnPage':>7} {'NotEDH':>7}")
            print("-" * 70)
            r = result
            print(f"{r['commander']:<40} {r['hi_syn']:>4}/50 {r['top']:>2}/50 "
                  f"{r['on_page']:>4}/50 {r['not_edh']:>4}/50")

    elif args.all or args.top:
        # Batch mode — load model once, score multiple commanders
        is_top_n = bool(args.top)
        if is_top_n:
            args.quiet = True

        print("Building slug-to-name mapping...")
        slug_to_name = build_slug_to_name(conn)
        total_slugs = conn.execute(
            "SELECT COUNT(DISTINCT commander_slug) FROM edhrec_card_synergy"
        ).fetchone()[0]
        print(f"Resolved {len(slug_to_name)} of {total_slugs} slugs")

        if is_top_n:
            # Sort by popularity (synergy row count = proxy for deck count)
            slug_popularity = {}
            for row in conn.execute(
                "SELECT commander_slug, COUNT(*) FROM edhrec_card_synergy "
                "GROUP BY commander_slug"
            ):
                slug_popularity[row[0]] = row[1]
            name_to_slug_pop = {v: k for k, v in slug_to_name.items()}
            commander_names = sorted(
                slug_to_name.values(),
                key=lambda n: slug_popularity.get(name_to_slug_pop.get(n, ""), 0),
                reverse=True
            )[:args.top]
        else:
            commander_names = sorted(slug_to_name.values())

        print(f"\nScoring {len(commander_names)} commanders (batch mode)...")
        t0 = time.time()
        all_recs = batch_recommend(conn, commander_names, top_n=50,
                                   verbose=not args.quiet)
        elapsed = time.time() - t0
        print(f"Batch scoring done: {elapsed:.1f}s "
              f"({elapsed/len(commander_names):.2f}s/commander)\n")

        # Compare each against EDHREC
        name_to_slug_map = {v: k for k, v in slug_to_name.items()}
        all_results = {}
        for name in commander_names:
            slug = name_to_slug_map.get(name, name_to_slug(name))
            our_recs = [n for n, _ in all_recs.get(name, [])]
            result = compare_commander(name, slug, conn, our_recs,
                                       verbose=not args.quiet)
            if result:
                all_results[slug] = result

        # Summary table
        print(f"\n{'='*95}")
        print("OVERALL SUMMARY")
        print(f"{'='*95}")
        print(f"{'Commander':<45} {'Hi-Syn':>7} {'Top':>5} {'OnPage':>7} {'NotEDH':>7}")
        print("-" * 75)

        totals = {"hi_syn": 0, "top": 0, "on_page": 0, "not_edh": 0}
        for slug in sorted(all_results):
            r = all_results[slug]
            print(f"{r['commander']:<45} {r['hi_syn']:>4}/50 {r['top']:>2}/50 "
                  f"{r['on_page']:>4}/50 {r['not_edh']:>4}/50")
            for k in totals:
                totals[k] += r[k]

        n = len(all_results)
        if n > 0:
            print("-" * 75)
            print(f"{'AVERAGE':<45} {totals['hi_syn']/n:>4.1f}/50 "
                  f"{totals['top']/n:>2.1f}/50 {totals['on_page']/n:>4.1f}/50 "
                  f"{totals['not_edh']/n:>4.1f}/50")
            print(f"\nCommanders evaluated: {n}")
            print(f"Time: {elapsed:.0f}s ({elapsed/n:.2f}s/commander)")

    conn.close()


if __name__ == "__main__":
    main()
