"""CLI-layer handlers for ``--repin`` and ``--expect-identity``.

Keeps the argparse-facing code thin: each handler parses its own args,
opens the DB, delegates to :mod:`bench.fixture`, and formats a human-
readable report. The functions here are what :mod:`bench.cli`'s handler
table binds to at import time (via ``register()``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mtg_synergy_graph.bench.fixture import (
    IdentityReport,
    PinnedFixture,
    build_fixture,
)
from mtg_synergy_graph.db import open_db


def _load_commanders_from_fixture(fixture: PinnedFixture) -> list[str]:
    """Extract the commander list from a fixture for re-scoring."""
    return [e.commander for e in fixture.entries]


def handle_repin(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --repin``.

    Without ``--yes``: prints a preview and exits non-zero so a stray
    invocation cannot overwrite the baseline. With ``--yes``: re-scores
    the commander list (from the existing fixture if present) and writes
    the new baseline.
    """
    fixture_path = Path(args.fixture)
    existing: PinnedFixture | None = None
    if fixture_path.exists():
        existing = PinnedFixture.load(fixture_path)

    if not args.yes:
        if existing is None:
            print(
                f"--repin would create a NEW fixture at {fixture_path}. Re-run with --yes to confirm.",
                file=sys.stderr,
            )
        else:
            print(
                f"--repin would overwrite {fixture_path} "
                f"(existing config_hash={existing.config_hash[:12]}..., "
                f"{len(existing.entries)} commanders). "
                "Re-run with --yes to confirm.",
                file=sys.stderr,
            )
        return 2

    if existing is None:
        print(
            f"error: no fixture at {fixture_path}; "
            "cannot determine commander list for repin. "
            "Bootstrap one via scripts/golden_set_track.py --bootstrap first.",
            file=sys.stderr,
        )
        return 2

    commanders = _load_commanders_from_fixture(existing)
    conn = open_db(args.db)
    try:
        fresh = build_fixture(conn, commanders, existing=existing)
    finally:
        conn.close()
    fresh.write(fixture_path)
    print(
        f"--repin wrote {len(fresh.entries)} commanders to {fixture_path} (config_hash={fresh.config_hash[:12]}...)",
        file=sys.stderr,
    )
    return 0


def handle_expect_identity(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --expect-identity``.

    Loads the pinned fixture, re-scores each commander with the current
    DB + scoring config, and asserts bitwise-identical per-(cmdr, cand)
    scores and tensor rows. Exits non-zero with an actionable summary on
    any mismatch.
    """
    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(
            f"error: pinned fixture {fixture_path} not found. Run `bench.py audit --repin --yes` to create one.",
            file=sys.stderr,
        )
        return 2

    pinned = PinnedFixture.load(fixture_path)
    commanders = _load_commanders_from_fixture(pinned)
    if not commanders:
        print(f"error: fixture {fixture_path} has no entries.", file=sys.stderr)
        return 2

    conn = open_db(args.db)
    try:
        live = build_fixture(conn, commanders)
    finally:
        conn.close()

    report = pinned.assert_identity(live)
    _print_identity_report(report, fixture_path)
    return 0 if report.is_identical else 1


def _print_identity_report(report: IdentityReport, fixture_path: Path) -> None:
    """Render a human-readable summary of an identity check.

    Kept simple: stderr summary plus the first ~10 mismatches per
    category. Unit 4's ``bench.py audit`` will offer richer reporting
    once it lands.
    """
    if report.is_identical:
        print(
            f"--expect-identity: PASS (fixture {fixture_path})",
            file=sys.stderr,
        )
        return

    if report.config_hash_mismatch:
        print(
            f"config_hash mismatch: {report.config_hash_mismatch}",
            file=sys.stderr,
        )
        print(
            "  Scoring config changed since the fixture was pinned. "
            "Review the change and re-pin if intentional: "
            "`bench.py audit --repin --yes`.",
            file=sys.stderr,
        )

    if report.missing_commanders:
        print(
            f"missing commanders in live run: {len(report.missing_commanders)}",
            file=sys.stderr,
        )
        for name in report.missing_commanders[:10]:
            print(f"  - {name}", file=sys.stderr)

    if report.score_mismatches:
        print(
            f"score mismatches: {len(report.score_mismatches)}",
            file=sys.stderr,
        )
        for delta in report.score_mismatches[:10]:
            print(
                f"  {delta.commander} / {delta.candidate}: "
                f"live={delta.live:.9f} pinned={delta.pinned:.9f} "
                f"(Δ={delta.delta:+.9f})",
                file=sys.stderr,
            )

    if report.tensor_mismatches:
        print(
            f"tensor mismatches: {len(report.tensor_mismatches)}",
            file=sys.stderr,
        )
        for tdelta in report.tensor_mismatches[:10]:
            print(
                f"  {tdelta.commander} / {tdelta.candidate} / "
                f"{tdelta.rule_id}: live={tdelta.live} pinned={tdelta.pinned}",
                file=sys.stderr,
            )

    print(
        "FAIL — pure refactors must preserve identity. "
        "Either fix the regression or `bench.py audit --repin --yes` "
        "if the drift is intentional.",
        file=sys.stderr,
    )
