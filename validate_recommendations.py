#!/usr/bin/env python3
"""Validate recommendations against EDHREC per-commander data.

Scrapes EDHREC's "High Synergy Cards" for each commander and compares
against our --recommend output. Measures: what % of EDHREC's top
recommendations appear in our top recommendations?

Usage:
    python3 validate_recommendations.py                    # all decks
    python3 validate_recommendations.py --deck krenko      # single deck
    python3 validate_recommendations.py --scrape-only      # just fetch EDHREC data
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
CACHE_DIR = os.path.join(DATA_DIR, "edhrec_commanders")
EDHREC_BASE = "https://json.edhrec.com/pages/commanders"


def fetch_commander_data(slug):
    """Fetch EDHREC per-commander data. Returns parsed JSON or None."""
    cache_path = os.path.join(CACHE_DIR, f"{slug}.json")

    # Use cache if fresh (< 7 days)
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < 7 * 86400:
            with open(cache_path) as f:
                return json.load(f)

    url = f"{EDHREC_BASE}/{slug}.json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"  Failed to fetch {slug}: {e}")
        return None

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(data, f)

    return data


def extract_high_synergy_cards(data):
    """Extract high synergy cards from EDHREC commander data.

    Returns list of {name, synergy, inclusion, section}.
    """
    cards = []
    cardlists = data.get("container", {}).get("json_dict", {}).get("cardlists", [])

    for cl in cardlists:
        header = cl.get("header", "")
        for cv in cl.get("cardviews", []):
            name = cv.get("name", "")
            synergy = cv.get("synergy", 0)
            inclusion = cv.get("inclusion", 0)
            cards.append({
                "name": name,
                "synergy": synergy,
                "inclusion": inclusion,
                "section": header,
            })

    return cards


def get_our_recommendations(deck_name, top_n=100):
    """Get our system's recommendations for a deck.

    Returns list of card names ranked by synergy score.
    """
    sys.path.insert(0, os.path.dirname(__file__))
    from decks import load_deck
    from tag_db import get_cards_by_names, find_synergy_candidates, DB_PATH as db_path
    from synergy_graph import build_graph, _candidate_scores, _detect_deck_types

    deck = load_deck(deck_name)
    deck_names = deck.DECKLIST + [deck.COMMANDER]

    cards = get_cards_by_names(deck_names, db_path)
    deck_set = set(deck.DECKLIST) | {deck.COMMANDER}

    # Get candidates (with commander bridge expansion)
    commander_card = next((c for c in cards if c["name"] == deck.COMMANDER), None)
    candidates = find_synergy_candidates(cards, db_path, commander=commander_card)

    # Filter by color identity
    from synergy_graph import _filter_candidates
    candidates = _filter_candidates(candidates, deck.COLOR_IDENTITY, db_path)

    deck_oids = {c["oracle_id"] for c in cards}
    for c in candidates:
        if c["oracle_id"] not in deck_oids:
            cards.append(c)

    # Build graph (pass deck_oids so fan-out caps preserve deck card edges)
    graph = build_graph(cards, deck_oids=deck_oids)

    # Get candidate scores (commander-weighted)
    from synergy_graph import _deck_card_scores
    deck_scores = _deck_card_scores(graph, deck_set)
    key_cards_ranked = sorted(
        [(name, info["total"]) for name, info in deck_scores.items() if name != deck.COMMANDER],
        key=lambda x: -x[1]
    )
    key_cards = {name for name, score in key_cards_ranked[:10]}
    scores = _candidate_scores(graph, deck_set, commander=deck.COMMANDER, key_cards=key_cards)

    # Rank by total score
    ranked = sorted(scores.items(), key=lambda x: x[1]["total"], reverse=True)

    # Also get ML-based recommendations (scores ALL color-legal cards)
    model_path = os.path.join(os.path.dirname(__file__), "data", "recommender_weights.json")
    if os.path.exists(model_path):
        from train_recommender import predict as ml_predict, _init_caches, compute_features
        import sqlite3
        with open(model_path) as f:
            model = json.load(f)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Initialize Spellbook and ability caches for feature computation
        if not hasattr(compute_features, '_spellbook_cache'):
            _init_caches(conn)

        # Get ALL legal cards in commander's colors
        all_cards = []
        ci_str = ','.join(f'"{c}"' for c in deck.COLOR_IDENTITY)
        for row in conn.execute(f"""
            SELECT * FROM cards
            WHERE legal_commander = 1
            AND edhrec_rank IS NOT NULL
            AND edhrec_rank < 10000
        """):
            card = dict(row)
            try:
                card_ci = set(json.loads(card["color_identity"])) if card["color_identity"] else set()
            except:
                card_ci = set()
            if card_ci <= deck.COLOR_IDENTITY and card["name"] not in deck_set:
                card["provides"] = [r[0] for r in conn.execute(
                    "SELECT tag FROM provides WHERE oracle_id = ?", (card["oracle_id"],))]
                card["wants"] = [r[0] for r in conn.execute(
                    "SELECT tag FROM wants WHERE oracle_id = ?", (card["oracle_id"],))]
                try:
                    card["keywords"] = json.loads(card["keywords"]) if card["keywords"] else []
                except:
                    card["keywords"] = []
                all_cards.append(card)
        conn.close()

        commander_card_data = next((c for c in cards if c["name"] == deck.COMMANDER), None)
        if commander_card_data:
            ml_scores = {}
            for card in all_cards:
                ml_scores[card["name"]] = ml_predict(model, commander_card_data, card)

            ml_ranked = sorted(ml_scores.items(), key=lambda x: -x[1])
            # Boost cards that also appear in graph candidates (confirms tag synergy)
            graph_names = {name for name, _ in ranked[:500]}
            final = []
            for name, ml_score in ml_ranked:
                boost = 1.5 if name in graph_names else 1.0
                final.append((name, ml_score * boost))
            final.sort(key=lambda x: -x[1])
            return [name for name, _ in final[:top_n]]

    return [name for name, info in ranked[:top_n]]


def validate_deck(deck_name, edhrec_slug):
    """Validate one deck's recommendations against EDHREC."""
    print(f"\n{'='*60}")
    print(f"  {deck_name} ({edhrec_slug})")
    print(f"{'='*60}")

    # Fetch EDHREC data
    data = fetch_commander_data(edhrec_slug)
    if not data:
        return None

    edhrec_cards = extract_high_synergy_cards(data)

    # Get high synergy cards (synergy > 0.10, not in our deck)
    from decks import load_deck
    deck = load_deck(deck_name)
    deck_set = set(deck.DECKLIST) | {deck.COMMANDER}

    edhrec_high = [c for c in edhrec_cards
                   if c["synergy"] > 0.10 and c["name"] not in deck_set]
    edhrec_high.sort(key=lambda x: -x["synergy"])
    edhrec_top_names = [c["name"] for c in edhrec_high[:30]]

    if not edhrec_top_names:
        print("  No EDHREC high synergy cards found")
        return None

    # Get our recommendations
    print(f"  EDHREC high synergy cards: {len(edhrec_top_names)}")
    print(f"  Computing our recommendations...")
    our_recs = get_our_recommendations(deck_name, top_n=100)
    print(f"  Our top recommendations: {len(our_recs)}")

    # Measure overlap
    our_top_30 = set(our_recs[:30])
    our_top_50 = set(our_recs[:50])
    our_top_100 = set(our_recs[:100])
    edhrec_set = set(edhrec_top_names)

    overlap_30 = edhrec_set & our_top_30
    overlap_50 = edhrec_set & our_top_50
    overlap_100 = edhrec_set & our_top_100

    pct_30 = len(overlap_30) * 100 // max(len(edhrec_set), 1)
    pct_50 = len(overlap_50) * 100 // max(len(edhrec_set), 1)
    pct_100 = len(overlap_100) * 100 // max(len(edhrec_set), 1)

    print(f"\n  EDHREC top 30 high-synergy vs our recommendations:")
    print(f"    In our top 30:  {len(overlap_30)}/{len(edhrec_set)} = {pct_30}%")
    print(f"    In our top 50:  {len(overlap_50)}/{len(edhrec_set)} = {pct_50}%")
    print(f"    In our top 100: {len(overlap_100)}/{len(edhrec_set)} = {pct_100}%")

    # Show what EDHREC recommends that we miss
    missed = [c for c in edhrec_high[:30] if c["name"] not in our_top_100]
    if missed:
        print(f"\n  EDHREC recommends but we miss (not in our top 100):")
        for c in missed[:8]:
            print(f"    {c['name']} (synergy: {c['synergy']:.0%}, section: {c['section']})")

    # Show what we recommend that EDHREC doesn't highlight
    our_only = [n for n in our_recs[:30] if n not in edhrec_set]
    if our_only:
        print(f"\n  We recommend but EDHREC doesn't highlight:")
        for name in our_only[:5]:
            print(f"    {name}")

    return {
        "deck": deck_name,
        "edhrec_count": len(edhrec_set),
        "overlap_30": len(overlap_30),
        "overlap_50": len(overlap_50),
        "overlap_100": len(overlap_100),
        "pct_30": pct_30,
        "pct_50": pct_50,
        "pct_100": pct_100,
        "missed": [c["name"] for c in missed],
    }


