#!/usr/bin/env python3
"""``scripts/forge_oracle.py`` — offline Forge-second-oracle pipeline CLI.

Subcommands:
  build       Build ``data/forge_oracle.db`` from Forge precon decks.
  inspect     (future) print PPMI + BoosterDraftAI score for one pair.
  propose-rules (future, Unit 8) emit N forge-signal-ranked scaffolds.
  upgrade     (future, Unit 5) bump Forge SHA + rebuild.

This script is offline infrastructure. It is NEVER invoked by the
inference path. The inference path's CI gate (``bench.py audit
--expect-identity``) and the structural grep fence (plan Unit 9)
guarantee this script's artifacts cannot leak into ``recommend.py``.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
