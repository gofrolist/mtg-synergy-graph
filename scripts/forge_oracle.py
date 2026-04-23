#!/usr/bin/env python3
"""``scripts/forge_oracle.py`` — offline Forge-second-oracle pipeline CLI.

Subcommands:
  build         Build ``data/forge_oracle.db`` from Forge precon decks.
  propose-rules Emit N forge-signal-ranked rule scaffold previews.

This script is offline infrastructure. It is NEVER invoked by the
inference path. The inference path's CI gate (``bench.py audit
--expect-identity``) and the structural grep fence (plan Unit 9)
guarantee this script's artifacts cannot leak into ``recommend.py``.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from mtg_synergy_graph.forge_oracle import config as fo_config
from mtg_synergy_graph.forge_oracle import ingest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _cmd_build(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stats = ingest.build_forge_oracle_db(
        synergy_db_path=Path(args.synergy_db),
        target_db_path=Path(args.target),
        min_decks_count=args.min_decks,
        smoothing_k=args.smoothing_k,
    )
    print(
        f"forge_oracle.db built: {stats.ppmi_rows_written} PPMI rows, "
        f"{stats.decks_parsed} decks parsed "
        f"({stats.decks_with_any_resolved_card} with known cards), "
        f"{stats.distinct_subkinds} distinct subkinds, "
        f"{stats.unknown_card_names} unknown card names"
    )
    return 0


def _cmd_propose_rules(args: argparse.Namespace) -> int:
    """Emit Markdown proposing N rules ranked by forge_signal-weighted impact.

    Iterates ``gap_report.rank_gaps`` proposals (Unit 6's re-ranking
    already incorporated there), picks the top N, and for each
    proposal whose template is registered in
    ``scripts/scaffold_rule._GENERATORS`` renders a preview of the
    generated helper + test + integration-patch lines. Proposals with
    no registered generator are still listed but flagged with
    ``needs_template`` so human reviewers see the gap without
    pretending the catalog covers it.

    Strict consumer: refuses to run when ``forge_oracle.db`` is
    missing or its stored config hash does not match current inputs.
    """
    forge_db = Path(args.forge_oracle_db)
    if not forge_db.is_file():
        print(
            f"error: forge_oracle.db not found at {forge_db}. Run `scripts/forge_oracle.py build` first.",
            file=sys.stderr,
        )
        return 2
    try:
        forge_conn = sqlite3.connect(forge_db)
    except sqlite3.DatabaseError as exc:
        print(f"error: forge_oracle.db unusable: {exc}", file=sys.stderr)
        return 2
    try:
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

    # Local imports — scripts/ is auto-prepended to sys.path when this
    # module runs as a script, but the imports are lazy to avoid
    # paying gap_report's load time in the build command.
    sys.path.insert(0, str(Path(__file__).parent))
    import gap_report as gr
    from scaffold_rule import _GENERATORS

    synergy_db = Path(args.synergy_db)
    if not synergy_db.is_file():
        print(f"error: synergy DB not found at {synergy_db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(synergy_db)
    conn.row_factory = sqlite3.Row
    try:
        proposals, stats_total, eligible_total = gr.rank_gaps(
            conn,
            forge_oracle_db=forge_db,
            max_activation=1.0,
            commanders_limit=0,
            warn_fn=lambda msg: print(msg, file=sys.stderr),
        )
        top = proposals[: args.top]
        rendered = _render_proposals_markdown(top, _GENERATORS, stats_total, eligible_total)
    finally:
        conn.close()

    output_target = getattr(args, "output", None)
    if output_target is None or output_target == "-":
        print(rendered)
    else:
        out_path = Path(output_target)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(f"forge_oracle.py propose-rules: report written to {out_path}", file=sys.stderr)
    return 0


def _render_proposals_markdown(
    proposals: list,  # list[gap_report.RuleProposal] — untyped to avoid a top-level import
    generators: dict,  # scaffold_rule._GENERATORS
    stats_total: int,
    eligible_total: int,
) -> str:
    """Render a ranked proposal list with scaffold previews inline.

    For each proposal whose template is in ``generators``, call the
    generator and embed a short preview of its artifacts. Proposals
    whose template is not registered are listed as ``needs_template``
    with the gap info but no scaffold.
    """
    lines: list[str] = []
    lines.append("# Forge-signal-ranked rule proposals")
    lines.append("")
    lines.append(
        "Auto-generated by `scripts/forge_oracle.py propose-rules`. "
        "Gaps ranked by `impact * forge_signal` (plan 002). Each "
        "proposal with a registered template shows a scaffold preview "
        "ready for human review + `scripts/scaffold_rule.py` invocation."
    )
    lines.append("")
    lines.append(f"**Total sub-cells scanned**: {stats_total}")
    lines.append(f"**Eligible gaps**: {eligible_total}")
    lines.append(f"**Shown below**: {len(proposals)}")
    lines.append("")

    if not proposals:
        lines.append("_No eligible gaps. Nothing to propose._")
        lines.append("")
        return "\n".join(lines)

    covered = sum(1 for p in proposals if p.template in generators)
    lines.append(f"**With registered generator**: {covered}")
    lines.append(f"**Awaiting new template**: {len(proposals) - covered}")
    lines.append("")

    for i, prop in enumerate(proposals, 1):
        pt, ev, sub = prop.gap.signature
        lines.append(f"## #{i}: `{pt}.{ev}[{sub or '*'}]`")
        lines.append(
            f"- **Impact**: {prop.gap.impact:.1f} "
            f"* forge_signal {prop.gap.forge_signal:.2f} "
            f"= **{prop.gap.weighted_impact:.1f}**"
        )
        lines.append(f"- **Template**: `{prop.template}`")
        lines.append(f"- **Reach**: {prop.gap.commanders} commanders, {prop.gap.activations} with any activation")
        if prop.gap.exemplars:
            lines.append(f"- **Exemplar commanders**: {', '.join(prop.gap.exemplars)}")
        lines.append(f"- **Rationale**: {prop.rationale}")

        gen = generators.get(prop.template)
        if gen is None:
            lines.append("- **Scaffold**: _no generator registered for this template yet._")
            lines.append("")
            continue
        try:
            artifacts = gen(prop)
        except Exception as exc:  # proposer must not crash on one bad template
            lines.append(f"- **Scaffold**: _generator raised_ `{type(exc).__name__}: {exc}`")
            lines.append("")
            continue
        lines.append(
            f"- **Scaffold preview**: helper `{artifacts.helper_module_path.name}`, "
            f"test `{artifacts.test_module_path.name}`, "
            f"rule_id `{artifacts.rule_id}`, bucket `{artifacts.bucket}`, "
            f"multiplier {artifacts.multiplier}"
        )
        lines.append(f"- **Run**: `uv run python scripts/scaffold_rule.py --template {prop.template}` to materialize.")
        lines.append("")

    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build", help="Build data/forge_oracle.db from Forge precon decks")
    build.add_argument(
        "--synergy-db",
        dest="synergy_db",
        default=str(_REPO_ROOT / "mtg_synergy.db"),
        help="Source mtg_synergy.db (must contain cards + port_nodes)",
    )
    build.add_argument(
        "--target",
        default=str(_REPO_ROOT / "data" / "forge_oracle.db"),
        help="Output oracle sidecar DB",
    )
    build.add_argument(
        "--min-decks",
        type=int,
        default=3,
        help="Minimum deck-cooccurrence count to persist a PPMI row (default 3)",
    )
    build.add_argument(
        "--smoothing-k",
        type=float,
        default=0.5,
        help="Laplace add-k smoothing constant (default 0.5)",
    )
    build.set_defaults(func=_cmd_build)

    propose = sub.add_parser(
        "propose-rules",
        help="Emit N rule scaffolds ranked by forge-signal-weighted impact",
    )
    propose.add_argument(
        "--synergy-db",
        dest="synergy_db",
        default=str(_REPO_ROOT / "mtg_synergy.db"),
        help="Source mtg_synergy.db (must contain cards + port_nodes)",
    )
    propose.add_argument(
        "--forge-oracle-db",
        dest="forge_oracle_db",
        default=str(_REPO_ROOT / "data" / "forge_oracle.db"),
        help="Forge oracle sidecar DB (must exist; hash must match current config)",
    )
    propose.add_argument("--top", type=int, default=20, help="Number of proposals to emit (default 20)")
    propose.add_argument(
        "--min-decks",
        type=int,
        default=3,
        help="Must match the --min-decks used at build time (default 3)",
    )
    propose.add_argument(
        "--smoothing-k",
        type=float,
        default=0.5,
        help="Must match the --smoothing-k used at build time (default 0.5)",
    )
    propose.add_argument(
        "--output",
        default=None,
        help="Write output to PATH instead of stdout. Use '-' for stdout.",
    )
    propose.set_defaults(func=_cmd_propose_rules)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
