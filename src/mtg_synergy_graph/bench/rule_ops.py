"""``--rule`` ablation and ``--inspect`` queries over the persisted tensor.

Both subcommands read from ``rule_contributions`` — no re-scoring. That's
the entire point of persisting the tensor: ``bench.py audit --rule X``
and ``--inspect X`` answer in < 2 s no matter how many commanders the
golden set grows to.

``compute_config_hash()`` is the filter key; rows from a stale config
are ignored with a clear message so queries never silently serve
pre-tune data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from mtg_synergy_graph.bench.tensor import compute_config_hash


@dataclass(frozen=True)
class AblationSummary:
    """Aggregated result of zeroing out one rule across all commanders."""

    rule_id: str
    config_hash: str
    commanders_affected: int
    candidates_affected: int
    aggregate_contribution_removed: float
    #: Top (commander, Σ contribution) tuples. Immutable so the frozen
    #: dataclass contract actually holds (list would be mutable).
    per_commander_removed: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class InspectRow:
    """One row of ``--inspect`` output."""

    commander: str
    candidate: str
    contribution: float
    idf_weight: float
    raw_count: int


def ablate_rule(conn: sqlite3.Connection, rule_id: str, config_hash: str | None = None) -> AblationSummary | None:
    """Summarize the aggregate effect of disabling ``rule_id``.

    Returns ``None`` if the rule has no rows in the current tensor —
    which means either the rule never fired on any golden commander or
    the persisted tensor is stale (caller decides which).
    """
    h = config_hash or compute_config_hash()
    # Aggregate |contribution| per commander + overall totals in one pass.
    rows = conn.execute(
        """
        SELECT commander, SUM(contribution) AS total_contribution,
               COUNT(DISTINCT candidate) AS n_cands
        FROM rule_contributions
        WHERE rule_id = ? AND config_hash = ?
        GROUP BY commander
        ORDER BY ABS(SUM(contribution)) DESC
        """,
        (rule_id, h),
    ).fetchall()
    if not rows:
        return None

    commanders_affected = len(rows)
    aggregate = sum(r["total_contribution"] for r in rows)
    per_commander = tuple((r["commander"], r["total_contribution"]) for r in rows[:20])
    cand_row = conn.execute(
        "SELECT COUNT(DISTINCT candidate) AS n FROM rule_contributions WHERE rule_id = ? AND config_hash = ?",
        (rule_id, h),
    ).fetchone()
    candidates_affected = cand_row["n"] if cand_row else 0

    return AblationSummary(
        rule_id=rule_id,
        config_hash=h,
        commanders_affected=commanders_affected,
        candidates_affected=candidates_affected,
        aggregate_contribution_removed=aggregate,
        per_commander_removed=per_commander,
    )


def inspect_rule(
    conn: sqlite3.Connection,
    rule_id: str,
    limit: int = 100,
    commander: str | None = None,
    config_hash: str | None = None,
) -> list[InspectRow]:
    """Top-N contribution rows for ``rule_id``, sorted by |contribution|.

    Accepts a commander filter so large rules (firing on 500+ cmdrs)
    can be inspected per-commander without flooding output.
    """
    h = config_hash or compute_config_hash()
    params: list[object] = [rule_id, h]
    sql = (
        "SELECT commander, candidate, contribution, idf_weight, raw_count "
        "FROM rule_contributions "
        "WHERE rule_id = ? AND config_hash = ?"
    )
    if commander is not None:
        sql += " AND commander = ?"
        params.append(commander)
    sql += " ORDER BY ABS(contribution) DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        InspectRow(
            commander=r["commander"],
            candidate=r["candidate"],
            contribution=r["contribution"],
            idf_weight=r["idf_weight"],
            raw_count=r["raw_count"],
        )
        for r in rows
    ]
