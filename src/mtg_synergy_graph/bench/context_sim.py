"""Deck-context two-pass kill-test instrument (plan 2026-07-06-001, Phase A).

Zero scoring-path impact. Pass 1 is the production ranking; the top-K
rule-covered candidates become the *context pool*; pass 2 awards every
legal card an IDF-weighted synergy contribution from its mechanical
matches against each context card (same complement rules, same IDF
form, context card standing in the commander slot). Candidates with
zero pass-1 score become reachable — the NO_RULES archetype-payoff
population (see plan 2026-07-02-005 DECLINE Correction).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..complement_rules import PortComplement, find_all_complements
from ..universal_scorer import _compute_idf_weights
from ..validate import compute_ndcg
from .hidden_gems import _cohort_median, _plausibility_from_maps
from .optimize import load_edhrec_labels
from .portfolio_sim import (
    PortfolioSimError,
    SelfCheckError,
    _write_report,
    bootstrap_band,
    load_fixture_commanders,
)

if TYPE_CHECKING:
    from ..engine import SynergyEngine


def select_context(
    pool_order: Sequence[str],
    n_rules_map: Mapping[str, int],
    k: int,
) -> tuple[str, ...]:
    """Top-``k`` pass-1 candidates with >=1 distinct rule (pool order).

    Staple-only / rule-free candidates are excluded: they carry no
    mechanical signature for the context pass to match against.
    """
    out: list[str] = []
    for name in pool_order:
        if n_rules_map.get(name, 0) >= 1:
            out.append(name)
            if len(out) == k:
                break
    return tuple(out)


def aggregate_context_scores(
    comps: Sequence[PortComplement],
    idf: Mapping[tuple[str, str, str, str], float],
    ctx_card: str,
) -> dict[str, float]:
    """Per-candidate IDF-weighted synergy sum vs one context card.

    Mirrors ``UniversalScore.score`` dedup (one contribution per
    ``(rule_id, cmdr_event, cand_event, filter_group)`` key per
    candidate), synergy direction only, no staple/circuit/cmc/rank
    bonuses — the context term is a pure mechanical-match signal.
    Self-pairs (candidate == ctx_card) are dropped.
    """
    out: dict[str, float] = defaultdict(float)
    seen: set[tuple[str, str, str, str, str]] = set()
    for c in comps:
        if c.direction != "synergy" or c.candidate == ctx_card:
            continue
        key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
        dkey = (c.candidate, *key)
        if dkey in seen:
            continue
        seen.add(dkey)
        out[c.candidate] += idf.get(key, 1.0)
    return dict(out)


#: Global cache: ctx card name -> {candidate -> context score}. Context
#: cards repeat heavily across commanders (staColor archetype overlap),
#: so this is the instrument's main wall-clock lever. Keyed by name only
#: — valid for one DB/config; the CLI process is single-run.
_CTX_SCORE_CACHE: dict[str, dict[str, float]] = {}


def context_scores_for_card(
    conn: sqlite3.Connection,
    ctx_card: str,
    *,
    candidate_cache=None,
) -> dict[str, float]:
    """Cached per-context-card scoring pass (one full complement walk)."""
    cached = _CTX_SCORE_CACHE.get(ctx_card)
    if cached is not None:
        return cached
    comps = find_all_complements(conn, [ctx_card], candidate_cache=candidate_cache)
    idf = _compute_idf_weights(comps)
    scores = aggregate_context_scores(comps, idf, ctx_card)
    _CTX_SCORE_CACHE[ctx_card] = scores
    return scores


_UNRANKED = 10**9


def _plausible_set(n_rules_map: Mapping[str, int], base_totals: Mapping[str, float]) -> frozenset[str]:
    """Mechanical-plausibility gate over ``base_totals`` (hidden_gems.py FR2 criteria).

    Delegates to the canonical ``_cohort_median`` / ``_plausibility_from_maps``
    helpers (``hidden_gems.py``) so this instrument's gate can never
    silently diverge from the audit's ``hidden_gem_hit_rate`` gate:
    >=2 distinct rules OR total above the median of *strictly-positive*
    totals — with the degenerate-case guard (median <= 0 forces the
    median-OR leg to False rather than degenerating to ``total > 0``,
    which would flag every positive candidate as plausible).
    """
    median = _cohort_median(base_totals)
    return frozenset(name for name in base_totals if _plausibility_from_maps(name, n_rules_map, base_totals, median))


@dataclass(frozen=True)
class ContextCell:
    k_context: int
    w_ctx: float


@dataclass
class ContextSim:
    """One commander's cached pass-1 state, ready for cheap cell re-assembly."""

    commander: str
    base_totals: dict[str, float]  # page() total per scored candidate
    base_top_30: tuple[str, ...]
    pool_order: tuple[str, ...]
    legal_pool: frozenset[str]  # engine.legal_cards() — new entrants gate
    context_max: tuple[str, ...]  # top K_MAX rule-covered candidates
    ctx_scores: dict[str, dict[str, float]]
    cmc_lookup: dict[str, float]
    rank_lookup: dict[str, int]
    graded_labels: dict[str, float]
    edhrec_top_30: frozenset[str] | None
    #: EDHREC labels with NO pass-1 score — the in-instrument NO_RULES proxy.
    zero_score_labels: frozenset[str]
    #: Mechanical-plausibility gate over ``base_totals`` (hidden_gems.py
    #: criteria, computed against this commander's own cohort): >=2
    #: distinct synergy rules OR a pass-1 total above the cohort median.
    #: Trailing default keeps existing positional/keyword constructor
    #: call sites (tests) working unchanged.
    plausible: frozenset[str] = frozenset()


