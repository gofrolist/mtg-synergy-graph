"""Activation-poverty census / queue / gate (coverage instrument CLI core).

Zero scoring-path impact. ``census`` runs the whole legal commander universe
through ``engine.page()`` and pins a config-hash-stamped baseline; ``queue``
ranks commanders by ``earned_top30`` (the coverage successor to gap_report);
``gate`` measures a cohort's ``earned_top30`` lift vs baseline against a
stratified control sample. See
``docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mtg_synergy_graph.bench.cohorts import (
    LEGAL_LEGENDARY_CREATURE_WHERE,
    attack_reward,
    team_anthem,
    toughness_payoff,
)
from mtg_synergy_graph.bench.coverage import CoverageMetrics, compute_coverage
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.complement_rules._interpreter_cache import clear_interpreter_cache
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.engine import SynergyEngine
from mtg_synergy_graph.graph_engine import clear_ports_cache

logger = logging.getLogger(__name__)

_METRIC_VERSION = 1

_DEFAULT_BASELINE = ".audit/coverage/baseline.json"
_DEFAULT_DB = "data/synergy.db"

#: Cohort-name -> predicate dispatch (mirrors ``bench/demand_coverage.py``'s
#: flat-dispatch convention).
_COHORT_DISPATCH = {
    "toughness_payoff": toughness_payoff,
    "team_anthem": team_anthem,
    "attack_reward": attack_reward,
}


def legal_commander_names(conn: sqlite3.Connection) -> list[str]:
    # Shares LEGAL_LEGENDARY_CREATURE_WHERE (aliased ``c``) with the cohort
    # predicates so census eligibility and cohort membership cannot drift.
    rows = conn.execute(f"SELECT c.name FROM cards c WHERE {LEGAL_LEGENDARY_CREATURE_WHERE} ORDER BY c.name")  # noqa: S608
    return [name for (name,) in rows]


def commander_coverage(engine: SynergyEngine, commander: str) -> CoverageMetrics:
    page = engine.page([commander], offset=0, limit=1_000_000)
    metrics = compute_coverage(page.items, top_n=30)
    # Bound memory across a ~2000-commander census: clear the engine's score
    # cache AND the module-level per-connection port/interpreter caches, which
    # otherwise accumulate one entry per commander for the run's lifetime
    # (the connection stays alive the whole census). Mirrors the per-iteration
    # clear_ports_cache() convention in bench/demand_coverage.py.
    engine._score_cache.clear()
    clear_ports_cache()
    clear_interpreter_cache()
    return metrics


def run_census(
    engine: SynergyEngine,
    conn: sqlite3.Connection,
    *,
    commanders: list[str] | None = None,
) -> dict[str, CoverageMetrics]:
    names = commanders if commanders is not None else legal_commander_names(conn)
    return {name: commander_coverage(engine, name) for name in names}


def write_baseline(
    path: str | Path,
    metrics_by_cmdr: dict[str, CoverageMetrics],
    *,
    config_hash: str,
) -> None:
    doc = {
        "config_hash": config_hash,
        "generated_metric_version": _METRIC_VERSION,
        "commanders": {
            name: {
                "earned_top30": m.earned_top30,
                "n_scored_cands": m.n_scored_cands,
                "n_synergy_buckets": m.n_synergy_buckets,
            }
            for name, m in sorted(metrics_by_cmdr.items())
        },
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def read_baseline(
    path: str | Path,
) -> tuple[str, dict[str, CoverageMetrics]]:
    doc = json.loads(Path(path).read_text())
    metrics = {
        name: CoverageMetrics(
            earned_top30=v["earned_top30"],
            n_scored_cands=v["n_scored_cands"],
            n_synergy_buckets=v["n_synergy_buckets"],
        )
        for name, v in doc["commanders"].items()
    }
    return doc["config_hash"], metrics


def poverty_queue(
    metrics: dict[str, CoverageMetrics],
) -> list[tuple[str, int]]:
    return sorted(
        ((name, m.earned_top30) for name, m in metrics.items()),
        key=lambda t: (t[1], t[0]),
    )


def stratified_control(
    metrics: dict[str, CoverageMetrics],
    *,
    exclude: set[str],
    size: int = 200,
    seed: int = 17,
) -> list[str]:
    """Deterministic sample spanning the earned_top30 distribution.

    Buckets the eligible pool into 10 bands by earned_top30, then draws
    ROUND-ROBIN across bands (one per band per pass, seeded shuffle within
    each band) so every non-empty band contributes a name before any band
    contributes a second. In this instrument's typical band-0-dominated pool
    that keeps the thin high-earned bands represented — a regression
    concentrated in any single band stays visible in the control, which a
    proportional draw (dominated by band 0) would not guarantee.

    Deterministic for a fixed seed; returns exactly ``size`` names when the
    pool exceeds ``size``, or the whole (sorted) pool otherwise.
    """
    pool = sorted(name for name in metrics if name not in exclude)
    if len(pool) <= size:
        return pool

    rng = random.Random(seed)  # noqa: S311
    strata: dict[int, list[str]] = {}
    for name in pool:
        band = min(metrics[name].earned_top30 // 3, 9)  # 0..9 (earned 0..30)
        strata.setdefault(band, []).append(name)
    for members in strata.values():
        members.sort()
        rng.shuffle(members)  # deterministic order within the band

    order = sorted(strata)
    cursor = dict.fromkeys(order, 0)
    picked: list[str] = []
    while len(picked) < size:
        progressed = False
        for band in order:
            if len(picked) >= size:
                break
            i = cursor[band]
            if i < len(strata[band]):
                picked.append(strata[band][i])
                cursor[band] = i + 1
                progressed = True
        if not progressed:  # pool exhausted (unreachable: len(pool) > size)
            break
    return sorted(picked)


def _compute_deltas(
    live: dict[str, CoverageMetrics],
    baseline: dict[str, CoverageMetrics],
) -> tuple[dict[str, int], float]:
    # A commander live-scored but absent from the pinned baseline cannot be
    # differenced and is dropped. This is silent data loss (the mean would be
    # computed over a smaller, possibly biased population) so warn loudly —
    # it happens when new commanders enter the legal pool after the baseline
    # census, which a cardsfolder data refresh does NOT force via config_hash.
    missing = sorted(name for name in live if name not in baseline)
    if missing:
        logger.warning(
            "coverage gate: %d live commander(s) absent from the pinned baseline, "
            "excluded from the delta (re-run `census` to refresh): %s",
            len(missing),
            ", ".join(missing),
        )
    deltas = {name: m.earned_top30 - baseline[name].earned_top30 for name, m in live.items() if name in baseline}
    mean = sum(deltas.values()) / len(deltas) if deltas else 0.0
    return deltas, mean


@dataclass(frozen=True)
class GateResult:
    cohort_delta_mean: float = 0.0
    cohort_deltas: dict[str, int] = field(default_factory=dict)
    control_delta_mean: float = 0.0
    control_deltas: dict[str, int] = field(default_factory=dict)


def run_gate(
    engine,
    conn,
    cohort_names,
    *,
    baseline: dict[str, CoverageMetrics],
    control_size: int = 200,
    seed: int = 17,
) -> GateResult:
    """Score the cohort + a stratified control and diff vs a pre-loaded,
    already-staleness-validated ``baseline``.

    The caller (:func:`_cmd_gate`) reads the baseline once and owns the
    config-hash staleness check, so it can fail fast WITHOUT opening the DB
    (a stale gate must not require a scored database) and this function does
    not re-read the baseline file.
    """
    cohort_set = set(cohort_names)
    control = stratified_control(baseline, exclude=cohort_set, size=control_size, seed=seed)
    live_cohort = run_census(engine, conn, commanders=sorted(cohort_set))
    live_control = run_census(engine, conn, commanders=control)

    cohort_deltas, cohort_mean = _compute_deltas(live_cohort, baseline)
    control_deltas, control_mean = _compute_deltas(live_control, baseline)
    return GateResult(
        cohort_delta_mean=cohort_mean,
        cohort_deltas=cohort_deltas,
        control_delta_mean=control_mean,
        control_deltas=control_deltas,
    )


# ---------------------------------------------------------------------------
# CLI (census / queue / gate)
# ---------------------------------------------------------------------------


def _resolve_cohort(name: str, conn: sqlite3.Connection) -> list[str]:
    predicate = _COHORT_DISPATCH.get(name)
    if predicate is None:
        raise ValueError(f"unknown cohort {name!r} — known cohorts: {sorted(_COHORT_DISPATCH)}")
    return sorted(predicate(conn))


def _open_engine_and_conn(db_path: str) -> tuple[SynergyEngine, sqlite3.Connection]:
    """Two-connection pattern (mirrors ``bench/forensics.py``): ``open_db``
    for direct SQL + a separate ``SynergyEngine`` for scoring, both against
    the same on-disk DB path, ``create=False`` so a missing DB raises
    ``FileNotFoundError`` with a rebuild hint instead of materializing an
    empty one."""
    conn = open_db(db_path, create=False)
    engine = SynergyEngine(Path(db_path))
    return engine, conn


def _cmd_census(args: argparse.Namespace) -> int:
    engine, conn = _open_engine_and_conn(args.db)
    try:
        metrics = run_census(engine, conn, commanders=args.commander)
    finally:
        engine.close()
        conn.close()
    config_hash = compute_config_hash()
    write_baseline(args.out, metrics, config_hash=config_hash)
    print(
        f"census: {len(metrics)} commander(s) scored; baseline written to {args.out} "
        f"(config_hash={config_hash[:12]}...)"
    )
    return 0


def _print_queue(rows: list[tuple[str, int]], *, fmt: str) -> None:
    if fmt == "csv":
        print("name,earned_top30")
        for name, earned in rows:
            print(f"{name},{earned}")
        return
    for name, earned in rows:
        print(f"{earned:3d}  {name}")


def _cmd_queue(args: argparse.Namespace) -> int:
    _config_hash, metrics = read_baseline(args.baseline)
    rows = poverty_queue(metrics)[: args.top]
    _print_queue(rows, fmt=args.format)
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    live_config_hash = args.force_config_hash if args.force_config_hash is not None else compute_config_hash()

    # Read the baseline ONCE and check staleness before opening the DB: a
    # stale gate must fail fast without requiring a scored database (open_db
    # would otherwise raise on a missing DB). The parsed baseline is handed to
    # run_gate so the file is not read twice.
    baseline_hash, baseline = read_baseline(args.baseline)
    if baseline_hash != live_config_hash:
        print(
            f"error: baseline is stale — baseline config_hash "
            f"{baseline_hash[:12]}... != live {live_config_hash[:12]}... "
            f"Re-run `census` to refresh {args.baseline} before gating."
        )
        return 1

    engine, conn = _open_engine_and_conn(args.db)
    try:
        cohort_names = _resolve_cohort(args.cohort, conn)
        result = run_gate(
            engine,
            conn,
            cohort_names,
            baseline=baseline,
            control_size=args.control_size,
            seed=args.seed,
        )
    finally:
        engine.close()
        conn.close()

    print(f"cohort delta mean: {result.cohort_delta_mean:+.3f} ({len(result.cohort_deltas)} commanders)")
    for name in sorted(result.cohort_deltas):
        print(f"  {name}: {result.cohort_deltas[name]:+d}")

    control_values = list(result.control_deltas.values())
    control_min = min(control_values) if control_values else 0
    negatives = sorted(name for name in result.control_deltas if result.control_deltas[name] < 0)
    print(f"control delta mean: {result.control_delta_mean:+.3f} ({len(result.control_deltas)} commanders)")
    print(f"control delta distribution: min={control_min:+d}, negatives={len(negatives)}")
    for name in negatives:
        print(f"  {name}: {result.control_deltas[name]:+d}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coverage_report.py",
        description="Activation-poverty coverage instrument (census / queue / gate).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_census = sub.add_parser("census", help="score the full legal-commander universe and pin a baseline")
    p_census.add_argument("--db", default=_DEFAULT_DB, help="synergy DB path")
    p_census.add_argument("--out", default=_DEFAULT_BASELINE, help="baseline output path")
    p_census.add_argument(
        "--commander",
        metavar="NAME",
        action="append",
        default=None,
        help="restrict census to this commander (exact name; repeatable; debugging/test subset)",
    )
    p_census.set_defaults(func=_cmd_census)

    p_queue = sub.add_parser("queue", help="print the poverty queue (poorest earned_top30 first) from a baseline")
    p_queue.add_argument("--baseline", default=_DEFAULT_BASELINE, help="baseline path (from `census`)")
    p_queue.add_argument("--top", type=int, default=50, help="number of poorest commanders to print")
    p_queue.add_argument("--format", choices=("text", "csv"), default="text", help="output format")
    p_queue.set_defaults(func=_cmd_queue)

    p_gate = sub.add_parser(
        "gate", help="measure a cohort's earned_top30 lift vs baseline against a stratified control"
    )
    p_gate.add_argument("--db", default=_DEFAULT_DB, help="synergy DB path")
    p_gate.add_argument("--baseline", default=_DEFAULT_BASELINE, help="baseline path (from `census`)")
    p_gate.add_argument("--cohort", choices=sorted(_COHORT_DISPATCH), required=True, help="named cohort predicate")
    p_gate.add_argument("--control-size", type=int, default=200, help="stratified control sample size")
    p_gate.add_argument("--seed", type=int, default=17, help="control-sample RNG seed")
    p_gate.add_argument(
        "--force-config-hash",
        default=None,
        help=argparse.SUPPRESS,  # test seam only: override the live config_hash
    )
    p_gate.set_defaults(func=_cmd_gate)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
