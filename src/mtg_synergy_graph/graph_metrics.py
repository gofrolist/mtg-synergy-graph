"""Build-time causal graph metrics (SPEC §6.8).

The causal graph is the bipartite projection of ``card_ports``: an
undirected edge ``(A, B)`` exists iff any port on A matches any port on B
under §6.1.2. Given that graph, we compute four per-card / per-pair metrics
that the current LightGBM pipeline relies on for ~25% of its feature
importance:

* ``card_hub_score``       — weighted degree of a card
* ``graph_neighbor_overlap`` — Jaccard overlap of two cards' 1-hop sets
* ``cmdr_2hop_ratio``      — share of a card's neighbours reachable from
                             the commander in ≤2 hops
* ``graph_pagerank``       — personalised PageRank anchored at the commander

SPEC §6.8 says NetworkX is permitted at build time but never at inference.
In practice, the stdlib implementation below is ~80 lines of pure Python
(power iteration + set intersections) and has no third-party dependency.

The heavy lifting — building the adjacency from scratch — runs once per
import and can be serialised to a cache table later. For the fixture sizes
in this package, ``build_causal_graph`` takes milliseconds.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as _np_typed
    from scipy.sparse import csr_matrix as _csr_matrix_typed

from .graph_engine import (
    CATCH_ALL_TRIGGERS,
    COST_FEEDS_TRIGGER,
    EVENT_MATCH_MAP,
)

#: Phase 4.6: detect numpy at import time. The numpy-backed personalised
#: PageRank is ~100× faster than the pure-Python CSR loop and unlocks
#: query-time graph_metrics on 32 k-card imports. If numpy is missing the
#: engine still works — it just falls back to the slow CSR path.
try:
    import numpy as _np

    HAS_NUMPY = True
except ImportError:  # pragma: no cover - numpy is in dev deps
    _np = None  # type: ignore[assignment]
    HAS_NUMPY = False

#: Phase 4.7: detect scipy.sparse for the fast PageRank path. scipy's
#: ``csr_matrix.dot`` uses BLAS-level routines that are ~5-10× faster than
#: ``np.add.at``, dropping the per-call PR cost from ~6 s to under 1 s on
#: the 32 k-card causal graph.
try:
    from scipy.sparse import csr_matrix as _csr_matrix

    HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _csr_matrix = None  # type: ignore[assignment]
    HAS_SCIPY = False

PortRow = dict[str, Any]


# ---------------------------------------------------------------------------
# Causal graph construction (§6.8)
# ---------------------------------------------------------------------------


def build_causal_graph(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Build the full causal graph as an adjacency dict.

    Returns ``{card_name: {neighbour_name, ...}}``.

    Implementation: an inverted index keyed by ``event_class`` lets us
    iterate only over genuinely-matching trigger / effect / cost pairs
    instead of every (card, card) pair. At 32 k cards / 170 k ports this
    drops the wall time from "minutes" (the naïve O(N² × P) loop) to a
    couple of seconds.

    Catch-all trigger event classes (``SpellCast``/``Attacks``/...) are
    intentionally excluded from edge construction — see CATCH_ALL_TRIGGERS
    for the full set. Their semantic is "the candidate card itself is the
    event", which the engine handles via the candidate-identity matcher.
    """
    cur = conn.execute("SELECT card_name, port_type, event_class, zone_destination, counter_type FROM card_ports")
    triggers_by_event: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    effects_by_event: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    costs_by_event: dict[str, list[str]] = defaultdict(list)
    all_cards: set[str] = set()

    for row in cur.fetchall():
        name = row["card_name"]
        event = row["event_class"] or ""
        all_cards.add(name)
        ptype = row["port_type"]
        if ptype == "trigger":
            if event in CATCH_ALL_TRIGGERS or not event:
                continue
            triggers_by_event[event].append(
                (
                    name,
                    {
                        "event_class": event,
                        "zone_destination": row["zone_destination"],
                        "counter_type": row["counter_type"],
                    },
                )
            )
        elif ptype == "effect":
            if not event:
                continue
            effects_by_event[event].append(
                (
                    name,
                    {
                        "event_class": event,
                        "zone_destination": row["zone_destination"],
                        "counter_type": row["counter_type"],
                    },
                )
            )
        elif ptype == "cost":
            if not event:
                continue
            costs_by_event[event].append(name)

    adjacency: dict[str, set[str]] = {n: set() for n in all_cards}

    def _add(a: str, b: str) -> None:
        if a == b:
            return
        adjacency[a].add(b)
        adjacency[b].add(a)

    # ------ trigger ↔ effect via EVENT_MATCH_MAP ---------------------------
    for trig_event, effect_targets in EVENT_MATCH_MAP.items():
        if trig_event in CATCH_ALL_TRIGGERS:
            continue
        trig_ports = triggers_by_event.get(trig_event, [])
        if not trig_ports:
            continue
        for effect_event, predicate in effect_targets.items():
            if effect_event == "*":
                continue  # catch-all already excluded above
            eff_ports = effects_by_event.get(effect_event, [])
            if not eff_ports:
                continue
            for trig_card, trig_dict in trig_ports:
                for eff_card, eff_dict in eff_ports:
                    if predicate(trig_dict, eff_dict):
                        _add(trig_card, eff_card)

    # ------ direct fallback (event_class equality for unmapped triggers) ---
    common_events = set(triggers_by_event) & set(effects_by_event)
    for event in common_events:
        if event in EVENT_MATCH_MAP:
            continue  # handled above with the predicate
        trig_cards = {c for c, _ in triggers_by_event[event]}
        eff_cards = {c for c, _ in effects_by_event[event]}
        for tc in trig_cards:
            for ec in eff_cards:
                _add(tc, ec)

    # ------ trigger ↔ cost via COST_FEEDS_TRIGGER --------------------------
    for cost_event, fed_triggers in COST_FEEDS_TRIGGER.items():
        cost_cards = set(costs_by_event.get(cost_event, []))
        if not cost_cards:
            continue
        for trig_event in fed_triggers:
            trig_ports = triggers_by_event.get(trig_event, [])
            for trig_card, _ in trig_ports:
                for cost_card in cost_cards:
                    _add(trig_card, cost_card)

    return adjacency