def assemble_cell(sim: ContextSim, cell: ContextCell) -> tuple[str, ...]:
    """Two-pass top-30 for one grid cell. w_ctx=0 is bitwise pass-1."""
    totals = dict(sim.base_totals)
    if cell.w_ctx > 0.0 and cell.k_context > 0:
        ctx = sim.context_max[: cell.k_context]
        agg: dict[str, float] = defaultdict(float)
        for ctx_card in ctx:
            for cand, s in sim.ctx_scores.get(ctx_card, {}).items():
                agg[cand] += s
        n = max(len(ctx), 1)
        for cand, s in agg.items():
            if cand == sim.commander or cand not in sim.legal_pool:
                continue
            totals[cand] = totals.get(cand, 0.0) + cell.w_ctx * (s / n)
    ranked = sorted(
        totals,
        key=lambda c: (
            -totals[c],
            sim.cmc_lookup.get(c, 99.0),
            sim.rank_lookup.get(c, _UNRANKED),
            c,
        ),
    )
    return tuple(ranked[:30])


def build_context_sim(engine, edhrec_conn, commander: str, *, k_max: int = 30) -> ContextSim:
    """Score one commander live, cache context scores, self-check w=0.

    Mirrors ``portfolio_sim.build_commander_sim``'s engine duck-typing
    (``page``, ``legal_cards``, ``_score_cache``, ``_candidate_cache``,
    ``_conn``). Raises ``SelfCheckError`` when the w=0 assembly diverges
    from ``page()``'s own top-30.
    """
    page = engine.page([commander], offset=0, limit=1_000_000)
    universal = engine._score_cache[(commander,)]
    base_totals = {rec.card: rec.total_score for rec in page.items}
    pool_order = tuple(rec.card for rec in page.items)

    cmc_lookup: dict[str, float] = {}
    rank_lookup: dict[str, int] = {}
    for name, row in engine._candidate_cache.candidate_rows.items():
        cmc_lookup[name] = row["cmc"] if row["cmc"] is not None else 99.0
        raw = row.get("edhrec_rank")
        rank_lookup[name] = int(raw) if raw is not None else _UNRANKED

    n_rules_map = {name: len(us.distinct_rules) for name, us in universal.items()}
    context_max = select_context(pool_order, n_rules_map, k_max)
    ctx_scores = {
        c: context_scores_for_card(engine._conn, c, candidate_cache=engine._candidate_cache) for c in context_max
    }
    legal_pool = frozenset(engine.legal_cards([commander]))

    plausible = _plausible_set(n_rules_map, base_totals)

    graded_labels: dict[str, float] = {}
    edhrec_top_30: frozenset[str] | None = None
    if edhrec_conn is not None:
        labels = load_edhrec_labels(edhrec_conn, commander)
        graded_labels = dict(labels.graded_labels)
        edhrec_top_30 = labels.top_30_set
    zero_score_labels = frozenset(n for n in graded_labels if n not in base_totals)

    sim = ContextSim(
        commander=commander,
        base_totals=base_totals,
        base_top_30=pool_order[:30],
        pool_order=pool_order,
        legal_pool=legal_pool,
        context_max=context_max,
        ctx_scores=ctx_scores,
        cmc_lookup=cmc_lookup,
        rank_lookup=rank_lookup,
        graded_labels=graded_labels,
        edhrec_top_30=edhrec_top_30,
        zero_score_labels=zero_score_labels,
        plausible=plausible,
    )
    check = assemble_cell(sim, ContextCell(0, 0.0))
    if check != sim.base_top_30:
        diff = [(a, b) for a, b in zip(check, sim.base_top_30, strict=False) if a != b][:5]
        raise SelfCheckError(f"{commander}: w=0 assembly diverges from page(); first mismatches: {diff}")
    return sim


