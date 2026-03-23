"""Deck analysis and synergy display."""
import json
import os
import sqlite3
from collections import defaultdict

from mtg_synergy.combos import find_combos_tiered, find_anti_synergy
from mtg_synergy.recommend.swaps import _classify_card_slot


def load_merged(path: str) -> list[dict]:
    from normalize_tags import normalize_cards

    with open(path) as f:
        cards = json.load(f)
    # Normalize provides/wants vocabulary + infer missing wants
    normalize_cards(cards)
    return cards


def show_card_synergies(graph: dict, card_name: str, top_n: int = 20):
    """Show top synergies for a specific card."""
    adj = graph["adjacency"]
    edges = adj.get(card_name, [])

    if not edges:
        print(f"No synergies found for '{card_name}'")
        # Try fuzzy match
        matches = [k for k in adj if card_name.lower() in k.lower()]
        if matches:
            print(f"Did you mean: {', '.join(matches[:5])}?")
        return

    ranked = sorted(edges, key=lambda e: e["score"], reverse=True)

    print(f"\nTop synergies for: {card_name}")
    print(f"{'─' * 60}")
    for edge in ranked[:top_n]:
        sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
        print(f"\n  {edge['target']}  (score: {edge['score']}, {sig})")
        for reason in edge["reasons"][:3]:
            print(f"    {reason}")


def show_deck_synergies(graph: dict, deck_cards: set[str], commander: str,
                        cards: list[dict] = None, top_n: int = 30):
    """Show the synergy network within the deck — which cards synergize with each other."""
    adj = graph["adjacency"]

    # Collect all edges between deck cards
    deck_edges = []
    seen = set()
    for card in deck_cards:
        for edge in adj.get(card, []):
            if edge["target"] in deck_cards:
                pair = tuple(sorted([edge["source"], edge["target"]]))
                if pair not in seen:
                    seen.add(pair)
                    deck_edges.append(edge)

    deck_edges.sort(key=lambda e: e["score"], reverse=True)

    print(f"\n{'═' * 70}")
    print(f"DECK SYNERGY MAP — {len(deck_edges)} edges between {len(deck_cards)} deck cards")
    print(f"{'═' * 70}")

    # Top edges within the deck
    print(f"\nTop {top_n} in-deck synergy pairs:")
    print(f"{'─' * 70}")
    for edge in deck_edges[:top_n]:
        sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
        print(f"  [{edge['score']:5.1f} {sig}] {edge['source']} ↔ {edge['target']}")
        for r in edge["reasons"][:2]:
            print(f"       {r}")

    # Per-card connectivity within the deck
    card_synergy = defaultdict(float)
    card_partners = defaultdict(int)
    for edge in deck_edges:
        card_synergy[edge["source"]] += edge["score"]
        card_synergy[edge["target"]] += edge["score"]
        card_partners[edge["source"]] += 1
        card_partners[edge["target"]] += 1

    ranked = sorted(card_synergy.items(), key=lambda x: x[1], reverse=True)
    print(f"\nDeck card synergy ranking (total score across in-deck edges):")
    print(f"{'─' * 70}")
    print(f"  {'Card':<35} {'Score':>7} {'Partners':>10}")
    for card, total in ranked:
        partners = card_partners[card]
        marker = " ★" if card == commander else ""
        print(f"  {card:<35} {total:7.1f} {partners:>10}{marker}")

    # Weakly connected cards (potential cuts)
    if ranked:
        median_score = ranked[len(ranked) // 2][1]
        weak = [(c, s) for c, s in ranked if s < median_score * 0.3]
        if weak:
            # Classify cards to distinguish cuttable from infrastructure
            card_list = cards or []
            slot_labels = {c: _classify_card_slot(c, card_list) for c, _ in weak}
            cuttable = [(c, s) for c, s in weak if slot_labels[c] == "spell"]
            protected = [(c, s) for c, s in weak if slot_labels[c] != "spell"]

            if cuttable:
                print(f"\nWeakly connected cards (potential cut candidates):")
                for card, total in cuttable:
                    print(f"  {card:<35} {total:7.1f} ({card_partners[card]} partners)")
            if protected:
                print(f"\nLow synergy but protected (infrastructure / lands):")
                for card, total in protected:
                    label = slot_labels[card]
                    print(f"  {card:<35} {total:7.1f} ({card_partners[card]} partners) [{label}]")


def show_deck_analysis(deck_cards, deck_oids, active_strategies, commander_name, db_path=None, graph=None, deck_set=None):
    """Enhanced deck analysis with strategy coverage."""
    import sqlite3
    if db_path is None:
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tags.db")
    conn = sqlite3.connect(db_path)

    # Count cards per strategy
    strat_counts = {}
    for oid in deck_oids:
        for row in conn.execute(
            "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3", (oid,)
        ):
            strat_counts[row[0]] = strat_counts.get(row[0], 0) + 1

    # Count non-land cards
    non_land = sum(1 for c in deck_cards if "Land" not in (c.get("type_line") or ""))

    # Count strategy-aligned cards
    aligned = 0
    if active_strategies:
        placeholders = ','.join('?' * len(active_strategies))
        for oid in deck_oids:
            rows = conn.execute(
                f"SELECT 1 FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3 AND strategy IN ({placeholders})",
                (oid, *active_strategies)
            ).fetchall()
            if rows:
                aligned += 1

    combos = find_combos_tiered(deck_oids, db_path)
    anti = find_anti_synergy(deck_oids, active_strategies, db_path, graph=graph, deck_cards_set=deck_set)
    conn.close()

    print(f"\n{'='*60}")
    print(f"DECK ANALYSIS: {commander_name}")
    print(f"{'='*60}")

    print(f"Detected strategies:")
    for strat in sorted(active_strategies):
        cnt = strat_counts.get(strat, 0)
        if cnt > 0:
            print(f"  {strat}: {cnt} cards")

    coverage = aligned * 100 // max(non_land, 1)
    print(f"Strategy coverage: {coverage}% of {non_land} non-land cards align with >=1 strategy")

    confirmed = sum(1 for c in combos if c["tier"] == "infinite-confirmed")
    likely = sum(1 for c in combos if c["tier"] == "combo-likely")
    synergy = sum(1 for c in combos if c["tier"] == "synergy")
    print(f"Confirmed combos: {confirmed} (Spellbook)")
    print(f"Likely combos: {likely} (trigger chain)")
    print(f"Synergy pairs: {synergy}")

    if anti:
        print(f"Anti-synergy cards: {len(anti)} (swap candidates)")
        for a in anti[:5]:
            print(f"  {a['name']} ({a['role'] or 'unknown'}) — {a['partners']} partners, score {a['synergy_score']}")