# ---------------------------------------------------------------------------
# Per-card / per-pair metrics (§6.8)
# ---------------------------------------------------------------------------


def card_hub_scores(adjacency: dict[str, set[str]]) -> dict[str, float]:
    """Normalised degree centrality. Larger value → more connected card."""
    if not adjacency:
        return {}
    max_deg = max(len(v) for v in adjacency.values()) or 1
    return {n: len(neigh) / max_deg for n, neigh in adjacency.items()}


def neighbor_overlap(
    adjacency: dict[str, set[str]],
    a: str,
    b: str,
) -> float:
    """Jaccard overlap of ``a`` and ``b``'s 1-hop neighbourhoods."""
    na = adjacency.get(a, set())
    nb = adjacency.get(b, set())
    if not na and not nb:
        return 0.0
    inter = len(na & nb)
    union = len(na | nb)
    if union == 0:
        return 0.0
    return inter / union


def cmdr_two_hop_ratio(
    adjacency: dict[str, set[str]],
    commander: str,
    candidate: str,
) -> float:
    """Share of ``candidate``'s neighbours reachable from ``commander`` in ≤2 hops."""
    cand_neigh = adjacency.get(candidate, set())
    if not cand_neigh:
        return 0.0
    hop1 = adjacency.get(commander, set())
    hop2: set[str] = set()
    for n in hop1:
        hop2.update(adjacency.get(n, set()))
    reachable = (hop1 | hop2) & cand_neigh
    return len(reachable) / len(cand_neigh)


def _adjacency_to_csr(
    adjacency: dict[str, set[str]],
) -> tuple[list[str], dict[str, int], list[int], list[int]]:
    """Flatten an adjacency dict into CSR-like parallel arrays.

    Returns ``(nodes, index, offsets, neighbours)`` where:

    * ``nodes``      — sorted list of node names
    * ``index``      — ``{name: integer id}``
    * ``offsets``    — length ``len(nodes) + 1``; ``neighbours[offsets[i]:offsets[i+1]]``
                       are the neighbours of ``nodes[i]``
    * ``neighbours`` — flat list of neighbour ids

    The flat array form lets PageRank iterate without dict lookups, which
    is the main hot spot for power iteration on the 32 k-node graph.
    """
    nodes = sorted(adjacency.keys())
    index = {name: i for i, name in enumerate(nodes)}
    offsets = [0]
    neighbours: list[int] = []
    for name in nodes:
        for neigh in adjacency[name]:
            j = index.get(neigh)
            if j is not None:
                neighbours.append(j)
        offsets.append(len(neighbours))
    return nodes, index, offsets, neighbours


