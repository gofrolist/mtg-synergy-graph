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
from mtg_synergy_graph.forge_oracle import ppmi as ppmi_math

if TYPE_CHECKING:
    from argparse import Namespace


def _nonneg_int(s: str) -> int:
    """argparse type callable: parse non-negative ``int``, else raise.

    Used for ``--trend-n`` so negative values (which silently produce
    empty output) are rejected at the CLI boundary with an actionable
    ``argparse.ArgumentTypeError``. Zero is accepted — a legitimate
    "render the header only" request.
    """
    try:
        value = int(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {s!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


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
    # Unit 5 of hidden-gem metric plan — print last N rows of
    # .audit/history.csv.
    "trend": _stubs.trend_stub,
    # Plan 002 Unit 7 — Forge CardRanker Kendall-τ sidecar.
    "vs_forge_oracle": _stubs.vs_forge_oracle_stub,
    # Plan 003 (content-embeddings) Unit 6 — rule-pair embedding dedup
    # diagnostic. Flags rule pairs whose candidate-activation sets are
    # near-parallel in embedding space. Read-only; never mutates the DB.
    "embedding_dedup": _stubs.embedding_dedup_stub,
    # Plan 2026-04-26-001 Unit 4 — Coordinate Ascent weight optimizer.
    # The stub here is overridden at runtime by ``bench/__init__.py`` via
    # ``register("optimize", handle_optimize)`` — same lazy-wiring pattern as
    # every other mode above. The real handler lives in
    # ``mtg_synergy_graph.bench.optimize.handle_optimize``.
    "optimize": _stubs.optimize_stub,
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
  BENCH_FORMAT   Override --format for the pre-commit hook. md | json | csv.
                 Default: md. The csv value is only meaningful for
                 `--trend`; the pre-commit hook treats csv as "skip
                 writing .audit/last.*" and emits a stderr warning.

Exit codes:
  0  Clean / identical to pinned baseline.
  1  Drift detected, or --repin dry-run (use --yes to confirm).
     For --optimize: driver-internal exception (DB I/O, JSON write failure).
  2  Usage / config error (missing DB, missing fixture, empty fixture,
     stale tensor under --optimize, fixture below minimum split size).
  3  --optimize only: planted-perturbation self-test failed (calibration
     issue — fix and rerun before trusting any future proposal).
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
        "--optimize",
        action="store_true",
        help="Run the Coordinate Ascent weight optimizer over "
        "_RULE_QUALITY_MULTIPLIER. Emits a candidate diff to "
        ".audit/optimize_proposal.json for human review. Never mutates "
        "data/scoring_weights.json — apply via --repin manually after "
        "reviewing the proposal.",
    )
    mode.add_argument(
        "--inspect-gems",
        action="store_true",
        help="Per-commander diff of hidden-gem sets between pinned and live. "
        "Shows lost/gained gems. Reads pinned fixture + re-scores live.",
    )
    mode.add_argument(
        "--trend",
        choices=("hidden_gems",),
        metavar="METRIC",
        help="Print the last N rows of .audit/history.csv. MVP supports "
        "METRIC=hidden_gems; argparse choices leaves room for additional "
        "metrics without breaking users.",
    )
    mode.add_argument(
        "--vs-forge-oracle",
        dest="vs_forge_oracle",
        action="store_true",
        help="Compare our top-N ranking to Forge CardRanker's ranking over "
        "the same candidate set. Reports aggregate Kendall τ + per-commander "
        "breakdown + top-10 divergences. Requires data/forge_oracle.db "
        "(plan 002 Unit 7). Tracking-only; does not gate commits.",
    )
    mode.add_argument(
        "--embedding-dedup",
        dest="embedding_dedup",
        action="store_true",
        help="Flag rule pairs whose candidate-activation sets are near-"
        "parallel in embedding space (read-only diagnostic). Requires "
        "card_embeddings to be populated — run `scripts/build_embeddings.py` "
        "first. See plan 2026-04-23-003 FR5.",
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
        choices=("md", "json", "csv"),
        default=None,
        help="Output format. Default: md (csv for --trend).",
    )
    audit.add_argument(
        "--trend-n",
        type=_nonneg_int,
        default=20,
        metavar="N",
        help="Number of rows to show for --trend. Default: 20. Must be >= 0. Meaningful only with --trend.",
    )
    audit.add_argument(
        "--history",
        metavar="PATH",
        default=".audit/history.csv",
        help="Path to the append-only history CSV. Default: .audit/history.csv.",
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
    # --vs-forge-oracle sidecar (plan 002 Unit 7) — forge_oracle.db path +
    # config knobs that must match the build's values so the stored hash
    # verifies.
    audit.add_argument(
        "--forge-oracle-db",
        dest="forge_oracle_db",
        metavar="PATH",
        default="data/forge_oracle.db",
        help="Used by --vs-forge-oracle only. Path to the forge_oracle.db sidecar. Default: data/forge_oracle.db.",
    )
    audit.add_argument(
        "--smoothing-k",
        dest="smoothing_k",
        type=float,
        default=ppmi_math.DEFAULT_SMOOTHING_K,
        help="Used by --vs-forge-oracle only. PPMI Laplace smoothing constant used when "
        "the sidecar was built. Must match `scripts/forge_oracle.py build --smoothing-k` "
        f"or the stored config hash will not verify. Default: {ppmi_math.DEFAULT_SMOOTHING_K}.",
    )
    audit.add_argument(
        "--min-decks",
        dest="min_decks",
        type=int,
        default=3,
        help="Used by --vs-forge-oracle only. Minimum-evidence threshold used when the "
        "sidecar was built. Must match `scripts/forge_oracle.py build --min-decks`. Default: 3.",
    )
    # --embedding-dedup flags (plan 003 Unit 6). Scoped by mode, not by
    # argparse — argparse doesn't natively gate flags by mutex-group
    # selection, matching how --limit / --inspect coexist today.
    audit.add_argument(
        "--threshold",
        dest="threshold",
        type=float,
        default=0.95,
        help="Used by --embedding-dedup only. Cosine-similarity cutoff for flagged rule pairs. Default: 0.95.",
    )
    audit.add_argument(
        "--min-activation",
        dest="min_activation",
        type=int,
        default=20,
        help="Used by --embedding-dedup only. Rules with fewer than this many "
        "distinct candidates in their activation set are dropped before "
        "pair comparison. Default: 20.",
    )
    # Plan 2026-04-26-001 Unit 4 — --optimize flag plumbing.
    audit.add_argument(
        "--max-sweeps",
        dest="max_sweeps",
        type=_nonneg_int,
        default=5,
        help="Used by --optimize only. Maximum number of full sweeps over _RULE_QUALITY_MULTIPLIER. Default: 5.",
    )
    audit.add_argument(
        "--seed",
        dest="seed",
        type=int,
        default=42,
        help="Used by --optimize only. Random seed for the train/held split. "
        "Non-default values are experimental — the proposal artifact loses "
        "comparability across runs with different seeds. Default: 42.",
    )
    audit.add_argument(
        "--no-self-test",
        dest="no_self_test",
        action="store_true",
        help="Used by --optimize only. Skip the FR10 planted-perturbation "
        "self-test. Use only when calibration was just validated; "
        "otherwise 'no improvement found' becomes unfalsifiable.",
    )
    audit.add_argument(
        "--proposal-path",
        dest="proposal_path",
        metavar="PATH",
        default=".audit/optimize_proposal.json",
        help="Used by --optimize only. Where to write the candidate diff. Default: .audit/optimize_proposal.json.",
    )
    audit.add_argument(
        "--optimize-history",
        dest="optimize_history",
        metavar="PATH",
        default=".audit/optimize_history.csv",
        help="Used by --optimize only. Append-only convergence log path. Default: .audit/optimize_history.csv.",
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
    if getattr(args, "trend", None) is not None:
        return "trend"
    if getattr(args, "vs_forge_oracle", False):
        return "vs_forge_oracle"
    if getattr(args, "embedding_dedup", False):
        return "embedding_dedup"
    if getattr(args, "optimize", False):
        return "optimize"
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

    # ``--format csv`` is only meaningful for ``--trend`` (the history
    # file itself is CSV). Silently falling back to markdown for every
    # other mode hid user typos; reject it at the CLI boundary so the
    # user sees the mismatch instead of wondering why the flag did
    # nothing.
    if getattr(args, "format", None) == "csv" and mode != "trend":
        parser.error("--format csv is only supported with --trend")

    # ``--commander`` filters a per-commander audit; it is meaningless
    # for ``--trend`` (which renders raw history rows without a
    # commander concept). Fail loudly instead of silently ignoring.
    if getattr(args, "commander", None) is not None and mode == "trend":
        parser.error("--commander is not supported with --trend")

    # ``--format`` is stored as ``None`` when not passed so we can
    # apply mode-specific defaults: ``--trend`` defaults to CSV (the
    # history file is already CSV), every other mode defaults to ``md``
    # (matches the legacy behaviour). Users still get ``md`` / ``json``
    # / ``csv`` explicit overrides.
    if getattr(args, "format", None) is None:
        args.format = "csv" if mode == "trend" else "md"

    # ``--trend-n`` only makes sense with ``--trend``. A lone
    # ``--trend-n`` is harmless — warn on stderr rather than erroring so
    # the user sees the flag had no effect.
    if mode != "trend" and getattr(args, "trend_n", 20) != 20:
        print(
            "bench.py audit: warning: --trend-n has no effect without --trend.",
            file=sys.stderr,
        )

    # ``--threshold`` / ``--min-activation`` only make sense with
    # ``--embedding-dedup``. Mirror the ``--trend-n`` pattern: warn on
    # stderr rather than erroring so the user sees the flag had no
    # effect.
    if mode != "embedding_dedup" and (
        getattr(args, "threshold", 0.95) != 0.95 or getattr(args, "min_activation", 20) != 20
    ):
        print(
            "bench.py audit: warning: --threshold and --min-activation have no effect without --embedding-dedup.",
            file=sys.stderr,
        )

    # ``--max-sweeps`` / ``--seed`` / ``--no-self-test`` / ``--proposal-path`` /
    # ``--optimize-history`` are companions to ``--optimize``. Warn on stderr
    # rather than erroring so the user sees the flag had no effect — same
    # pattern as ``--trend-n`` and ``--threshold`` above.
    if mode != "optimize":
        ignored = []
        if getattr(args, "max_sweeps", 5) != 5:
            ignored.append("--max-sweeps")
        if getattr(args, "seed", 42) != 42:
            ignored.append("--seed")
        if getattr(args, "no_self_test", False):
            ignored.append("--no-self-test")
        if getattr(args, "proposal_path", ".audit/optimize_proposal.json") != ".audit/optimize_proposal.json":
            ignored.append("--proposal-path")
        if getattr(args, "optimize_history", ".audit/optimize_history.csv") != ".audit/optimize_history.csv":
            ignored.append("--optimize-history")
        if ignored:
            print(
                f"bench.py audit: warning: {', '.join(ignored)} "
                f"{'has' if len(ignored) == 1 else 'have'} no effect without --optimize.",
                file=sys.stderr,
            )

    handler = _HANDLERS[mode]
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
