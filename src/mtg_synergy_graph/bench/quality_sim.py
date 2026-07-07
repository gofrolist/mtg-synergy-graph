"""Effect-per-mana quality-prior kill-test instrument (plan 2026-07-06-001, Task 10).

Zero scoring-path impact. Reuses ``portfolio_sim.build_commander_sim``'s
cached per-commander pool + decomposition (committed granularity only —
no family-map plumbing needed here). ``assemble_quality`` re-scales each
POOL card's production total by
``quality.quality_multiplier(rate, q=q, r0=r0)`` and re-sorts with the
production 4-key — an OUTRANKED lever only: no new entrants are admitted,
the same way ``portfolio_sim``'s cells re-rank an already-scored pool.
The NO_RULES / unreachable-candidate population stays Phase A's
(``context_sim``) job.

``rate_signal`` is Forge-native (port amounts + engine-shape marker +
cmc, see ``mtg_synergy_graph.quality``) and is computed ONCE per run,
shared across every commander and grid cell — it does not depend on any
per-commander state.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..quality import quality_multiplier, rate_signal
from ..validate import compute_ndcg
from .portfolio_sim import (
    CommanderSim,
    PortfolioSimError,
    SelfCheckError,
    _write_report,
    bootstrap_band,
    build_commander_sim,
    gem_rate_for_assembly,
    load_fixture_commanders,
    resolve_trap_commanders,
)

if TYPE_CHECKING:
    from ..engine import SynergyEngine

_UNRANKED = 10**9


# ---------------------------------------------------------------------------
# Per-commander cached state + assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualitySim:
    """One commander's cached pool + totals, ready for quality-prior re-assembly.

    Wraps ``portfolio_sim.build_commander_sim``'s :class:`CommanderSim`,
    materializing the committed-granularity per-card totals as
    ``base_totals`` — ``CommanderSim`` itself only exposes per-card
    ``ScoreDecomposition`` objects via ``decomps``, not a flat totals
    dict. ``edhrec_top_30`` / ``plausible`` keep the same names/types as
    ``CommanderSim`` so a :class:`QualitySim` can stand in for it at
    ``gem_rate_for_assembly`` call sites (see the ``cast`` below).
    """

    commander: str
    base_totals: dict[str, float]
    base_top_30: tuple[str, ...]
    pool_order: tuple[str, ...]
    cmc_lookup: Mapping[str, float]
    rank_lookup: Mapping[str, int]
    graded_labels: Mapping[str, float]
    edhrec_top_30: frozenset[str] | None
    plausible: frozenset[str]
    base_ndcg: float
    base_gem_rate: float | None
    base_gems: frozenset[str]


def assemble_quality(
    sim: QualitySim,
    rates: Mapping[str, float],
    *,
    q: float,
    r0: float,
) -> tuple[str, ...]:
    """Top-30 with each pool card's total scaled by the quality prior.

    Pool cards only (``sim.base_totals``'s keys) — no new entrants ever
    enter. ``q=0`` is bitwise-identical to ``sim.base_top_30`` (the
    build-path self-check below enforces this at build time).
    """
    totals = {
        card: total * quality_multiplier(rates.get(card, 0.0), q=q, r0=r0) for card, total in sim.base_totals.items()
    }
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


def build_quality_sim(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection | None,
    commander: str,
    rates: Mapping[str, float],
    *,
    granularity: str = "committed",
) -> QualitySim:
    """Score one commander live (via ``build_commander_sim``), self-check q=0.

    Raises :class:`SelfCheckError` when the q=0 assembly diverges from
    the engine's own ``page()`` top-30 (mirrors ``build_commander_sim``'s
    and ``build_context_sim``'s own identity guards).
    """
    cs: CommanderSim = build_commander_sim(engine, edhrec_conn, commander)
    base_totals = {card: cs.decomps[granularity][card].total for card in cs.pool_order}
    sim = QualitySim(
        commander=cs.commander,
        base_totals=base_totals,
        base_top_30=cs.base_top_30,
        pool_order=cs.pool_order,
        cmc_lookup=cs.cmc_lookup,
        rank_lookup=cs.rank_lookup,
        graded_labels=cs.graded_labels,
        edhrec_top_30=cs.edhrec_top_30,
        plausible=cs.plausible,
        base_ndcg=cs.base_ndcg,
        base_gem_rate=cs.base_gem_rate,
        base_gems=cs.base_gems,
    )
    check = assemble_quality(sim, rates, q=0.0, r0=1.0)
    if check != sim.base_top_30:
        diff = [(a, b) for a, b in zip(check, sim.base_top_30, strict=False) if a != b][:5]
        raise SelfCheckError(f"{commander}: q=0 assembly diverges from page() top-30; first mismatches: {diff}")
    return sim


def gem_rate_for_quality(
    sim: QualitySim,
    assembled: Sequence[str],
    *,
    k: int = 30,
) -> tuple[float, frozenset[str]] | None:
    """``portfolio_sim.gem_rate_for_assembly`` over a :class:`QualitySim`.

    ``gem_rate_for_assembly`` only reads ``sim.edhrec_top_30`` and
    ``sim.plausible`` — both present on :class:`QualitySim` under the
    same names/types as :class:`CommanderSim` — so the cast is a pure
    type-checker accommodation, not a runtime behavior change.
    """
    return gem_rate_for_assembly(cast(CommanderSim, sim), assembled, k=k)


# ---------------------------------------------------------------------------
# Subcommand: bands
# ---------------------------------------------------------------------------

#: Grid of quality-prior strengths (`q`, the asymptote above 1.0) and
#: half-saturation rates (`r0`) swept by ``run_sweep``'s default cell set.
Q_GRID: tuple[float, ...] = (0.05, 0.1, 0.2)
R0_GRID: tuple[float, ...] = (1.0, 2.0, 4.0)


def run_bands(
    engine: SynergyEngine,
    edhrec_conn: sqlite3.Connection,
    commanders: Sequence[str],
    rates: Mapping[str, float],
    *,
    seed: int = 17,
) -> tuple[dict[str, Any], list[QualitySim]]:
    """q=0 baseline distribution -> bootstrap noise bands (pin BEFORE sweep).

    Returns ``(report, sims)`` — the cached :class:`QualitySim` list is
    reused by ``run_sweep`` in the same process so a ``sweep`` CLI
    invocation only scores each commander once.

    Note: ``per_commander_ndcg`` is built by tracking ``(commander,
    ndcg)`` pairs directly in the scoring loop rather than zipping
    ``sims`` against a separately-collected ndcg list post-hoc —
    commanders without graded EDHREC labels are skipped from the ndcg
    list but still appear in ``sims``, so a positional zip would
    silently mis-pair names against the wrong NDCG values whenever any
    commander in the batch lacks labels.
    """
    ndcgs: list[float] = []
    gem_rates: list[float] = []
    per_commander_ndcg: dict[str, float] = {}
    sims: list[QualitySim] = []
    total = len(commanders)
    for i, commander in enumerate(commanders, start=1):
        sim = build_quality_sim(engine, edhrec_conn, commander, rates)
        sims.append(sim)
        print(
            f"[{i}/{total}] {commander}: pool={len(sim.pool_order)} (q=0 self-check OK)",
            file=sys.stderr,
        )
        if sim.graded_labels:
            per_commander_ndcg[sim.commander] = sim.base_ndcg
            ndcgs.append(sim.base_ndcg)
        if sim.base_gem_rate is not None:
            gem_rates.append(sim.base_gem_rate)
    return {
        "n_commanders": len(sims),
        "ndcg_band": bootstrap_band(ndcgs, seed=seed),
        "gem_band": bootstrap_band(gem_rates, seed=seed + 1),
        "per_commander_ndcg": per_commander_ndcg,
    }, sims


# ---------------------------------------------------------------------------
# Subcommand: sweep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityCell:
    q: float
    r0: float

    @property
    def label(self) -> str:
        return f"q={self.q:g}/r0={self.r0:g}"


def run_sweep(
    sims: Sequence[QualitySim],
    rates: Mapping[str, float],
    *,
    cells: Sequence[QualityCell] | None = None,
) -> dict[str, Any]:
    """Re-assemble every cell from cached sims; per-cell metrics + trap sidecar.

    Commanders with no graded EDHREC labels are skipped from every
    per-cell aggregate (ndcg delta, cliffs, gem delta) — mirrors
    ``run_bands``'s same skip.
    """
    cells = list(cells) if cells else [QualityCell(q, r0) for q in Q_GRID for r0 in R0_GRID]
    graded_sims = [sim for sim in sims if sim.graded_labels]

    trap_by_name, trap_absent = resolve_trap_commanders([sim.commander for sim in sims])
    trap_names = set(trap_by_name.values())

    out: list[dict[str, Any]] = []
    for cell in cells:
        deltas: list[tuple[str, float]] = []
        cliff = 0
        gem_deltas: list[float] = []
        trap_deltas: dict[str, float] = {}
        for sim in graded_sims:
            top = assemble_quality(sim, rates, q=cell.q, r0=cell.r0)
            d = compute_ndcg(list(top), dict(sim.graded_labels)) - sim.base_ndcg
            deltas.append((sim.commander, d))
            if d < -0.05:
                cliff += 1
            if sim.commander in trap_names:
                trap_deltas[sim.commander] = d
            if sim.base_gem_rate is not None:
                gem = gem_rate_for_quality(sim, top, k=30)
                if gem is not None:
                    gem_deltas.append(gem[0] - sim.base_gem_rate)
        mean = sum(d for _, d in deltas) / max(len(deltas), 1)
        mean_gem_delta = (sum(gem_deltas) / len(gem_deltas)) if gem_deltas else None
        out.append(
            {
                "q": cell.q,
                "r0": cell.r0,
                "mean_ndcg_delta": mean,
                "cliffs": cliff,
                "gem_delta": mean_gem_delta,
                "trap_deltas": dict(sorted(trap_deltas.items())),
                "per_commander": dict(deltas),
            }
        )
    return {"cells": out, "trap_commanders": trap_by_name, "trap_fragments_absent": trap_absent}


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_bands_markdown(report: dict[str, Any]) -> str:
    nb = report["ndcg_band"]
    gb = report["gem_band"]
    lines = [
        "# quality_sim bands",
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
    h_500q: float | None,
    g_500q: float | None,
) -> str:
    """Rendered comparison only — no routing/decision logic here.

    Phase C gate: mean_ndcg_delta >= h_500q AND cliffs == 0 AND
    gem_delta >= -g_500q AND every trap-commander delta >= -0.05.
    """
    lines = [
        "## Decision gate (Phase C)",
        "",
        f"pinned: h_500q={h_500q}  g_500q={g_500q}",
        "",
        f"{'q':>6} {'r0':>6} {'gate':>5}",
        f"{'-' * 6} {'-' * 6} {'-' * 5}",
    ]
    for cell in report["cells"]:
        if h_500q is None or g_500q is None:
            gate = "-"
        else:
            gem = cell["gem_delta"]
            gem_ok = gem is None or gem >= -g_500q
            traps_ok = all(v >= -0.05 for v in cell["trap_deltas"].values())
            gate = "Y" if (cell["mean_ndcg_delta"] >= h_500q and cell["cliffs"] == 0 and gem_ok and traps_ok) else "n"
        lines.append(f"{cell['q']:>6.2f} {cell['r0']:>6.2f} {gate:>5}")
    return "\n".join(lines) + "\n"


def _render_sweep_markdown(report: dict[str, Any], *, gates: str | None = None) -> str:
    lines = [
        "# quality_sim sweep",
        "",
        f"fixture: {report.get('fixture', '?')}  |  commanders: {report.get('n_commanders', '?')}",
        "",
        f"{'q':>6} {'r0':>6} {'mean_ndcg_delta':>16} {'cliffs':>7} {'gem_delta':>10}",
        f"{'-' * 6} {'-' * 6} {'-' * 16} {'-' * 7} {'-' * 10}",
    ]
    for cell in report["cells"]:
        gem = cell["gem_delta"]
        gem_str = f"{gem:+.4f}" if gem is not None else "-"
        lines.append(
            f"{cell['q']:>6.2f} {cell['r0']:>6.2f} {cell['mean_ndcg_delta']:>+16.4f} {cell['cliffs']:>7} {gem_str:>10}"
        )
    if report.get("trap_fragments_absent"):
        lines += ["", f"trap fragments absent from fixture: {', '.join(report['trap_fragments_absent'])}"]
    lines += ["", "## Trap sidecar (NDCG delta per cell)", ""]
    trap_names = sorted({name for cell in report["cells"] for name in cell["trap_deltas"]})
    if trap_names:
        for cell in report["cells"]:
            rendered = ", ".join(f"{n}: {cell['trap_deltas'].get(n, float('nan')):+.4f}" for n in trap_names)
            lines.append(f"- q={cell['q']:g}/r0={cell['r0']:g}: {rendered}")
    else:
        lines.append("(no trap commanders present in this run)")
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

_DEFAULT_FIXTURE = "tests/fixtures/golden_set_run_500.json"
_DEFAULT_OUTPUT_DIR = ".audit/quality_sim"


class QualitySimError(RuntimeError):
    """Base error for the quality_sim CLI (bad --cells syntax, etc.)."""


def _parse_cells(raw: str) -> list[QualityCell]:
    """Parse ``"q,r0;q,r0"`` into a :class:`QualityCell` list."""
    cells: list[QualityCell] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            q_str, r0_str = part.split(",")
            cells.append(QualityCell(float(q_str.strip()), float(r0_str.strip())))
        except ValueError as exc:
            raise QualitySimError(f"--cells: malformed cell {part!r} in {raw!r} (expected 'q,r0'): {exc}") from exc
    if not cells:
        raise QualitySimError(f"--cells produced no cells from {raw!r}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quality_sim.py",
        description="Effect-per-mana quality-prior kill-test instrument (plan 2026-07-06-001 Task 10)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_bands = sub.add_parser("bands", help="q=0 baseline distribution -> bootstrap noise bands")
    _add_common_args(p_bands)

    p_sweep = sub.add_parser("sweep", help="Q_GRID x R0_GRID cell sweep (or --cells) over the cached sims")
    _add_common_args(p_sweep)
    p_sweep.add_argument(
        "--cells",
        default=None,
        help='semicolon-separated "q,r0" pairs, e.g. "0.05,1.0;0.1,2.0" (default: full Q_GRID x R0_GRID)',
    )
    p_sweep.add_argument("--h-500q", type=float, default=None, help="pinned 500-cmdr NDCG-delta floor (gate)")
    p_sweep.add_argument("--g-500q", type=float, default=None, help="pinned 500-cmdr gem-delta floor (gate)")

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

    # Local import: engine pulls the scoring stack; keep module import light.
    from ..engine import SynergyEngine

    outdir = Path(args.output_dir)
    edhrec_conn = sqlite3.connect(edhrec_path)
    edhrec_conn.row_factory = sqlite3.Row
    engine = SynergyEngine(db_path)
    started = time.perf_counter()
    try:
        rates = rate_signal(engine._conn)
        report, sims = run_bands(engine, edhrec_conn, commanders, rates, seed=args.seed)
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
            sweep_report = run_sweep(sims, rates, cells=cells)
            sweep_report["fixture"] = str(fixture_path)
            sweep_report["n_commanders"] = report["n_commanders"]
            gates_md = None
            if args.h_500q is not None or args.g_500q is not None:
                gates_md = _render_gates_markdown(sweep_report, h_500q=args.h_500q, g_500q=args.g_500q)
            sweep_path = _write_report(outdir, "sweep.json", sweep_report)
            md = _render_sweep_markdown(sweep_report, gates=gates_md)
            md_path = _write_text(outdir, "sweep.md", md)
            print(md)
            print(f"reports: {sweep_path}, {md_path}")
    except SelfCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (PortfolioSimError, QualitySimError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
        edhrec_conn.close()

    print(f"total wall clock: {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return 0
