"""CLI dispatcher for the MTG Synergy Graph toolkit.

Usage:
    python3 scripts/synergy_graph.py --commander "Krenko, Mob Boss" --recommend
    python3 scripts/synergy_graph.py --commander "Krenko, Mob Boss" --recommend --top 10
    python3 scripts/synergy_graph.py --commander "Krenko, Mob Boss" --gems
"""

import argparse
import json
import sqlite3

from mtg_synergy.recommend import recommend_cards
from mtg_synergy_train.analysis.strategy import _detect_deck_types


def run():
    parser = argparse.ArgumentParser(description="MTG Synergy Graph — commander recommendations")
    parser.add_argument("--commander", type=str, default=None,
                        help="Commander name (e.g. 'Krenko, Mob Boss')")
    parser.add_argument("--recommend", action="store_true",
                        help="Recommend cards for this commander")
    parser.add_argument("--gems", action="store_true",
                        help="Find hidden gems — rare cards with strong mechanical synergy")
    parser.add_argument("--deck", type=str, default=None,
                        help="Path to deck file (one card name per line)")
    parser.add_argument("--top", type=int, default=50, help="Top N results (default: 50)")
    parser.add_argument("--strategies", default="auto",
                        help="Comma-separated strategies to focus (default: auto-detect)")
    parser.add_argument("--exclude-strategies", default=None,
                        help="Comma-separated strategies to exclude")
    args = parser.parse_args()

    if not args.recommend and not args.gems:
        parser.error("Must specify --recommend or --gems")

    if not args.commander:
        parser.error("--recommend and --gems require --commander")

    from mtg_synergy_train.tag_db import get_cards_by_names, DB_PATH

    conn = sqlite3.connect(DB_PATH)

    # --- Commander-based modes ---
    cmdr_row = conn.execute(
        "SELECT name, oracle_id, color_identity, type_line FROM cards "
        "WHERE LOWER(name) = LOWER(?)", (args.commander,)
    ).fetchone()
    if not cmdr_row:
        print(f"Commander not found: {args.commander}")
        conn.close()
        return
    cmdr_name, cmdr_oid, ci_json, _ = cmdr_row
    color_identity = set(json.loads(ci_json or "[]"))
    cards = get_cards_by_names([cmdr_name], DB_PATH)
    deck_set = {cmdr_name}

    # If --deck provided with --commander, load the deck list too
    if args.deck:
        try:
            with open(args.deck) as f:
                card_names = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except OSError as e:
            parser.error(f"Cannot read deck file {args.deck!r}: {e}")
        extra_cards = get_cards_by_names(card_names, DB_PATH)
        cards.extend(extra_cards)
        deck_set.update(c["name"] for c in extra_cards)

    # Detect strategies from commander
    active_strategies = set()
    if args.strategies == "auto":
        try:
            from strategy_detector import detect_strategies
        except ImportError:
            # strategy_detector is a standalone script in scripts/
            # Available when running from monorepo, not from installed wheel
            import sys, pathlib
            for parent in pathlib.Path(__file__).resolve().parents:
                candidate = parent / "scripts" / "strategy_detector.py"
                if candidate.exists():
                    sys.path.insert(0, str(candidate.parent))
                    from strategy_detector import detect_strategies
                    break
            else:
                detect_strategies = None
                print("WARNING: strategy_detector not found; strategy auto-detection disabled")
        if detect_strategies is not None:
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

    if args.gems:
        from mtg_synergy.recommend.hidden_gems import show_hidden_gems
        show_hidden_gems(cmdr_oid, conn, top_n=args.top)

    conn.close()
