#!/usr/bin/env python3
"""Optimize scoring weights against EDHREC ground truth.

Primary metric: Recall@100 against EDHREC average decklists.
Fallback: synergy-based overlap (when average decks not fetched).

Usage:
    python3 optimize_weights.py              # Grid search (optimizes Recall@100)
    python3 optimize_weights.py --quick      # Fast mode (50 commanders)
    python3 optimize_weights.py --evaluate   # Evaluate current weights
    python3 optimize_weights.py --evaluate --no-llm  # Coverage mode (no LLM)
    python3 optimize_weights.py --evaluate --novelty  # Show novel picks
    python3 optimize_weights.py --evaluate --deck krenko-mob-boss  # Deep dive
"""
import argparse
import itertools
import json
import sys
import time
from collections import defaultdict

from mtg_synergy.db import get_connection
from mtg_synergy.config import SCORING_WEIGHTS


def load_ground_truth(conn, min_cards=50):
    """Load EDHREC synergy data as ground truth.

    Returns: {commander_slug: {card_name: synergy_score}}
    Only includes commanders with at least min_cards synergy entries.
    """
    ground_truth = defaultdict(dict)
    for row in conn.execute(
        "SELECT commander_slug, card_name, synergy FROM edhrec_card_synergy"
    ).fetchall():
        ground_truth[row[0]][row[1]] = row[2]

    # Filter commanders with enough data
    return {slug: cards for slug, cards in ground_truth.items()
            if len(cards) >= min_cards}


def load_commander_info(conn):
    """Load commander oracle_ids and deck info.

    Returns: {slug: {name, oracle_id, color_identity}}
    """
    info = {}
    # Build slug → name mapping from edhrec data
    slugs = conn.execute(
        "SELECT DISTINCT commander_slug FROM edhrec_card_synergy"
    ).fetchall()

    for (slug,) in slugs:
        # Try to find commander by slug pattern
        # EDHREC slugs are like "krenko-mob-boss" → "Krenko, Mob Boss"
        # We'll match via the cards table
        name_parts = slug.replace("-", " ").title()
        # Try exact match first
        row = conn.execute(
            "SELECT oracle_id, name, color_identity FROM cards WHERE LOWER(REPLACE(name, ',', '')) LIKE ?",
            (f"%{slug.replace('-', '%')}%",)
        ).fetchone()
        if row:
            info[slug] = {"oracle_id": row[0], "name": row[1],
                          "color_identity": row[2]}
    return info


def precompute_scores(conn, ground_truth, commander_info, max_commanders=None):
    """Precompute all per-card feature vectors for each commander.

    Returns: {slug: [(card_name, {feature_dict}), ...]}
    Feature dict has: edhrec_syn, causal, llm, cmdr_overlap, mechanics,
    strat_keywords, tribal, rank, forge_overlap, tower, deck_overlap, strat_overlap
    """
    from mtg_synergy.recommend.scoring import DeckContext, compute_dynamic_score

    precomputed = {}
    commanders = list(ground_truth.items())
    if max_commanders:
        commanders = commanders[:max_commanders]

    # Pre-load card data for fast lookup
    card_data_map = {}
    card_oracle_map = {}
    for row in conn.execute(
        "SELECT name, oracle_id, type_line, edhrec_rank, oracle_text FROM cards"
    ):
        card_data_map[row[0]] = {
            "name": row[0], "oracle_id": row[1],
            "type_line": row[1] and row[2] or "", "edhrec_rank": row[3],
        }
        card_oracle_map[row[0]] = row[4] or ""

    for i, (slug, edhrec_cards) in enumerate(commanders):
        info = commander_info.get(slug)
        if not info:
            continue

        cmdr_name = info["name"]
        # Build a minimal cards list for DeckContext
        cards_list = []
        for card_name in edhrec_cards:
            cd = card_data_map.get(card_name)
            if cd:
                cards_list.append(cd)
        # Add commander
        cmdr_cd = card_data_map.get(cmdr_name)
        if cmdr_cd and cmdr_cd not in cards_list:
            cards_list.append(cmdr_cd)

        try:
            ctx = DeckContext(conn, cmdr_name, set(), cards_list,
                              edhrec_slug=slug)
        except Exception:
            continue

        scored = []
        for card_name, edhrec_syn in edhrec_cards.items():
            cd = card_data_map.get(card_name)
            if not cd:
                continue
            try:
                features = compute_dynamic_score(
                    card_name, cd, ctx, conn,
                    oracle_text=card_oracle_map.get(card_name, ""))
                features["edhrec_syn"] = edhrec_syn
                scored.append((card_name, features))
            except Exception:
                continue

        if len(scored) >= 10:
            precomputed[slug] = scored

        if (i + 1) % 10 == 0:
            print(f"  Precomputed {i+1}/{len(commanders)} commanders...")

    return precomputed


