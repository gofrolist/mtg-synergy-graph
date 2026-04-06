#!/usr/bin/env python3
"""Compare strategy labels (EDHREC) vs mechanics vectors (Forge-derived).

For each strategy label, checks whether mechanics vectors can distinguish
cards with that strategy from cards without it. If mech vectors separate
well, we don't need external strategy labels — Forge data alone captures it.

Usage:
    python3 scripts/compare_strategy_vs_mech.py
    python3 scripts/compare_strategy_vs_mech.py --strategy tokens
    python3 scripts/compare_strategy_vs_mech.py --top 30
"""

import argparse
import os
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "packages", "mtg-synergy", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "packages", "mtg-synergy-train", "src"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")


def _load_strategies(conn, min_confidence=0.3):
    """Load strategy assignments: {strategy -> set of oracle_ids}."""
    strat_to_cards = {}
    for oid, strat in conn.execute(
        "SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= ?",
        (min_confidence,),
    ):
        strat_to_cards.setdefault(strat, set()).add(oid)
    return strat_to_cards


def _load_card_names(conn):
    """Load oracle_id -> name mapping."""
    return dict(conn.execute("SELECT oracle_id, name FROM cards"))


def _centroid(vecs):
    """Compute mean vector from list of vectors."""
    if not vecs:
        return None
    return np.mean(vecs, axis=0)


