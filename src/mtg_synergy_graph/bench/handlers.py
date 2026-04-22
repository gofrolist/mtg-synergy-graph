"""CLI-layer handlers for every ``bench.py audit`` mode.

Covers ``--repin``, ``--expect-identity``, ``--rule``, ``--inspect``,
``--collinearity``, and ``--unknowns``. Keeps the argparse-facing code
thin: each handler parses its own args, opens the DB, delegates to
:mod:`bench.fixture` (or the per-mode helpers in this module), and
formats a human-readable or JSON report. The functions here are what
:mod:`bench.cli`'s handler table binds to at import time (via
``register()``).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from mtg_synergy_graph.bench.collinearity import (
    CollinearityReport,
    compute_collinearity,
)
from mtg_synergy_graph.bench.fixture import (
    IdentityReport,
    PinnedFixture,
    build_fixture,
)
from mtg_synergy_graph.bench.rule_ops import (
    inspect_rule,
    summarize_rule_contributions,
)
from mtg_synergy_graph.db import open_db


def _load_commanders_from_fixture(fixture: PinnedFixture) -> list[str]:
    """Extract the commander list from a fixture for re-scoring."""
    return [e.commander for e in fixture.entries]


def handle_repin(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --repin``.

    Without ``--yes``: prints a preview and exits 1 (confirmation
    gate — a deliberate dry-run, not an error). With ``--yes``: re-
    scores the commander list (from the existing fixture if present)
    and writes the new baseline. Missing fixture or other usage errors
    return 2 so CI callers can distinguish "user needs to add --yes"
    from "harness is broken." On success returns 0.
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
        return 1

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
        # Clear any stale rows for the current config_hash before
        # repopulating; TensorWriter's INSERT OR REPLACE handles the
        # per-primary-key case but a rule that no longer fires would
        # leave an orphan row otherwise.
        from mtg_synergy_graph.bench.tensor import (  # local import: avoid cycle
            TensorWriter,
            compute_config_hash,
        )

        live_hash = compute_config_hash()
        conn.execute("DELETE FROM rule_contributions WHERE config_hash = ?", (live_hash,))
        conn.commit()

        writer = TensorWriter(conn, config_hash=live_hash)
        with writer:
            fresh = build_fixture(conn, commanders, existing=existing, tensor_writer=writer)
        # rows_written is updated on _flush(), which __exit__ calls via close().
        rows_written = writer.rows_written
    finally:
        conn.close()
    fresh.write(fixture_path)
    print(
        f"--repin wrote {len(fresh.entries)} commanders to {fixture_path} "
        f"(config_hash={fresh.config_hash[:12]}..., "
        f"tensor rows persisted to SQLite: ~{rows_written})",
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

    print(
        "FAIL — pure refactors must preserve identity. "
        "Either fix the regression or `bench.py audit --repin --yes` "
        "if the drift is intentional.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Unit 6 handlers: --rule / --inspect / --collinearity
# ---------------------------------------------------------------------------


def handle_rule(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --rule RULE_ID`` — per-rule raw-contribution summary.

    Reports how much this rule contributed across the golden set.
    **Not** an ablation / score-delta estimate — the underlying
    per-(cmdr, cand) values in the tensor are pre-dampening. See
    ``bench/rule_ops.py`` module docstring for why.
    """
    conn = open_db(args.db)
    try:
        summary = summarize_rule_contributions(conn, args.rule)
    finally:
        conn.close()

    if summary is None:
        print(
            f"no tensor rows for rule {args.rule!r} under the current config_hash. "
            "The rule may never fire on golden commanders, or the persisted tensor "
            "is stale — run `bench.py audit --repin --yes` to refresh.",
            file=sys.stderr,
        )
        return 1

    print(f"# bench.py audit --rule {summary.rule_id}")
    print(f"config_hash: {summary.config_hash[:12]}...")
    print(f"commanders_affected: {summary.commanders_affected}")
    print(f"candidates_affected: {summary.candidates_affected}")
    print(f"aggregate_contribution (raw / pre-dampening): {summary.aggregate_contribution:+.4f}")
    print()
    print("Top commanders by |aggregate contribution|:")
    for cmdr, contrib in summary.per_commander:
        print(f"  {cmdr}: {contrib:+.4f}")
    return 0


