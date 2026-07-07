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
- Append an aggregate summary block (mean pinned / mean live / mean
  delta — the SHIP gate number — + violation count) and an
  identity-size slice table (colorless / mono / 2..5-color from
  ``cards.color_identity`` pip count; commanders missing from the
  ``cards`` table land in an explicit "unknown" slice). Plan
  2026-07-02-001 Unit 3 (R8, R12).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from mtg_synergy_graph.bench.cohorts import archetype_payoff_cohort
from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture, score_commander
from mtg_synergy_graph.bench.optimize import load_edhrec_labels
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.validate import compute_ndcg

#: Per-commander regression threshold flagged in the output. Matches
#: the BM25 IDF probe plan's hard prerequisite gate (any commander
#: losing more than 0.05 NDCG@30 → DECLINE).
PER_COMMANDER_REGRESSION_THRESHOLD: float = -0.05

#: Canonical rendering order for the identity-size slice table.
#: "unknown" collects fixture commanders missing from the ``cards``
#: table (post-refresh renames) — explicit slice, never silently
#: miscounted into another class.
IDENTITY_CLASS_ORDER: tuple[str, ...] = (
    "colorless",
    "mono",
    "2-color",
    "3-color",
    "4-color",
    "5-color",
    "unknown",
)


@dataclass(frozen=True)
class PerCommanderNdcgRow:
    """One commander's pinned/live/delta NDCG@30 numbers."""

    commander: str
    pinned_ndcg: float
    live_ndcg: float
    delta: float


@dataclass(frozen=True)
class IdentitySliceRow:
    """Aggregated delta stats for one identity-size class."""

    identity_class: str
    n: int
    mean_delta: float
    worst_delta: float
    violations: int


def sort_rows_ascending(rows: list[PerCommanderNdcgRow]) -> list[PerCommanderNdcgRow]:
    """Sort rows by delta ascending; tiebreak by commander name."""
    return sorted(rows, key=lambda r: (r.delta, r.commander))


def identity_class_from_color_identity(color_identity: str | None) -> str:
    """Map a ``cards.color_identity`` value to an identity-size class.

    ``color_identity`` is a comma-separated pip list ("W,U" → 2-color).
    Empty string / NULL → "colorless"; one pip → "mono"; otherwise
    ``"{n}-color"``.
    """
    if color_identity is None:
        return "colorless"
    pips = [p for p in color_identity.split(",") if p.strip()]
    if not pips:
        return "colorless"
    if len(pips) == 1:
        return "mono"
    return f"{len(pips)}-color"


def fetch_identity_classes(
    conn: sqlite3.Connection,
    commanders: list[str],
) -> dict[str, str]:
    """Map each commander name to its identity-size class.

    Reads ``cards.color_identity`` from the same DB connection the
    handler scores against. Commanders absent from the ``cards`` table
    (e.g. renamed in a data refresh after the fixture was pinned) map
    to "unknown" — an explicit slice rather than a crash or a silent
    miscount. ``FixtureEntry.commander`` is a single name, so no
    partner-union handling is needed here.
    """
    classes: dict[str, str] = {}
    for name in commanders:
        row = conn.execute(
            "SELECT color_identity FROM cards WHERE name = ?",
            (name,),
        ).fetchone()
        classes[name] = "unknown" if row is None else identity_class_from_color_identity(row[0])
    return classes


def compute_identity_slices(
    rows: list[PerCommanderNdcgRow],
    identity_class_by_commander: dict[str, str],
) -> list[IdentitySliceRow]:
    """Aggregate per-commander deltas into identity-size slice rows.

    Only classes with at least one commander are returned, ordered by
    ``IDENTITY_CLASS_ORDER`` (unrecognized labels sort last, by name).
    Commanders missing from the mapping fall into "unknown".
    """
    deltas_by_class: dict[str, list[float]] = {}
    for row in rows:
        cls = identity_class_by_commander.get(row.commander, "unknown")
        deltas_by_class.setdefault(cls, []).append(row.delta)

    order = {cls: i for i, cls in enumerate(IDENTITY_CLASS_ORDER)}
    slices = [
        IdentitySliceRow(
            identity_class=cls,
            n=len(deltas),
            mean_delta=sum(deltas) / len(deltas),
            worst_delta=min(deltas),
            violations=sum(1 for d in deltas if d < PER_COMMANDER_REGRESSION_THRESHOLD),
        )
        for cls, deltas in deltas_by_class.items()
    ]
    return sorted(slices, key=lambda s: (order.get(s.identity_class, len(order)), s.identity_class))


