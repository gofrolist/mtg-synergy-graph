"""Commander target vector (plan 003 Unit 4).

Builds the L2-normalized mean of a commander's own port-feature vector
and — when an EDHREC connection is provided — the vectors of its top
high-synergy cards. Partner pairs compose by averaging over both
commanders' own vectors plus both commanders' hi-syn lists.

The commander-target composition is the structural bridge between a
commander and the long tail of cards whose ports the rule system
doesn't yet match. The target is consumed at inference by the scoring
term wired in plan 003 Unit 7 — a dot product against the (already
L2-normalized) candidate vector yields cosine similarity.

Caching
-------

The hi-syn DB fetch is memoized at a per-(connection, commander,
limit) granularity via ``functools.cache`` on the helper
``_fetch_hi_syn_names_cached``. The SQLite ``Connection`` is hashable
by object identity in CPython, so each test's fresh in-memory
connection begins with a cold cache and the production bench run
reuses a single long-lived connection.

The top-level ``build_commander_target_vector`` is *not* cached —
composing a mean of unit vectors is microseconds, and attempting to
cache over ``Mapping[str, np.ndarray]`` would require a heavy
identity-or-hashable contract on callers that doesn't pay for itself.

Tests can call ``clear_cache()`` to reset the memoization between
scenarios without rebuilding connections.

Plan: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md Unit 4.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Mapping

import numpy as np

from mtg_synergy_graph.edhrec_helpers import fetch_high_synergy_top_n_ordered

logger = logging.getLogger(__name__)

#: Smallest norm treated as nonzero when L2-normalizing. Below this
#: threshold we return ``None`` rather than a near-random unit vector
#: obtained by dividing by float-dust.
_NORM_EPSILON: float = 1e-12


@functools.cache
def _fetch_hi_syn_names_cached(
    edhrec_conn: sqlite3.Connection,
    commander_name: str,
    hi_syn_limit: int,
) -> tuple[str, ...]:
    """Cached wrapper over ``fetch_high_synergy_top_n_ordered``.

    Errors from the underlying query (missing table, schema drift,
    corrupt row, disk-corruption) degrade to an empty tuple plus a
    ``DEBUG`` log, so callers always get a tuple they can zip with
    ``vectors``. The warning about *why* hi-syn is empty is the
    caller's responsibility — this helper only owns the fetch.
    """
    try:
        return fetch_high_synergy_top_n_ordered(
            edhrec_conn,
            commander_name,
            limit=hi_syn_limit,
        )
    except sqlite3.DatabaseError as exc:
        # Parent of OperationalError; also covers DatabaseError (disk
        # corruption) and its siblings — same degradation intent.
        logger.debug(
            "hi-syn fetch failed for %r (likely missing table, schema drift, or corruption): %s",
            commander_name,
            exc,
        )
        return ()


def clear_cache() -> None:
    """Reset the module-level memoization.

    Intended for tests that reuse a single process across multiple
    fixture states. Production code has no reason to call this —
    vectors stay stable between rebuilds and the cache naturally
    scales with the golden-set size (~100 entries per partner).
    """
    _fetch_hi_syn_names_cached.cache_clear()


def _l2_normalize(vec: np.ndarray) -> np.ndarray | None:
    """Return ``vec / ||vec||`` as ``float32``, or ``None`` if near-zero.

    Callers that receive ``None`` propagate it as the "skip this
    contribution" sentinel all the way back to the scorer so downstream
    arithmetic never sees a NaN from divide-by-zero.
    """
    norm = float(np.linalg.norm(vec))
    if norm < _NORM_EPSILON:
        return None
    return (vec / norm).astype(np.float32)


def build_commander_target_vector(
    commander_names: tuple[str, ...],
    vectors: Mapping[str, np.ndarray],
    edhrec_conn: sqlite3.Connection | None = None,
    *,
    hi_syn_limit: int = 10,
) -> np.ndarray | None:
    """Return the L2-normalized target vector for one or more commanders.

    Composition, per plan R2:

    * For each commander in ``commander_names``, include the
      commander's own port-feature vector from ``vectors`` (when
      present).
    * When ``edhrec_conn`` is provided, also include the vectors of
      the commander's top-``hi_syn_limit`` hi-syn cards that appear
      in ``vectors``. Missing hi-syn names are silently skipped.
    * The target is ``L2-normalize(mean(all collected vectors))``.

    Returns ``None`` when no vectors can be collected (all commanders
    missing from ``vectors`` AND no hi-syn cards match), or when the
    resulting mean is numerically zero. This is the "caller should
    skip the embedding contribution for this (commander, candidate)"
    sentinel — the scorer short-circuits to ``0.0`` in that case.

    ``hi_syn_limit <= 0`` raises ``ValueError``: a zero-limit call
    would degenerate to "commander's own vector only," which is
    already expressible by passing ``edhrec_conn=None``. The explicit
    error prevents silent misuse.
    """
    if hi_syn_limit <= 0:
        raise ValueError(f"hi_syn_limit must be >= 1; got {hi_syn_limit}")

    if not commander_names:
        return None

    collected: list[np.ndarray] = []

    for commander_name in commander_names:
        own_vec = vectors.get(commander_name)
        if own_vec is not None:
            collected.append(np.asarray(own_vec, dtype=np.float32))

        if edhrec_conn is None:
            continue

        hi_syn_names = _fetch_hi_syn_names_cached(
            edhrec_conn,
            commander_name,
            hi_syn_limit,
        )
        for name in hi_syn_names:
            hi_vec = vectors.get(name)
            if hi_vec is not None:
                collected.append(np.asarray(hi_vec, dtype=np.float32))

    if not collected:
        return None

    mean = np.mean(np.stack(collected, axis=0), axis=0)
    return _l2_normalize(mean)


__all__ = [
    "build_commander_target_vector",
    "clear_cache",
]