def handle_inspect(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --inspect RULE_ID`` — per-(cmdr, cand) rows."""
    conn = open_db(args.db)
    try:
        rows = inspect_rule(conn, args.inspect, limit=args.limit, commander=args.commander)
    finally:
        conn.close()

    if not rows:
        scope = f" for commander {args.commander!r}" if args.commander else ""
        print(
            f"no tensor rows for rule {args.inspect!r}{scope} under the current config_hash.",
            file=sys.stderr,
        )
        return 1

    print(f"# bench.py audit --inspect {args.inspect}")
    if args.commander:
        print(f"commander filter: {args.commander}")
    print(f"rows: {len(rows)} (limit: {args.limit})")
    print()
    print(f"{'commander':<40} {'candidate':<40} {'contrib':>10} {'idf':>8} {'cnt':>4}")
    for row in rows:
        print(
            f"{row.commander[:40]:<40} {row.candidate[:40]:<40} "
            f"{row.contribution:>+10.4f} {row.idf_weight:>8.4f} {row.raw_count:>4d}"
        )
    return 0


def handle_collinearity(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --collinearity`` — pairwise VIF + Pearson."""
    conn = open_db(args.db)
    try:
        report = compute_collinearity(conn)
    finally:
        conn.close()

    _print_collinearity_report(report)
    return 0


def handle_unknowns(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --unknowns`` — report port_nodes rows
    with ``node_kind = 'UNKNOWN'`` ranked by
    ``distinct_cards × sum_edhrec_rank_weight`` so an operator can
    see which novel Forge port shapes are most worth adding to the
    canonical vocabulary.

    Emits a Markdown table (or JSON with ``--format json``) to stdout.
    Exit code is always 0 — the command is informational; UNKNOWN rows
    existing is a normal steady state (Forge ships new mechanics
    regularly).
    """
    if getattr(args, "commander", None):
        print(
            "error: --commander is not supported with --unknowns (unknowns are global across all cards).",
            file=sys.stderr,
        )
        return 2

    conn = open_db(args.db)
    try:
        try:
            rows = conn.execute(
                # rank_weight: EDHREC ranks from ~1 (most-played) to ~30000.
                # Invert so high-rank-popularity contributes more to the
                # weight; floor at 0 so missing / >30000 ranks don't go
                # negative. COALESCE handles the LEFT JOIN nulls when a
                # card_ports row references a name absent from cards
                # (shouldn't happen but defends against import races).
                "SELECT pn.subkind, "
                "       COUNT(DISTINCT pn.card_name) AS distinct_cards, "
                "       COALESCE("
                "           SUM(CASE "
                "                   WHEN c.edhrec_rank IS NULL THEN 0 "
                "                   WHEN c.edhrec_rank > 30000 THEN 0 "
                "                   ELSE (30001 - c.edhrec_rank) "
                "               END), "
                "           0"
                "       ) AS rank_weight "
                "FROM port_nodes pn "
                "LEFT JOIN cards c ON c.name = pn.card_name "
                "WHERE pn.node_kind = 'UNKNOWN' "
                "GROUP BY pn.subkind "
                "ORDER BY rank_weight DESC, distinct_cards DESC, pn.subkind ASC"
            ).fetchall()
            total_unknown_cards = conn.execute(
                "SELECT COUNT(DISTINCT card_name) AS n FROM port_nodes WHERE node_kind = 'UNKNOWN'"
            ).fetchone()["n"]
        except sqlite3.OperationalError as exc:
            print(
                f"error: port_nodes view not available on {args.db}: {exc}. "
                "Re-import the DB via scripts/import_cardsfolder.py.",
                file=sys.stderr,
            )
            return 2
    finally:
        conn.close()

    if getattr(args, "format", "md") == "json":
        import json as _json

        payload = {
            "total_unknown_subkinds": len(rows),
            "total_unknown_cards": total_unknown_cards,
            "rows": [
                {
                    "subkind": r["subkind"],
                    "distinct_cards": r["distinct_cards"],
                    "rank_weight": int(r["rank_weight"]),
                }
                for r in rows
            ],
        }
        print(_json.dumps(payload, indent=2))
        return 0

    print("# bench.py audit --unknowns")
    if not rows:
        print()
        print("No UNKNOWN port shapes detected.")
        return 0

    print()
    print(f"{len(rows)} distinct UNKNOWN subkind(s) across {total_unknown_cards} card(s).")
    print()
    print(f"{'subkind':<40} {'distinct_cards':>15} {'rank_weight':>14}")
    print("-" * 71)
    for r in rows:
        print(f"{r['subkind'][:40]:<40} {r['distinct_cards']:>15} {int(r['rank_weight']):>14}")
    return 0


def _print_collinearity_report(report: CollinearityReport) -> None:
    print("# bench.py audit --collinearity")
    print(f"config_hash: {report.config_hash[:12]}...")
    print(f"rules_examined: {report.rules_examined}")
    if report.rules_dropped:
        print(
            f"rules dropped (zero variance): {len(report.rules_dropped)} — "
            f"{', '.join(report.rules_dropped[:5])}" + (", …" if len(report.rules_dropped) > 5 else "")
        )
    if not report.pairs_flagged:
        print()
        print("No collinear pairs detected (VIF > 5 AND |r| > 0.8).")
        return

    print()
    print(f"{'rule_a':<30} {'rule_b':<30} {'r':>7} {'VIF_a':>8} {'VIF_b':>8}")
    for pair in report.pairs_flagged[:30]:
        print(
            f"{pair.rule_a[:30]:<30} {pair.rule_b[:30]:<30} "
            f"{pair.pearson_r:>+7.3f} {pair.vif_a:>8.2f} {pair.vif_b:>8.2f}"
        )
