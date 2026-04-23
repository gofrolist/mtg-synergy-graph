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
import json as _json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mtg_synergy_graph.bench.collinearity import (
    CollinearityReport,
    compute_collinearity,
)
from mtg_synergy_graph.bench.fixture import (
    FixtureEntry,
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


# ---------------------------------------------------------------------------
# Unit 4 (hidden-gem plan): --inspect-gems
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HiddenGemDelta:
    """One commander's hidden-gem delta: rate change + lost/gained sets.

    Immutable record rendered into either a markdown table row or a
    JSON object. Sorting key is ``abs(rate_delta)`` descending with
    ``None`` entries pushed to the end; commander name is the stable
    tiebreaker so identical magnitudes render reproducibly.
    """

    commander: str
    pinned_rate: float | None
    live_rate: float | None
    rate_delta: float | None
    lost: tuple[str, ...]
    gained: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "commander": self.commander,
            "pinned_rate": self.pinned_rate,
            "live_rate": self.live_rate,
            "rate_delta": self.rate_delta,
            "lost": list(self.lost),
            "gained": list(self.gained),
        }


def _gem_rate(entry: FixtureEntry) -> float | None:
    """Extract ``hidden_gem_hit_rate`` from ``legacy``, returning None
    when absent or non-numeric."""
    value = entry.legacy.get("hidden_gem_hit_rate")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _gem_cards(entry: FixtureEntry) -> frozenset[str]:
    """Extract the ``hidden_cards`` list from ``legacy`` as a frozenset."""
    raw = entry.legacy.get("hidden_cards")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(c) for c in raw)


def _has_gem_data(fixture: PinnedFixture) -> bool:
    """True iff any entry in the fixture carries ``hidden_gem_hit_rate``
    or a non-empty ``hidden_cards`` list."""
    for entry in fixture.entries:
        if _gem_rate(entry) is not None:
            return True
        if _gem_cards(entry):
            return True
    return False


def _build_deltas(
    pinned: PinnedFixture,
    live: PinnedFixture,
) -> tuple[list[HiddenGemDelta], int]:
    """Diff pinned vs live, returning (rows, skipped_count).

    Skipped = commanders present in pinned but absent from live. Rows
    cover the intersection of commanders in both fixtures.
    """
    live_by_cmdr = {e.commander: e for e in live.entries}
    rows: list[HiddenGemDelta] = []
    skipped = 0

    for pinned_entry in pinned.entries:
        live_entry = live_by_cmdr.get(pinned_entry.commander)
        if live_entry is None:
            skipped += 1
            continue

        pinned_cards = _gem_cards(pinned_entry)
        live_cards = _gem_cards(live_entry)
        pinned_rate = _gem_rate(pinned_entry)
        live_rate = _gem_rate(live_entry)

        rate_delta: float | None = (
            live_rate - pinned_rate if pinned_rate is not None and live_rate is not None else None
        )

        rows.append(
            HiddenGemDelta(
                commander=pinned_entry.commander,
                pinned_rate=pinned_rate,
                live_rate=live_rate,
                rate_delta=rate_delta,
                lost=tuple(sorted(pinned_cards - live_cards)),
                gained=tuple(sorted(live_cards - pinned_cards)),
            )
        )

    return rows, skipped


def _sort_deltas(rows: list[HiddenGemDelta]) -> list[HiddenGemDelta]:
    """Sort rows by ``|rate_delta|`` desc with None last, name as
    tiebreaker."""

    def key(row: HiddenGemDelta) -> tuple[int, float, str]:
        if row.rate_delta is None:
            return (1, 0.0, row.commander)
        return (0, -abs(row.rate_delta), row.commander)

    return sorted(rows, key=key)


def _format_delta_count(rate_delta: float | None) -> str:
    """Render Δ as rounded-integer count out of 30 (more intuitive than
    a float since the metric is a ratio of cards in a 30-card window)."""
    if rate_delta is None:
        return "—"
    count = round(rate_delta * 30)
    # Normalize -0 to 0 so the rendered output doesn't flap on tiny
    # floating-point noise near zero.
    if count == 0:
        return "0"
    return f"{count:+d}"


def _format_card_list(names: tuple[str, ...]) -> str:
    """Render a tuple of card names as a compact, comma-separated cell."""
    if not names:
        return "—"
    preview = ", ".join(names[:3])
    if len(names) > 3:
        preview += f" … +{len(names) - 3} more"
    return preview