def personalised_pagerank(
    adjacency: dict[str, set[str]],
    source: str,
    *,
    damping: float = 0.85,
    iterations: int = 25,
    tol: float = 1e-5,
) -> dict[str, float]:
    """Personalised PageRank anchored at ``source`` (power iteration).

    Implementation: builds a CSR-like flat representation once, then
    iterates over plain Python lists. This is ~10× faster than the naïve
    dict-of-set version because every inner step is an integer index, not
    a string-keyed dict lookup. Still pure stdlib — no numpy dependency.

    For the 32 k-node Forge graph the per-call cost is ~5 s (vs minutes
    with the dict-only version). Phase 4.4's ``graph_cache`` table caches
    the result so query-time graph_metrics is a constant-time lookup.
    """
    if not adjacency or source not in adjacency:
        return {}

    nodes, index, offsets, neighbours = _adjacency_to_csr(adjacency)
    n = len(nodes)
    src_idx = index[source]

    rank = [0.0] * n
    rank[src_idx] = 1.0
    reset = rank.copy()

    out_degree = [offsets[i + 1] - offsets[i] for i in range(n)]
    one_minus_damping = 1.0 - damping

    for _ in range(iterations):
        new_rank = [one_minus_damping * reset[i] for i in range(n)]
        dangling_mass = 0.0
        for i in range(n):
            d = out_degree[i]
            if d == 0:
                dangling_mass += rank[i]
                continue
            contrib = damping * rank[i] / d
            start = offsets[i]
            end = offsets[i + 1]
            for k in range(start, end):
                new_rank[neighbours[k]] += contrib
        if dangling_mass:
            spread = damping * dangling_mass / n
            for i in range(n):
                new_rank[i] += spread
        delta = sum(abs(new_rank[i] - rank[i]) for i in range(n))
        rank = new_rank
        if delta < tol:
            break

    return {nodes[i]: rank[i] for i in range(n)}


def numpy_personalised_pagerank(
    adjacency: dict[str, set[str]],
    source: str,
    *,
    damping: float = 0.85,
    iterations: int = 25,
    tol: float = 1e-5,
) -> dict[str, float]:
    """Personalised PageRank backed by numpy (Phase 4.6).

    Uses ``np.repeat`` + ``np.add.at`` for a fully-vectorised sparse
    matrix-vector product. On the 32 k-node / 32 M-edge causal graph this
    runs in ~100-500 ms (vs ~35 s for the pure-Python CSR loop), unlocking
    live graph_metrics at query time.

    Falls back to :func:`personalised_pagerank` if numpy is missing — the
    function signature stays identical so callers don't need to branch.
    """
    if not HAS_NUMPY:
        return personalised_pagerank(
            adjacency,
            source,
            damping=damping,
            iterations=iterations,
            tol=tol,
        )
    if not adjacency or source not in adjacency:
        return {}

    nodes, index, offsets, neighbours = _adjacency_to_csr(adjacency)
    n = len(nodes)
    src_idx = index[source]

    np = _np  # local alias for type checkers
    offsets_arr = np.asarray(offsets, dtype=np.int64)
    neighbours_arr = np.asarray(neighbours, dtype=np.int64)
    out_degree = np.diff(offsets_arr).astype(np.float64)
    has_out = out_degree > 0
    safe_degree = np.where(has_out, out_degree, 1.0)

    # Per-edge source index, used by np.add.at to scatter contributions
    # from each source's contribution onto its neighbours in one shot.
    edge_source = np.repeat(np.arange(n, dtype=np.int64), out_degree.astype(np.int64))

    rank = np.zeros(n, dtype=np.float64)
    rank[src_idx] = 1.0
    reset = rank.copy()

    one_minus_damping = 1.0 - damping

    for _ in range(iterations):
        new_rank = one_minus_damping * reset
        # Per-source contribution = damping * rank / out_degree
        contrib = damping * np.where(has_out, rank / safe_degree, 0.0)
        # Scatter contribution onto each edge's target.
        edge_contrib = contrib[edge_source]
        np.add.at(new_rank, neighbours_arr, edge_contrib)
        # Dangling node mass redistributed uniformly per spec.
        dangling_mass = float(rank[~has_out].sum()) if (~has_out).any() else 0.0
        if dangling_mass:
            new_rank += damping * dangling_mass / n
        delta = float(np.abs(new_rank - rank).sum())
        rank = new_rank
        if delta < tol:
            break

    return {nodes[i]: float(rank[i]) for i in range(n)}


