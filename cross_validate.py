"""
Cross-validation tool: measures agreement between our tag system and EDHREC theme data.

Usage:
    python3 cross_validate.py                    # full report
    python3 cross_validate.py --theme tokens     # single theme
    python3 cross_validate.py --summary          # just counts
"""

import argparse
import json
import os
import sqlite3
from collections import defaultdict

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
EDHREC_PATH = os.path.join(DATA_DIR, "edhrec_theme_cards.json")

# Map EDHREC theme names to our internal strategy names.
# None means skip (too generic / infrastructure).
THEME_MAP = {
    "tokens": "tokens",
    "+1/+1-counters": "+1/+1-counters",
    "lifegain": "lifegain",
    "aristocrats": "aristocrats",
    "spellslinger": "spellslinger",
    "reanimator": "reanimator",
    "artifacts": "artifacts",
    "enchantress": "enchantress",
    "voltron": "voltron",
    "mill": "mill",
    "blink": "blink",
    "landfall": "landfall",
    "stax": "stax",
    "storm": "storm",
    "wheels": "wheels",
    "treasure": "treasure",
    "proliferate": "proliferate",
    "sacrifice": "aristocrats",
    "lifedrain": "lifedrain",
    "equipment": "equipment",
    "control": "control",
    "aggro": "go-wide",
    "burn": "lifedrain",
    "combo": None,      # too generic
    "ramp": None,       # infrastructure, not strategy
    "card-draw": None,  # infrastructure
}

EDHREC_SYNERGY_THRESHOLD = 0.15
STRATEGY_CONFIDENCE_THRESHOLD = 0.3


def load_edhrec_data(path: str = EDHREC_PATH) -> dict:
    """Load EDHREC theme cards JSON."""
    with open(path) as f:
        return json.load(f)


def get_our_strategy_cards(strategy: str, db_path: str = DB_PATH) -> set:
    """Return set of oracle_ids we tag with the given strategy (confidence >= threshold)."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT oracle_id FROM card_strategies WHERE strategy = ? AND confidence >= ?",
            (strategy, STRATEGY_CONFIDENCE_THRESHOLD),
        )
        return {row[0] for row in cursor.fetchall()}
    finally:
        conn.close()


def get_our_strategy_count(strategy: str, db_path: str = DB_PATH) -> int:
    """Return count of cards we tag with the given strategy."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM card_strategies WHERE strategy = ? AND confidence >= ?",
            (strategy, STRATEGY_CONFIDENCE_THRESHOLD),
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