# ---------------------------------------------------------------------------
# Metrics: NDCG + gem-rate deltas
# ---------------------------------------------------------------------------


def gem_rate_for_context(
    sim: ContextSim,
    assembled: Sequence[str],
    *,
    k: int = 30,
) -> tuple[float, frozenset[str]] | None:
    """hidden-gem rate for an assembled top-``k`` (None without EDHREC data).

    Mirrors ``portfolio_sim.gem_rate_for_assembly``'s formula against
    :class:`ContextSim`'s own fields: ``(assembled - edhrec_top_30) &
    plausible``, rate = ``len(gems) / k``.
    """
    if sim.edhrec_top_30 is None:
        return None
    gems = frozenset((set(assembled) - sim.edhrec_top_30) & sim.plausible)
    return len(gems) / k, gems


# ---------------------------------------------------------------------------
# Subcommand: bands
# ---------------------------------------------------------------------------

#: Grid of context-pool depths (`k_context`) and context-term weights
#: (`w_ctx`) swept by ``run_sweep``'s default cell set.
K_GRID: tuple[int, ...] = (10, 20, 30)
W_GRID: tuple[float, ...] = (0.1, 0.25, 0.5)


def run_bands(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    *,
    seed: int = 17,
    k_max: int = 30,
) -> tuple[dict[str, Any], list[ContextSim]]:
    """w=0 baseline distribution -> bootstrap noise bands (pin BEFORE sweep).

    Returns ``(report, sims)`` — the cached :class:`ContextSim` list is
    reused by ``run_sweep`` in the same process so a ``sweep`` CLI
    invocation only scores each commander once.

    Note: the report's ``per_commander_ndcg`` is built by tracking
    ``(commander, ndcg)`` pairs directly in the scoring loop rather than
    zipping ``sims`` against ``ndcgs`` post-hoc — commanders without
    graded EDHREC labels are skipped from ``ndcgs`` but still appear in
    ``sims``, so a positional zip would silently mis-pair names against
    the wrong NDCG values whenever any commander in the batch lacks
    labels.
    """
    ndcgs: list[float] = []
    gem_rates: list[float] = []
    per_commander_ndcg: dict[str, float] = {}
    sims: list[ContextSim] = []
    total = len(commanders)
    for i, commander in enumerate(commanders, start=1):
        sim = build_context_sim(engine, edhrec_conn, commander, k_max=k_max)
        sims.append(sim)
        print(
            f"[{i}/{total}] {commander}: pool={len(sim.pool_order)} (w=0 self-check OK)",
            file=sys.stderr,
        )
        if sim.graded_labels:
            ndcg = compute_ndcg(list(sim.base_top_30), sim.graded_labels)
            ndcgs.append(ndcg)
            per_commander_ndcg[sim.commander] = ndcg
            gem = gem_rate_for_context(sim, sim.base_top_30, k=30)
            if gem is not None:
                gem_rates.append(gem[0])
    return {
        "n_commanders": len(sims),
        "ndcg_band": bootstrap_band(ndcgs, seed=seed),
        "gem_band": bootstrap_band(gem_rates, seed=seed + 1),
        "per_commander_ndcg": per_commander_ndcg,
    }, sims


