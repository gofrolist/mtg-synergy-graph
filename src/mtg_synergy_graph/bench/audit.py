"""``bench.py audit`` main subcommand — full-fixture audit runner.

Loads the pinned fixture, re-scores every commander it lists using the
current DB + scoring config, and produces an ``AuditReport``. MVP is
serial — the 30-second latency target on the 100-commander golden set
is aspirational; once real timing data lands we can add
ProcessPoolExecutor without changing the reporting contract.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mtg_synergy_graph.bench.fixture import (
    PinnedFixture,
    build_fixture,
)
from mtg_synergy_graph.bench.report import AuditReport, build_report
from mtg_synergy_graph.db import open_db


def run_audit(db_path: str | Path, fixture_path: str | Path) -> AuditReport:
    """Load fixture, re-score, return an aggregated AuditReport."""
    fixture_path = Path(fixture_path)
    pinned = PinnedFixture.load(fixture_path)
    commanders = [e.commander for e in pinned.entries]
    conn = open_db(db_path)
    try:
        live = build_fixture(conn, commanders)
    finally:
        conn.close()
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

    report = run_audit(args.db, fixture_path)

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

    _print_summary(report)
    return 0 if report.is_identical else 1


def _write_default_output(rendered: str, fixture_path: Path, fmt: str) -> None:
    """Also persist the rendered report to `.audit/last.{md,json}` for
    pre-commit-hook consumption (Unit 7)."""
    default_dir = Path(".audit")
    default_dir.mkdir(exist_ok=True)
    suffix = "json" if fmt == "json" else "md"
    (default_dir / f"last.{suffix}").write_text(rendered, encoding="utf-8")


def _print_summary(report: AuditReport) -> None:
    """One-line status summary to stderr (visible in pre-commit output)."""
    status = "PASS" if report.is_identical else "DRIFT"
    print(
        f"bench.py audit: {status} "
        f"(Δ={report.aggregate_score_delta:+.4f}, "
        f"{report.commanders_compared} cmdrs, "
        f"{len(report.per_commander)} changed)",
        file=sys.stderr,
    )
