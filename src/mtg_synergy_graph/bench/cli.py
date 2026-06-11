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


def _alpha(s: str) -> float:
    """argparse type callable: parse a [0, 1] alpha-blend weight."""
    try:
        value = float(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a float, got {s!r}") from exc
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"alpha must be in [0, 1], got {value}")
    return value


def _ratio(s: str) -> float:
    """argparse type callable: parse a (0, 1) train/held ratio (open interval)."""
    try:
        value = float(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a float, got {s!r}") from exc
    if not 0.0 < value < 1.0:
        raise argparse.ArgumentTypeError(f"ratio must be in (0, 1), got {value}")
    return value


def _positive_float(s: str) -> float:
    """argparse type callable: parse a strictly-positive float (excludes zero and negatives)."""
    try:
        value = float(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a float, got {s!r}") from exc
    if value <= 0.0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


def _nonneg_float(s: str) -> float:
    """argparse type callable: parse a non-negative float (zero accepted)."""
    try:
        value = float(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a float, got {s!r}") from exc
    if value < 0.0:
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
    # BM25 IDF probe (plan 2026-05-04-001) — per-commander NDCG@30
    # diff handler. Stubbed here, real handler at
    # ``mtg_synergy_graph.bench.per_commander_ndcg.handle_per_commander_ndcg``.
    "per_commander_ndcg": _stubs.per_commander_ndcg_stub,
    # Divergence forensics (plan 2026-06-10-001 Unit 4) — per-miss
    # failure taxonomy + metric sidecars. Stubbed here, real handler at
    # ``mtg_synergy_graph.bench.forensics_report.handle_forensics``.
    "forensics": _stubs.forensics_stub,
    # Plan 2026-06-10-001 Unit 5 — ``--trend forensics`` reader over the
    # sibling forensics history CSV (boundary-marker grouping). Stubbed
    # here, real handler at
    # ``mtg_synergy_graph.bench.forensics_history.handle_trend_forensics``.
    "trend_forensics": _stubs.trend_forensics_stub,
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
        "src/mtg_synergy_graph/data/scoring_weights.json — apply via "
        "--repin manually after reviewing the proposal.",
    )
    mode.add_argument(
        "--inspect-gems",
        action="store_true",
        help="Per-commander diff of hidden-gem sets between pinned and live. "
        "Shows lost/gained gems. Reads pinned fixture + re-scores live.",
    )
    mode.add_argument(
        "--trend",
        choices=("hidden_gems", "forensics"),
        metavar="METRIC",
        help="Print the last N rows of a history CSV. METRIC=hidden_gems "
        "reads .audit/history.csv (CSV default); METRIC=forensics reads "
        ".audit/forensics_history.csv (md default, with "
        "(config_hash, snapshot) boundary markers; override the path via "
        "--forensics-history).",
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
    mode.add_argument(
        "--per-commander-ndcg",
        dest="per_commander_ndcg",
        action="store_true",
        help="Per-commander NDCG@30 deltas (live - pinned) sorted ascending. "
        "Read-only diagnostic. Used by audit-gated probes that need a "
        "per-commander prerequisite check (e.g., BM25 IDF probe — any "
        "commander losing >0.05 NDCG@30 routes to DECLINE).",
    )
    mode.add_argument(
        "--forensics",
        action="store_true",
        help="Divergence forensics: classify every missed EDHREC label card "
        "into failure buckets (NEAR_MISS/OUTRANKED/FILTERED/DATA_GAP/"
        "NO_RULES) with NDCG/raw-DCG sidecars and the justified-divergence "
        "view. Read-only diagnostic; writes .audit/forensics.md by default. "
        "Requires the persisted tensor at the current config_hash and "
        "--edhrec-db (default data/tags.db).",
    )

    # Companion flag for --forensics (NOT a mode): plan 2026-06-10-001
    # Unit 6 (R8) one-off tiebreaker-ablation measurement.
    audit.add_argument(
        "--ablate-tiebreak",
        dest="ablate_tiebreak",
        action="store_true",
        help="Used by --forensics only. One-off tiebreaker ablation (R8): "
        "re-sort the captured rankings under the weak (-total_score, name) "
        "and strong (-total_score, cmc, name) replacement keys and report "
        "the bracketed NDCG@30 delta range vs the production key. Emits a "
        "ready-to-paste RULE_HISTORY entry when the upper bound of unearned "
        "EDHREC tiebreak credit exceeds 0.01. Pure re-sort — no scoring "
        "change; the forensics history row is not extended.",
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
        "--forensics-history",
        dest="forensics_history",
        metavar="PATH",
        default=".audit/forensics_history.csv",
        help="Used by --forensics (append target) and --trend forensics "
        "(read source) only. Path to the append-only forensics history "
        "CSV. Default: .audit/forensics_history.csv.",
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
    # OptimizerConfig knobs exposed for agent-driven sweep pipelines.
    # Defaults mirror OptimizerConfig in src/mtg_synergy_graph/bench/optimize.py;
    # change defaults THERE not here, otherwise the two drift.
    audit.add_argument(
        "--alpha",
        dest="alpha",
        type=_alpha,
        default=0.5,
        help="Used by --optimize only. Composite-objective blend: "
        "alpha*mean_ndcg + (1-alpha)*hidden_gem_hit_rate. "
        "alpha=0 → gem-only; alpha=1 → nDCG-only. Default: 0.5.",
    )
    audit.add_argument(
        "--grid",
        dest="grid",
        type=_positive_float,
        nargs="+",
        default=[0.5, 0.75, 1.25, 1.5, 2.0],
        metavar="MULT",
        help="Used by --optimize only. Multiplicative perturbation grid. "
        "Each rule is evaluated at current_value*mult for each mult. "
        "Default: 0.5 0.75 1.25 1.5 2.0.",
    )
    audit.add_argument(
        "--eps-step",
        dest="eps_step",
        type=_nonneg_float,
        default=0.005,
        help="Used by --optimize only. Held-out drop tolerance per accepted "
        "step. Steps with held composite worse than current by more than "
        "this are rejected. Default: 0.005.",
    )
    audit.add_argument(
        "--eps-cumulative",
        dest="eps_cumulative",
        type=_nonneg_float,
        default=0.005,
        help="Used by --optimize only. Held-out drop tolerance accumulated "
        "across all accepts in a sweep. Trips the drift-revert when "
        "cumulative held drop exceeds this. Default: 0.005.",
    )
    audit.add_argument(
        "--clamp-min",
        dest="clamp_min",
        type=_positive_float,
        default=0.01,
        help="Used by --optimize only. Lower bound on rule multipliers. "
        "Grid cells clamped at this floor. Default: 0.01.",
    )
    audit.add_argument(
        "--clamp-max",
        dest="clamp_max",
        type=_positive_float,
        default=5.0,
        help="Used by --optimize only. Upper bound on rule multipliers. "
        "Grid cells clamped at this ceiling. Default: 5.0.",
    )
    audit.add_argument(
        "--train-ratio",
        dest="train_ratio",
        type=_ratio,
        default=0.8,
        help="Used by --optimize only. Train fraction in the train/held split. Held = 1 - train_ratio. Default: 0.8.",
    )
    audit.add_argument(
        "--wall-clock-seconds",
        dest="wall_clock_seconds",
        type=_positive_float,
        default=300.0,
        help="Used by --optimize only. Wall-clock budget. Sweep aborts mid-loop "
        "when exceeded; partial_sweep=True in the proposal. Default: 300.0.",
    )
    audit.add_argument(
        "--self-test-seed",
        dest="self_test_seed",
        type=int,
        default=7,
        help="Used by --optimize only. Random seed for the planted-perturbation "
        "self-test (rule selection). Useful for reproducing flaky self-test "
        "failures. Default: 7.",
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
        # METRIC selects the handler slot: ``forensics`` reads the
        # sibling forensics history CSV (plan 2026-06-10-001 Unit 5);
        # everything else stays on the original hidden_gems reader.
        return "trend_forensics" if args.trend == "forensics" else "trend"
    if getattr(args, "vs_forge_oracle", False):
        return "vs_forge_oracle"
    if getattr(args, "embedding_dedup", False):
        return "embedding_dedup"
    if getattr(args, "optimize", False):
        return "optimize"
    if getattr(args, "per_commander_ndcg", False):
        return "per_commander_ndcg"
    if getattr(args, "forensics", False):
        return "forensics"
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

    # ``--format csv`` is only meaningful for the ``--trend`` modes (the
    # history files themselves are CSV). Silently falling back to
    # markdown for every other mode hid user typos; reject it at the
    # CLI boundary so the user sees the mismatch instead of wondering
    # why the flag did nothing.
    if getattr(args, "format", None) == "csv" and mode not in ("trend", "trend_forensics"):
        parser.error("--format csv is only supported with --trend")

    # ``--commander`` filters a per-commander audit; it is meaningless
    # for ``--trend`` (which renders raw history rows without a
    # commander concept). Fail loudly instead of silently ignoring.
    if getattr(args, "commander", None) is not None and mode in ("trend", "trend_forensics"):
        parser.error("--commander is not supported with --trend")

    # ``--format`` is stored as ``None`` when not passed so we can
    # apply mode-specific defaults: ``--trend hidden_gems`` defaults to
    # CSV (the history file is already CSV), every other mode —
    # including ``--trend forensics``, whose boundary-marker view reads
    # best as a table — defaults to ``md`` (matches the legacy
    # behaviour). Users still get ``md`` / ``json`` / ``csv`` explicit
    # overrides.
    if getattr(args, "format", None) is None:
        args.format = "csv" if mode == "trend" else "md"

    # ``--trend-n`` only makes sense with the ``--trend`` modes. A lone
    # ``--trend-n`` is harmless — warn on stderr rather than erroring so
    # the user sees the flag had no effect.
    if mode not in ("trend", "trend_forensics") and getattr(args, "trend_n", 20) != 20:
        print(
            "bench.py audit: warning: --trend-n has no effect without --trend.",
            file=sys.stderr,
        )

    # ``--forensics-history`` is the append target for ``--forensics``
    # and the read source for ``--trend forensics``. Same
    # warn-don't-error companion-flag pattern as ``--trend-n``.
    if (
        mode not in ("forensics", "trend_forensics")
        and getattr(args, "forensics_history", ".audit/forensics_history.csv") != ".audit/forensics_history.csv"
    ):
        print(
            "bench.py audit: warning: --forensics-history has no effect without --forensics or --trend forensics.",
            file=sys.stderr,
        )

    # ``--ablate-tiebreak`` is a companion to ``--forensics`` (plan
    # 2026-06-10-001 Unit 6). Same warn-don't-error pattern as
    # ``--trend-n``.
    if mode != "forensics" and getattr(args, "ablate_tiebreak", False):
        print(
            "bench.py audit: warning: --ablate-tiebreak has no effect without --forensics.",
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

    # All --optimize companion flags warn (rather than error) when used
    # without --optimize — same pattern as ``--trend-n`` and ``--threshold``.
    # Each tuple is (attr_name, default_value, flag_name); compare attr
    # against default via != to detect "user supplied a non-default value".
    if mode != "optimize":
        _OPTIMIZE_COMPANION_FLAGS = (
            ("max_sweeps", 5, "--max-sweeps"),
            ("seed", 42, "--seed"),
            ("no_self_test", False, "--no-self-test"),
            ("proposal_path", ".audit/optimize_proposal.json", "--proposal-path"),
            ("optimize_history", ".audit/optimize_history.csv", "--optimize-history"),
            ("alpha", 0.5, "--alpha"),
            ("eps_step", 0.005, "--eps-step"),
            ("eps_cumulative", 0.005, "--eps-cumulative"),
            ("clamp_min", 0.01, "--clamp-min"),
            ("clamp_max", 5.0, "--clamp-max"),
            ("train_ratio", 0.8, "--train-ratio"),
            ("wall_clock_seconds", 300.0, "--wall-clock-seconds"),
            ("self_test_seed", 7, "--self-test-seed"),
        )
        ignored = [flag for attr, default, flag in _OPTIMIZE_COMPANION_FLAGS if getattr(args, attr, default) != default]
        # --grid is a list, can't use the simple-equality comparison.
        if getattr(args, "grid", None) is not None and list(args.grid) != [0.5, 0.75, 1.25, 1.5, 2.0]:
            ignored.append("--grid")
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