# ---------------------------------------------------------------------------
# Subcommand: sweep
# ---------------------------------------------------------------------------


def run_sweep(
    sims: Sequence[ContextSim],
    *,
    cells: Sequence[ContextCell] | None = None,
) -> dict[str, Any]:
    """Re-assemble every cell from cached sims; per-cell metrics + traps.

    Commanders with no graded EDHREC labels are skipped from every
    per-cell aggregate (ndcg delta, cliffs, reach, gem delta) — mirrors
    ``run_bands``'s same skip.
    """
    cells = list(cells) if cells else [ContextCell(k, w) for k in K_GRID for w in W_GRID]
    graded_sims = [sim for sim in sims if sim.graded_labels]

    base_ndcg: dict[str, float] = {
        sim.commander: compute_ndcg(list(sim.base_top_30), sim.graded_labels) for sim in graded_sims
    }
    base_gem: dict[str, float] = {}
    for sim in graded_sims:
        gem = gem_rate_for_context(sim, sim.base_top_30, k=30)
        if gem is not None:
            base_gem[sim.commander] = gem[0]

    out: list[dict[str, Any]] = []
    for cell in cells:
        deltas: list[tuple[str, float]] = []
        cliff = 0
        reach = 0
        gem_deltas: list[float] = []
        for sim in graded_sims:
            top = assemble_cell(sim, cell)
            d = compute_ndcg(list(top), sim.graded_labels) - base_ndcg[sim.commander]
            deltas.append((sim.commander, d))
            if d < -0.05:
                cliff += 1
            reach += len(sim.zero_score_labels & set(top))
            if sim.commander in base_gem:
                gem = gem_rate_for_context(sim, top, k=30)
                if gem is not None:
                    gem_deltas.append(gem[0] - base_gem[sim.commander])
        mean = sum(d for _, d in deltas) / max(len(deltas), 1)
        mean_gem_delta = (sum(gem_deltas) / len(gem_deltas)) if gem_deltas else None
        out.append(
            {
                "k_context": cell.k_context,
                "w_ctx": cell.w_ctx,
                "mean_ndcg_delta": mean,
                "cliffs": cliff,
                "reach": reach,
                "mean_gem_delta": mean_gem_delta,
                "per_commander": dict(deltas),
            }
        )
    return {"cells": out}


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_bands_markdown(report: dict[str, Any]) -> str:
    nb = report["ndcg_band"]
    gb = report["gem_band"]
    lines = [
        "# context_sim bands",
        "",
        f"fixture: {report.get('fixture', '?')}  |  commanders: {report['n_commanders']}",
        "",
        f"NDCG@30   mean={nb['mean']:.4f}  95% CI [{nb['ci95_low']:.4f}, {nb['ci95_high']:.4f}]  "
        f"half-width={nb['half_width']:.4f}",
        f"gem rate  mean={gb['mean']:.4f}  95% CI [{gb['ci95_low']:.4f}, {gb['ci95_high']:.4f}]  "
        f"half-width={gb['half_width']:.4f}",
    ]
    return "\n".join(lines) + "\n"


