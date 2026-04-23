"""``bench.py audit`` main subcommand — full-fixture audit runner.

Loads the pinned fixture, re-scores every commander it lists using the
current DB + scoring config, and produces an ``AuditReport``. MVP is
serial — the 30-second latency target on the 100-commander golden set
is aspirational; once real timing data lands we can add
ProcessPoolExecutor without changing the reporting contract.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from mtg_synergy_graph.bench import history as bench_history
from mtg_synergy_graph.bench.fixture import (
    PinnedFixture,
    build_fixture,
)
from mtg_synergy_graph.bench.report import AuditReport, build_report
from mtg_synergy_graph.db import open_db


def run_audit(
    db_path: str | Path,
    fixture_path: str | Path,
    edhrec_db: str | Path | None = None,
) -> AuditReport:
    """Load fixture, re-score, return an aggregated AuditReport.

    When ``edhrec_db`` is provided and exists, the EDHREC DB is opened
    and passed to ``build_fixture`` so per-commander
    ``hidden_gem_hit_rate`` gets computed on the live side. This makes
    the aggregate hidden-gem metric and its FR3/FR4 delta+warning
    functional in the normal audit path — without it, the legacy path
    produces gem-less live entries and the delta is always ``—``.

    If ``edhrec_db`` is ``None`` or the path doesn't exist, we skip the
    open silently (for ``None``) or emit a stderr warning (for a
    non-existent explicit path) and build the fixture without an
    ``edhrec_conn`` — the metric is then unavailable but the audit
    still completes normally.
    """
    fixture_path = Path(fixture_path)
    pinned = PinnedFixture.load(fixture_path)
    commanders = [e.commander for e in pinned.entries]

    edhrec_conn: sqlite3.Connection | None = None
    if edhrec_db is not None:
        edhrec_path = Path(edhrec_db)
        if edhrec_path.exists():
            edhrec_conn = sqlite3.connect(edhrec_path)
            edhrec_conn.row_factory = sqlite3.Row
        else:
            print(
                f"bench.py audit: warning: edhrec_db {edhrec_path} not found — hidden_gem_hit_rate will be unavailable",
                file=sys.stderr,
            )

    conn = open_db(db_path)
    try:
        live = build_fixture(conn, commanders, edhrec_conn=edhrec_conn)
    finally:
        conn.close()
        if edhrec_conn is not None:
            edhrec_conn.close()
    return build_report(str(fixture_path), pinned, live)


def handle_audit(args: argparse.Namespace) -> int:
    """CLI handler for the default ``bench.py audit`` mode.

    Exit codes:
    * ``0`` — identical to pinned baseline
    * ``1`` — drift detected (any score / tensor mismatch)
    * ``2`` — usage error (missing fixture, empty fixture, I/O failure)
    """
    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(
            f"error: pinned fixture {fixture_path} not found. Run `bench.py audit --repin --yes` to create one.",
            file=sys.stderr,
        )
        return 2

    pinned = PinnedFixture.load(fixture_path)
    if not pinned.entries:
        print(f"error: fixture {fixture_path} has no entries.", file=sys.stderr)
        return 2

    report = run_audit(args.db, fixture_path, edhrec_db=getattr(args, "edhrec_db", None))

    rendered = report.to_json() if args.format == "json" else report.to_markdown()

    output_target = args.output
    if output_target is None or output_target == "-":
        _write_default_output(rendered, fixture_path, args.format)
        print(rendered, file=sys.stdout)
    else:
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        print(
            f"bench.py audit: report written to {output_path}",
            file=sys.stderr,
        )

    _print_hidden_gem_warning(report)
    _print_summary(report)

    # Append a row to ``.audit/history.csv`` so ``bench.py audit --trend
    # hidden_gems`` can replay deltas over time. ``append_run`` swallows
    # I/O errors internally; the outer try/except is belt-and-suspenders
    # in case a future refactor lets an exception leak — a history-write
    # failure must never turn a PASS audit into a failure.
    history_path = getattr(args, "history", ".audit/history.csv")
    try:
        bench_history.append_run(report, path=history_path)
    except Exception as exc:  # pragma: no cover — defensive
        print(
            f"bench.py audit: warning: unexpected error appending history row: {exc}",
            file=sys.stderr,
        )

    return 0 if report.is_identical else 1


def _write_default_output(rendered: str, fixture_path: Path, fmt: str) -> None:
    """Also persist the rendered report to `.audit/last.{md,json}` for
    pre-commit-hook consumption (Unit 7).

    A read-only ``.audit/`` (or any other OSError during mkdir / write)
    must NOT turn a PASS audit into a failure — the primary output
    has already been printed to stdout. Degrade to a stderr warning
    and return.
    """
    default_dir = Path(".audit")
    suffix = "json" if fmt == "json" else "md"
    target = default_dir / f"last.{suffix}"
    try:
        default_dir.mkdir(exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        print(
            f"bench.py audit: warning: could not write {target}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )


def _print_hidden_gem_warning(report: AuditReport) -> None:
    """FR4 warning: drop in aggregate ``hidden_gem_hit_rate`` exceeded threshold.

    Printed to stderr once per audit run, before the single-line summary,
    so it is the first thing the user sees. Advisory only — does not
    affect the audit exit code.
    """
    if not report.hidden_gem_warning:
        return
    delta = report.hidden_gem_hit_rate_delta
    pinned = report.pinned_hidden_gem_hit_rate
    live = report.aggregate_hidden_gem_hit_rate
    if delta is None or pinned is None or live is None:
        # Defensive — ``hidden_gem_warning`` is only True when all three
        # are set, but the type system can't prove it at this call site.
        return
    print(
        f"⚠ hidden_gem_hit_rate dropped {abs(delta):.3f} on this change "
        f"(from {pinned:.3f} to {live:.3f}).\n"
        f"  Inspect: `bench.py audit --inspect-gems` to see which gems were lost.",
        file=sys.stderr,
    )


def _print_summary(report: AuditReport) -> None:
    """One-line status summary to stderr (visible in pre-commit output)."""
    status = "PASS" if report.is_identical else "DRIFT"
    gem_summary = _format_gem_summary(report)
    print(
        f"bench.py audit: {status} [{report.verdict.value}] "
        f"(Δ={report.aggregate_score_delta:+.4f}, "
        f"{report.commanders_compared} cmdrs, "
        f"{len(report.per_commander)} changed, "
        f"no_change={report.histogram.no_change}, "
        f"shuffle_across={report.histogram.rank_shuffle_across_top30_boundary}, "
        f"{gem_summary})",
        file=sys.stderr,
    )


def _format_gem_summary(report: AuditReport) -> str:
    """Render the trailing ``gem_Δ=...`` fragment for the audit summary."""
    delta = report.hidden_gem_hit_rate_delta
    if delta is None:
        return "gem_Δ=—"
    fragment = f"gem_Δ={delta:+.4f}"
    if report.hidden_gem_warning:
        fragment += " WARN"
    return fragment