@dataclass
class ScipyPRContext:
    """Pre-built scipy.sparse state reused across personalised-PR calls.

    The CSR matrix construction (~4 s on a 32 k-node graph) is
    commander-independent — only the rank vector and source index change
    per call. Caching this drops the per-call cost from ~5 s to ~0.5 s.

    Memory note (Phase 4.7): a previous attempt also cached a symmetric
    adjacency CSR matrix here for vectorised Jaccard / 2-hop. At 32 k
    nodes / 32 M edges that doubled context memory to ~1.5 GB and OOM'd
    on the host. The vectorised path was reverted in favour of the
    Python top-K loop in scoring.py — this context now holds ONLY the
    transition matrix needed for personalised PageRank.
    """

    nodes: list[str]
    index: dict[str, int]
    transition_t: _csr_matrix_typed
    n: int
    dangling: _np_typed.ndarray
    reset_zero: _np_typed.ndarray


def build_scipy_pr_context(adjacency: dict[str, set[str]]) -> ScipyPRContext | None:
    """Build a reusable scipy CSR context from an adjacency dict.

    Memory footprint at 32 k nodes / 32 M edges: roughly 384 MB for the
    transition CSR (data + indices + indptr) plus 384 MB for its
    transpose. Held once per ``SynergyEngine`` instance.
    """
    if not HAS_SCIPY or not adjacency:
        return None
    np = _np
    nodes, index, offsets, neighbours = _adjacency_to_csr(adjacency)
    n = len(nodes)

    offsets_arr = np.asarray(offsets, dtype=np.int32)
    neighbours_arr = np.asarray(neighbours, dtype=np.int32)
    out_degree = np.diff(offsets_arr).astype(np.float64)
    has_out = out_degree > 0
    safe_degree = np.where(has_out, out_degree, 1.0)

    # Build the per-edge weight vector via per-edge source index. We
    # release ``edge_source`` immediately after consumption to keep the
    # transient peak below 512 MB.
    edge_source = np.repeat(np.arange(n, dtype=np.int32), out_degree.astype(np.int32))
    data = np.ones(len(neighbours_arr), dtype=np.float64) / safe_degree[edge_source]
    del edge_source

    transition_csr = _csr_matrix((data, neighbours_arr, offsets_arr), shape=(n, n))
    transition_t = transition_csr.transpose().tocsr()
    del transition_csr  # release the source-major copy ASAP

    return ScipyPRContext(
        nodes=nodes,
        index=index,
        transition_t=transition_t,
        n=n,
        dangling=~has_out,
        reset_zero=np.zeros(n, dtype=np.float64),
    )


def scipy_personalised_pagerank_cached(
    ctx: ScipyPRContext,
    source: str,
    *,
    damping: float = 0.85,
    iterations: int = 10,
    tol: float = 1e-4,
) -> dict[str, float]:
    """Run personalised PR against a pre-built :class:`ScipyPRContext`.

    Per-call cost on a 32 k-node graph: ~0.5 s. Per-call cost when the
    context is rebuilt every time: ~5 s. Use this when you're going to
    score many commanders against the same graph.
    """
    if source not in ctx.index:
        return {}
    np = _np
    src_idx = ctx.index[source]
    n = ctx.n

    rank = ctx.reset_zero.copy()
    rank[src_idx] = 1.0
    reset = rank.copy()
    one_minus_damping = 1.0 - damping
    has_dangling = bool(ctx.dangling.any())

    for _ in range(iterations):
        contribution = damping * ctx.transition_t.dot(rank)
        if has_dangling:
            dangling_mass = float(rank[ctx.dangling].sum())
            if dangling_mass:
                contribution += damping * dangling_mass / n
        new_rank = one_minus_damping * reset + contribution
        delta = float(np.abs(new_rank - rank).sum())
        rank = new_rank
        if delta < tol:
            break

    return {ctx.nodes[i]: float(rank[i]) for i in range(n)}


def scipy_personalised_pagerank(
    adjacency: dict[str, set[str]],
    source: str,
    *,
    damping: float = 0.85,
    iterations: int = 10,
    tol: float = 1e-4,
) -> dict[str, float]:
    """One-shot personalised PageRank backed by scipy.sparse.

    For repeated calls against the same adjacency, prefer
    :func:`build_scipy_pr_context` + :func:`scipy_personalised_pagerank_cached`
    — that path is what the engine uses, and it pays the ~4 s CSR build
    cost only once.

    This wrapper exists for callers that have a single one-off lookup
    and don't want to manage a context. It builds and discards the
    context inline. Falls through to numpy → pure-Python when scipy is
    missing.
    """
    if not HAS_SCIPY:
        return numpy_personalised_pagerank(
            adjacency,
            source,
            damping=damping,
            iterations=iterations,
            tol=tol,
        )
    ctx = build_scipy_pr_context(adjacency)
    if ctx is None:
        return numpy_personalised_pagerank(
            adjacency,
            source,
            damping=damping,
            iterations=iterations,
            tol=tol,
        )
    return scipy_personalised_pagerank_cached(
        ctx,
        source,
        damping=damping,
        iterations=iterations,
        tol=tol,
    )


