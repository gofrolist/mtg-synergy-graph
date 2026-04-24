#!/usr/bin/env python3
"""Pre-commit quality gate for new complement rules.

Catches the class of failure exemplified by ``ward_2_tribal`` (reverted
2026-04-24): rules that are technically correct and pass the golden-set
audit with Δ=0, but whose emissions are mechanically vacuous — they
only fill sparse pages for commanders that have no other rule firing,
producing flat-noise recommendations at uniform low scores.

The existing gates — ``bench.py audit`` (100 golden commanders),
``--expect-identity`` (bitwise equality), ``--rule`` (per-rule ablation
via persisted tensor) — all live inside the golden-set bubble and
cannot see long-tail rules whose target commanders are outside it.

This script measures two signals per rule, entirely from live scoring:

**Gate A — pre-existing coverage.** For each commander the rule fires
on, count distinct *other* rule_ids already firing. If the median is
<3, the rule is filling a mechanical vacuum rather than amplifying
signal — warn. If <1, target commanders genuinely have no other rules
— reject absent a compelling coverage story.

**Gate B — top-30 score dispersion on target commanders.** For each
target, compute coefficient of variation (stdev / |mean|) of the top-30
total scores. Low CV = flat noise, order near-arbitrary. Reject <0.02,
warn <0.05.

Both gates must trip for REJECT. Either alone → WARN.

Usage::

    uv run scripts/rule_quality_gate.py --rule prowess_tribal
    uv run scripts/rule_quality_gate.py --all-declarative
    uv run scripts/rule_quality_gate.py --all-declarative --format json

Exit codes: 0 = all PASS, 1 = at least one WARN, 2 = at least one REJECT.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mtg_synergy_graph.complement_rules import find_all_complements  # noqa: E402
from mtg_synergy_graph.complement_rules.core import PortRow, load_ports_for_set  # noqa: E402
from mtg_synergy_graph.complement_rules.registry import (  # noqa: E402
    DECLARATIVE_RULE_IDS,
    RULE_GATES,
)
from mtg_synergy_graph.db import open_db  # noqa: E402
from mtg_synergy_graph.engine import SynergyEngine  # noqa: E402
from mtg_synergy_graph.port_graph.interpreter import RuleInterpreter  # noqa: E402

#: Pre-existing coverage thresholds (median distinct other rule_ids).
_WARN_COVERAGE = 3
_REJECT_COVERAGE = 1

#: Top-30 score CV thresholds.
_WARN_CV = 0.05
_REJECT_CV = 0.02

Verdict = Literal["PASS", "WARN", "REJECT"]


@dataclass(frozen=True, slots=True)
class RuleQualityResult:
    rule_id: str
    n_targets: int
    median_pre_existing_coverage: float
    median_top30_cv: float
    reasons: tuple[str, ...]
    verdict: Verdict


def _load_gate_predicate(conn, rule_id: str):
    """Return a port-level predicate callable for ``rule_id``, or None.

    Prefers the Python ``RULE_GATES`` entry (cheap) and falls back to
    a ``RuleInterpreter`` built from the live ``rules`` table, so the
    gate resolution works for rules that exist only in the DB (e.g.,
    during ad-hoc verification of a candidate rule) as well as for the
    production ``DECLARATIVE_RULE_IDS`` set.
    """
    for g in RULE_GATES:
        if g.rule_id == rule_id:
            return g.predicate
    interp = RuleInterpreter(conn)
    for c in interp._compiled:
        if c.row.rule_id == rule_id:
            return c.gate
    return None


def _find_target_commanders(conn, rule_id: str) -> list[str]:
    """Find every legal commander whose ports match the rule's gate.

    Uses the compiled gate predicate against each commander's ports —
    port-level matching, no SQL-per-rule and no full ``find_all_complements``
    scan. Fast enough to run in batch mode.
    """
    predicate = _load_gate_predicate(conn, rule_id)
    if predicate is None:
        return []
    commanders = [
        r[0] for r in conn.execute("SELECT name FROM cards WHERE legal_commander = 1 ORDER BY name").fetchall()
    ]
    # Load all ports for the union in one pass — ``load_ports_for_set``
    # is already indexed and returns ``PortRow`` objects matching the
    # predicate's expected shape.
    ports_by_cmdr: dict[str, list[PortRow]] = {}
    for name in commanders:
        ports_by_cmdr[name] = load_ports_for_set(conn, [name])
    targets: list[str] = []
    for name, ports in ports_by_cmdr.items():
        if any(predicate(p) for p in ports):
            targets.append(name)
    return targets


def _commander_rule_counts(conn, cmdr: str) -> set[str]:
    """Return the set of rule_ids that fire on this commander."""
    comps = find_all_complements(conn, [cmdr])
    return {c.rule_id for c in comps}


def _score_top30_cv(eng: SynergyEngine, cmdr: str) -> float:
    """Coefficient of variation of total scores across top-30."""
    page = eng.page(cmdr, limit=30)
    scores = [r.total_score for r in page.items]
    if len(scores) < 3:
        return 0.0
    mean = statistics.mean(scores)
    if abs(mean) < 1e-9:
        return 0.0
    return statistics.stdev(scores) / abs(mean)


def _verdict_for(coverage_median: float, cv_median: float) -> tuple[Verdict, tuple[str, ...]]:
    reasons: list[str] = []
    cov_reject = coverage_median < _REJECT_COVERAGE
    cov_warn = coverage_median < _WARN_COVERAGE
    cv_reject = cv_median < _REJECT_CV
    cv_warn = cv_median < _WARN_CV

    if cov_reject:
        reasons.append(
            f"median pre-existing coverage {coverage_median:.1f} < {_REJECT_COVERAGE} "
            "(targets have essentially no other rules firing — pure vacuum fill)"
        )
    elif cov_warn:
        reasons.append(
            f"median pre-existing coverage {coverage_median:.1f} < {_WARN_COVERAGE} "
            "(targets are thinly covered — rule may dominate)"
        )

    if cv_reject:
        reasons.append(
            f"median top-30 CV {cv_median:.3f} < {_REJECT_CV} "
            "(recommendations are flat noise — order is near-arbitrary)"
        )
    elif cv_warn:
        reasons.append(f"median top-30 CV {cv_median:.3f} < {_WARN_CV} (recommendations are weakly differentiated)")

    if cov_reject and cv_reject:
        return "REJECT", tuple(reasons)
    if cov_warn or cv_warn:
        return "WARN", tuple(reasons)
    return "PASS", ()


def evaluate_rule(
    conn,
    eng: SynergyEngine,
    rule_id: str,
    *,
    sample_size: int | None = None,
    cache_coverage: dict[str, set[str]] | None = None,
    cache_cv: dict[str, float] | None = None,
) -> RuleQualityResult:
    """Run both gates for ``rule_id`` and return the combined result."""
    if cache_coverage is None:
        cache_coverage = {}
    if cache_cv is None:
        cache_cv = {}

    targets = _find_target_commanders(conn, rule_id)
    if not targets:
        return RuleQualityResult(
            rule_id=rule_id,
            n_targets=0,
            median_pre_existing_coverage=0.0,
            median_top30_cv=0.0,
            reasons=("rule gate matches zero legal commanders",),
            verdict="REJECT",
        )

    eval_targets = targets if sample_size is None else targets[:sample_size]

    coverage_counts: list[int] = []
    cv_values: list[float] = []
    for c in eval_targets:
        if c not in cache_coverage:
            cache_coverage[c] = _commander_rule_counts(conn, c)
        if c not in cache_cv:
            cache_cv[c] = _score_top30_cv(eng, c)
        coverage_counts.append(len(cache_coverage[c] - {rule_id}))
        cv_values.append(cache_cv[c])

    coverage_median = statistics.median(coverage_counts)
    cv_median = statistics.median(cv_values)
    verdict, reasons = _verdict_for(coverage_median, cv_median)

    return RuleQualityResult(
        rule_id=rule_id,
        n_targets=len(targets),
        median_pre_existing_coverage=coverage_median,
        median_top30_cv=cv_median,
        reasons=reasons,
        verdict=verdict,
    )


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--rule", help="Single rule_id to evaluate")
    g.add_argument(
        "--all-declarative",
        action="store_true",
        help="Evaluate every rule_id in DECLARATIVE_RULE_IDS",
    )
    g.add_argument(
        "--all",
        action="store_true",
        help="Evaluate DECLARATIVE_RULE_IDS union RULE_GATES.rule_id",
    )
    p.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Cap target commanders per rule (default: all). Use for quick iteration.",
    )
    p.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    if not args.db.exists():
        print(f"error: --db path does not exist: {args.db}", file=sys.stderr)
        return 2

    conn = open_db(args.db)
    eng = SynergyEngine(str(args.db))

    if args.rule:
        rule_ids = [args.rule]
    elif args.all_declarative:
        rule_ids = sorted(DECLARATIVE_RULE_IDS)
    else:  # args.all
        rule_ids = sorted(DECLARATIVE_RULE_IDS | {g.rule_id for g in RULE_GATES})

    cache_coverage: dict[str, set[str]] = {}
    cache_cv: dict[str, float] = {}

    results: list[RuleQualityResult] = []
    for rid in rule_ids:
        print(f"[gate] evaluating {rid}...", file=sys.stderr, flush=True)
        res = evaluate_rule(
            conn,
            eng,
            rid,
            sample_size=args.sample,
            cache_coverage=cache_coverage,
            cache_cv=cache_cv,
        )
        results.append(res)

    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "rule_id": r.rule_id,
                        "n_targets": r.n_targets,
                        "median_pre_existing_coverage": r.median_pre_existing_coverage,
                        "median_top30_cv": r.median_top30_cv,
                        "verdict": r.verdict,
                        "reasons": list(r.reasons),
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        print(f"{'rule_id':<48} {'targets':>7} {'cov':>6} {'cv':>6}  verdict")
        print("-" * 80)
        for r in results:
            print(
                f"{r.rule_id:<48} {r.n_targets:>7} "
                f"{r.median_pre_existing_coverage:>6.1f} "
                f"{r.median_top30_cv:>6.3f}  {r.verdict}"
            )
            for reason in r.reasons:
                print(f"    - {reason}")

    if any(r.verdict == "REJECT" for r in results):
        return 2
    if any(r.verdict == "WARN" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