@dataclass(frozen=True)
class CohortSliceRow:
    """Aggregated pinned/live/delta stats for one cohort partition."""

    slice_name: str
    n: int
    mean_pinned: float
    mean_live: float
    mean_delta: float
    worst_delta: float
    violations: int


def _cohort_slice_row(slice_name: str, group: list[PerCommanderNdcgRow]) -> CohortSliceRow:
    """Aggregate one partition; an empty group yields a guarded zero-count row."""
    n = len(group)
    if n == 0:
        return CohortSliceRow(slice_name, 0, 0.0, 0.0, 0.0, 0.0, 0)
    return CohortSliceRow(
        slice_name=slice_name,
        n=n,
        mean_pinned=sum(r.pinned_ndcg for r in group) / n,
        mean_live=sum(r.live_ndcg for r in group) / n,
        mean_delta=sum(r.delta for r in group) / n,
        worst_delta=min(r.delta for r in group),
        violations=sum(1 for r in group if r.delta < PER_COMMANDER_REGRESSION_THRESHOLD),
    )


def compute_cohort_slices(
    rows: list[PerCommanderNdcgRow],
    cohort_names: set[str],
) -> list[CohortSliceRow]:
    """Partition per-commander rows into ``in-cohort`` / ``rest`` slices.

    Both rows are always returned in a fixed order (``in-cohort`` first) even
    when a partition is empty — a missing block would read as "no cohort" rather
    than "cohort empty here" (plan 2026-07-03-001 Unit 3). Means are guarded
    against division by zero.
    """
    in_cohort = [r for r in rows if r.commander in cohort_names]
    rest = [r for r in rows if r.commander not in cohort_names]
    return [
        _cohort_slice_row("in-cohort", in_cohort),
        _cohort_slice_row("rest", rest),
    ]


def _render_cohort_slices(slices: list[CohortSliceRow], *, from_snapshot: bool) -> list[str]:
    """Render the archetype-payoff cohort slice table (plan 2026-07-03-001 R3).

    A cohort-NDCG gain here is necessary-but-not-sufficient (the cohort is
    selected by the same predicate a future rule would key on); the membership
    provenance line records whether the partition is reproducible from the pin.
    """
    provenance = (
        "membership: pinned `cohort_members` snapshot (reproducible from the pin)"
        if from_snapshot
        else "membership: live `archetype_payoff_cohort()` — NOT pin-reproducible "
        "(fixture carries no cohort_members snapshot)"
    )
    lines = [
        "## Cohort slices (archetype-payoff)",
        "",
        provenance,
        "",
        f"{'slice':<12} {'n':>6} {'mean pinned':>12} {'mean live':>12} {'mean delta':>12} {'violations':>10}",
        f"{'-' * 12} {'-' * 6} {'-' * 12} {'-' * 12} {'-' * 12} {'-' * 10}",
    ]
    for s in slices:
        lines.append(
            f"{s.slice_name:<12} {s.n:>6} {s.mean_pinned:>12.4f} {s.mean_live:>12.4f} "
            f"{s.mean_delta:>+12.4f} {s.violations:>10}"
        )
    return lines


