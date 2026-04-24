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

Three checks per rule, entirely from live scoring:

**Gate A — pre-existing coverage.** For each commander the rule fires
on, count distinct *other* rule_ids already firing. <1 = vacuum fill;
<3 = thinly covered.

**Gate B — top-30 score dispersion on target commanders.** Coefficient
of variation of top-30 total scores. <0.02 = flat noise; <0.05 =
weakly differentiated.

**Gate C — EDHREC NDCG delta on target commanders.** For each target
commander with EDHREC coverage, compare top-30 recommendations WITH
the rule enabled vs WITH the rule disabled against EDHREC's curated
Hi-Syn / OnPage sets. Positive delta = rule improves recommendations
relative to ground truth. Negative delta = rule makes them worse.
Near-zero delta = rule is filler — recommendations change but match
EDHREC no better (or worse) than without the rule. This closes the
Ward:2 blind spot: the scaffold→gate→audit loop used to declare a
rule "safe" based solely on golden-set Δ=0, which only sees 100 of
2,761 EDHREC-covered commanders. Gate C measures NDCG-style signal
on the rule's own target population.

Verdict combines all three:

* ``REJECT`` — Gate A and B both in reject band, or Gate C shows
  negative EDHREC delta (rule *degrades* recommendations).
* ``WARN`` — any gate in warn band, or Gate C near-zero when A/B
  otherwise look clean (classical filler signature).
* ``PASS`` — all clear.

Usage::

    uv run scripts/rule_quality_gate.py --rule prowess_tribal
    uv run scripts/rule_quality_gate.py --rule prowess_tribal --no-ndcg  # skip Gate C
    uv run scripts/rule_quality_gate.py --all-declarative                # Gate C off by default (slow)
    uv run scripts/rule_quality_gate.py --all-declarative --ndcg         # include Gate C

Exit codes: 0 = all PASS, 1 = at least one WARN, 2 = at least one REJECT.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mtg_synergy_graph.complement_rules import core as _core  # noqa: E402
from mtg_synergy_graph.complement_rules import find_all_complements  # noqa: E402
from mtg_synergy_graph.complement_rules._interpreter_cache import (  # noqa: E402
    clear_interpreter_cache,
)
from mtg_synergy_graph.complement_rules.core import PortRow, load_ports_for_set  # noqa: E402
from mtg_synergy_graph.complement_rules.registry import (  # noqa: E402
    DECLARATIVE_RULE_IDS,
    RULE_GATES,
)
from mtg_synergy_graph.db import open_db  # noqa: E402
from mtg_synergy_graph.engine import SynergyEngine  # noqa: E402
from mtg_synergy_graph.graph_engine import clear_ports_cache  # noqa: E402
from mtg_synergy_graph.port_graph.interpreter import RuleInterpreter  # noqa: E402
from mtg_synergy_graph.validate import commander_to_slug  # noqa: E402

#: Pre-existing coverage thresholds (median distinct other rule_ids).
_WARN_COVERAGE = 3
_REJECT_COVERAGE = 1

#: Top-30 score CV thresholds.
_WARN_CV = 0.05
_REJECT_CV = 0.02

#: Gate C NDCG delta thresholds — measured as (Hi-Syn + 0.25*OnPage)
#: delta per commander, averaged across targets with EDHREC coverage.
_REJECT_NDCG_DELTA = -0.10  # rule meaningfully degrades vs without
_WARN_NDCG_NEUTRAL = 0.05  # rule barely changes ground-truth match — filler

Verdict = Literal["PASS", "WARN", "REJECT"]


@dataclass(frozen=True, slots=True)
class RuleQualityResult:
    rule_id: str
    n_targets: int
    median_pre_existing_coverage: float
    median_top30_cv: float
    # Gate C fields — populated only when --ndcg is enabled AND the
    # rule has at least one target commander with EDHREC coverage.
    ndcg_evaluated: bool
    n_edhrec_targets: int
    mean_hisyn_delta: float  # with-rule minus without-rule
    mean_onpage_delta: float
    reasons: tuple[str, ...]
    verdict: Verdict


