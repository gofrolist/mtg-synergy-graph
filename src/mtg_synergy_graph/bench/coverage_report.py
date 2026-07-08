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
import sqlite3
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
