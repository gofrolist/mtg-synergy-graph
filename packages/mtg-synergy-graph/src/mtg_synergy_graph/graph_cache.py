"""Precomputed causal-graph cache (SPEC §6.8 / Phase 4.4).

The query-time engine cannot afford to rebuild the causal graph + run
PageRank for every page request — at 32 k cards even the optimised
CSR-based PageRank takes a few seconds, and the engine has a sub-second
warm budget.

Phase 4.4 stores the heavy work in two tables built once after import:

* ``graph_cache``       — per-card metrics (``hub_score``, ``pagerank``,
                          ``degree``, ``built_at``)
* ``causal_neighbours`` — sparse adjacency, optionally capped per card so
                          the on-disk size stays bounded

The engine joins ``graph_cache`` for per-card features and uses
``causal_neighbours`` for cheap per-pair metrics (Jaccard overlap, 2-hop
ratio) without ever recomputing the graph.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from .graph_metrics import build_causal_graph, card_hub_scores, global_pagerank

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphCacheStats:
    cards:        int
    edges:        int
    capped_edges: int
    elapsed_s:    float
    built_at:     str


def cache_is_populated(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM graph_cache").fetchone()
    return bool(row and row[0])


def clear_graph_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM causal_neighbours")
    conn.execute("DELETE FROM graph_cache")
    conn.commit()


def build_graph_cache(
    conn: sqlite3.Connection,
    *,
    neighbour_cap: int | None = 200,
    pagerank_iterations: int = 25,
) -> GraphCacheStats:
    """Build the causal graph and persist metrics + neighbours.

    ``neighbour_cap`` limits how many neighbours we keep per card in
    ``causal_neighbours``. Set to ``None`` to keep all edges (warning:
    32 M rows on a full Forge import). The default of 200 keeps the cache
    under ~250 MB while preserving the most-connected neighbours per card.

    Returns a :class:`GraphCacheStats` describing the build.
    """
    import time

    t0 = time.perf_counter()
    log.info("building causal graph")
    adjacency = build_causal_graph(conn)

    log.info("computing global pagerank over %d nodes", len(adjacency))
    pr = global_pagerank(adjacency, iterations=pagerank_iterations)
    hubs = card_hub_scores(adjacency)

    built_at = _dt.datetime.now(_dt.UTC).isoformat()

    clear_graph_cache(conn)

    cache_rows = [
        (name, hubs.get(name, 0.0), pr.get(name, 0.0), len(neighbours), built_at)
        for name, neighbours in adjacency.items()
    ]
    conn.executemany(
        "INSERT INTO graph_cache (card_name, hub_score, pagerank, degree, built_at) "
        "VALUES (?, ?, ?, ?, ?)",
        cache_rows,
    )

    edge_count = 0
    capped_count = 0
    neighbour_rows: list[tuple[str, str]] = []
    for name, neighbours in adjacency.items():
        # Sort neighbours by their global pagerank so the cap keeps the
        # most-central ones — that's what the per-pair metrics actually
        # care about.
        sorted_neighbours = sorted(
            neighbours, key=lambda n: pr.get(n, 0.0), reverse=True
        )
        edge_count += len(sorted_neighbours)
        kept = (
            sorted_neighbours[:neighbour_cap]
            if neighbour_cap is not None
            else sorted_neighbours
        )
        capped_count += len(kept)
        for neigh in kept:
            neighbour_rows.append((name, neigh))

    # Chunked insert keeps SQLite memory bounded on big imports.
    chunk = 50_000
    for i in range(0, len(neighbour_rows), chunk):
        conn.executemany(
            "INSERT OR IGNORE INTO causal_neighbours (card_name, neighbour_name) "
            "VALUES (?, ?)",
            neighbour_rows[i : i + chunk],
        )

    conn.commit()
    elapsed = time.perf_counter() - t0
    return GraphCacheStats(
        cards=len(adjacency),
        edges=edge_count // 2,                # undirected edges
        capped_edges=capped_count // 2,
        elapsed_s=round(elapsed, 2),
        built_at=built_at,
    )


# ---------------------------------------------------------------------------
# Lookup helpers used by the engine at query time
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedCardMetrics:
    hub_score: float
    pagerank:  float
    degree:    int


def load_card_metrics(conn: sqlite3.Connection) -> dict[str, CachedCardMetrics]:
    """Bulk-load every cached card row.

    Returns ``{}`` if the cache is empty so callers can fall back to live
    computation.
    """
    rows = conn.execute(
        "SELECT card_name, hub_score, pagerank, degree FROM graph_cache"
    ).fetchall()
    return {
        r["card_name"]: CachedCardMetrics(
            hub_score=float(r["hub_score"]),
            pagerank=float(r["pagerank"]),
            degree=int(r["degree"]),
        )
        for r in rows
    }


def neighbours_of(
    conn: sqlite3.Connection,
    card_names: Iterable[str],
) -> dict[str, set[str]]:
    """Return ``{card_name: {neighbour, ...}}`` for one or more cards."""
    names = list(card_names)
    if not names:
        return {}
    placeholders = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT card_name, neighbour_name FROM causal_neighbours "
        f"WHERE card_name IN ({placeholders})",
        tuple(names),
    ).fetchall()
    out: dict[str, set[str]] = {n: set() for n in names}
    for r in rows:
        out[r["card_name"]].add(r["neighbour_name"])
    return out


def commander_neighbours(
    conn: sqlite3.Connection,
    commander_set: Iterable[str],
) -> set[str]:
    """Union of neighbour sets for the commander pair."""
    return {
        neighbour
        for cmdr_set in neighbours_of(conn, commander_set).values()
        for neighbour in cmdr_set
    }