def _render_gates_markdown(
    report: dict[str, Any],
    *,
    h_cohort: float | None,
    h_500: float | None,
    g_500: float | None,
) -> str:
    """Rendered comparison only — no routing/decision logic here.

    G1: mean_ndcg_delta > h_cohort (cohort-fixture noise band).
    G2: mean_ndcg_delta > -h_500 AND zero cliffs AND mean_gem_delta > -g_500
        (500-cmdr no-regression band on both axes).
    G3: reach >= 100 (NO_RULES-population reachability floor).
    """
    lines = [
        "## Decision gates",
        "",
        f"pinned: h_cohort={h_cohort}  h_500={h_500}  g_500={g_500}",
        "",
        f"{'k':>4} {'w':>6} {'G1':>4} {'G2':>4} {'G3':>4}",
        f"{'-' * 4} {'-' * 6} {'-' * 4} {'-' * 4} {'-' * 4}",
    ]
    for cell in report["cells"]:
        g1 = "-" if h_cohort is None else ("Y" if cell["mean_ndcg_delta"] > h_cohort else "n")
        if h_500 is None or g_500 is None:
            g2 = "-"
        else:
            gem = cell["mean_gem_delta"]
            gem_ok = gem is None or gem > -g_500
            g2 = "Y" if (cell["mean_ndcg_delta"] > -h_500 and cell["cliffs"] == 0 and gem_ok) else "n"
        g3 = "Y" if cell["reach"] >= 100 else "n"
        lines.append(f"{cell['k_context']:>4} {cell['w_ctx']:>6.2f} {g1:>4} {g2:>4} {g3:>4}")
    return "\n".join(lines) + "\n"


def _render_sweep_markdown(report: dict[str, Any], *, gates: str | None = None) -> str:
    lines = [
        "# context_sim sweep",
        "",
        f"fixture: {report.get('fixture', '?')}  |  commanders: {report.get('n_commanders', '?')}",
        "",
        f"{'k':>4} {'w':>6} {'mean_ndcg_delta':>16} {'cliffs':>7} {'reach':>7} {'mean_gem_delta':>15}",
        f"{'-' * 4} {'-' * 6} {'-' * 16} {'-' * 7} {'-' * 7} {'-' * 15}",
    ]
    for cell in report["cells"]:
        gem = cell["mean_gem_delta"]
        gem_str = f"{gem:+.4f}" if gem is not None else "-"
        lines.append(
            f"{cell['k_context']:>4} {cell['w_ctx']:>6.2f} {cell['mean_ndcg_delta']:>+16.4f} "
            f"{cell['cliffs']:>7} {cell['reach']:>7} {gem_str:>15}"
        )
    if gates is not None:
        lines += ["", gates]
    return "\n".join(lines) + "\n"


def _write_text(outdir: Path, name: str, text: str) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DEFAULT_FIXTURE = "tests/fixtures/golden_set_archetype_payoff.json"
_DEFAULT_OUTPUT_DIR = ".audit/context_sim"


class ContextSimError(RuntimeError):
    """Base error for the context_sim CLI (bad --cells syntax, etc.)."""


