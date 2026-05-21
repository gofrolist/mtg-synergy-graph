"""Per-commander NDCG@30 audit reporting (``bench.py audit --per-commander-ndcg``).

Emits a sorted-ascending table of per-commander NDCG@30 deltas
(live - pinned) so reviewers can quickly spot regressions. Built to
support the BM25 IDF probe's per-commander prerequisite gate (any
commander losing more than 0.05 NDCG@30 → DECLINE outcome), but the
handler is general-purpose: it works for any audit comparing live
scoring to a pinned fixture.

Read-only diagnostic. Does not mutate the DB or the pinned fixture.

Data flow:
- Load pinned fixture (``PinnedFixture.load(path)``).
- For each commander:
  - Compute pinned NDCG@30 from ``FixtureEntry.scores`` against EDHREC
    graded labels.
  - Compute live NDCG@30 from a fresh re-score against the same labels.
  - Delta = live - pinned.
- Render as Markdown table sorted ascending by delta (worst first).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture, score_commander
from mtg_synergy_graph.bench.optimize import load_edhrec_labels
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.validate import compute_ndcg

#: Per-commander regression threshold flagged in the output. Matches
#: the BM25 IDF probe plan's hard prerequisite gate (any commander
#: losing more than 0.05 NDCG@30 → DECLINE).
PER_COMMANDER_REGRESSION_THRESHOLD: float = -0.05


@dataclass(frozen=True)
class PerCommanderNdcgRow:
    """One commander's pinned/live/delta NDCG@30 numbers."""

    commander: str
    pinned_ndcg: float
    live_ndcg: float
    delta: float


def sort_rows_ascending(rows: list[PerCommanderNdcgRow]) -> list[PerCommanderNdcgRow]:
    """Sort rows by delta ascending; tiebreak by commander name."""
    return sorted(rows, key=lambda r: (r.delta, r.commander))


def render_per_commander_ndcg_markdown(
    rows: list[PerCommanderNdcgRow],
    *,
    fixture_path: str,
    config_hash: str,
) -> str:
    """Render rows as a Markdown table.

    Caller decides the order; this function preserves the input order
    (use ``sort_rows_ascending`` first for the worst-first convention
    the BM25 probe's per-commander gate expects).
    """
    lines: list[str] = []
    lines.append("# bench.py audit --per-commander-ndcg")
    lines.append("")
    lines.append(f"fixture: {fixture_path}")
    lines.append(f"config_hash: {config_hash[:12]}...")
    lines.append("")
    lines.append(
        f"Threshold: rows with delta < {PER_COMMANDER_REGRESSION_THRESHOLD:+.2f} "
        f"would fail the BM25 IDF probe's per-commander prerequisite gate."
    )
    lines.append("")

    if not rows:
        lines.append("No commanders in fixture (empty result).")
        return "\n".join(lines)

    lines.append(f"{'commander':<40} {'pinned':>10} {'live':>10} {'delta':>10}")
    lines.append(f"{'-' * 40} {'-' * 10} {'-' * 10} {'-' * 10}")
    for row in rows:
        flag = " ⚠" if row.delta < PER_COMMANDER_REGRESSION_THRESHOLD else ""
        lines.append(
            f"{row.commander[:40]:<40} {row.pinned_ndcg:>10.4f} {row.live_ndcg:>10.4f} {row.delta:>+10.4f}{flag}"
        )
    return "\n".join(lines)


def _ndcg_for_entry(
    entry: FixtureEntry,
    graded_labels: dict[str, float],
) -> float:
    """Compute NDCG@30 for a fixture entry against EDHREC graded labels."""
    top_30 = list(entry.scores.keys())[:30]
    return compute_ndcg(top_30, graded_labels)


def compute_per_commander_ndcg_rows(
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    pinned: PinnedFixture,
) -> list[PerCommanderNdcgRow]:
    """Compute pinned/live/delta NDCG@30 rows for every commander in the fixture.

    Live re-scoring uses :func:`score_commander` against the current DB
    + scoring config. EDHREC graded labels are the same source the
    optimizer uses (3.0 for high-synergy cards, 1.0 for other on-page
    cards, 0 elsewhere).
    """
    rows: list[PerCommanderNdcgRow] = []
    for entry in pinned.entries:
        labels = load_edhrec_labels(edhrec_conn, entry.commander)
        graded = dict(labels.graded_labels)

        pinned_ndcg = _ndcg_for_entry(entry, graded)

        live_scores, _live_rows = score_commander(conn, entry.commander)
        live_top_30 = list(live_scores.keys())[:30]
        live_ndcg = compute_ndcg(live_top_30, graded)

        rows.append(
            PerCommanderNdcgRow(
                commander=entry.commander,
                pinned_ndcg=pinned_ndcg,
                live_ndcg=live_ndcg,
                delta=live_ndcg - pinned_ndcg,
            )
        )
    return rows


def handle_per_commander_ndcg(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --per-commander-ndcg``.

    Loads the pinned fixture at ``--fixture``, re-scores each commander
    live, computes per-commander NDCG@30 deltas against EDHREC labels
    from ``--edhrec-db``, and emits a Markdown table sorted ascending
    by delta (worst regression first).

    Exit codes:
    * ``0`` — report emitted successfully (regardless of whether any
      commander exceeds the threshold; callers interpret the table).
    * ``2`` — usage error (missing fixture, missing EDHREC DB, etc.).
    """
    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(
            f"error: pinned fixture {fixture_path} not found.",
            file=sys.stderr,
        )
        return 2

    pinned = PinnedFixture.load(fixture_path)
    if not pinned.entries:
        print(f"error: fixture {fixture_path} has no entries.", file=sys.stderr)
        return 2

    edhrec_db_path = Path(getattr(args, "edhrec_db", "data/tags.db"))
    if not edhrec_db_path.exists():
        print(
            f"error: EDHREC DB {edhrec_db_path} not found. Required for NDCG@30 labels.",
            file=sys.stderr,
        )
        return 2

    conn = open_db(args.db)
    edhrec_conn = sqlite3.connect(edhrec_db_path)
    edhrec_conn.row_factory = sqlite3.Row
    try:
        rows = compute_per_commander_ndcg_rows(conn, edhrec_conn, pinned)
    finally:
        conn.close()
        edhrec_conn.close()

    sorted_rows = sort_rows_ascending(rows)
    rendered = render_per_commander_ndcg_markdown(
        sorted_rows,
        fixture_path=str(fixture_path),
        config_hash=pinned.config_hash,
    )

    output_target = getattr(args, "output", None)
    if output_target is None or output_target == "-":
        print(rendered, file=sys.stdout)
    else:
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        print(
            f"bench.py audit --per-commander-ndcg: written to {output_path}",
            file=sys.stderr,
        )
    return 0