def _render_gems_markdown(
    rows: list[HiddenGemDelta],
    aggregate_rate_delta: float | None,
    skipped_count: int,
) -> str:
    lines: list[str] = []
    lines.append("# bench.py audit --inspect-gems")
    lines.append("")
    if aggregate_rate_delta is None:
        lines.append("**Aggregate Δ:** `—`")
    else:
        lines.append(f"**Aggregate Δ:** `{aggregate_rate_delta:+.4f}`")
    lines.append(f"**Commanders:** {len(rows)}")
    if skipped_count:
        lines.append(f"**Skipped (missing from live):** {skipped_count}")
    lines.append("")
    lines.append("| commander | Δ | lost gems | gained gems |")
    lines.append("|-----------|--:|-----------|-------------|")
    for row in rows:
        lines.append(
            f"| {row.commander} | {_format_delta_count(row.rate_delta)} "
            f"| {_format_card_list(row.lost)} | {_format_card_list(row.gained)} |"
        )
    return "\n".join(lines) + "\n"


def _render_gems_json(
    rows: list[HiddenGemDelta],
    aggregate_rate_delta: float | None,
    skipped_count: int,
) -> str:
    payload = {
        "aggregate_rate_delta": aggregate_rate_delta,
        "n_commanders": len(rows),
        "skipped_from_live": skipped_count,
        "rows": [row.to_dict() for row in rows],
    }
    return _json.dumps(payload, indent=2, ensure_ascii=False)


def _aggregate_rate_delta(rows: list[HiddenGemDelta]) -> float | None:
    deltas = [r.rate_delta for r in rows if r.rate_delta is not None]
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def handle_inspect_gems(args: argparse.Namespace) -> int:
    """Handle ``bench.py audit --inspect-gems``.

    Per-commander diff of hidden-gem sets between pinned and live.
    Reads the pinned fixture, re-scores the same commander list using
    the current DB + EDHREC DB via ``build_fixture``, and emits a table
    (markdown or JSON) of per-commander ``(Δ, lost_gems, gained_gems)``
    sorted by ``|rate_delta|`` desc.

    Exit codes:
    * ``0`` — normal (even when there is no baseline gem data to compare).
    * ``2`` — missing fixture, missing EDHREC DB, or ``--commander NAME``
      does not match any fixture entry.
    """
    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(
            f"error: pinned fixture {fixture_path} not found. Run `bench.py audit --repin --yes` to create one.",
            file=sys.stderr,
        )
        return 2

    pinned = PinnedFixture.load(fixture_path)

    # --commander filter: reject unknown names before doing any work.
    commander_filter: str | None = getattr(args, "commander", None)
    if commander_filter is not None and not any(e.commander == commander_filter for e in pinned.entries):
        print(
            f"error: commander {commander_filter!r} not in fixture {fixture_path}. "
            "Check spelling or run `bench.py audit --repin --yes` to refresh.",
            file=sys.stderr,
        )
        return 2

    # Short-circuit when the pinned fixture carries no gem data — no
    # useful diff is possible. Predates Unit 2 plumbing of edhrec_conn
    # into handle_repin; once that lands, pins will carry gem data by
    # default and this branch becomes a rare edge case.
    if not _has_gem_data(pinned):
        print("no baseline gems to compare", file=sys.stderr)
        return 0

    # EDHREC DB is mandatory for a live gem rebuild. Missing here is a
    # usage error, not an empty-diff condition.
    edhrec_db_path = getattr(args, "edhrec_db", None)
    if edhrec_db_path is None or not Path(edhrec_db_path).exists():
        print(
            f"error: EDHREC DB {edhrec_db_path!r} not found. Pass --edhrec-db PATH or import the EDHREC data first.",
            file=sys.stderr,
        )
        return 2

    commanders = [e.commander for e in pinned.entries]
    conn = open_db(args.db)
    edhrec_conn = sqlite3.connect(edhrec_db_path)
    edhrec_conn.row_factory = sqlite3.Row
    try:
        live = build_fixture(
            conn,
            commanders,
            existing=pinned,
            tensor_writer=None,
            edhrec_conn=edhrec_conn,
        )
    finally:
        edhrec_conn.close()
        conn.close()

    rows, skipped_count = _build_deltas(pinned, live)
    if commander_filter is not None:
        rows = [r for r in rows if r.commander == commander_filter]
    rows = _sort_deltas(rows)

    limit = getattr(args, "limit", 100)
    if limit is not None and limit >= 0:
        rows = rows[:limit]

    aggregate = _aggregate_rate_delta(rows)

    fmt = getattr(args, "format", "md")
    if fmt == "json":
        rendered = _render_gems_json(rows, aggregate, skipped_count)
    else:
        rendered = _render_gems_markdown(rows, aggregate, skipped_count)

    output_target = getattr(args, "output", None)
    if output_target is None or output_target == "-":
        print(rendered)
    else:
        output_path = Path(output_target)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        print(
            f"bench.py audit --inspect-gems: report written to {output_path}",
            file=sys.stderr,
        )

    if skipped_count:
        print(
            f"skipped {skipped_count} commander(s) missing from live",
            file=sys.stderr,
        )

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