def resolve_names_to_oracle_ids(names: list, db_path: str = DB_PATH) -> dict:
    """
    Given a list of card names, return {name: oracle_id} for cards found in our DB.
    Uses exact name match. Missing names are omitted.
    """
    if not names:
        return {}
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" * len(names))
        cursor = conn.execute(
            f"SELECT name, oracle_id FROM cards WHERE name IN ({placeholders})",
            names,
        )
        return {row[0]: row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def validate_theme(
    edhrec_theme: str,
    our_strategy: str,
    edhrec_cards: list,
    db_path: str = DB_PATH,
) -> dict:
    """
    Compare EDHREC high-synergy cards for a theme against our strategy tags.

    Returns a dict with:
        edhrec_high_synergy_count  - number of EDHREC cards above threshold (in our DB)
        our_strategy_count         - number of cards we tag with the strategy
        agreement                  - cards in both sets
        false_negatives            - count of EDHREC-only cards
        false_negative_cards       - list of {name, synergy} sorted by synergy desc
        us_only_count              - cards we tag but EDHREC doesn't list as high-synergy
    """
    # Filter EDHREC cards to high-synergy ones
    high_synergy = [c for c in edhrec_cards if c.get("synergy", 0) > EDHREC_SYNERGY_THRESHOLD]

    # Resolve names to oracle_ids (only cards that exist in our DB)
    names = [c["name"] for c in high_synergy]
    name_to_oracle = resolve_names_to_oracle_ids(names, db_path)
    synergy_by_name = {c["name"]: c["synergy"] for c in high_synergy}

    # Build set of oracle_ids for EDHREC high-synergy cards that are in our DB
    edhrec_oracle_ids = set(name_to_oracle.values())
    edhrec_in_db_count = len(edhrec_oracle_ids)

    # Build oracle_id -> name mapping for reporting
    oracle_to_name = {v: k for k, v in name_to_oracle.items()}

    # Our strategy cards
    our_oracle_ids = get_our_strategy_cards(our_strategy, db_path)
    our_count = len(our_oracle_ids)

    # Agreement: in both
    agreement_ids = edhrec_oracle_ids & our_oracle_ids
    agreement_count = len(agreement_ids)

    # False negatives: EDHREC says high-synergy, we don't tag with strategy
    fn_ids = edhrec_oracle_ids - our_oracle_ids
    false_negative_cards = []
    for oid in fn_ids:
        card_name = oracle_to_name.get(oid, oid)
        syn = synergy_by_name.get(card_name, 0.0)
        false_negative_cards.append({"name": card_name, "synergy": syn, "oracle_id": oid})
    false_negative_cards.sort(key=lambda x: x["synergy"], reverse=True)

    # Us-only: we tag it but EDHREC doesn't list as high-synergy
    us_only_count = len(our_oracle_ids - edhrec_oracle_ids)

    return {
        "edhrec_theme": edhrec_theme,
        "our_strategy": our_strategy,
        "edhrec_high_synergy_count": edhrec_in_db_count,
        "our_strategy_count": our_count,
        "agreement": agreement_count,
        "false_negatives": len(fn_ids),
        "false_negative_cards": false_negative_cards,
        "us_only_count": us_only_count,
    }


def run_full_validation(
    edhrec_data: dict,
    db_path: str = DB_PATH,
    single_theme: str = None,
    top_fn: int = 5,
) -> list:
    """
    Run validation for all mapped themes (or a single theme).
    Returns list of result dicts.
    """
    results = []
    themes_to_check = {}

    for edhrec_theme, our_strategy in THEME_MAP.items():
        if our_strategy is None:
            continue
        if single_theme and edhrec_theme != single_theme:
            continue
        if edhrec_theme not in edhrec_data:
            continue
        # Deduplicate: if multiple EDHREC themes map to same strategy, merge cards
        key = (edhrec_theme, our_strategy)
        themes_to_check[key] = edhrec_data[edhrec_theme]

    for (edhrec_theme, our_strategy), cards in themes_to_check.items():
        result = validate_theme(edhrec_theme, our_strategy, cards, db_path)
        result["top_fn"] = top_fn
        results.append(result)

    return results


def print_results(results: list, summary_only: bool = False, top_fn: int = 5):
    """Print formatted validation results."""
    if not results:
        print("No themes to validate.")
        return

    print("=== Cross-Validation: Our Tags vs EDHREC Themes ===\n")

    total_edhrec = 0
    total_agreement = 0
    total_fn = 0

    # Collect per-oracle_id false-negative appearances for summary
    fn_appearances = defaultdict(list)  # oracle_id -> [(theme, synergy, name)]

    for result in results:
        edhrec_theme = result["edhrec_theme"]
        our_strategy = result["our_strategy"]
        edhrec_count = result["edhrec_high_synergy_count"]
        our_count = result["our_strategy_count"]
        agreement = result["agreement"]
        fn_count = result["false_negatives"]
        fn_cards = result["false_negative_cards"]
        us_only = result["us_only_count"]

        coverage_pct = (agreement / edhrec_count * 100) if edhrec_count else 0.0
        total_edhrec += edhrec_count
        total_agreement += agreement
        total_fn += fn_count

        for card in fn_cards:
            fn_appearances[card["oracle_id"]].append({
                "theme": edhrec_theme,
                "synergy": card["synergy"],
                "name": card["name"],
            })

        if not summary_only:
            label = edhrec_theme
            if our_strategy != edhrec_theme:
                label = f"{edhrec_theme} → {our_strategy}"
            print(f"{label}:")
            print(f"  EDHREC high-synergy: {edhrec_count} cards | Our strategy: {our_count} cards")
            print(f"  Agreement: {agreement} ({coverage_pct:.0f}% of EDHREC)")
            if fn_count > 0:
                print(f"  False negatives (EDHREC says yes, we say no): {fn_count}")
                for card in fn_cards[:top_fn]:
                    print(f"    - {card['name']} (EDHREC synergy: {card['synergy']:.2f})")
            print()

    # Summary section
    themes_checked = len(results)
    avg_coverage = (total_agreement / total_edhrec * 100) if total_edhrec else 0.0

    # Find cards appearing in 3+ themes as false negatives
    multi_fn = {
        oid: entries
        for oid, entries in fn_appearances.items()
        if len(entries) >= 3
    }

    print("=== SUMMARY ===")
    print(f"Themes checked: {themes_checked}")
    print(f"Average EDHREC coverage: {avg_coverage:.0f}%")
    print(f"Total false negatives: {total_fn} (cards EDHREC flags that we miss)")

    if multi_fn:
        print(f"\nTop missed cards (appear in 3+ EDHREC themes but have no strategy):")
        # Sort by number of appearances descending
        sorted_multi = sorted(multi_fn.items(), key=lambda x: len(x[1]), reverse=True)
        for oid, entries in sorted_multi[:10]:
            name = entries[0]["name"]
            theme_names = [e["theme"] for e in entries]
            print(f"  - {name} (themes: {', '.join(theme_names)})")


def main():
    parser = argparse.ArgumentParser(
        description="Cross-validate our tag system against EDHREC theme data."
    )
    parser.add_argument("--theme", help="Validate a single EDHREC theme (e.g. tokens)")
    parser.add_argument(
        "--summary", action="store_true", help="Print only summary counts, not per-card details"
    )
    parser.add_argument(
        "--db", default=DB_PATH, help="Path to tags.db (default: data/tags.db)"
    )
    parser.add_argument(
        "--edhrec", default=EDHREC_PATH, help="Path to edhrec_theme_cards.json"
    )
    parser.add_argument(
        "--top-fn", type=int, default=5, help="Number of top false negatives to show per theme"
    )
    args = parser.parse_args()

    edhrec_data = load_edhrec_data(args.edhrec)
    results = run_full_validation(
        edhrec_data,
        db_path=args.db,
        single_theme=args.theme,
        top_fn=args.top_fn,
    )
    print_results(results, summary_only=args.summary, top_fn=args.top_fn)


if __name__ == "__main__":
    main()