# ----------------------------------------------------------------------
# Gate predicate resolution
# ----------------------------------------------------------------------


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


def _legendary_creature_names(edhrec_conn: sqlite3.Connection | None) -> set[str] | None:
    """Return the set of legendary creature names from tags.db, or None.

    ``synergy.db.cards.legal_commander`` means "legal in Commander
    *format*", which includes Sol Ring and every non-commander staple.
    Actual commanders need ``type_line`` info that only tags.db has
    (``Legendary Creature — ...``). When tags.db isn't available, we
    fall back to the format-legal filter and accept some pollution.
    """
    if edhrec_conn is None:
        return None
    return {
        r[0]
        for r in edhrec_conn.execute("SELECT name FROM cards WHERE type_line LIKE '%Legendary%Creature%'").fetchall()
    }


def _find_target_commanders(
    conn,
    rule_id: str,
    *,
    legendary_names: set[str] | None = None,
) -> list[str]:
    """Find every legal commander whose ports match the rule's gate.

    Results are ordered by ``edhrec_rank ASC`` (popular first) so
    ``--sample N`` biases toward commanders with EDHREC coverage.
    NULL ranks sort last so obscure cards don't starve Gate C of
    ground-truth comparisons.
    """
    predicate = _load_gate_predicate(conn, rule_id)
    if predicate is None:
        return []
    rows = conn.execute(
        "SELECT name FROM cards WHERE legal_commander = 1 ORDER BY edhrec_rank IS NULL, edhrec_rank ASC, name"
    ).fetchall()
    if legendary_names is not None:
        commanders = [r[0] for r in rows if r[0] in legendary_names]
    else:
        commanders = [r[0] for r in rows]
    ports_by_cmdr: dict[str, list[PortRow]] = {}
    for name in commanders:
        ports_by_cmdr[name] = load_ports_for_set(conn, [name])
    targets: list[str] = []
    for name, ports in ports_by_cmdr.items():
        if any(predicate(p) for p in ports):
            targets.append(name)
    return targets


# ----------------------------------------------------------------------
# Gate A + B primitives
# ----------------------------------------------------------------------


def _commander_rule_counts(conn, cmdr: str) -> set[str]:
    """Return the set of rule_ids that fire on this commander."""
    comps = find_all_complements(conn, [cmdr])
    return {c.rule_id for c in comps}


def _score_top30(eng: SynergyEngine, cmdr: str) -> list[tuple[str, float]]:
    """Return the top-30 (card, total_score) for a commander."""
    page = eng.page(cmdr, limit=30)
    return [(r.card, r.total_score) for r in page.items]


def _cv(values: list[float]) -> float:
    """Coefficient of variation; 0 when fewer than 3 or mean ≈ 0."""
    if len(values) < 3:
        return 0.0
    mean = statistics.mean(values)
    if abs(mean) < 1e-9:
        return 0.0
    return statistics.stdev(values) / abs(mean)


# ----------------------------------------------------------------------
# Gate C — EDHREC NDCG delta
# ----------------------------------------------------------------------