def _cosine(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def analyze_strategy(strategy, strat_oids, produces, consumes, all_oids):
    """Analyze how well mech vectors separate cards with a given strategy.

    Returns dict with separation metrics.
    """
    # Cards with this strategy that have mech vectors
    in_strat = [oid for oid in strat_oids if oid in produces or oid in consumes]
    out_strat = [oid for oid in all_oids
                 if oid not in strat_oids and (oid in produces or oid in consumes)]

    if len(in_strat) < 5:
        return None  # too few cards to analyze

    # Build produces/consumes vectors for in-strategy cards
    in_prod = [produces[oid] for oid in in_strat if oid in produces]
    in_cons = [consumes[oid] for oid in in_strat if oid in consumes]

    # Sample same number of out-strategy cards for fair comparison
    rng = np.random.default_rng(42)
    n_sample = min(len(out_strat), len(in_strat) * 3)
    out_sample = rng.choice(out_strat, size=n_sample, replace=False)
    out_prod = [produces[oid] for oid in out_sample if oid in produces]
    out_cons = [consumes[oid] for oid in out_sample if oid in consumes]

    # Compute centroids
    in_prod_c = _centroid(in_prod)
    in_cons_c = _centroid(in_cons)
    out_prod_c = _centroid(out_prod)
    out_cons_c = _centroid(out_cons)

    # Intra-strategy coherence: avg cosine to own centroid
    in_coherence_prod = 0.0
    if in_prod_c is not None and len(in_prod) > 1:
        in_coherence_prod = np.mean([_cosine(v, in_prod_c) for v in in_prod])

    in_coherence_cons = 0.0
    if in_cons_c is not None and len(in_cons) > 1:
        in_coherence_cons = np.mean([_cosine(v, in_cons_c) for v in in_cons])

    # Out-strategy avg cosine to in-strategy centroid (should be lower)
    out_sim_prod = 0.0
    if in_prod_c is not None and out_prod:
        out_sim_prod = np.mean([_cosine(v, in_prod_c) for v in out_prod])

    out_sim_cons = 0.0
    if in_cons_c is not None and out_cons:
        out_sim_cons = np.mean([_cosine(v, in_cons_c) for v in out_cons])

    # Separation = coherence - outsider similarity
    sep_prod = in_coherence_prod - out_sim_prod
    sep_cons = in_coherence_cons - out_sim_cons

    # Find top dimensions that characterize this strategy
    top_dims_prod = []
    if in_prod_c is not None and out_prod_c is not None:
        diff = in_prod_c - out_prod_c
        top_idx = np.argsort(-np.abs(diff))[:5]
        top_dims_prod = [(int(i), float(diff[i])) for i in top_idx]

    top_dims_cons = []
    if in_cons_c is not None and out_cons_c is not None:
        diff = in_cons_c - out_cons_c
        top_idx = np.argsort(-np.abs(diff))[:5]
        top_dims_cons = [(int(i), float(diff[i])) for i in top_idx]

    return {
        "strategy": strategy,
        "n_cards": len(in_strat),
        "n_with_produces": len(in_prod),
        "n_with_consumes": len(in_cons),
        "coherence_prod": round(in_coherence_prod, 3),
        "coherence_cons": round(in_coherence_cons, 3),
        "outsider_sim_prod": round(out_sim_prod, 3),
        "outsider_sim_cons": round(out_sim_cons, 3),
        "separation_prod": round(sep_prod, 3),
        "separation_cons": round(sep_cons, 3),
        "top_dims_prod": top_dims_prod,
        "top_dims_cons": top_dims_cons,
    }


def _coverage_stats(strat_to_cards, produces, consumes):
    """Cards with strategy labels vs cards with mech vectors."""
    strat_oids = set()
    for oids in strat_to_cards.values():
        strat_oids |= oids
    mech_oids = set(produces.keys()) | set(consumes.keys())
    both = strat_oids & mech_oids
    strat_only = strat_oids - mech_oids
    mech_only = mech_oids - strat_oids
    return {
        "strat_total": len(strat_oids),
        "mech_total": len(mech_oids),
        "both": len(both),
        "strat_only": len(strat_only),
        "mech_only": len(mech_only),
    }


def main():
    parser = argparse.ArgumentParser(description="Compare strategy labels vs mechanics vectors")
    parser.add_argument("--strategy", type=str, help="Analyze a single strategy")
    parser.add_argument("--top", type=int, default=20, help="Show top N strategies by card count")
    parser.add_argument("--show-cards", action="store_true",
                        help="Show example cards for each strategy")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    print("Loading data...")

    # Load strategies
    strat_to_cards = _load_strategies(conn)
    print(f"  {len(strat_to_cards)} strategies, "
          f"{sum(len(v) for v in strat_to_cards.values())} assignments")

    # Build mechanics vectors
    from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
    produces, consumes, dim, subtype_idx, category_dims = build_mechanics_vectors(conn)

    all_oids = list(set(produces.keys()) | set(consumes.keys()))
    card_names = _load_card_names(conn)

    # Coverage stats
    cov = _coverage_stats(strat_to_cards, produces, consumes)
    print(f"\n{'='*70}")
    print("COVERAGE")
    print(f"{'='*70}")
    print(f"  Cards with strategy labels:  {cov['strat_total']:,}")
    print(f"  Cards with mech vectors:     {cov['mech_total']:,}")
    print(f"  Cards with both:             {cov['both']:,}")
    print(f"  Strategy-only (no vector):   {cov['strat_only']:,}")
    print(f"  Mech-only (no strategy):     {cov['mech_only']:,}")

    # Analyze strategies
    if args.strategy:
        strategies = [args.strategy]
    else:
        # Sort by card count descending
        strategies = sorted(strat_to_cards.keys(),
                            key=lambda s: len(strat_to_cards[s]), reverse=True)
        strategies = strategies[:args.top]

    # Collect concept names for readable output
    from mtg_synergy.recommend.mechanics_vectors import GAME_CONCEPTS
    concept_names = {i: str(t) for i, t in enumerate(GAME_CONCEPTS)}
    for st, idx in subtype_idx.items():
        concept_names[idx] = f"subtype:{st}"

    print(f"\n{'='*70}")
    print("STRATEGY SEPARATION ANALYSIS")
    print(f"{'='*70}")
    print(f"  High separation = mech vectors CAN distinguish this strategy")
    print(f"  Low separation  = mech vectors CANNOT distinguish (EDHREC adds value)")
    print()

    results = []
    for strat in strategies:
        oids = strat_to_cards[strat]
        result = analyze_strategy(strat, oids, produces, consumes, all_oids)
        if result:
            results.append(result)

    # Sort by best separation (produces or consumes)
    results.sort(key=lambda r: max(r["separation_prod"], r["separation_cons"]),
                 reverse=True)

    # Print results
    print(f"{'Strategy':<25} {'Cards':>5} {'Coher':>6} {'OutSim':>6} "
          f"{'Sep':>6}  {'Top Distinguishing Dimensions'}")
    print("-" * 110)

    well_separated = []
    poorly_separated = []

    for r in results:
        # Use the better of produces/consumes
        if r["separation_prod"] >= r["separation_cons"]:
            coh = r["coherence_prod"]
            out = r["outsider_sim_prod"]
            sep = r["separation_prod"]
            top_dims = r["top_dims_prod"]
            side = "P"
        else:
            coh = r["coherence_cons"]
            out = r["outsider_sim_cons"]
            sep = r["separation_cons"]
            top_dims = r["top_dims_cons"]
            side = "C"

        dim_strs = []
        for idx, val in top_dims[:3]:
            name = concept_names.get(idx, f"dim{idx}")
            dim_strs.append(f"{name}({val:+.3f})")

        marker = "OK" if sep > 0.05 else "WEAK" if sep > 0.02 else "NONE"
        if sep > 0.05:
            well_separated.append(r["strategy"])
        elif sep < 0.02:
            poorly_separated.append(r["strategy"])

        print(f"{r['strategy']:<25} {r['n_cards']:>5} {coh:>6.3f} {out:>6.3f} "
              f"{sep:>+6.3f}{side} {marker:<4} {', '.join(dim_strs)}")

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"  Well separated (sep>0.05):  {len(well_separated)}/{len(results)} — "
          f"mech vectors can identify these")
    print(f"  Poorly separated (sep<0.02): {len(poorly_separated)}/{len(results)} — "
          f"EDHREC labels add value here")
    if well_separated:
        print(f"\n  Mech vectors capture well: {', '.join(well_separated[:10])}")
    if poorly_separated:
        print(f"  EDHREC still needed for:   {', '.join(poorly_separated[:10])}")

    conn.close()


if __name__ == "__main__":
    main()
