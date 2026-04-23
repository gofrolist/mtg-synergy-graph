"""Ranking helpers for the ``--vs-forge-oracle`` sidecar.

Two pure-function helpers that let consumers compare rankings between
our live scorer and Forge's ``CardRanker`` pair scorer:

- :func:`forge_rank_candidates` — score a list of candidate oracle_ids
  against a commander using the Unit 3 ``pair_scorer.rate_pair`` port,
  sort by descending score, and return ``(oracle_id, score)`` pairs.
- :func:`kendall_tau` — Kendall's rank-correlation τ-b, pure Python,
  O(n²). Adequate for the plan's per-commander sample size of N=30
  (900 comparisons per commander, 90 k across the 100-commander golden
  set — runs in a fraction of a second without SciPy).

The plan originally flagged SciPy as a possible transitive dep; this
project does not carry it, so Unit 7 ships a hand-rolled τ rather
than add the dep.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 7.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable

from mtg_synergy_graph.forge_oracle import pair_scorer


def forge_rank_candidates(
    cmdr_oracle_id: str,
    candidate_oracle_ids: Iterable[str],
    conn: sqlite3.Connection,
) -> list[tuple[str, float]]:
    """Score each candidate against ``cmdr_oracle_id`` via ``pair_scorer``
    and return them sorted by descending score.

    Directionality: ``pair_scorer.rate_pair(A, B)`` asks "does B want A,
    minus A's needs given {B}". For the rank-candidates-against-commander
    use case we pass ``A = candidate`` and ``B = commander`` — "does the
    commander want this candidate?" mirrors Forge's
    ``CardRanker.getScoreForDeckHints(card, otherCards=[commander])``.

    Ties broken by oracle_id (lexical) for deterministic output across
    runs and platforms.

    Raises ``LookupError`` if any oracle_id is unknown to the DB.
    """
    scored: list[tuple[str, float]] = []
    for cand_oid in candidate_oracle_ids:
        score = pair_scorer.rate_pair(conn, cand_oid, cmdr_oracle_id)
        scored.append((cand_oid, score))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored


def kendall_tau(rank_pairs: list[tuple[int, int]]) -> float:
    """Kendall's τ-b over a list of ``(rank_a, rank_b)`` paired observations.

    Convention: ranks are integers (1-indexed or 0-indexed, doesn't
    matter — only the relative order within each column matters).
    Returns a value in ``[-1.0, 1.0]``:

    - ``+1.0`` — rankings perfectly agree.
    - ``0.0`` — rankings are uncorrelated (or so few items that
      the denominator collapses to zero).
    - ``-1.0`` — rankings are perfectly inverse.

    Edge cases:

    - Fewer than 2 observations → vacuously agreeing, returns ``1.0``.
    - All items tied on one side → denominator is zero on that side;
      returns ``0.0``.
    """
    n = len(rank_pairs)
    if n < 2:
        return 1.0

    concordant = 0
    discordant = 0
    ties_a = 0
    ties_b = 0
    for i in range(n):
        ra_i, rb_i = rank_pairs[i]
        for j in range(i + 1, n):
            ra_j, rb_j = rank_pairs[j]
            delta_a = ra_i - ra_j
            delta_b = rb_i - rb_j
            if delta_a == 0 and delta_b == 0:
                continue
            if delta_a == 0:
                ties_a += 1
                continue
            if delta_b == 0:
                ties_b += 1
                continue
            same_sign = (delta_a > 0) == (delta_b > 0)
            if same_sign:
                concordant += 1
            else:
                discordant += 1

    denom_a = concordant + discordant + ties_a
    denom_b = concordant + discordant + ties_b
    if denom_a == 0 or denom_b == 0:
        return 0.0
    return (concordant - discordant) / math.sqrt(denom_a * denom_b)


def ranks_of(ordered_items: list[str]) -> dict[str, int]:
    """Return ``{item: 0-indexed rank}`` for an ordered list.

    Convenience for callers that have two ordered lists and want to
    build ``rank_pairs`` for :func:`kendall_tau`. Ties are resolved
    by list position — the input order is authoritative.
    """
    return {item: rank for rank, item in enumerate(ordered_items)}