def _edhrec_sections(edhrec_conn: sqlite3.Connection, cmdr_name: str) -> dict[str, set[str]]:
    """Return {'High Synergy Cards': {...}, 'Top Cards': {...}, ...}.

    Empty dict when the commander has no EDHREC slug match.
    """
    slug = commander_to_slug(cmdr_name)
    rows = edhrec_conn.execute(
        "SELECT section, card_name FROM edhrec_card_synergy WHERE commander_slug = ?",
        (slug,),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for section, card_name in rows:
        out.setdefault(section or "", set()).add(card_name)
    return out


def _edhrec_match_counts(
    top30_names: list[str],
    sections: dict[str, set[str]],
) -> tuple[int, int]:
    """Return (hi_syn_hits, on_page_hits) for ``top30_names`` vs EDHREC sections."""
    hi_syn = sections.get("High Synergy Cards", set())
    on_page: set[str] = set()
    for cards in sections.values():
        on_page |= cards
    hisyn_hits = sum(1 for c in top30_names if c in hi_syn)
    onpage_hits = sum(1 for c in top30_names if c in on_page)
    return hisyn_hits, onpage_hits


@contextlib.contextmanager
def _rule_disabled(conn: sqlite3.Connection, rule_id: str):
    """Context manager that temporarily disables ``rule_id`` across substrates.

    Declarative rules: UPDATE ``rules.active = 0`` + clear interpreter
    cache. Python helpers (``_find_<rule_id>``): monkey-patch the
    module-level binding in ``complement_rules.core`` to return ``[]``.
    Both paths restored on exit via try/finally so an exception mid-
    measurement doesn't leave the DB or module in a bad state.
    """
    declarative_was_active = False
    row = conn.execute("SELECT active FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
    if row is not None and row[0] == 1:
        declarative_was_active = True
        conn.execute("UPDATE rules SET active=0 WHERE rule_id=?", (rule_id,))
        conn.commit()

    helper_name = f"_find_{rule_id}"
    orig_helper = None
    if hasattr(_core, helper_name):
        orig_helper = getattr(_core, helper_name)
        setattr(_core, helper_name, lambda *a, **kw: [])

    clear_interpreter_cache()
    clear_ports_cache()
    try:
        yield
    finally:
        if declarative_was_active:
            conn.execute("UPDATE rules SET active=1 WHERE rule_id=?", (rule_id,))
            conn.commit()
        if orig_helper is not None:
            setattr(_core, helper_name, orig_helper)
        clear_interpreter_cache()
        clear_ports_cache()


def _gate_c_ndcg_delta(
    eng: SynergyEngine,
    conn: sqlite3.Connection,
    edhrec_conn: sqlite3.Connection,
    rule_id: str,
    targets: list[str],
) -> tuple[int, float, float]:
    """Measure per-target Hi-Syn / OnPage delta induced by ``rule_id``.

    Returns ``(n_edhrec_targets, mean_hisyn_delta, mean_onpage_delta)``.
    Only targets with EDHREC coverage contribute — commanders with no
    slug match are skipped (no ground truth to compare against).
    """
    # Pre-fetch EDHREC sections; skip targets with no coverage.
    target_sections: dict[str, dict[str, set[str]]] = {}
    for name in targets:
        sections = _edhrec_sections(edhrec_conn, name)
        if sections:
            target_sections[name] = sections

    if not target_sections:
        return 0, 0.0, 0.0

    # Score WITH rule active (current state).
    with_hisyn: dict[str, int] = {}
    with_onpage: dict[str, int] = {}
    for name, sections in target_sections.items():
        top30 = [c for c, _ in _score_top30(eng, name)]
        hs, op = _edhrec_match_counts(top30, sections)
        with_hisyn[name] = hs
        with_onpage[name] = op

    # Score WITHOUT rule; restore afterwards.
    without_hisyn: dict[str, int] = {}
    without_onpage: dict[str, int] = {}
    with _rule_disabled(conn, rule_id):
        # SynergyEngine caches IDF/ports internally; rebuild to pick up
        # the disabled rule.
        eng2 = SynergyEngine(eng._db_path if hasattr(eng, "_db_path") else str(conn))
        for name, sections in target_sections.items():
            top30 = [c for c, _ in _score_top30(eng2, name)]
            hs, op = _edhrec_match_counts(top30, sections)
            without_hisyn[name] = hs
            without_onpage[name] = op

    hisyn_deltas = [with_hisyn[n] - without_hisyn[n] for n in target_sections]
    onpage_deltas = [with_onpage[n] - without_onpage[n] for n in target_sections]
    return (
        len(target_sections),
        statistics.mean(hisyn_deltas),
        statistics.mean(onpage_deltas),
    )


# ----------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------


def _verdict_for(
    coverage_median: float,
    cv_median: float,
    ndcg_evaluated: bool,
    mean_hisyn_delta: float,
    mean_onpage_delta: float,
) -> tuple[Verdict, tuple[str, ...]]:
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

    # Gate C — EDHREC ground-truth match delta. Hi-Syn is the stricter
    # signal (curated synergy cards); OnPage is broader. Weight
    # Hi-Syn 4× OnPage since Hi-Syn deltas are inherently smaller
    # magnitude (Hi-Syn lists are 10 cards vs OnPage's ~100).
    ndcg_combined = mean_hisyn_delta + 0.25 * mean_onpage_delta
    ndcg_reject = False
    ndcg_filler_warn = False
    if ndcg_evaluated:
        if ndcg_combined <= _REJECT_NDCG_DELTA:
            ndcg_reject = True
            reasons.append(
                f"EDHREC match delta {ndcg_combined:+.3f} "
                f"(Hi-Syn Δ {mean_hisyn_delta:+.3f}, OnPage Δ {mean_onpage_delta:+.3f}) "
                f"≤ {_REJECT_NDCG_DELTA} — rule REDUCES ground-truth match on its targets"
            )
        elif abs(ndcg_combined) < _WARN_NDCG_NEUTRAL and cov_warn:
            # Filler signature: near-zero EDHREC delta AND the target
            # commanders are thinly covered. On its own, near-zero
            # delta is fine — it just means the rule reorders an
            # already-rich page (amplifier behavior on popular
            # commanders). Combined with thin coverage, it's the
            # Ward:2 pattern.
            ndcg_filler_warn = True
            reasons.append(
                f"EDHREC match delta {ndcg_combined:+.3f} "
                f"(Hi-Syn Δ {mean_hisyn_delta:+.3f}, OnPage Δ {mean_onpage_delta:+.3f}) "
                f"~= 0 on thinly-covered targets — classical filler (Ward:2) signature"
            )

    # REJECT: Gate C negative OR (Gate A + Gate B both in reject band).
    if ndcg_reject or (cov_reject and cv_reject):
        return "REJECT", tuple(reasons)
    if cov_warn or cv_warn or ndcg_filler_warn:
        return "WARN", tuple(reasons)
    return "PASS", ()


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


def evaluate_rule(
    conn,
    eng: SynergyEngine,
    rule_id: str,
    *,
    edhrec_conn: sqlite3.Connection | None = None,
    legendary_names: set[str] | None = None,
    run_ndcg: bool = False,
    sample_size: int | None = None,
    cache_coverage: dict[str, set[str]] | None = None,
    cache_cv: dict[str, float] | None = None,
) -> RuleQualityResult:
    """Run gates A, B and (if ``run_ndcg``) C for ``rule_id``."""
    if cache_coverage is None:
        cache_coverage = {}
    if cache_cv is None:
        cache_cv = {}

    targets = _find_target_commanders(conn, rule_id, legendary_names=legendary_names)
    if not targets:
        return RuleQualityResult(
            rule_id=rule_id,
            n_targets=0,
            median_pre_existing_coverage=0.0,
            median_top30_cv=0.0,
            ndcg_evaluated=False,
            n_edhrec_targets=0,
            mean_hisyn_delta=0.0,
            mean_onpage_delta=0.0,
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
            cache_cv[c] = _cv([s for _, s in _score_top30(eng, c)])
        coverage_counts.append(len(cache_coverage[c] - {rule_id}))
        cv_values.append(cache_cv[c])

    coverage_median = statistics.median(coverage_counts)
    cv_median = statistics.median(cv_values)

    n_edhrec_targets = 0
    hisyn_delta = 0.0
    onpage_delta = 0.0
    if run_ndcg and edhrec_conn is not None:
        n_edhrec_targets, hisyn_delta, onpage_delta = _gate_c_ndcg_delta(eng, conn, edhrec_conn, rule_id, eval_targets)

    verdict, reasons = _verdict_for(
        coverage_median,
        cv_median,
        ndcg_evaluated=bool(run_ndcg and n_edhrec_targets > 0),
        mean_hisyn_delta=hisyn_delta,
        mean_onpage_delta=onpage_delta,
    )

    return RuleQualityResult(
        rule_id=rule_id,
        n_targets=len(targets),
        median_pre_existing_coverage=coverage_median,
        median_top30_cv=cv_median,
        ndcg_evaluated=bool(run_ndcg and n_edhrec_targets > 0),
        n_edhrec_targets=n_edhrec_targets,
        mean_hisyn_delta=hisyn_delta,
        mean_onpage_delta=onpage_delta,
        reasons=reasons,
        verdict=verdict,
    )


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    p.add_argument("--edhrec-db", type=Path, default=Path("data/tags.db"))
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
        "--ndcg",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run Gate C (EDHREC NDCG delta). Default: on for --rule, off for --all/--all-declarative.",
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
        run_ndcg = True if args.ndcg is None else args.ndcg
    elif args.all_declarative:
        rule_ids = sorted(DECLARATIVE_RULE_IDS)
        run_ndcg = False if args.ndcg is None else args.ndcg
    else:  # args.all
        rule_ids = sorted(DECLARATIVE_RULE_IDS | {g.rule_id for g in RULE_GATES})
        run_ndcg = False if args.ndcg is None else args.ndcg

    edhrec_conn: sqlite3.Connection | None = None
    if args.edhrec_db.exists():
        edhrec_conn = sqlite3.connect(args.edhrec_db)
        edhrec_conn.row_factory = sqlite3.Row
    elif run_ndcg:
        print(f"error: --edhrec-db path does not exist: {args.edhrec_db}", file=sys.stderr)
        return 2

    legendary_names = _legendary_creature_names(edhrec_conn)

    cache_coverage: dict[str, set[str]] = {}
    cache_cv: dict[str, float] = {}

    results: list[RuleQualityResult] = []
    for rid in rule_ids:
        print(f"[gate] evaluating {rid}{' (+ndcg)' if run_ndcg else ''}...", file=sys.stderr, flush=True)
        res = evaluate_rule(
            conn,
            eng,
            rid,
            edhrec_conn=edhrec_conn,
            legendary_names=legendary_names,
            run_ndcg=run_ndcg,
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
                        "ndcg_evaluated": r.ndcg_evaluated,
                        "n_edhrec_targets": r.n_edhrec_targets,
                        "mean_hisyn_delta": r.mean_hisyn_delta,
                        "mean_onpage_delta": r.mean_onpage_delta,
                        "verdict": r.verdict,
                        "reasons": list(r.reasons),
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        header = (
            f"{'rule_id':<48} {'targets':>7} {'cov':>5} {'cv':>6} {'edh_n':>5} {'hisyn_Δ':>8} {'onpage_Δ':>9}  verdict"
        )
        print(header)
        print("-" * len(header))
        for r in results:
            ndcg_n = f"{r.n_edhrec_targets}" if r.ndcg_evaluated else "-"
            hisyn = f"{r.mean_hisyn_delta:+.3f}" if r.ndcg_evaluated else "-"
            onpage = f"{r.mean_onpage_delta:+.3f}" if r.ndcg_evaluated else "-"
            print(
                f"{r.rule_id:<48} {r.n_targets:>7} "
                f"{r.median_pre_existing_coverage:>5.1f} "
                f"{r.median_top30_cv:>6.3f} "
                f"{ndcg_n:>5} {hisyn:>8} {onpage:>9}  {r.verdict}"
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
