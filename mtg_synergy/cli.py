"""CLI dispatcher for the MTG Synergy Graph toolkit.

Usage:
    python3 synergy_graph.py --deck kyler              # build + print top synergies
    python3 synergy_graph.py --deck krenko --validate  # compare vs hand-curated pairs
    python3 synergy_graph.py --deck kyler --card "Hardened Scales"
    python3 synergy_graph.py --deck kyler --visualize  # interactive HTML graph
    python3 synergy_graph.py --deck kyler --export
"""

import argparse
import json
import os

from mtg_synergy.config import DATA_DIR
from mtg_synergy.combos import find_combos, find_combos_tiered
from mtg_synergy.combos.display import show_combos, show_combos_tiered, validate_against_curated
from mtg_synergy.recommend import recommend_cards, suggest_swaps, show_swaps
from mtg_synergy.analysis import (
    show_card_synergies, show_deck_synergies, show_deck_analysis,
    load_merged, build_from_commander,
)
from mtg_synergy.analysis.strategy import _detect_deck_types
from mtg_synergy.analysis.visualization import generate_visualization


def run():
    from decks import list_decks

    parser = argparse.ArgumentParser(description="Build MTG synergy graph")
    parser.add_argument("--deck", choices=list_decks(), help="Deck config to use")
    parser.add_argument("--commander", type=str, help="Build recommendations from commander alone")
    parser.add_argument("--build", action="store_true",
                        help="Build deck from commander (use with --commander)")
    parser.add_argument("--input", type=str, help="Override: load cards from JSON file instead of DB")
    parser.add_argument("--card", type=str, help="Show synergies for specific card")
    parser.add_argument("--deck-view", action="store_true",
                        help="Show synergy network within the deck")
    parser.add_argument("--recommend", action="store_true",
                        help="Recommend cards based on synergy with the deck")
    parser.add_argument("--combos", action="store_true",
                        help="Detect 3- and 4-card combos in the deck")
    parser.add_argument("--swaps", action="store_true",
                        help="Suggest card swaps to improve deck synergy")
    parser.add_argument("--validate", action="store_true",
                        help="Validate against hand-curated synergy pairs")
    parser.add_argument("--visualize", action="store_true",
                        help="Generate interactive HTML visualization")
    parser.add_argument("--export", action="store_true", help="Export graph as JSON")
    parser.add_argument("--top", type=int, default=30, help="Top N edges to show")
    parser.add_argument("--strategies", default="auto",
                        help="Comma-separated strategies to focus (default: auto-detect)")
    parser.add_argument("--exclude-strategies", default=None,
                        help="Comma-separated strategies to exclude")
    args = parser.parse_args()

    # Commander build mode — no deck needed
    if args.commander and args.build:
        build_from_commander(args.commander, args.top)
        return

    if not args.deck:
        parser.error("--deck is required (or use --commander with --build)")

    if args.input:
        # Manual override: load from JSON file
        cards = load_merged(args.input)
        print(f"Loaded {len(cards)} cards from {args.input}")
    else:
        # Default: load deck cards from SQLite DB
        from tag_db import get_cards_by_names, DB_PATH
        from decks import load_deck
        deck = load_deck(args.deck)
        deck_names = deck.DECKLIST + [deck.COMMANDER]

        cards = get_cards_by_names(deck_names, DB_PATH)
        print(f"Loaded {len(cards)} deck cards from DB")

        # For --recommend and --swaps, tower pre-filter handles candidate discovery.
        # --card and --visualize require the legacy graph which has been removed.

    # --- Strategy detection ---
    active_strategies = set()
    db_path = None
    if not args.input:
        from tag_db import DB_PATH as _db_path
        db_path = _db_path
        from strategy_detector import detect_strategies
        commander_card = next((c for c in cards if c["name"] == deck.COMMANDER), None)
        commander_oid = commander_card["oracle_id"] if commander_card else None
        if args.strategies == "auto" and commander_oid:
            detected = detect_strategies(commander_oid, db_path)
            active_strategies = {s["name"] for s in detected if s["confidence"] >= 0.3}
            # Also detect strategies from deck composition:
            # 1. Tribal strategies from creature type distribution
            deck_names_set = set(deck.DECKLIST) | {deck.COMMANDER}
            deck_cards_for_types = [c for c in cards if c["name"] in deck_names_set]
            deck_types = _detect_deck_types(deck_cards_for_types, deck_names_set)
            if deck_types:
                from strategy_detector import CREATURE_TYPE_STRATEGIES
                import sqlite3 as _sqlite3
                _conn = _sqlite3.connect(db_path)
                for dtype in deck_types:
                    strat = CREATURE_TYPE_STRATEGIES.get(dtype.lower())
                    if strat and strat not in active_strategies:
                        has_cards = _conn.execute(
                            "SELECT 1 FROM card_strategies WHERE strategy = ? LIMIT 1",
                            (strat,)
                        ).fetchone()
                        if has_cards:
                            active_strategies.add(strat)
                _conn.close()

            # 2. Strategies shared by 20%+ of non-land deck cards
            import sqlite3 as _sqlite3
            _conn = _sqlite3.connect(db_path)
            deck_oid_set = {c["oracle_id"] for c in cards if c["name"] in deck_names_set}
            non_land_count = sum(1 for c in cards
                                 if c["name"] in deck_names_set and "Land" not in c.get("type_line", ""))
            if non_land_count > 0:
                from collections import Counter as _Counter
                strat_counts = _Counter()
                for oid in deck_oid_set:
                    for row in _conn.execute(
                        "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
                        (oid,)
                    ):
                        strat_counts[row[0]] += 1
                for strat, cnt in strat_counts.items():
                    if cnt / non_land_count >= 0.2 and strat not in active_strategies:
                        active_strategies.add(strat)
            _conn.close()
        elif args.strategies != "auto":
            active_strategies = set(args.strategies.split(","))
        if args.exclude_strategies:
            active_strategies -= set(args.exclude_strategies.split(","))
        if active_strategies:
            print(f"Active strategies: {', '.join(sorted(active_strategies))}")

    # The legacy provides/wants graph has been removed.
    # --card and --visualize are no longer supported.
    # --recommend and --swaps use the tower pre-filter + causal graph.
    graph = {"adjacency": {}, "edges": [], "stats": {}}

    # Ensure deck config is loaded (already set in DB path, need it for --input path)
    if args.input:
        from decks import load_deck
        deck = load_deck(args.deck)

    if args.card or args.visualize:
        print("Note: --card and --visualize require the legacy provides/wants graph which has been removed.")
        print("Use --recommend instead.")
        return
    elif args.deck_view or args.recommend or args.combos or args.swaps:
        deck_set = set(deck.DECKLIST) | {deck.COMMANDER}
        deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_set}
        if args.deck_view:
            show_deck_synergies(graph, deck_set, deck.COMMANDER, cards, args.top)
            if db_path and active_strategies:
                deck_cards_in_set = [c for c in cards if c["name"] in deck_set]
                show_deck_analysis(deck_cards_in_set, deck_oids, active_strategies, deck.COMMANDER, db_path, graph=graph, deck_set=deck_set)
        if args.combos:
            if db_path:
                # Use enhanced 3-tier combo detection
                show_combos_tiered(deck_oids, deck.COMMANDER, db_path, color_identity=deck.COLOR_IDENTITY)
            else:
                # Fallback to legacy combo detection
                combos = find_combos(graph, cards, deck_set, deck.COMMANDER, args.top)
                show_combos(combos, deck.COMMANDER, args.top)
        if args.swaps:
            swap_deck_types = _detect_deck_types(cards, deck_set)
            swaps = suggest_swaps(graph, deck_set, deck.COMMANDER, cards, args.top,
                                  active_strategies=active_strategies, db_path=db_path,
                                  deck_types=swap_deck_types,
                                  edhrec_slug=getattr(deck, 'EDHREC_SLUG', None),
                                  color_identity=deck.COLOR_IDENTITY)
            show_swaps(swaps, args.top)
        if args.recommend:
            # Auto-detect dominant creature types for tribal boost
            deck_types = _detect_deck_types(cards, deck_set)
            recommend_cards(graph, deck_set, cards, deck_types, args.top,
                            active_strategies=active_strategies, db_path=db_path,
                            color_identity=deck.COLOR_IDENTITY, commander=deck.COMMANDER,
                            edhrec_slug=getattr(deck, 'EDHREC_SLUG', None))
    elif args.validate:
        validate_against_curated(graph, deck.SYNERGY_PAIRS)
    elif args.export:
        graph_output = os.path.join(str(DATA_DIR), f"{args.deck}_synergy_graph.json")
        export = {
            "edges": graph["edges"],
            "stats": graph["stats"],
        }
        with open(graph_output, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\nExported graph to {graph_output}")
    else:
        # Show top edges
        print(f"\nTop {args.top} synergy edges:")
        print(f"{'─' * 70}")
        for edge in graph["edges"][:args.top]:
            sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
            print(f"  [{edge['score']:5.1f} {sig}] {edge['source']} ↔ {edge['target']}")
            for r in edge["reasons"][:2]:
                print(f"       {r}")
