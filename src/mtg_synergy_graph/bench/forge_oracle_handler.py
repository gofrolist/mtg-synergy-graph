"""``bench.py audit --vs-forge-oracle`` handler.

For each commander in the pinned fixture:

1. Take the top-N candidates from the pinned entry's ``scores`` map
   (sorted by score desc; N = ``--limit``, default 30).
2. Score the SAME candidates against the commander via
   :func:`forge_oracle.ranking.forge_rank_candidates`.
3. Compute per-commander Kendall's τ over the two rankings.

Aggregate τ (unweighted mean across commanders) is the primary metric.
Top-10 (cmdr, candidate) divergence pairs — where Forge ranks a card
high but our scorer ranks it low — are the rule-proposal seeds the
rule-authoring workflow feeds into ``scripts/forge_oracle.py
propose-rules`` (Unit 8).

Exit codes:
- ``0`` normal.
- ``2`` missing fixture, missing forge_oracle.db, stale/missing config
  hash, or unusable DB.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 7.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.forge_oracle import config as fo_config
from mtg_synergy_graph.forge_oracle import ranking


@dataclass(frozen=True)
class _CommanderReport:
    commander: str
    n_compared: int
    tau: float
    top_divergences: list[tuple[str, int, int]] = field(default_factory=list)
    # Each: (candidate_name, our_rank_0indexed, forge_rank_0indexed)


def _our_topn(entry: FixtureEntry, n: int) -> list[str]:
    """Return the top-N candidate **names** by pinned score (desc, name tiebreak)."""
    items = sorted(entry.scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in items[:n]]


def _resolve_name_to_oid(conn: sqlite3.Connection, names: list[str]) -> dict[str, str]:
    """Lookup oracle_id for each card name. Missing names are absent from the result."""
    if not names:
        return {}
    placeholders = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT name, oracle_id FROM cards "  # noqa: S608 — placeholders only
        f"WHERE name IN ({placeholders}) AND oracle_id IS NOT NULL",
        names,
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _score_commander(
    entry: FixtureEntry,
    conn: sqlite3.Connection,
    *,
    topn: int,
) -> _CommanderReport | None:
    """Return a per-commander report, or None if the commander could not be
    scored (unknown oracle_id or <2 resolvable candidates)."""
    our_names = _our_topn(entry, topn)
    if not our_names:
        return None
    name_to_oid = _resolve_name_to_oid(conn, [entry.commander, *our_names])
    cmdr_oid = name_to_oid.get(entry.commander)
    if cmdr_oid is None:
        return None
    resolved_names = [n for n in our_names if n in name_to_oid]
    if len(resolved_names) < 2:
        return None
    resolved_oids = [name_to_oid[n] for n in resolved_names]

    # Our ranking — positional, from the pinned top-N (already in order).
    our_rank: dict[str, int] = {name: r for r, name in enumerate(resolved_names)}

    # Forge ranking — score the same candidates against the commander
    # and sort desc.
    try:
        forge_ordered = ranking.forge_rank_candidates(cmdr_oid, resolved_oids, conn)
    except LookupError:
        # One of the resolved oracle_ids is absent from cards (should not
        # happen since we resolved them from cards, but guard anyway).
        return None
    # Map oid → rank; translate back to name-indexed.
    oid_to_name = {name_to_oid[n]: n for n in resolved_names}
    forge_rank_by_name: dict[str, int] = {}
    for rank, (oid, _score) in enumerate(forge_ordered):
        forge_rank_by_name[oid_to_name[oid]] = rank

    # Kendall τ over (our_rank, forge_rank) for each resolved candidate.
    pairs = [(our_rank[n], forge_rank_by_name[n]) for n in resolved_names]
    tau = ranking.kendall_tau(pairs)

    # Top divergences for this commander: cards Forge ranks high AND we
    # rank low. Heuristic gap = our_rank - forge_rank (positive = Forge
    # boosted it, we missed it). Sort desc, take top 3 per commander —
    # aggregated top-10 across commanders is the final report section.
    divergences = sorted(
        ((n, our_rank[n], forge_rank_by_name[n]) for n in resolved_names),
        key=lambda t: -(t[1] - t[2]),
    )[:3]

    return _CommanderReport(
        commander=entry.commander,
        n_compared=len(resolved_names),
        tau=tau,
        top_divergences=divergences,
    )


def _render_markdown(reports: list[_CommanderReport], aggregate_tau: float) -> str:
    lines: list[str] = []
    lines.append("# bench.py audit --vs-forge-oracle")
    lines.append("")
    lines.append(f"- **Aggregate Forge-agreement (mean τ)**: `{aggregate_tau:.4f}`")
    lines.append(f"- **Commanders compared**: {len(reports)}")
    lines.append("")
    lines.append("Interpretation: τ = +1 means our top-N agrees perfectly with")
    lines.append("Forge's CardRanker over the same candidate set; τ = 0 means no")
    lines.append("rank correlation; τ = -1 means perfectly inverse. Values are")
    lines.append("*tracking-only* at MVP — the gate stays on the NDCG histogram.")
    lines.append("")
    lines.append("## Per-commander")
    lines.append("")
    lines.append("| Commander | n | τ |")
    lines.append("|---|---|---|")
    for r in sorted(reports, key=lambda x: x.tau):
        lines.append(f"| {r.commander} | {r.n_compared} | {r.tau:+.4f} |")
    lines.append("")
    lines.append("## Top-10 divergences (Forge high, we low)")
    lines.append("")
    lines.append("Candidates the Forge pair scorer ranks much higher than our")
    lines.append("scorer — seeds for rule proposals. `our_rank - forge_rank` shows")
    lines.append("the delta (positive = we're under-ranking).")
    lines.append("")
    lines.append("| Commander | Candidate | Our rank | Forge rank | Δ |")
    lines.append("|---|---|---|---|---|")
    # Aggregate across commanders.
    all_divs = [(r.commander, *d) for r in reports for d in r.top_divergences]
    all_divs.sort(key=lambda t: -(t[2] - t[3]))
    for cmdr, cand, our_r, forge_r in all_divs[:10]:
        delta = our_r - forge_r
        lines.append(f"| {cmdr} | {cand} | {our_r} | {forge_r} | {delta:+d} |")
    lines.append("")
    return "\n".join(lines)


def _render_json(reports: list[_CommanderReport], aggregate_tau: float) -> str:
    payload = {
        "aggregate_tau": aggregate_tau,
        "n_commanders": len(reports),
        "per_commander": [
            {
                "commander": r.commander,
                "n_compared": r.n_compared,
                "tau": r.tau,
                "top_divergences": [
                    {"candidate": cand, "our_rank": our_r, "forge_rank": forge_r}
                    for cand, our_r, forge_r in r.top_divergences
                ],
            }
            for r in reports
        ],
    }
    return json.dumps(payload, indent=2)


def handle_vs_forge_oracle(args: argparse.Namespace) -> int:
    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(
            f"error: pinned fixture {fixture_path} not found. Run `bench.py audit --repin --yes` to create one.",
            file=sys.stderr,
        )
        return 2

    forge_db_path = Path(args.forge_oracle_db)
    if not forge_db_path.exists():
        print(
            f"error: forge_oracle.db not found at {forge_db_path}. Run `scripts/forge_oracle.py build` first.",
            file=sys.stderr,
        )
        return 2

    # Verify config hash — stale sidecar means the PPMI / pair scorer data
    # was built under different inputs. Refuse rather than report against
    # stale numbers.
    try:
        forge_conn = sqlite3.connect(forge_db_path)
    except sqlite3.DatabaseError as exc:
        print(f"error: forge_oracle.db is unusable: {exc}", file=sys.stderr)
        return 2
    try:
        # Config inputs rely on live Forge HEAD + smoothing defaults.
        # For --vs-forge-oracle we use the same defaults as the build
        # command (k=0.5, min_decks=3) — if a user built with other
        # values, the verify_current_or_raise will flag it.
        inputs = fo_config.get_oracle_config_inputs(
            ppmi_smoothing_k=args.smoothing_k,
            min_decks_count=args.min_decks,
        )
        try:
            fo_config.verify_current_or_raise(forge_conn, inputs)
        except (fo_config.OracleConfigStaleError, fo_config.OracleConfigMissingError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    finally:
        forge_conn.close()

    pinned = PinnedFixture.load(fixture_path)
    if not pinned.entries:
        print("error: pinned fixture has no entries", file=sys.stderr)
        return 2

    # --commander filter: reject unknown names before doing any work.
    commander_filter: str | None = getattr(args, "commander", None)
    if commander_filter is not None and not any(e.commander == commander_filter for e in pinned.entries):
        print(
            f"error: commander {commander_filter!r} not in fixture {fixture_path}.",
            file=sys.stderr,
        )
        return 2

    topn = int(getattr(args, "limit", None) or 30)
    entries = (
        pinned.entries if commander_filter is None else [e for e in pinned.entries if e.commander == commander_filter]
    )

    conn = open_db(args.db)
    try:
        reports: list[_CommanderReport] = []
        skipped_commanders = 0
        for entry in entries:
            report = _score_commander(entry, conn, topn=topn)
            if report is None:
                skipped_commanders += 1
                continue
            reports.append(report)
    finally:
        conn.close()

    if not reports:
        print(
            f"no commanders scorable (skipped={skipped_commanders}). "
            "Check that `data/synergy.db` was imported against the same "
            "card corpus as the pinned fixture.",
            file=sys.stderr,
        )
        return 2

    aggregate_tau = sum(r.tau for r in reports) / len(reports)

    fmt = getattr(args, "format", None) or "md"
    rendered = _render_json(reports, aggregate_tau) if fmt == "json" else _render_markdown(reports, aggregate_tau)

    output_target = getattr(args, "output", None)
    if output_target is None or output_target == "-":
        print(rendered)
    else:
        out_path = Path(output_target)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(
            f"bench.py audit --vs-forge-oracle: report written to {out_path}",
            file=sys.stderr,
        )

    if skipped_commanders:
        print(f"skipped {skipped_commanders} commander(s) (fewer than 2 resolvable cards)", file=sys.stderr)

    return 0
