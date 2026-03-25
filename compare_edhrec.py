#!/usr/bin/env python3
"""Compare our --recommend output against EDHREC top cards for each deck.

Usage:
    python3 compare_edhrec.py                # full run (fetch EDHREC + run recommend + compare)
    python3 compare_edhrec.py --refresh      # re-run recommend for all decks, then compare
    python3 compare_edhrec.py --fast         # compare using cached recommend output only
    python3 compare_edhrec.py --deck krenko  # single deck only
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from decks import list_decks, load_deck


EDHREC_CACHE_DIR = Path("data/edhrec_commanders")
EDHREC_CACHE_DIR.mkdir(parents=True, exist_ok=True)

RECOMMEND_CACHE_DIR = Path("data/recommend_cache")
RECOMMEND_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def fetch_edhrec(slug: str) -> dict:
    """Fetch EDHREC commander page JSON, with file cache."""
    cache = EDHREC_CACHE_DIR / f"{slug}.json"
    if cache.exists():
        return json.loads(cache.read_text())

    url = f"https://json.edhrec.com/pages/commanders/{slug}.json"
    print(f"  Fetching {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "MTG-Synergy-Graph/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        cache.write_text(json.dumps(data, indent=2))
        time.sleep(1)
        return data
    except Exception as e:
        print(f"  ERROR fetching {slug}: {e}")
        return {}


def extract_edhrec_cards(data: dict) -> list[dict]:
    """Extract card names + synergy + inclusion from EDHREC JSON."""
    cards = []
    seen = set()
    container = data.get("container", {}).get("json_dict", {})
    cardlists = container.get("cardlists", [])
    for cl in cardlists:
        for cv in cl.get("cardviews", []):
            name = cv.get("name", "")
            if name and name not in seen:
                seen.add(name)
                cards.append({
                    "name": name,
                    "synergy": cv.get("synergy", 0.0),
                    "inclusion": cv.get("inclusion", 0),
                    "num_decks": cv.get("num_decks", 0),
                    "label": cv.get("label", ""),
                })
    return cards


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
                if "[" in rest:
                    card_name = rest[:rest.index("[")].strip()
                else:
                    card_name = rest.strip()
                if card_name:
                    cards.append(card_name)
    return cards


def get_our_recommendations(deck_name: str, use_cache: bool = False) -> list[str]:
    """Get recommendations, with caching."""
    cache = RECOMMEND_CACHE_DIR / f"{deck_name}.json"

    if use_cache and cache.exists():
        return json.loads(cache.read_text())

    try:
        result = subprocess.run(
            [sys.executable, "synergy_graph.py", "--deck", deck_name, "--recommend"],
            capture_output=True, text=True, timeout=120
        )
        cards = parse_recommend_output(result.stdout)
        cache.write_text(json.dumps(cards, indent=2))
        return cards
    except Exception as e:
        print(f"  ERROR running recommend for {deck_name}: {e}")
        if cache.exists():
            return json.loads(cache.read_text())
        return []


def normalize(name: str) -> str:
    """Normalize card name for comparison. Handles DFC cards (Front // Back → front)."""
    name = name.lower().strip()
    if " // " in name:
        name = name.split(" // ")[0].strip()
    return name


def compare_deck(deck_name: str, use_cache: bool = False, verbose: bool = True) -> dict:
    """Compare one deck against EDHREC. Returns metrics dict."""
    deck = load_deck(deck_name)
    commander = deck.COMMANDER
    slug = deck.EDHREC_SLUG
    decklist = {normalize(c) for c in deck.DECKLIST}

    if verbose:
        print(f"\n{'='*70}")
        print(f"DECK: {commander} ({deck_name})")
        print(f"{'='*70}")

    edhrec_data = fetch_edhrec(slug)
    if not edhrec_data:
        if verbose:
            print("  Skipping (no EDHREC data)")
        return {}

    edhrec_cards = extract_edhrec_cards(edhrec_data)
    edhrec_by_synergy = sorted(edhrec_cards, key=lambda x: x["synergy"], reverse=True)
    # Note: this compares our recommendations against EDHREC synergy-sorted cards.
    # The primary evaluation metric is Recall@K in optimize_weights.py, which
    # compares against EDHREC average decklists (a fuller ground truth).
    edhrec_top30_set = {normalize(c["name"]) for c in edhrec_by_synergy[:30]}
    edhrec_high_syn = {normalize(c["name"]): c for c in edhrec_cards if c["synergy"] >= 0.3}
    edhrec_all_names = {normalize(c["name"]): c for c in edhrec_cards}

    our_recs = get_our_recommendations(deck_name, use_cache=use_cache)
    our_recs_set = {normalize(c) for c in our_recs}

    overlap = edhrec_top30_set & our_recs_set
    edhrec_top30_in_deck = edhrec_top30_set & decklist
    our_in_edhrec_high = our_recs_set & set(edhrec_high_syn.keys())
    edhrec_high_not_in_ours = set(edhrec_high_syn.keys()) - our_recs_set - decklist
    our_not_in_edhrec = [n for n in our_recs[:30] if normalize(n) not in edhrec_all_names]
    our_on_edhrec = [n for n in our_recs[:30] if normalize(n) in edhrec_all_names]

    our_low_edhrec_syn = []
    for name in our_recs[:30]:
        edh = edhrec_all_names.get(normalize(name))
        if edh and edh["synergy"] < 0.1:
            our_low_edhrec_syn.append((name, edh))

    if verbose:
        print(f"\n  EDHREC top 30 (by synergy, not in deck):")
        shown = 0
        for c in edhrec_by_synergy:
            if normalize(c["name"]) in decklist:
                continue
            shown += 1
            if shown > 30:
                break
            in_ours = "✓ OUR REC" if normalize(c["name"]) in our_recs_set else "MISSED"
            print(f"    {shown:2}. {c['name']:<40} syn={c['synergy']:.2f}  incl={c['inclusion']:>5}  [{in_ours}]")

        print(f"\n  OUR top 30 recommendations:")
        for i, name in enumerate(our_recs[:30]):
            edh = edhrec_all_names.get(normalize(name))
            if edh:
                print(f"    {i+1:2}. {name:<40} EDHREC syn={edh['synergy']:.2f}  incl={edh['inclusion']:>5}")
            else:
                print(f"    {i+1:2}. {name:<40} NOT ON EDHREC PAGE")

        print(f"\n  SUMMARY:")
        print(f"    EDHREC high-synergy cards (≥0.3):   {len(edhrec_high_syn)}")
        print(f"    Our recs on EDHREC page:             {len(our_on_edhrec)}/30")
        print(f"    Our recs in EDHREC high-synergy:     {len(our_in_edhrec_high)}/30")
        print(f"    EDHREC top30 overlap w/ our top30:   {len(overlap)}")
        print(f"    EDHREC high-synergy MISSED by us:    {len(edhrec_high_not_in_ours)}")

        if edhrec_high_not_in_ours:
            missed_sorted = sorted(edhrec_high_not_in_ours,
                                   key=lambda x: edhrec_high_syn[x]["synergy"], reverse=True)
            print(f"\n  TOP MISSES (EDHREC high-synergy, not in our recs, not in deck):")
            for name_lower in missed_sorted[:10]:
                c = edhrec_high_syn[name_lower]
                print(f"    - {c['name']:<40} syn={c['synergy']:.2f}  incl={c['inclusion']:>5}")

        if our_not_in_edhrec:
            print(f"\n  ⚠ OUR RECS NOT ON EDHREC PAGE ({len(our_not_in_edhrec)}):")
            for name in our_not_in_edhrec[:10]:
                print(f"    - {name}")

        if our_low_edhrec_syn:
            print(f"\n  ⚠ LOW EDHREC SYNERGY (<0.1) in our recs:")
            for name, edh in our_low_edhrec_syn:
                print(f"    - {name:<40} EDHREC syn={edh['synergy']:.2f}")

    return {
        "commander": commander,
        "edhrec_top30_overlap": len(overlap),
        "our_in_edhrec_high": len(our_in_edhrec_high),
        "our_on_edhrec": len(our_on_edhrec),
        "edhrec_high_missed": len(edhrec_high_not_in_ours),
        "our_not_on_edhrec": len(our_not_in_edhrec),
        "our_low_syn": len(our_low_edhrec_syn),
    }


def main():
    parser = argparse.ArgumentParser(description="Compare recommendations vs EDHREC")
    parser.add_argument("--fast", action="store_true", help="Use cached recommend output")
    parser.add_argument("--refresh", action="store_true", help="Re-run recommend (clear cache)")
    parser.add_argument("--deck", help="Single deck to compare")
    parser.add_argument("--quiet", action="store_true", help="Summary only")
    args = parser.parse_args()

    if args.refresh:
        import shutil
        if RECOMMEND_CACHE_DIR.exists():
            shutil.rmtree(RECOMMEND_CACHE_DIR)
            RECOMMEND_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    decks = [args.deck] if args.deck else list_decks()
    print(f"Comparing {len(decks)} deck(s) against EDHREC\n")

    # Pre-generate recommendations in parallel (biggest bottleneck)
    if not args.fast and len(decks) > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        missing = [d for d in decks if not (RECOMMEND_CACHE_DIR / f"{d}.json").exists()]
        if missing:
            print(f"Generating recommendations for {len(missing)} decks in parallel...")
            with ProcessPoolExecutor(max_workers=min(4, len(missing))) as pool:
                futures = {pool.submit(get_our_recommendations, d, False): d for d in missing}
                for future in as_completed(futures):
                    deck_name = futures[future]
                    try:
                        future.result()
                        print(f"  ✓ {deck_name}")
                    except Exception as e:
                        print(f"  ✗ {deck_name}: {e}")
            print()

    all_results = {}
    for deck_name in decks:
        # Use cache since we just generated it (or it already existed)
        result = compare_deck(deck_name, use_cache=True, verbose=not args.quiet)
        if result:
            all_results[deck_name] = result

    print(f"\n\n{'='*70}")
    print("OVERALL SUMMARY")
    print(f"{'='*70}")
    print(f"{'Deck':<15} {'Commander':<35} {'On EDHREC':>9} {'Hi-Syn':>7} {'Top30':>6} {'Missed':>7} {'NotEDH':>7} {'LowSyn':>7}")
    print("-" * 100)
    totals = {"on": 0, "hi": 0, "t30": 0, "missed": 0, "not_edh": 0, "low": 0}
    for name, r in all_results.items():
        print(f"{name:<15} {r['commander']:<35} {r['our_on_edhrec']:>6}/30 {r['our_in_edhrec_high']:>4}/30 {r['edhrec_top30_overlap']:>6} {r['edhrec_high_missed']:>7} {r['our_not_on_edhrec']:>7} {r['our_low_syn']:>7}")
        totals["on"] += r["our_on_edhrec"]
        totals["hi"] += r["our_in_edhrec_high"]
        totals["t30"] += r["edhrec_top30_overlap"]
        totals["missed"] += r["edhrec_high_missed"]
        totals["not_edh"] += r["our_not_on_edhrec"]
        totals["low"] += r["our_low_syn"]
    n = len(all_results)
    if n > 1:
        print("-" * 100)
        print(f"{'AVERAGE':<15} {'':<35} {totals['on']/n:>6.1f}/30 {totals['hi']/n:>4.1f}/30 {totals['t30']/n:>6.1f} {totals['missed']/n:>7.1f} {totals['not_edh']/n:>7.1f} {totals['low']/n:>7.1f}")


if __name__ == "__main__":
    main()