def _parse_cells(raw: str) -> list[ContextCell]:
    """Parse ``"K,W;K,W"`` into a :class:`ContextCell` list."""
    cells: list[ContextCell] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            k_str, w_str = part.split(",")
            cells.append(ContextCell(int(k_str.strip()), float(w_str.strip())))
        except ValueError as exc:
            raise ContextSimError(f"--cells: malformed cell {part!r} in {raw!r} (expected 'K,W'): {exc}") from exc
    if not cells:
        raise ContextSimError(f"--cells produced no cells from {raw!r}")
    return cells


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default="data/synergy.db", help="synergy DB path")
    p.add_argument("--edhrec-db", default="data/tags.db", help="EDHREC tags DB path")
    p.add_argument("--fixture", default=_DEFAULT_FIXTURE, help="golden-set fixture (commander names only)")
    p.add_argument(
        "--limit-commanders",
        type=int,
        default=None,
        help="limit to the first N fixture commanders (smoke runs)",
    )
    p.add_argument("--output-dir", default=_DEFAULT_OUTPUT_DIR, help="report output directory")
    p.add_argument("--seed", type=int, default=17, help="bootstrap RNG seed")
    p.add_argument("--k-max", type=int, default=30, help="context-pool candidate cap (select_context k)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context_sim.py",
        description="Deck-context two-pass kill-test instrument (plan 2026-07-06-001)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_bands = sub.add_parser("bands", help="w=0 baseline distribution -> bootstrap noise bands")
    _add_common_args(p_bands)

    p_sweep = sub.add_parser("sweep", help="K_GRID x W_GRID cell sweep (or --cells) over the cached sims")
    _add_common_args(p_sweep)
    p_sweep.add_argument(
        "--cells",
        default=None,
        help='semicolon-separated "k_context,w_ctx" pairs, e.g. "10,0.1;20,0.25" (default: full K_GRID x W_GRID)',
    )
    p_sweep.add_argument(
        "--whitelist-baseline",
        action="store_true",
        help="whitelist-equivalence baseline cell (stub — lands in Task 4)",
    )
    p_sweep.add_argument("--h-cohort", type=float, default=None, help="pinned cohort NDCG band half-width (gate G1)")
    p_sweep.add_argument("--h-500", type=float, default=None, help="pinned 500-cmdr NDCG band half-width (gate G2)")
    p_sweep.add_argument("--g-500", type=float, default=None, help="pinned 500-cmdr gem band half-width (gate G2)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    db_path = Path(args.db)
    edhrec_path = Path(args.edhrec_db)
    fixture_path = Path(args.fixture)
    for path, what in ((db_path, "synergy DB"), (edhrec_path, "EDHREC DB"), (fixture_path, "fixture")):
        if not path.exists():
            print(f"error: {what} {path} not found", file=sys.stderr)
            return 2

    try:
        commanders = load_fixture_commanders(fixture_path)
    except PortfolioSimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.limit_commanders is not None:
        commanders = commanders[: args.limit_commanders]

    if args.command == "sweep" and args.whitelist_baseline:
        print("error: whitelist baseline lands in Task 4", file=sys.stderr)
        return 1

    # Local import: engine pulls the scoring stack; keep module import light.
    from ..engine import SynergyEngine

    outdir = Path(args.output_dir)
    edhrec_conn = sqlite3.connect(edhrec_path)
    edhrec_conn.row_factory = sqlite3.Row
    engine = SynergyEngine(db_path)
    started = time.perf_counter()
    try:
        report, sims = run_bands(engine, edhrec_conn, commanders, seed=args.seed, k_max=args.k_max)
        report["fixture"] = str(fixture_path)
        bands_path = _write_report(outdir, "bands.json", report)
        _write_text(outdir, "bands.md", _render_bands_markdown(report))
        nb, gb = report["ndcg_band"], report["gem_band"]
        print(f"bands: n={report['n_commanders']} seed={args.seed}")
        print(
            f"  NDCG@30  mean={nb['mean']:.4f}  95% CI [{nb['ci95_low']:.4f}, {nb['ci95_high']:.4f}]  "
            f"noise band (half-width) = {nb['half_width']:.4f}"
        )
        print(
            f"  gem rate mean={gb['mean']:.4f}  95% CI [{gb['ci95_low']:.4f}, {gb['ci95_high']:.4f}]  "
            f"non-regression band (half-width) = {gb['half_width']:.4f}"
        )
        print(f"report: {bands_path}")

        if args.command == "sweep":
            cells = _parse_cells(args.cells) if args.cells else None
            sweep_report = run_sweep(sims, cells=cells)
            sweep_report["fixture"] = str(fixture_path)
            sweep_report["n_commanders"] = report["n_commanders"]
            gates_md = None
            if args.h_cohort is not None or args.h_500 is not None or args.g_500 is not None:
                gates_md = _render_gates_markdown(
                    sweep_report, h_cohort=args.h_cohort, h_500=args.h_500, g_500=args.g_500
                )
            sweep_path = _write_report(outdir, "sweep.json", sweep_report)
            md = _render_sweep_markdown(sweep_report, gates=gates_md)
            md_path = _write_text(outdir, "sweep.md", md)
            print(md)
            print(f"reports: {sweep_path}, {md_path}")
    except SelfCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (PortfolioSimError, ContextSimError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
        edhrec_conn.close()

    print(f"total wall clock: {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return 0
