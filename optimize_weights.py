#!/usr/bin/env python3
"""Optimize scoring weights against EDHREC ground truth.

Uses 502 commanders with ~263 synergy-scored cards each as training data.
Grid searches weight combinations to maximize alignment between our
causal scoring and EDHREC synergy rankings.

Usage:
    python3 optimize_weights.py              # Full optimization
    python3 optimize_weights.py --quick      # Fast mode (50 commanders)
    python3 optimize_weights.py --evaluate   # Evaluate current weights
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
    """Precompute all per-card scores for each commander (expensive, do once).

    Returns: {slug: [(card_name, edhrec_syn, causal, llm), ...]}
    """
    from mtg_synergy.causal import CausalContext

    precomputed = {}
    commanders = list(ground_truth.items())
    if max_commanders:
        commanders = commanders[:max_commanders]

    # Pre-load all LLM scores
    llm_all = defaultdict(dict)
    try:
        for row in conn.execute("SELECT commander_oid, card_oid, score FROM synergy_scores"):
            llm_all[row[0]][row[1]] = row[2]
    except Exception:
        pass

    # Pre-load card name → oid mapping
    card_oid_map = {}
    for row in conn.execute("SELECT name, oracle_id FROM cards"):
        card_oid_map[row[0]] = row[1]

    for i, (slug, edhrec_cards) in enumerate(commanders):
        info = commander_info.get(slug)
        if not info:
            continue

        cmdr_oid = info["oracle_id"]
        try:
            ctx = CausalContext(conn, cmdr_oid, set())
        except Exception:
            continue

        cmdr_llm = llm_all.get(cmdr_oid, {})
        scored = []
        for card_name, edhrec_syn in edhrec_cards.items():
            card_oid = card_oid_map.get(card_name)
            if not card_oid:
                continue
            causal = ctx.causal_score(card_oid)
            llm = cmdr_llm.get(card_oid, 0)
            scored.append((card_name, edhrec_syn, causal, llm))

        if len(scored) >= 10:
            precomputed[slug] = scored

        if (i + 1) % 10 == 0:
            print(f"  Precomputed {i+1}/{len(commanders)} commanders...")

    return precomputed


def evaluate_weights(weights, precomputed):
    """Evaluate weights using precomputed scores (fast — no DB queries).

    IMPORTANT: EDHREC synergy is the TARGET, not a feature.
    We only score using independent signals (LLM, CAUSAL) and measure
    how well they predict EDHREC's ranking.
    """
    total_score = 0
    n_evaluated = 0

    for slug, scored_cards in precomputed.items():
        ranked = []
        for card_name, edhrec_syn, causal, llm in scored_cards:
            # Score using ONLY independent signals — NOT edhrec_syn
            total = (llm * weights.get("LLM", 0)
                     + causal * weights.get("CAUSAL", 0))
            ranked.append((card_name, total, edhrec_syn))

        our_top30 = {name for name, _, _ in sorted(ranked, key=lambda x: -x[1])[:30]}
        edhrec_top30 = {name for name, _, syn in sorted(ranked, key=lambda x: -x[2])[:30]}
        overlap = len(our_top30 & edhrec_top30)
        total_score += overlap
        n_evaluated += 1

    return total_score / max(n_evaluated, 1), n_evaluated


def grid_search(precomputed):
    """Grid search over weight combinations (fast — uses precomputed scores).

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
        score, n = evaluate_weights(weights, precomputed)
        results.append((score, weights, n))

        if score > best_score:
            best_score = score
            best_weights = weights
            print(f"  [{i+1}/{total}] NEW BEST: {score:.1f}/30 with {weights}")

    return best_score, best_weights, results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Fast mode (50 commanders)")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate current weights only")
    parser.add_argument("--max-commanders", type=int, default=None)
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
        current = {k: v for k, v in SCORING_WEIGHTS.items()
                   if k in ("LLM", "CAUSAL", "EDHREC_SYNERGY")}
        print(f"\nEvaluating current weights: {current}")
        score, n = evaluate_weights(current, precomputed)
        print(f"Score: {score:.1f}/30 ({n} commanders evaluated)")
    else:
        t0 = time.time()
        best_score, best_weights, results = grid_search(precomputed)
        elapsed = time.time() - t0

        print(f"\n{'='*60}")
        print(f"BEST: {best_score:.1f}/30 with {best_weights}")
        print(f"Grid search time: {elapsed:.0f}s")

        results.sort(key=lambda x: -x[0])
        print(f"\nTop 10 weight combinations:")
        for score, weights, n in results[:10]:
            print(f"  {score:.1f}/30  {weights}")

    conn.close()


if __name__ == "__main__":
    main()