def fast_personalised_pagerank(
    adjacency: dict[str, set[str]],
    source: str,
    *,
    damping: float = 0.85,
    iterations: int = 10,
    tol: float = 1e-4,
) -> dict[str, float]:
    """Best-available personalised PageRank.

    Dispatches scipy → numpy → pure-Python in that order. Same signature
    and return shape as the underlying functions, so callers don't need
    to know which backend is in use.
    """
    if HAS_SCIPY:
        return scipy_personalised_pagerank(
            adjacency,
            source,
            damping=damping,
            iterations=iterations,
            tol=tol,
        )
    if HAS_NUMPY:
        return numpy_personalised_pagerank(
            adjacency,
            source,
            damping=damping,
            iterations=iterations,
            tol=tol,
        )
    return personalised_pagerank(
        adjacency,
        source,
        damping=damping,
        iterations=iterations,
        tol=tol,
    )


def global_pagerank(
    adjacency: dict[str, set[str]],
    *,
    damping: float = 0.85,
    iterations: int = 25,
    tol: float = 1e-5,
) -> dict[str, float]:
    """Non-personalised (uniform reset) PageRank — node centrality.

    Cheaper to cache than personalised PageRank because it doesn't depend
    on a source node, so a single ``graph_cache`` row per card covers
    every commander.
    """
    if not adjacency:
        return {}

    nodes, _index, offsets, neighbours = _adjacency_to_csr(adjacency)
    n = len(nodes)
    rank = [1.0 / n] * n
    out_degree = [offsets[i + 1] - offsets[i] for i in range(n)]
    one_minus_damping = 1.0 - damping
    base = one_minus_damping / n

    for _ in range(iterations):
        new_rank = [base] * n
        dangling_mass = 0.0
        for i in range(n):
            d = out_degree[i]
            if d == 0:
                dangling_mass += rank[i]
                continue
            contrib = damping * rank[i] / d
            start = offsets[i]
            end = offsets[i + 1]
            for k in range(start, end):
                new_rank[neighbours[k]] += contrib
        if dangling_mass:
            spread = damping * dangling_mass / n
            for i in range(n):
                new_rank[i] += spread
        delta = sum(abs(new_rank[i] - rank[i]) for i in range(n))
        rank = new_rank
        if delta < tol:
            break

    return {nodes[i]: rank[i] for i in range(n)}


# ---------------------------------------------------------------------------
# Bundled metric computation for a commander
# ---------------------------------------------------------------------------


def compute_commander_metrics(
    adjacency: dict[str, set[str]],
    commander_set: Iterable[str],
) -> dict[str, dict[str, float]]:
    """Compute per-candidate graph metrics for a commander (or partner pair).

    Returns ``{card_name: {metric: value}}`` for every candidate in the
    graph, excluding the commander set itself. The candidates' hub scores
    are included so downstream scoring can read a single dict.
    """
    cmdr_list = list(commander_set)
    cmdr_set = set(cmdr_list)

    hubs = card_hub_scores(adjacency)

    # Merge adjacency from the partner pair into a synthetic "commander"
    # node so Jaccard / 2-hop / PageRank all see the union.
    merged_cmdr = set()
    for name in cmdr_list:
        merged_cmdr.update(adjacency.get(name, set()))
    merged_cmdr -= cmdr_set
    merged_name = "__commander__"
    merged_adj = dict(adjacency)
    merged_adj[merged_name] = merged_cmdr
    for neigh in merged_cmdr:
        # Add the synthetic commander to each neighbour's adjacency list so
        # PageRank/neighbor overlap can traverse back. We clone the set to
        # avoid mutating the caller's graph.
        merged_adj[neigh] = adjacency.get(neigh, set()) | {merged_name}

    pr = personalised_pagerank(merged_adj, merged_name)
    max_pr = max(pr.values()) if pr else 0.0

    out: dict[str, dict[str, float]] = {}
    for cand in adjacency:
        if cand in cmdr_set:
            continue
        out[cand] = {
            "card_hub_score": hubs.get(cand, 0.0),
            "graph_neighbor_overlap": neighbor_overlap(merged_adj, merged_name, cand),
            "cmdr_2hop_ratio": cmdr_two_hop_ratio(merged_adj, merged_name, cand),
            "graph_pagerank": (pr.get(cand, 0.0) / max_pr) if max_pr else 0.0,
        }
    return out