def print_summary(results):
    """Print summary across all decks."""
    valid = [r for r in results if r is not None]
    if not valid:
        print("No results to summarize.")
        return

    print(f"\n{'='*60}")
    print(f"  RECOMMENDATION VALIDATION SUMMARY")
    print(f"{'='*60}")

    total_edhrec = sum(r["edhrec_count"] for r in valid)
    total_30 = sum(r["overlap_30"] for r in valid)
    total_50 = sum(r["overlap_50"] for r in valid)
    total_100 = sum(r["overlap_100"] for r in valid)

    print(f"\n  Per deck (EDHREC top 30 high-synergy found in our top N):")
    for r in valid:
        bar_30 = "█" * (r["pct_30"] // 5) + "░" * (20 - r["pct_30"] // 5)
        print(f"    {r['deck']:<15} top30: {bar_30} {r['pct_30']:>3}%  top100: {r['pct_100']:>3}%")

    avg_30 = total_30 * 100 // max(total_edhrec, 1)
    avg_50 = total_50 * 100 // max(total_edhrec, 1)
    avg_100 = total_100 * 100 // max(total_edhrec, 1)

    print(f"\n  Overall (across {len(valid)} decks):")
    print(f"    EDHREC high-synergy in our top 30:  {total_30}/{total_edhrec} = {avg_30}%")
    print(f"    EDHREC high-synergy in our top 50:  {total_50}/{total_edhrec} = {avg_50}%")
    print(f"    EDHREC high-synergy in our top 100: {total_100}/{total_edhrec} = {avg_100}%")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate recommendations against EDHREC")
    parser.add_argument("--deck", default=None, help="Single deck to validate")
    parser.add_argument("--scrape-only", action="store_true", help="Just fetch EDHREC data")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(__file__))
    from decks import list_decks, load_deck

    if args.deck:
        deck_list = [args.deck]
    else:
        deck_list = list_decks()

    if args.scrape_only:
        for d in deck_list:
            deck = load_deck(d)
            slug = getattr(deck, "EDHREC_SLUG", None)
            if slug:
                print(f"Fetching {d} ({slug})...")
                fetch_commander_data(slug)
                time.sleep(1.5)
        print("Done.")
    else:
        results = []
        for d in deck_list:
            deck = load_deck(d)
            slug = getattr(deck, "EDHREC_SLUG", None)
            if slug:
                result = validate_deck(d, slug)
                results.append(result)
                time.sleep(1.5)  # Rate limit EDHREC

        print_summary(results)
