"""CLI dispatcher for the MTG Synergy Graph toolkit.

Usage:
    python3 synergy_graph.py --commander "Krenko, Mob Boss" --recommend
    python3 synergy_graph.py --commander "Krenko, Mob Boss" --recommend --top 10
    python3 synergy_graph.py --commander "Krenko, Mob Boss" --combos
"""

import argparse
import json
import sqlite3

from mtg_synergy.recommend import recommend_cards
from mtg_synergy.analysis.strategy import _detect_deck_types


def run():
    parser = argparse.ArgumentParser(description="MTG Synergy Graph — commander recommendations")
    parser.add_argument("--commander", type=str, required=True,
                        help="Commander name (e.g. 'Krenko, Mob Boss')")
    parser.add_argument("--recommend", action="store_true",
                        help="Recommend cards for this commander")
    parser.add_argument("--combos", action="store_true",
                        help="Detect combos for this commander's color identity")
    parser.add_argument("--gems", action="store_true",
                        help="Find hidden gems — rare cards with strong mechanical synergy")
    parser.add_argument("--top", type=int, default=50, help="Top N results (default: 50)")
    parser.add_argument("--strategies", default="auto",
                        help="Comma-separated strategies to focus (default: auto-detect)")
    parser.add_argument("--exclude-strategies", default=None,
                        help="Comma-separated strategies to exclude")
    args = parser.parse_args()

    if not args.recommend and not args.combos and not args.gems:
        parser.error("Must specify --recommend, --combos, or --gems")

    from tag_db import get_cards_by_names, DB_PATH

    conn = sqlite3.connect(DB_PATH)
    cmdr_row = conn.execute(
        "SELECT name, oracle_id, color_identity, type_line FROM cards "
        "WHERE LOWER(name) = LOWER(?)", (args.commander,)
    ).fetchone()
    if not cmdr_row:
        print(f"Commander not found: {args.commander}")
        conn.close()
        return
    cmdr_name, cmdr_oid, ci_json, cmdr_type = cmdr_row
    color_identity = set(json.loads(ci_json or "[]"))
    cards = get_cards_by_names([cmdr_name], DB_PATH)
    deck_set = {cmdr_name}

    # Detect strategies from commander
    from strategy_detector import detect_strategies
    active_strategies = set()
    if args.strategies == "auto":
        detected = detect_strategies(cmdr_oid, DB_PATH)
        active_strategies = {s["name"] for s in detected if s["confidence"] >= 0.3}
    else:
        active_strategies = set(args.strategies.split(","))

    if args.exclude_strategies:
        active_strategies -= set(args.exclude_strategies.split(","))

    # Detect tribal from commander type
    deck_types = _detect_deck_types(cards, deck_set)

    if active_strategies:
        print(f"Active strategies: {', '.join(sorted(active_strategies))}")

    if args.recommend:
        graph = {"adjacency": {}, "edges": [], "stats": {}}
        recommend_cards(graph, deck_set, cards, deck_types, args.top,
                        active_strategies=active_strategies, db_path=DB_PATH,
                        color_identity=color_identity, commander=cmdr_name)

    if args.combos:
        from mtg_synergy.combos.display import show_combos_tiered
        deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_set}
        show_combos_tiered(deck_oids, cmdr_name, DB_PATH, color_identity=color_identity)

    if args.gems:
        from mtg_synergy.recommend.hidden_gems import show_hidden_gems
        show_hidden_gems(cmdr_oid, conn, top_n=args.top)

    conn.close()
