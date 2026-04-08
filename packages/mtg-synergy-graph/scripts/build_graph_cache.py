"""Precompute the causal graph cache (SPEC §6.8 / Phase 4.4).

Run this once after a fresh ``import_cardsfolder.py`` to populate
``graph_cache`` and ``causal_neighbours``. The :class:`SynergyEngine`
auto-detects the cache when ``graph_metrics=True`` and falls back to live
computation when it's missing.

Usage:
    uv run python packages/mtg-synergy-graph/scripts/build_graph_cache.py \\
        --db /tmp/synergy_full.db \\
        --neighbour-cap 200
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_cache import build_graph_cache


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--neighbour-cap", type=int, default=200,
                        help="Top-K neighbours to keep per card (None = all)")
    parser.add_argument("--unbounded", action="store_true",
                        help="Keep every edge (warning: 32M rows on full Forge import)")
    parser.add_argument("--iterations", type=int, default=25,
                        help="PageRank power-iteration count")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"error: {args.db} does not exist", file=sys.stderr)
        return 2

    cap = None if args.unbounded else args.neighbour_cap

    conn = open_db(args.db)
    print(f"building graph cache for {args.db}")
    print(f"  neighbour cap : {cap if cap is not None else 'unbounded'}")
    print(f"  pagerank iter : {args.iterations}")
    stats = build_graph_cache(conn, neighbour_cap=cap, pagerank_iterations=args.iterations)
    conn.close()

    print()
    print(f"  cards         : {stats.cards:,}")
    print(f"  edges (full)  : {stats.edges:,}")
    print(f"  edges (kept)  : {stats.capped_edges:,}")
    print(f"  elapsed       : {stats.elapsed_s:.1f}s")
    print(f"  built_at      : {stats.built_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
