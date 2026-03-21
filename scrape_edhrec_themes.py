"""
Scrape EDHREC theme pages to build card→theme ground truth mappings.

Fetches staple cards + synergy scores for each EDHREC theme from
the JSON API, producing a dataset for training and validation.

Usage:
    python3 scrape_edhrec_themes.py                    # scrape top 50 themes
    python3 scrape_edhrec_themes.py --themes 100       # scrape top 100
    python3 scrape_edhrec_themes.py --all               # all themes from registry
    python3 scrape_edhrec_themes.py --dry-run           # show plan
    python3 scrape_edhrec_themes.py --stats             # analyze existing data
"""

import argparse
import json
import os
import time
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "edhrec_theme_cards.json")
REGISTRY_FILE = os.path.join(os.path.dirname(__file__), "synergy_tag_registry.json")

EDHREC_JSON_BASE = "https://json.edhrec.com/pages/tags"


def fetch_theme_data(theme_slug: str) -> list[dict]:
    """Fetch card data for an EDHREC theme. Returns list of card dicts."""
    url = f"{EDHREC_JSON_BASE}/{theme_slug}.json"

    req = urllib.request.Request(url, headers={
        "User-Agent": "MTGSynergyGraph/1.0 (research project)",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  FAILED: {theme_slug} — {e}")
        return []

    cards = []
    json_dict = data.get("container", {}).get("json_dict", {})

    for cardlist in json_dict.get("cardlists", []):
        section = cardlist.get("tag", "")
        header = cardlist.get("header", "")

        # Skip commander lists and land sections
        if section in ("newcommanders", "topcommanders"):
            continue

        for card in cardlist.get("cardviews", []):
            name = card.get("name", "")
            if not name:
                continue

            cards.append({
                "name": name,
                "section": header,
                "inclusion": card.get("inclusion", 0),
                "synergy": card.get("synergy", 0),
                "num_decks": card.get("num_decks", 0),
            })

    return cards


def theme_to_slug(theme: str) -> str:
    """Convert theme name to EDHREC URL slug."""
    slug = theme.lower()
    # Special cases
    slug = slug.replace("+1/+1-counters", "plus-1-plus-1-counters")
    slug = slug.replace("-1/-1-counters", "minus-1-minus-1-counters")
    slug = slug.replace(" / ", "-")
    slug = slug.replace(" ", "-")
    return slug


def print_stats(data: dict):
    """Print statistics about scraped data."""
    print(f"\n=== EDHREC Theme Data Statistics ===\n")
    print(f"Themes scraped: {len(data)}")

    # Unique cards
    all_cards = {}
    for theme, cards in data.items():
        for card in cards:
            name = card["name"]
            if name not in all_cards:
                all_cards[name] = []
            all_cards[name].append({
                "theme": theme,
                "synergy": card.get("synergy", 0),
                "inclusion": card.get("inclusion", 0),
            })

    print(f"Unique cards: {len(all_cards)}")
    print(f"Total card-theme associations: {sum(len(v) for v in all_cards.values())}")

    # Cards per theme
    counts = [len(cards) for cards in data.values()]
    print(f"\nCards per theme: min={min(counts)}, max={max(counts)}, "
          f"avg={sum(counts)/len(counts):.0f}")

    # Theme coverage per card
    theme_counts = [len(v) for v in all_cards.values()]
    print(f"Themes per card: min={min(theme_counts)}, max={max(theme_counts)}, "
          f"avg={sum(theme_counts)/len(theme_counts):.1f}")

    # Top cards by theme membership
    top_cards = sorted(all_cards.items(), key=lambda x: -len(x[1]))[:25]
    print(f"\nTop 25 cards by theme count:")
    for name, themes in top_cards:
        avg_synergy = sum(t["synergy"] for t in themes) / len(themes)
        print(f"  {name:<40} {len(themes):>3} themes  (avg synergy: {avg_synergy:>+.0%})")

    # High synergy cards (avg synergy > 20%)
    high_syn = [(name, themes) for name, themes in all_cards.items()
                if sum(t["synergy"] for t in themes) / len(themes) > 0.2
                and len(themes) >= 3]
    high_syn.sort(key=lambda x: -sum(t["synergy"] for t in x[1]) / len(x[1]))
    print(f"\nHigh synergy cards (avg >20%, 3+ themes): {len(high_syn)}")
    for name, themes in high_syn[:20]:
        avg_syn = sum(t["synergy"] for t in themes) / len(themes)
        theme_names = [t["theme"] for t in themes]
        print(f"  {name:<35} {avg_syn:>+.0%} across {theme_names[:5]}")

    # Themes sorted by card count
    print(f"\nThemes by card count:")
    for theme, cards in sorted(data.items(), key=lambda x: -len(x[1]))[:20]:
        high_syn_count = sum(1 for c in cards if c.get("synergy", 0) > 0.15)
        print(f"  {theme:<30} {len(cards):>4} cards ({high_syn_count} high-synergy)")


def main():
    parser = argparse.ArgumentParser(description="Scrape EDHREC theme staples")
    parser.add_argument("--themes", type=int, default=50,
                        help="Number of top themes to scrape (default: 50)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without fetching")
    parser.add_argument("--all", action="store_true",
                        help="Scrape all themes from registry")
    parser.add_argument("--stats", action="store_true",
                        help="Show stats on existing data")
    args = parser.parse_args()

    if args.stats:
        if not os.path.exists(OUTPUT_FILE):
            print(f"No data found at {OUTPUT_FILE}")
            return
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
        print_stats(data)
        return

    # Load themes from registry
    with open(REGISTRY_FILE) as f:
        registry = json.load(f)

    archetype_themes = registry["themes"]["archetype"]
    typal_themes = registry["themes"]["typal"]

    if args.all:
        themes = archetype_themes + typal_themes
    else:
        # Prioritize archetype themes, then typal
        themes = archetype_themes[:args.themes]
        remaining = args.themes - len(themes)
        if remaining > 0:
            themes += typal_themes[:remaining]

    print(f"Themes to scrape: {len(themes)}")

    if args.dry_run:
        for i, theme in enumerate(themes, 1):
            slug = theme_to_slug(theme)
            print(f"  {i:>3}. {theme:<30} → /tags/{slug}")
        return

    # Resume support
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        print(f"Resuming: {len(existing)} themes already scraped")

    results = dict(existing)
    scraped = 0
    failed = 0
    t0 = time.time()

    for i, theme in enumerate(themes, 1):
        if theme in results:
            continue

        slug = theme_to_slug(theme)
        cards = fetch_theme_data(slug)

        if cards:
            results[theme] = cards
            scraped += 1
            high_syn = sum(1 for c in cards if c.get("synergy", 0) > 0.15)
            print(f"  [{i}/{len(themes)}] {theme}: {len(cards)} cards "
                  f"({high_syn} high-synergy)", flush=True)
        else:
            failed += 1
            print(f"  [{i}/{len(themes)}] {theme}: FAILED", flush=True)

        # Save every 10 themes
        if scraped % 10 == 0 and scraped > 0:
            with open(OUTPUT_FILE, "w") as f:
                json.dump(results, f, indent=2)

        # Rate limit: be polite to EDHREC
        time.sleep(1.5)

    # Final save
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    elapsed = time.time() - t0
    print(f"\nDone: {scraped} scraped, {failed} failed in {elapsed/60:.1f}m")
    print(f"Output: {OUTPUT_FILE}")

    # Show stats
    if results:
        print_stats(results)


if __name__ == "__main__":
    main()