def _rank_cards(scored_cards, weights):
    """Rank cards by weighted score. Returns [(name, score, edhrec_syn), ...]."""
    ranked = []
    for card_name, features in scored_cards:
        total = (features.get("llm", 0) * weights.get("LLM", 0)
                 + features.get("causal", 0) * weights.get("CAUSAL", 0)
                 + features.get("cmdr_overlap", 0) * weights.get("CMDR_TAG_OVERLAP", 0)
                 + features.get("deck_overlap", 0) * weights.get("DECK_TAG_OVERLAP", 0)
                 + features.get("mechanics", 0) * weights.get("MECHANICS", 0)
                 + features.get("strat_overlap", 0) * weights.get("STRATEGY", 0)
                 + features.get("strat_keywords", 0) * weights.get("STRATEGY_KEYWORD", 0)
                 + features.get("rank_score", 0) * weights.get("RANK", 0)
                 + features.get("forge_overlap", 0) * weights.get("FORGE_DECK_OVERLAP", 0)
                 + features.get("tower", 0) * weights.get("TOWER", 0))
        ranked.append((card_name, total, features.get("edhrec_syn", 0)))
    return sorted(ranked, key=lambda x: -x[1])


def evaluate_weights(weights, precomputed, conn=None):
    """Evaluate weights using Recall@K as primary metric.

    If conn is provided and edhrec_average_decks table exists, uses Recall@100
    against average decklists. Otherwise falls back to synergy-based overlap.

    IMPORTANT: EDHREC synergy is the TARGET, not a feature.
    """
    # Try Recall@100 against average decks (primary metric)
    if conn:
        has_avg = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_average_decks'"
        ).fetchone()[0]
        if has_avg:
            recalls = []
            for slug, scored_cards in precomputed.items():
                avg_deck = set(r[0] for r in conn.execute(
                    "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?",
                    (slug,)))
                if len(avg_deck) < 20:
                    continue
                ranked = _rank_cards(scored_cards, weights)
                our_ranked = [name for name, _, _ in ranked]
                recalls.append(compute_recall_at_k(our_ranked, avg_deck, k=100))
            if recalls:
                return sum(recalls) / len(recalls), len(recalls)

    # Fallback: synergy-based overlap (legacy, used when no average decks available)
    total_score = 0
    n_evaluated = 0
    for slug, scored_cards in precomputed.items():
        ranked = _rank_cards(scored_cards, weights)
        our_top30 = {name for name, _, _ in ranked[:30]}
        edhrec_top30 = {card[0] for card
                        in sorted(scored_cards, key=lambda x: -x[1].get("edhrec_syn", 0))[:30]}
        overlap = len(our_top30 & edhrec_top30)
        total_score += overlap / 30.0
        n_evaluated += 1

    return total_score / max(n_evaluated, 1), n_evaluated


def compute_recall_at_k(our_ranked: list[str], edhrec_deck: set[str], k: int = 100) -> float:
    """Compute Recall@K: fraction of EDHREC deck cards found in our top K."""
    if not edhrec_deck:
        return 0.0
    our_top_k = set(our_ranked[:k])
    found = len(our_top_k & edhrec_deck)
    return found / len(edhrec_deck)


def evaluate_recall(conn, precomputed, weights, k_values=(30, 50, 100)):
    """Evaluate Recall@K against EDHREC average decks."""
    has_avg = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_average_decks'"
    ).fetchone()[0]
    if not has_avg:
        print("  No edhrec_average_decks table. Run: python3 fetch_edhrec_decks.py")
        return

    recalls = {k: [] for k in k_values}
    for slug, scored_cards in precomputed.items():
        avg_deck = set(r[0] for r in conn.execute(
            "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?",
            (slug,)))
        if len(avg_deck) < 20:
            continue

        ranked = _rank_cards(scored_cards, weights)
        our_ranked = [name for name, _, _ in ranked]

        for k in k_values:
            recalls[k].append(compute_recall_at_k(our_ranked, avg_deck, k))

    for k in k_values:
        if recalls[k]:
            avg = sum(recalls[k]) / len(recalls[k])
            print(f"  Recall@{k}: {avg:.1%} ({len(recalls[k])} commanders)")


