"""Activation-poverty census / queue / gate (coverage instrument CLI core).

Zero scoring-path impact. ``census`` runs the whole legal commander universe
through ``engine.page()`` and pins a config-hash-stamped baseline; ``queue``
ranks commanders by ``earned_top30`` (the coverage successor to gap_report);
``gate`` measures a cohort's ``earned_top30`` lift vs baseline against a
stratified control sample. See
``docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md``.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from mtg_synergy_graph.bench.coverage import CoverageMetrics, compute_coverage
from mtg_synergy_graph.engine import SynergyEngine

_METRIC_VERSION = 1


def legal_commander_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM cards "
        "WHERE legal_commander = 1 "
        "AND supertypes LIKE '%Legendary%' "
        "AND card_types LIKE '%Creature%' "
        "ORDER BY name"
    )
    return [name for (name,) in rows]


def commander_coverage(engine: SynergyEngine, commander: str) -> CoverageMetrics:
    page = engine.page([commander], offset=0, limit=1_000_000)
    metrics = compute_coverage(page.items, top_n=30)
    engine._score_cache.clear()  # bound memory across a ~2000-commander loop
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

    Buckets the eligible pool into 10 strata by earned_top30, then draws
    proportionally (seeded) so the control spans poverty-poor to
    poverty-rich commanders — a regression anywhere is visible.
    """
    pool = sorted(name for name in metrics if name not in exclude)
    if len(pool) <= size:
        return pool

    rng = random.Random(seed)  # noqa: S311
    strata: dict[int, list[str]] = {}
    for name in pool:
        band = min(metrics[name].earned_top30 // 3, 9)  # 0..9 (earned 0..30)
        strata.setdefault(band, []).append(name)

    picked: list[str] = []
    per = max(1, size // max(1, len(strata)))
    for band in sorted(strata):
        members = sorted(strata[band])
        rng.shuffle(members)
        picked.extend(members[:per])

    # Top up / trim deterministically to exactly `size`.
    if len(picked) < size:
        remaining = sorted(set(pool) - set(picked))
        rng.shuffle(remaining)
        picked.extend(remaining[: size - len(picked)])
    return sorted(picked[:size])


def _compute_deltas(
    live: dict[str, CoverageMetrics],
    baseline: dict[str, CoverageMetrics],
) -> tuple[dict[str, int], float]:
    deltas = {name: m.earned_top30 - baseline[name].earned_top30 for name, m in live.items() if name in baseline}
    mean = sum(deltas.values()) / len(deltas) if deltas else 0.0
    return deltas, mean


@dataclass(frozen=True)
class GateResult:
    cohort_delta_mean: float = 0.0
    cohort_deltas: dict[str, int] = field(default_factory=dict)
    control_delta_mean: float = 0.0
    control_deltas: dict[str, int] = field(default_factory=dict)
    stale_baseline: bool = False


def run_gate(
    engine,
    conn,
    baseline_path,
    cohort_names,
    *,
    live_config_hash: str,
    control_size: int = 200,
    seed: int = 17,
) -> GateResult:
    baseline_hash, baseline = read_baseline(baseline_path)
    if baseline_hash != live_config_hash:
        return GateResult(stale_baseline=True)

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
        stale_baseline=False,
    )