def _render_aggregate_summary(rows: list[PerCommanderNdcgRow]) -> list[str]:
    """Render the aggregate summary block (plan 2026-07-02-001 R8).

    The mean delta line is the SHIP gate number — the machine-emitted
    mean of per-commander deltas Unit 4's verdict reads off directly.
    """
    n = len(rows)
    mean_pinned = sum(r.pinned_ndcg for r in rows) / n
    mean_live = sum(r.live_ndcg for r in rows) / n
    mean_delta = sum(r.delta for r in rows) / n
    violations = sum(1 for r in rows if r.delta < PER_COMMANDER_REGRESSION_THRESHOLD)
    return [
        "## Aggregate summary",
        "",
        f"{'commanders':<28} {n:>10}",
        f"{'mean pinned NDCG@30':<28} {mean_pinned:>10.4f}",
        f"{'mean live NDCG@30':<28} {mean_live:>10.4f}",
        f"{'mean delta (SHIP gate)':<28} {mean_delta:>+10.4f}",
        f"{'violations (delta < ' + format(PER_COMMANDER_REGRESSION_THRESHOLD, '+.2f') + ')':<28} {violations:>10}",
    ]


def _render_identity_slices(slices: list[IdentitySliceRow]) -> list[str]:
    """Render the identity-size slice table (plan 2026-07-02-001 R12)."""
    lines = [
        "## Identity-size slices",
        "",
        f"{'identity class':<16} {'n':>6} {'mean delta':>12} {'worst delta':>12} {'violations':>10}",
        f"{'-' * 16} {'-' * 6} {'-' * 12} {'-' * 12} {'-' * 10}",
    ]
    for s in slices:
        lines.append(
            f"{s.identity_class:<16} {s.n:>6} {s.mean_delta:>+12.4f} {s.worst_delta:>+12.4f} {s.violations:>10}"
        )
    return lines


def render_per_commander_ndcg_markdown(
    rows: list[PerCommanderNdcgRow],
    *,
    fixture_path: str,
    config_hash: str,
    identity_class_by_commander: dict[str, str] | None = None,
    cohort_names: set[str] | None = None,
    cohort_from_snapshot: bool = True,
) -> str:
    """Render rows as a Markdown table.

    Caller decides the order; this function preserves the input order
    (use ``sort_rows_ascending`` first for the worst-first convention
    the BM25 probe's per-commander gate expects).

    When ``rows`` is non-empty an aggregate summary block follows the
    table; when ``identity_class_by_commander`` is also provided (the
    handler passes :func:`fetch_identity_classes` output), an
    identity-size slice table follows the aggregate block. When
    ``cohort_names`` is provided, an archetype-payoff cohort slice
    (``in-cohort`` / ``rest``) follows the identity block;
    ``cohort_from_snapshot`` records whether membership came from the
    fixture snapshot (reproducible) or a live computation.
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

    lines.append("")
    lines.extend(_render_aggregate_summary(rows))

    if identity_class_by_commander is not None:
        slices = compute_identity_slices(rows, identity_class_by_commander)
        lines.append("")
        lines.extend(_render_identity_slices(slices))

    if cohort_names is not None:
        cohort_slices = compute_cohort_slices(rows, cohort_names)
        lines.append("")
        lines.extend(_render_cohort_slices(cohort_slices, from_snapshot=cohort_from_snapshot))
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
        identity_classes = fetch_identity_classes(conn, [e.commander for e in pinned.entries])
        # Cohort membership must be resolved BEFORE conn.close() — the report
        # renders after the connection is closed, so a live cohort query issued
        # post-close would fail. Prefer the fixture's snapshot (reproducible from
        # the pin); fall back to a live, non-reproducible computation otherwise
        # (Key Decision 4).
        if pinned.cohort_members:
            cohort_names: set[str] = set(pinned.cohort_members)
            cohort_from_snapshot = True
        else:
            cohort_names = archetype_payoff_cohort(conn)
            cohort_from_snapshot = False
    finally:
        conn.close()
        edhrec_conn.close()

    sorted_rows = sort_rows_ascending(rows)
    rendered = render_per_commander_ndcg_markdown(
        sorted_rows,
        fixture_path=str(fixture_path),
        config_hash=pinned.config_hash,
        identity_class_by_commander=identity_classes,
        cohort_names=cohort_names,
        cohort_from_snapshot=cohort_from_snapshot,
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
