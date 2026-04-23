"""Argparse-based CLI dispatcher for ``bench.py``.

The dispatcher keeps Unit 1 thin: it parses arguments, validates
mutually-exclusive flags, and delegates to a registered handler. Unit 2+
replace each stub in the ``_HANDLERS`` table with a real implementation.

Design choices
--------------
* One subcommand (``audit``) with a family of flags instead of multiple
  subcommands. The flags are conceptually modes on the same pipeline
  (compare live tensor against pinned baseline), not independent
  workflows.
* ``--rule``, ``--inspect``, ``--collinearity``, ``--repin``,
  ``--expect-identity`` are mutually exclusive: each selects one mode.
* Handler table lookup at dispatch time so Unit 2+ can override a slot
  by reassigning ``_HANDLERS[key]`` from their module's ``__init__``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from mtg_synergy_graph.bench import _stubs

if TYPE_CHECKING:
    from argparse import Namespace

#: Handler table. Unit 2+ reassign these to real implementations.
_HANDLERS: dict[str, Callable[[Namespace], int]] = {
    "audit": _stubs.audit_stub,
    "repin": _stubs.repin_stub,
    "expect_identity": _stubs.expect_identity_stub,
    "inspect": _stubs.inspect_stub,
    "rule": _stubs.rule_stub,
    "collinearity": _stubs.collinearity_stub,
    # Unit 6 of plan 003 — report UNKNOWN-kind port_nodes rows
    # ranked by distinct cards × EDHREC rank weight.
    "unknowns": _stubs.unknowns_stub,
    # Unit 4 of hidden-gem metric plan — per-commander diff of
    # hidden-gem sets between pinned and live.
    "inspect_gems": _stubs.inspect_gems_stub,
}


def register(mode: str, handler: Callable[[Namespace], int]) -> None:
    """Register a real handler for a mode. Called by Unit 2+ modules."""

    if mode not in _HANDLERS:
        raise KeyError(f"unknown mode {mode!r}; valid modes: {sorted(_HANDLERS)}")
    _HANDLERS[mode] = handler


_ENV_VAR_EPILOG = """\
Environment variables (hook mode):
  BENCH_DB       Override --db for the pre-commit hook. Default: data/synergy.db.
  BENCH_FIXTURE  Override --fixture for the pre-commit hook.
                 Default: tests/fixtures/golden_set_run.json.
  BENCH_FORMAT   Override --format for the pre-commit hook. md | json. Default: md.

Exit codes:
  0  Clean / identical to pinned baseline.
  1  Drift detected, or --repin dry-run (use --yes to confirm).
  2  Usage / config error (missing DB, missing fixture, empty fixture).
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench.py",
        description=(
            "Unified eval harness for the MTG Synergy Graph scoring path. "
            "Replaces _audit_rule_impact.py, golden_set_track.py, "
            "compare_edhrec.py, weight_grid_search.py, broad_set_track.py."
        ),
        epilog=_ENV_VAR_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    audit = sub.add_parser(
        "audit",
        help=("Run audit on the 100-commander golden set against the pinned reference fixture."),
    )

    # Mode flags are mutually exclusive. Default (no flag) = full audit.
    mode = audit.add_mutually_exclusive_group()
    mode.add_argument(
        "--rule",
        metavar="RULE_ID",
        help="Per-rule ablation: diff the pinned baseline vs the same scores with RULE_ID disabled.",
    )
    mode.add_argument(
        "--inspect",
        metavar="RULE_ID",
        help="Inspect RULE_ID's contribution rows in the persisted tensor, sorted by |contribution| desc.",
    )
    mode.add_argument(
        "--collinearity",
        action="store_true",
        help="Emit pairwise VIF + Pearson correlation across all registered "
        "rules. Flags pairs with VIF > 5 AND |r| > 0.8.",
    )
    mode.add_argument(
        "--repin",
        action="store_true",
        help="Rebuild the pinned reference fixture from the current working tree. Requires --yes.",
    )
    mode.add_argument(
        "--expect-identity",
        action="store_true",
        help="Assert bitwise-identical per-(cmdr, cand) scores against the "
        "pinned baseline. Used to verify pure-refactor changes.",
    )
    mode.add_argument(
        "--unknowns",
        action="store_true",
        help="Report port_nodes rows with node_kind='UNKNOWN', ranked by "
        "distinct_cards x EDHREC rank weight. Surfaces novel Forge "
        "port shapes that need canonical-vocabulary coverage.",
    )
    mode.add_argument(
        "--inspect-gems",
        action="store_true",
        help="Per-commander diff of hidden-gem sets between pinned and live. "
        "Shows lost/gained gems. Reads pinned fixture + re-scores live.",
    )

    # Shared flags.
    audit.add_argument(
        "--commander",
        metavar="NAME",
        help="Restrict the audit to a single commander (by name).",
    )
    audit.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max rows to show for --inspect output. Default: 100.",
    )
    audit.add_argument(
        "--format",
        choices=("md", "json"),
        default="md",
        help="Output format. Default: md.",
    )
    audit.add_argument(
        "--output",
        metavar="PATH",
        help="Write output to PATH instead of stdout. Use '-' for stdout.",
    )
    audit.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation for --repin (overwrites the pinned fixture).",
    )
    audit.add_argument(
        "--db",
        metavar="PATH",
        default="data/synergy.db",
        help="Path to the SQLite synergy DB. Default: data/synergy.db "
        "(matches the legacy scripts and scripts/import_cardsfolder.py output).",
    )
    audit.add_argument(
        "--fixture",
        metavar="PATH",
        default="tests/fixtures/golden_set_run.json",
        help="Path to the pinned reference fixture. Default: tests/fixtures/golden_set_run.json.",
    )
    audit.add_argument(
        "--edhrec-db",
        metavar="PATH",
        default="data/tags.db",
        help="Path to the EDHREC synergy DB. Used by --inspect-gems to "
        "rebuild live hidden-gem data. Default: data/tags.db.",
    )

    return parser


def _resolve_mode(args: Namespace) -> str:
    """Select which handler table slot the user's flags select."""

    if args.rule is not None:
        return "rule"
    if args.inspect is not None:
        return "inspect"
    if args.collinearity:
        return "collinearity"
    if args.repin:
        return "repin"
    if args.expect_identity:
        return "expect_identity"
    if args.unknowns:
        return "unknowns"
    if args.inspect_gems:
        return "inspect_gems"
    return "audit"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # The argparse `dest` for the top-level subcommand is `subcommand`.
    # There is only one subcommand at MVP (`audit`); adding sibling
    # subcommands later (e.g. `compare-edhrec`) is straightforward.
    if args.subcommand != "audit":
        parser.error(f"unknown subcommand: {args.subcommand!r}")

    mode = _resolve_mode(args)
    handler = _HANDLERS[mode]
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