def novelty_report(conn, precomputed, weights, slug_filter=None, top_k=100):
    """Show cards we recommend that EDHREC average deck doesn't include."""
    has_avg = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_average_decks'"
    ).fetchone()[0]
    if not has_avg:
        print("No edhrec_average_decks table. Run: python3 fetch_edhrec_decks.py")
        return

    slugs = [slug_filter] if slug_filter else list(precomputed.keys())[:10]
    for slug in slugs:
        if slug not in precomputed:
            continue
        avg_deck = set(r[0] for r in conn.execute(
            "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?", (slug,)))
        if len(avg_deck) < 20:
            continue

        scored = precomputed[slug]
        ranked = _rank_cards(scored, weights)
        our_top = [name for name, _, _ in ranked[:top_k]]

        novel = [c for c in our_top if c not in avg_deck]
        in_edhrec = [c for c in our_top if c in avg_deck]
        recall = len(in_edhrec) / len(avg_deck) if avg_deck else 0

        print(f"\n{'='*60}")
        print(f"{slug}: Recall@{top_k}={recall:.0%} | {len(novel)} novel picks")
        print(f"  Novel (not in EDHREC avg deck):")
        for c in novel[:15]:
            match = next((s for s in scored if s[0] == c), None)
            if match:
                f = match[1]
                print(f"    {c}  (causal={f.get('causal',0):.1f}, llm={f.get('llm',0)}, overlap={f.get('cmdr_overlap',0)})")


def grid_search(precomputed, conn=None):
    """Grid search over weight combinations optimizing Recall@100.

    Only optimizes LLM and CAUSAL weights since EDHREC is the target metric.
    """
    llm_range = [0, 1, 2, 5, 10, 20, 50, 100]
    causal_range = [0, 0.5, 1, 2, 5, 10, 20, 50]

    best_score = 0
    best_weights = {}
    results = []

    total = len(llm_range) * len(causal_range)
    print(f"Grid search: {total} combinations (LLM × CAUSAL)")

    for i, (llm_w, causal_w) in enumerate(
        itertools.product(llm_range, causal_range)
    ):
        weights = {"LLM": llm_w, "CAUSAL": causal_w}
        score, n = evaluate_weights(weights, precomputed, conn=conn)
        results.append((score, weights, n))

        if score > best_score:
            best_score = score
            best_weights = weights
            print(f"  [{i+1}/{total}] NEW BEST: Recall@100={score:.1%} with {weights}")

    return best_score, best_weights, results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Fast mode (50 commanders)")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate current weights only")
    parser.add_argument("--max-commanders", type=int, default=None)
    parser.add_argument("--no-llm", action="store_true", help="Evaluate without LLM scores")
    parser.add_argument("--novelty", action="store_true", help="Show novel picks not in EDHREC")
    parser.add_argument("--deck", type=str, help="Single commander deep dive")
    args = parser.parse_args()

    conn = get_connection()

    print("Loading EDHREC ground truth...")
    ground_truth = load_ground_truth(conn)
    print(f"  {len(ground_truth)} commanders with enough data")

    print("Loading commander info...")
    commander_info = load_commander_info(conn)
    print(f"  {len(commander_info)} commanders matched to cards")

    max_c = args.max_commanders or (50 if args.quick else len(ground_truth))

    print(f"\nPrecomputing scores for {max_c} commanders...")
    t0 = time.time()
    precomputed = precompute_scores(conn, ground_truth, commander_info,
                                     max_commanders=max_c)
    t_precompute = time.time() - t0
    print(f"Precomputed {len(precomputed)} commanders in {t_precompute:.0f}s")

    if args.evaluate:
        weights = {"LLM": 0 if args.no_llm else SCORING_WEIGHTS.get("LLM", 10),
                   "CAUSAL": SCORING_WEIGHTS.get("CAUSAL", 2)}
        mode = "no-LLM" if args.no_llm else "all signals"
        print(f"\nEvaluating ({mode}): {weights}")
        score, n = evaluate_weights(weights, precomputed, conn=conn)
        print(f"Recall@100: {score:.1%} ({n} commanders)")
        evaluate_recall(conn, precomputed, weights)

        if args.novelty:
            novelty_report(conn, precomputed, weights, slug_filter=args.deck)
        elif args.deck:
            novelty_report(conn, precomputed, weights, slug_filter=args.deck)
    else:
        t0 = time.time()
        best_score, best_weights, results = grid_search(precomputed, conn=conn)
        elapsed = time.time() - t0

        print(f"\n{'='*60}")
        print(f"BEST: Recall@100={best_score:.1%} with {best_weights}")
        print(f"Grid search time: {elapsed:.0f}s")

        results.sort(key=lambda x: -x[0])
        print(f"\nTop 10 weight combinations:")
        for score, weights, n in results[:10]:
            print(f"  Recall@100={score:.1%}  {weights}")

    conn.close()


if __name__ == "__main__":
    main()
