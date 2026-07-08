"""Stage 0 demand-coverage instrument (plan 2026-07-02-005, Unit 2).

READ-ONLY measurement over existing tables — zero scoring-path
changes. For every commander in the golden-set fixture it classifies
each commander port THREE ways (unconsumed / consumed-but-starved /
consumed-with-material-yield), diagnoses feeder yield for the named
starved feeders, and measures the addressable share of NO_RULES
forensics misses against the pre-pinned provisional pairing table
(``src/mtg_synergy_graph/data/resource_flows_seed.json``, committed in
Unit 1 BEFORE this instrument existed — the R1b-pin ordering
evidence).

Consumption predicate (R1, pinned)
----------------------------------
A commander port is CONSUMED when any gate in
``complement_rules.registry.RULE_GATES`` fires on it EXCLUDING
rule_ids in ``registry.CARD_LEVEL_RULES``, unioned with the
declarative interpreter's compiled gate closures (the
``scripts/gap_report.py::_scan_universe`` precedent). This module is
the FIRST PRODUCTION CONSUMER of the ``CARD_LEVEL_RULES`` subtraction:
those rules match on the commander's card attributes (subtypes, text),
so their firing says nothing about per-PORT coverage — counting them
would mark a tribal commander's every port "covered" because the
commander happens to be a Goblin. (In the current registry the two
sets are disjoint, so the subtraction is a standing guard rather than
an active filter; it is implemented and tested regardless because the
origin pins it and future gate registrations must not silently bypass
it.)

Starved predicate (pinned in origin R1, not revisable)
------------------------------------------------------
A consumed port is STARVED when the commander's distinct
activated-candidate count (``forensics.load_tensor_candidates``) is
below :data:`ACTIVATED_CANDIDATE_FLOOR`, OR every consuming rule
contributes zero cards to the commander's live top-30. (The origin
phrases the second leg per consuming rule; for a port with multiple
consumers, material yield requires at least one consumer actually
delivering into the top-30 — documented interpretation.)

Starved-yield diagnosis (pinned verbatim in the plan's
"Deferred to Implementation" block)
------------------------------------------------------
Per starved consuming rule: **IDF-burial** when the rule's supply
cards ARE present in the commander's tensor rows (the rule fires and
delivers candidates) but ZERO of them reach the commander's top-30
AND the rule's supply pool size is >= :data:`BURIAL_POOL_FLOOR`
(large pool → low IDF is the burial mechanism). **Wrong-supply-cards**
= the rule fires but its delivered candidates do not intersect the
commander's NO_RULES miss list (note: by tensor semantics a delivered
candidate can never be a NO_RULES miss, so this is the expected
diagnosis for delivering-but-not-buried rules — exactly the origin's
"supply cards that are not the labeled misses" reading).
**Pool-size** = everything else (rule fires, pool < 500, few or no
candidates delivered). "Supply pool size" for a consuming rule is
operationalized as the rule's fixture-wide distinct delivered-candidate
count in the persisted tensor at the current config_hash — the rule's
effective supply pool.

NO_RULES miss lists / live top-30
---------------------------------
One in-process ``forensics.compute_forensics(...)`` call over the
fixture, with ``independent_check=False`` — the sampled
independent-engine re-score is redundant here (this instrument
consumes bucket membership and the live top-30, not the NDCG
sidecars) and skipping it saves a second scoring pass. The call DOES
run a live production ranking per commander, so a full-fixture run
takes minutes. The live top-30 used by the starved predicate and the
burial criterion comes from this same pass
(``CommanderForensics.live_top_30``) — no second hand-rolled ranking.

Exit-code taxonomy (mirrors ``bench/portfolio_sim.py``):
0 reports written; 1 named harness failure; 2 missing precondition
(missing DB / tags DB / fixture / flows seed, stale tensor); 3
self-check failure — do not trust any report from an exit-3 run.
Reports go to ``--output`` (default ``.audit/demand_coverage/``).
This instrument NEVER appends to ``.audit/history.csv``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import random
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mtg_synergy_graph.bench.forensics import (
    BUCKET_NO_RULES,
    ForensicsPreconditionError,
    ForensicsReconciliationError,
    ForensicsReport,
    compute_forensics,
    load_tensor_candidates,
    load_tensor_contributions,
    normalize_label_name,
    synergy_content_digest,
)
from mtg_synergy_graph.bench.tensor import commander_filter_sql, compute_config_hash
from mtg_synergy_graph.complement_rules.core import PortRow
from mtg_synergy_graph.complement_rules.registry import (
    CARD_LEVEL_RULES,
    RULE_GATES,
    RuleGate,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import clear_ports_cache, load_ports_for_set
from mtg_synergy_graph.port_graph.interpreter import RuleInterpreter
from mtg_synergy_graph.port_graph.resource_flows import (
    PortShape,
    ResourceFlow,
    load_resource_flows,
)

# ---------------------------------------------------------------------------
# Pinned boundaries (origin R1/R1b-pin/R2 + plan "Deferred to
# Implementation"). NOT revisable after data is seen; the keyword
# parameters on the pure functions exist as TEST SEAMS only — the CLI
# never exposes them.
# ---------------------------------------------------------------------------

#: Starved predicate leg 1: activated-candidate universe floor.
ACTIVATED_CANDIDATE_FLOOR = 1_000

#: IDF-burial diagnosis: minimum rule supply-pool size.
BURIAL_POOL_FLOOR = 500

#: R1b-pin anti-gaming bound: a flow whose measured supplier pool
#: exceeds this is EXCLUDED from the funding numerator and reported
#: as bound-failing ("indiscriminate matching" DECLINE evidence).
SUPPLY_POOL_CAP = 2_000

#: R2 routing bars — reported for the Unit 3 adjudication; NON-GATING
#: here (the instrument measures, Unit 3 routes).
FUNDING_SHARE_BAR = 0.25
AGGREGATE_LABEL_FLOOR = 100

#: Null-model RNG seed (R1b-pin: fixed seed, deterministic).
NULL_MODEL_SEED = 17

#: The seven named commanders — qualitative VERIFICATION set only;
#: the cohort is defined BY RULE (< ACTIVATED_CANDIDATE_FLOOR).
NAMED_VERIFICATION_COMMANDERS: tuple[str, ...] = (
    "Yawgmoth, Thran Physician",
    "Borborygmos Enraged",
    "Araumi of the Dead Tide",
    "Phenax, God of Deception",
    "Slimefoot, the Stowaway",
    "Osgir, the Reconstructor",
    "The Gitrog Monster",
)

#: Feeder-yield diagnosis targets (origin R1(a)).
YIELD_DIAGNOSIS_RULES: tuple[str, ...] = ("gy_fuel_feeder", "cost_payoff")

# Three-way port classification labels.
CLASS_UNCONSUMED = "unconsumed"
CLASS_STARVED = "consumed_starved"
CLASS_MATERIAL = "consumed_material_yield"

# Starved-yield diagnosis labels (pinned).
DIAG_IDF_BURIAL = "idf_burial"
DIAG_WRONG_SUPPLY = "wrong_supply_cards"
DIAG_POOL_SIZE = "pool_size"

# Per-flow commander demand status.
DEMAND_HEALTHY = "healthy"  # matches an unconsumed or non-burial-starved port
DEMAND_BURIAL_ONLY = "burial_only"  # matches ONLY burial-diagnosed starved ports
DEMAND_NONE = "none"  # no under-served consumer-port match

# Per-miss reachability classification.
REACH_HEALTHY = "reachable"
REACH_BURIAL_EXCLUDED = "burial_excluded"
REACH_NONE = "unreachable"

_DEFAULT_FIXTURE = "tests/fixtures/golden_set_run.json"
_DEFAULT_OUTPUT_DIR = ".audit/demand_coverage"


class DemandCoverageError(RuntimeError):
    """Base named error for the demand-coverage instrument (exit 1)."""


class DemandPreconditionError(DemandCoverageError):
    """A precondition failed — nothing was computed (exit 2)."""


class SelfCheckError(DemandCoverageError):
    """An internal arithmetic invariant failed (exit 3): do not trust
    any report from this run."""


# ---------------------------------------------------------------------------
# Consumption predicate (pure once gates are built)
# ---------------------------------------------------------------------------

GatePair = tuple[str, Callable[[PortRow], bool]]


def build_consumption_gates(
    conn: sqlite3.Connection,
    *,
    rule_gates: Sequence[RuleGate] = RULE_GATES,
    card_level: frozenset[str] = CARD_LEVEL_RULES,
) -> tuple[GatePair, ...]:
    """Gate union for the R1 consumption predicate.

    ``RULE_GATES`` minus ``CARD_LEVEL_RULES`` (see module docstring —
    first production consumer of the subtraction), unioned with the
    declarative interpreter's compiled gate closures exactly as
    ``gap_report._scan_universe`` does. ``rule_gates`` / ``card_level``
    are injectable test seams; production callers use the defaults.
    """
    python_gates: list[GatePair] = [(g.rule_id, g.predicate) for g in rule_gates if g.rule_id not in card_level]
    interpreter = RuleInterpreter(conn)
    declarative_gates: list[GatePair] = [(c.row.rule_id, c.gate) for c in interpreter._compiled]
    return tuple(python_gates + declarative_gates)


def consuming_rules_for_port(port: PortRow, gates: Sequence[GatePair]) -> frozenset[str]:
    """Rule ids whose consumption gate fires on ``port``."""
    return frozenset(rule_id for rule_id, predicate in gates if predicate(port))


def classify_port_state(
    consuming_rules: frozenset[str],
    *,
    activated_count: int,
    rule_top30_counts: Mapping[str, int],
    floor: int = ACTIVATED_CANDIDATE_FLOOR,
    top30_available: bool = True,
) -> str:
    """Three-way classification for one commander port (pinned).

    ``rule_top30_counts`` maps rule_id → number of that rule's
    tensor-delivered candidates inside the commander's live top-30.
    ``top30_available=False`` is the carve-out for zero-label
    (forensics-skipped) commanders whose live ranking was never
    extracted: the zero-top-30 OR leg cannot be evaluated there, so
    only the activated-count leg applies (documented measurement
    limitation; the canonical 100-commander fixture has no skipped
    entries).
    """
    if not consuming_rules:
        return CLASS_UNCONSUMED
    if activated_count < floor:
        return CLASS_STARVED
    if top30_available and all(rule_top30_counts.get(rule_id, 0) == 0 for rule_id in consuming_rules):
        return CLASS_STARVED
    return CLASS_MATERIAL


def diagnose_starved_rule(
    *,
    delivered: frozenset[str],
    top30_delivered: int,
    pool_size: int,
    no_rules_misses: frozenset[str],
    burial_pool_floor: int = BURIAL_POOL_FLOOR,
) -> str:
    """Pinned starved-yield diagnosis for one consuming rule.

    See module docstring for the verbatim criterion. ``delivered`` is
    the rule's tensor-delivered candidate set for THIS commander;
    ``pool_size`` is the rule's fixture-wide distinct delivered-candidate
    count (the effective supply pool).
    """
    if delivered and top30_delivered == 0 and pool_size >= burial_pool_floor:
        return DIAG_IDF_BURIAL
    if delivered and not (delivered & no_rules_misses):
        return DIAG_WRONG_SUPPLY
    return DIAG_POOL_SIZE


def port_starved_diagnosis(rule_diagnoses: Mapping[str, str]) -> str:
    """Fold per-rule diagnoses into one port-level label.

    A port is burial-diagnosed only when EVERY consuming rule's
    diagnosis is IDF-burial — if any consumer starves for a
    non-burial reason, a new rule over a different pool would not
    inherit the burial, so the port stays in the healthy under-served
    mass. Otherwise wrong-supply wins over pool-size (it is the
    actionable diagnosis).
    """
    if not rule_diagnoses:
        raise ValueError("port_starved_diagnosis requires at least one rule diagnosis")
    diagnoses = set(rule_diagnoses.values())
    if diagnoses == {DIAG_IDF_BURIAL}:
        return DIAG_IDF_BURIAL
    if DIAG_WRONG_SUPPLY in diagnoses:
        return DIAG_WRONG_SUPPLY
    return DIAG_POOL_SIZE


# ---------------------------------------------------------------------------
# Shape matching (consumer side: pure Python over PortRow dicts)
# ---------------------------------------------------------------------------


def shape_matches_port(
    shape: PortShape,
    port: PortRow,
    *,
    attrs: Sequence[tuple[str, str]] = (),
    card_types: str = "",
) -> bool:
    """Does one seed shape match one port row?

    ``attrs`` are the port's non-negated ``port_attributes``
    ``(attr_kind, attr_value)`` rows; ``card_types`` is the carrying
    card's ``cards.types`` string (space-separated, repo convention:
    substring containment, mirroring the ``LIKE '%...%'`` idiom the
    SQL side uses). ``cost_target_not`` treats a NULL ``cost_target``
    as passing (NULL is "not self").
    """
    if (port.get("port_type") or "").strip() != shape.port_type:
        return False
    event_class = (port.get("event_class") or "").strip()
    if shape.event_class is not None and event_class != shape.event_class:
        return False
    if shape.event_class_prefix is not None and not event_class.startswith(shape.event_class_prefix):
        return False
    if shape.cost_target is not None and (port.get("cost_target") or "").strip() != shape.cost_target:
        return False
    if shape.cost_target_not is not None and (port.get("cost_target") or "").strip() == shape.cost_target_not:
        return False
    if shape.zone_origin is not None and (port.get("zone_origin") or "").strip() != shape.zone_origin:
        return False
    if shape.zone_destination is not None and (port.get("zone_destination") or "").strip() != shape.zone_destination:
        return False
    if shape.valid_filter is not None and (port.get("valid_filter") or "").strip() != shape.valid_filter:
        return False
    if shape.attr_kind is not None:
        not_in = shape.attr_value_not_in or ()
        if not any(kind == shape.attr_kind and value not in not_in for kind, value in attrs):
            return False
    if shape.card_types_not_contains is None:
        return True
    return not any(token in card_types for token in shape.card_types_not_contains)


def flow_demand_status(
    flow: ResourceFlow,
    ports: Sequence[PortRow],
    port_classes: Mapping[int, str],
    port_diagnoses: Mapping[int, str],
    *,
    attrs_by_port: Mapping[int, Sequence[tuple[str, str]]],
    card_types: str,
) -> str:
    """Per-commander demand status for one flow.

    Walks the commander's ports; for each port matched by any of the
    flow's consumer shapes, looks at its three-way class:
    ``healthy`` when any matched port is unconsumed or starved with a
    non-burial diagnosis; ``burial_only`` when the only matched
    under-served ports are burial-diagnosed; ``none`` when no matched
    port is under-served. ``port_classes`` / ``port_diagnoses`` /
    ``attrs_by_port`` are keyed by ``card_ports.id``.
    """
    saw_burial_only = False
    for port in ports:
        port_id = int(port["id"])
        if not any(
            shape_matches_port(shape, port, attrs=attrs_by_port.get(port_id, ()), card_types=card_types)
            for shape in flow.consumers
        ):
            continue
        cls = port_classes[port_id]
        if cls == CLASS_UNCONSUMED:
            return DEMAND_HEALTHY
        if cls == CLASS_STARVED:
            if port_diagnoses.get(port_id) == DIAG_IDF_BURIAL:
                saw_burial_only = True
            else:
                return DEMAND_HEALTHY
    return DEMAND_BURIAL_ONLY if saw_burial_only else DEMAND_NONE


def classify_miss_reachability(
    miss_name: str,
    flow_pools: Mapping[str, frozenset[str]],
    flow_status: Mapping[str, str],
) -> str:
    """R2 reachability for one NO_RULES miss (pinned semantics).

    ``flow_pools`` must already exclude bound-failing flows. A miss is
    ``reachable`` when at least one flow both supplies it (pool
    membership) and has HEALTHY under-served demand on this commander;
    ``burial_excluded`` when every supplying flow's demand is
    burial-only ("reachable ONLY via burial-diagnosed ports");
    ``unreachable`` otherwise.
    """
    saw_burial_only = False
    for flow_name, pool in flow_pools.items():
        if miss_name not in pool:
            continue
        status = flow_status.get(flow_name, DEMAND_NONE)
        if status == DEMAND_HEALTHY:
            return REACH_HEALTHY
        if status == DEMAND_BURIAL_ONLY:
            saw_burial_only = True
    return REACH_BURIAL_EXCLUDED if saw_burial_only else REACH_NONE


def build_null_pools(
    flow_pools: Mapping[str, frozenset[str]],
    all_card_names: Sequence[str],
    *,
    seed: int = NULL_MODEL_SEED,
) -> dict[str, frozenset[str]]:
    """Same-size random supply pool per flow (R1b-pin null model).

    Deterministic: one ``random.Random(seed)`` walked over the flows
    in sorted-name order, sampling from the sorted card-name universe.
    """
    rng = random.Random(seed)  # noqa: S311 — statistical null model, not crypto
    universe = sorted(all_card_names)
    out: dict[str, frozenset[str]] = {}
    for flow_name in sorted(flow_pools):
        k = min(len(flow_pools[flow_name]), len(universe))
        out[flow_name] = frozenset(rng.sample(universe, k))
    return out


# ---------------------------------------------------------------------------
# Supplier pools (SQL over card_ports / port_attributes / cards)
# ---------------------------------------------------------------------------


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _shape_pool(conn: sqlite3.Connection, shape: PortShape) -> frozenset[str]:
    """Distinct card names carrying a port that matches ``shape``.

    SQL is assembled from FIXED fragments only; every value is a bound
    parameter (repo convention — the shape fields were already
    frozenset-validated by ``load_resource_flows``).
    """
    clauses = ["cp.port_type = ?"]
    params: list[object] = [shape.port_type]
    if shape.event_class is not None:
        clauses.append("cp.event_class = ?")
        params.append(shape.event_class)
    if shape.event_class_prefix is not None:
        clauses.append("cp.event_class LIKE ? ESCAPE '\\'")
        params.append(_escape_like(shape.event_class_prefix) + "%")
    if shape.cost_target is not None:
        clauses.append("cp.cost_target = ?")
        params.append(shape.cost_target)
    if shape.cost_target_not is not None:
        clauses.append("(cp.cost_target IS NULL OR cp.cost_target <> ?)")
        params.append(shape.cost_target_not)
    if shape.zone_origin is not None:
        clauses.append("cp.zone_origin = ?")
        params.append(shape.zone_origin)
    if shape.zone_destination is not None:
        clauses.append("cp.zone_destination = ?")
        params.append(shape.zone_destination)
    if shape.valid_filter is not None:
        clauses.append("cp.valid_filter = ?")
        params.append(shape.valid_filter)
    if shape.attr_kind is not None:
        not_in = shape.attr_value_not_in or ()
        placeholders = ",".join("?" * len(not_in))
        # Only ? placeholder COUNTS are interpolated; values are bound.
        not_in_sql = f" AND pa.attr_value NOT IN ({placeholders})" if not_in else ""
        clauses.append(
            "EXISTS (SELECT 1 FROM port_attributes pa WHERE pa.port_id = cp.id "  # noqa: S608 — placeholders only
            "AND pa.attr_kind = ? AND pa.is_negated = 0" + not_in_sql + ")"
        )
        params.append(shape.attr_kind)
        params.extend(not_in)
    if shape.card_types_not_contains is not None:
        for token in shape.card_types_not_contains:
            clauses.append("COALESCE(c.types, '') NOT LIKE ? ESCAPE '\\'")
            params.append("%" + _escape_like(token) + "%")
    # Fixed, frozenset-validated fragments only; all values are bound
    # parameters (repo convention).
    sql = "SELECT DISTINCT cp.card_name FROM card_ports cp JOIN cards c ON c.name = cp.card_name WHERE " + " AND ".join(  # noqa: S608
        clauses
    )
    return frozenset(row[0] for row in conn.execute(sql, params).fetchall())


def supplier_pool(conn: sqlite3.Connection, flow: ResourceFlow) -> frozenset[str]:
    """Union of the flow's supplier-shape pools (distinct card names)."""
    pool: frozenset[str] = frozenset()
    for shape in flow.suppliers:
        pool |= _shape_pool(conn, shape)
    return pool


def _load_port_attributes(
    conn: sqlite3.Connection,
    port_ids: Sequence[int],
) -> dict[int, tuple[tuple[str, str], ...]]:
    """Non-negated ``port_attributes`` rows keyed by port id."""
    if not port_ids:
        return {}
    placeholders = ",".join("?" * len(port_ids))
    rows = conn.execute(
        "SELECT port_id, attr_kind, attr_value FROM port_attributes "  # noqa: S608 — placeholders only
        f"WHERE is_negated = 0 AND port_id IN ({placeholders}) "
        "ORDER BY port_id, attr_kind, attr_value",
        tuple(port_ids),
    ).fetchall()
    out: dict[int, list[tuple[str, str]]] = {}
    for row in rows:
        out.setdefault(int(row["port_id"]), []).append((str(row["attr_kind"]), str(row["attr_value"])))
    return {port_id: tuple(attrs) for port_id, attrs in out.items()}


def _rule_pool_sizes(
    conn: sqlite3.Connection,
    config_hash: str,
    commanders: Sequence[str] | None = None,
) -> dict[str, int]:
    """Fixture-wide distinct delivered-candidate count per rule_id.

    ``commanders`` MUST be the run's fixture commanders. Additive
    ``--repin`` lets several fixtures share one ``config_hash``, so an
    unfiltered read spans the UNION of pinned fixtures — which inflates
    each rule's pool size and can push it across the fixed
    ``BURIAL_POOL_FLOOR`` (500), misclassifying a rule as IDF-burial. The
    filter keeps the count genuinely fixture-wide (the ``pool_size``/
    ``pool_size_fixture_wide`` this feeds is compared against a constant
    threshold, so a widened population is NOT harmless here). ``None`` is
    accepted only for direct callers/tests that want the union.
    """
    cmdr_sql, cmdr_params = commander_filter_sql(commanders)
    rows = conn.execute(
        f"SELECT rule_id, COUNT(DISTINCT candidate) AS n FROM rule_contributions "  # noqa: S608
        f"WHERE config_hash = ?{cmdr_sql} GROUP BY rule_id",
        (config_hash, *cmdr_params),
    ).fetchall()
    return {str(row["rule_id"]): int(row["n"]) for row in rows}


def _port_signature(port: PortRow) -> str:
    """Human-readable per-port signature for the report."""
    sig = f"{(port.get('port_type') or '').strip()} {(port.get('event_class') or '').strip()}"
    cost_target = (port.get("cost_target") or "").strip()
    if cost_target:
        sig += f"/{cost_target}"
    return sig


# ---------------------------------------------------------------------------
# Per-commander classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortReport:
    """One classified commander port (report row)."""

    port_id: int
    signature: str
    classification: str
    consuming_rules: tuple[str, ...]
    starved_diagnosis: str | None
    rule_diagnoses: Mapping[str, str]


def classify_commander_ports(
    ports: Sequence[PortRow],
    gates: Sequence[GatePair],
    *,
    activated_count: int,
    rule_delivered: Mapping[str, frozenset[str]],
    live_top30: frozenset[str],
    rule_pool_sizes: Mapping[str, int],
    no_rules_misses: frozenset[str],
    floor: int = ACTIVATED_CANDIDATE_FLOOR,
    burial_pool_floor: int = BURIAL_POOL_FLOOR,
    top30_available: bool = True,
) -> list[PortReport]:
    """Three-way classify + diagnose every port of one commander."""
    rule_top30_counts = {rule_id: len(delivered & live_top30) for rule_id, delivered in rule_delivered.items()}
    out: list[PortReport] = []
    for port in ports:
        consuming = consuming_rules_for_port(port, gates)
        cls = classify_port_state(
            consuming,
            activated_count=activated_count,
            rule_top30_counts=rule_top30_counts,
            floor=floor,
            top30_available=top30_available,
        )
        rule_diagnoses: dict[str, str] = {}
        diagnosis: str | None = None
        if cls == CLASS_STARVED:
            for rule_id in sorted(consuming):
                delivered = rule_delivered.get(rule_id, frozenset())
                rule_diagnoses[rule_id] = diagnose_starved_rule(
                    delivered=delivered,
                    top30_delivered=rule_top30_counts.get(rule_id, 0),
                    pool_size=rule_pool_sizes.get(rule_id, 0),
                    no_rules_misses=no_rules_misses,
                    burial_pool_floor=burial_pool_floor,
                )
            diagnosis = port_starved_diagnosis(rule_diagnoses)
        out.append(
            PortReport(
                port_id=int(port["id"]),
                signature=_port_signature(port),
                classification=cls,
                consuming_rules=tuple(sorted(consuming)),
                starved_diagnosis=diagnosis,
                rule_diagnoses=rule_diagnoses,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _filtered_fixture(fixture_path: Path, commanders: Sequence[str], outdir: Path) -> Path:
    """Write a commander-filtered copy of the fixture for the
    ``compute_forensics`` pass (``--commander`` debugging filter)."""
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    wanted = set(commanders)
    entries = [e for e in data.get("entries", []) if e.get("commander") in wanted]
    filtered = {**data, "entries": entries}
    outdir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="fixture_subset_", suffix=".json", dir=outdir)
    path = Path(tmp_name)
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(filtered, f)
    return path


def _check_tensor_fresh(conn: sqlite3.Connection, config_hash: str) -> None:
    """Fail fast (exit 2) on a stale/absent tensor BEFORE the
    minutes-long forensics pass. Mirrors
    ``forensics._check_tensor_populated`` semantics."""
    row = conn.execute(
        "SELECT COUNT(*) FROM rule_contributions WHERE config_hash = ?",
        (config_hash,),
    ).fetchone()
    if int(row[0]) == 0:
        raise DemandPreconditionError(
            f"rule_contributions has no rows at the current config_hash "
            f"{config_hash[:12]}... — the persisted tensor is stale or absent. "
            "Re-pin via `bench.py audit --repin --yes` before running the "
            "demand-coverage instrument."
        )


def run_demand_coverage(
    *,
    db_path: Path | str,
    tags_path: Path | str,
    fixture_path: Path | str,
    commander_filter: Sequence[str] | None = None,
    output_dir: Path | str = _DEFAULT_OUTPUT_DIR,
    flows_seed_path: Path | str | None = None,
    seed: int = NULL_MODEL_SEED,
    top_n: int = 30,
    activated_floor: int = ACTIVATED_CANDIDATE_FLOOR,
    burial_pool_floor: int = BURIAL_POOL_FLOOR,
    supply_pool_cap: int = SUPPLY_POOL_CAP,
) -> dict[str, Any]:
    """Run the full Stage 0 measurement and return the report dict.

    Raises :class:`DemandPreconditionError` (exit 2) on missing/stale
    inputs, :class:`SelfCheckError` (exit 3) on arithmetic invariant
    failures, :class:`DemandCoverageError` (exit 1) on other named
    failures. The ``activated_floor`` / ``burial_pool_floor`` /
    ``supply_pool_cap`` keywords are TEST SEAMS with pinned defaults —
    the CLI never exposes them.
    """
    db_path = Path(db_path)
    tags_path = Path(tags_path)
    fixture_path = Path(fixture_path)
    outdir = Path(output_dir)

    # Preconditions BEFORE the expensive forensics pass.
    try:
        flows = load_resource_flows(Path(flows_seed_path) if flows_seed_path is not None else None)
    except ValueError as exc:
        raise DemandPreconditionError(str(exc)) from exc
    try:
        conn = open_db(db_path, create=False)
    except FileNotFoundError as exc:
        raise DemandPreconditionError(str(exc)) from exc

    forensics_fixture = fixture_path
    try:
        config_hash = compute_config_hash()
        _check_tensor_fresh(conn, config_hash)
        db_digest = synergy_content_digest(conn)

        # Supplier pools + R1b-pin bound check (cheap, pre-forensics).
        measured_pools = {name: supplier_pool(conn, flow) for name, flow in flows.items()}
        bound_failing = sorted(name for name, pool in measured_pools.items() if len(pool) > supply_pool_cap)
        eligible_pools = {name: pool for name, pool in measured_pools.items() if name not in bound_failing}

        all_card_names = [row[0] for row in conn.execute("SELECT name FROM cards ORDER BY name").fetchall()]
        known_names = frozenset(all_card_names)
        null_pools = build_null_pools(eligible_pools, all_card_names, seed=seed)
        # Self-check: null-model determinism under the fixed seed.
        if build_null_pools(eligible_pools, all_card_names, seed=seed) != null_pools:
            raise SelfCheckError("null-model pools are not deterministic under the fixed seed")
        for name, pool in null_pools.items():
            if len(pool) != min(len(eligible_pools[name]), len(all_card_names)):
                raise SelfCheckError(f"null pool size mismatch for flow {name!r}")

        if commander_filter:
            forensics_fixture = _filtered_fixture(fixture_path, commander_filter, outdir)

        # ONE in-process forensics pass: NO_RULES miss lists + live
        # top-30 per commander. independent_check=False — see module
        # docstring. This runs live production ranking (minutes).
        report: ForensicsReport = compute_forensics(
            db_path=db_path,
            tags_path=tags_path,
            fixture_path=forensics_fixture,
            top_n=top_n,
            independent_check=False,
        )

        gates = build_consumption_gates(conn)

        per_commander: list[dict[str, Any]] = []
        under_served_by_shape: dict[str, dict[str, Any]] = {}
        yield_rows: list[dict[str, Any]] = []
        cohort: list[str] = []
        cohort_counts = {REACH_HEALTHY: 0, REACH_BURIAL_EXCLUDED: 0, REACH_NONE: 0}
        cohort_null_reachable = 0
        cohort_total_misses = 0
        aggregate_counts = {REACH_HEALTHY: 0, REACH_BURIAL_EXCLUDED: 0, REACH_NONE: 0}
        aggregate_null_reachable = 0
        aggregate_total_misses = 0

        all_entries = list(report.entries)
        skipped = set(report.skipped_commanders)
        entry_by_name = {e.commander: e for e in all_entries}
        fixture_commanders = sorted(set(entry_by_name) | skipped)
        # Pool sizes MUST be scoped to this run's fixture commanders: they
        # feed a fixed-threshold burial test, so the additive-repin union
        # would otherwise inflate them and misclassify rules (see
        # _rule_pool_sizes).
        pool_sizes = _rule_pool_sizes(conn, config_hash, fixture_commanders)

        for commander in fixture_commanders:
            entry = entry_by_name.get(commander)
            is_skipped = entry is None
            live_top30 = frozenset(entry.live_top_30) if entry is not None else frozenset()

            tensor_rows = load_tensor_contributions(conn, commander, config_hash)
            rule_delivered: dict[str, frozenset[str]] = {}
            delivered_acc: dict[str, set[str]] = {}
            for candidate, rule_id, _value in tensor_rows:
                delivered_acc.setdefault(rule_id, set()).add(candidate)
            rule_delivered = {rule_id: frozenset(cands) for rule_id, cands in delivered_acc.items()}
            tensor_candidates = load_tensor_candidates(conn, commander, config_hash)
            # Self-check: the two tensor reads must agree on the
            # activated-candidate universe.
            derived = frozenset(cand for cands in rule_delivered.values() for cand in cands)
            if derived != tensor_candidates:
                raise SelfCheckError(
                    f"tensor candidate sets diverge for {commander!r}: "
                    f"{len(derived)} derived vs {len(tensor_candidates)} via load_tensor_candidates"
                )
            activated_count = len(tensor_candidates)

            no_rules_misses: frozenset[str] = frozenset()
            if entry is not None:
                resolved_misses = {
                    normalize_label_name(m.card_name, known_names) or m.card_name
                    for m in entry.misses
                    if m.bucket == BUCKET_NO_RULES
                }
                no_rules_misses = frozenset(resolved_misses)

            clear_ports_cache()
            ports = load_ports_for_set(conn, [commander])
            attrs_by_port = _load_port_attributes(conn, [int(p["id"]) for p in ports])
            cmdr_row = conn.execute("SELECT types FROM cards WHERE name = ?", (commander,)).fetchone()
            card_types = str(cmdr_row["types"] or "") if cmdr_row is not None else ""

            port_reports = classify_commander_ports(
                ports,
                gates,
                activated_count=activated_count,
                rule_delivered=rule_delivered,
                live_top30=live_top30,
                rule_pool_sizes=pool_sizes,
                no_rules_misses=no_rules_misses,
                floor=activated_floor,
                burial_pool_floor=burial_pool_floor,
                top30_available=not is_skipped,
            )
            class_counts = {CLASS_UNCONSUMED: 0, CLASS_STARVED: 0, CLASS_MATERIAL: 0}
            for pr in port_reports:
                class_counts[pr.classification] += 1
                if pr.classification in (CLASS_UNCONSUMED, CLASS_STARVED):
                    slot = under_served_by_shape.setdefault(pr.signature, {"ports": 0, "commanders": set()})
                    slot["ports"] += 1
                    slot["commanders"].add(commander)
            if sum(class_counts.values()) != len(ports):
                raise SelfCheckError(f"port classification partition does not sum for {commander!r}")

            # Feeder-yield diagnosis rows (origin R1(a)) — every
            # commander where the named feeder's gate fires on a port.
            for rule_id in YIELD_DIAGNOSIS_RULES:
                if not any(rule_id in pr.consuming_rules for pr in port_reports):
                    continue
                delivered = rule_delivered.get(rule_id, frozenset())
                top30_delivered = len(delivered & live_top30)
                yield_rows.append(
                    {
                        "commander": commander,
                        "rule_id": rule_id,
                        "delivered_candidates": len(delivered),
                        "top30_delivered": top30_delivered,
                        "pool_size_fixture_wide": pool_sizes.get(rule_id, 0),
                        "diagnosis": diagnose_starved_rule(
                            delivered=delivered,
                            top30_delivered=top30_delivered,
                            pool_size=pool_sizes.get(rule_id, 0),
                            no_rules_misses=no_rules_misses,
                            burial_pool_floor=burial_pool_floor,
                        ),
                    }
                )

            port_classes = {pr.port_id: pr.classification for pr in port_reports}
            port_diagnoses = {pr.port_id: pr.starved_diagnosis for pr in port_reports if pr.starved_diagnosis}
            flow_status = {
                name: flow_demand_status(
                    flow,
                    ports,
                    port_classes,
                    port_diagnoses,
                    attrs_by_port=attrs_by_port,
                    card_types=card_types,
                )
                for name, flow in flows.items()
            }
            eligible_status = {name: flow_status[name] for name in eligible_pools}

            reach_counts = {REACH_HEALTHY: 0, REACH_BURIAL_EXCLUDED: 0, REACH_NONE: 0}
            null_reachable = 0
            for miss in sorted(no_rules_misses):
                reach_counts[classify_miss_reachability(miss, eligible_pools, eligible_status)] += 1
                if classify_miss_reachability(miss, null_pools, eligible_status) == REACH_HEALTHY:
                    null_reachable += 1
            if sum(reach_counts.values()) != len(no_rules_misses):
                raise SelfCheckError(f"miss reachability partition does not sum for {commander!r}")

            in_cohort = activated_count < activated_floor
            if in_cohort:
                cohort.append(commander)
                cohort_total_misses += len(no_rules_misses)
                cohort_null_reachable += null_reachable
                for key, value in reach_counts.items():
                    cohort_counts[key] += value
            aggregate_total_misses += len(no_rules_misses)
            aggregate_null_reachable += null_reachable
            for key, value in reach_counts.items():
                aggregate_counts[key] += value

            per_commander.append(
                {
                    "commander": commander,
                    "skipped_zero_labels": is_skipped,
                    "activated_candidates": activated_count,
                    "in_cohort": in_cohort,
                    "ports_total": len(ports),
                    "classification_counts": class_counts,
                    "ports": [
                        {
                            "signature": pr.signature,
                            "classification": pr.classification,
                            "consuming_rules": list(pr.consuming_rules),
                            "starved_diagnosis": pr.starved_diagnosis,
                            "rule_diagnoses": dict(pr.rule_diagnoses),
                        }
                        for pr in port_reports
                    ],
                    "no_rules_misses": len(no_rules_misses),
                    "reachable": reach_counts[REACH_HEALTHY],
                    "burial_excluded": reach_counts[REACH_BURIAL_EXCLUDED],
                    "unreachable": reach_counts[REACH_NONE],
                    "null_model_reachable": null_reachable,
                    "flow_demand_status": flow_status,
                }
            )

        share = cohort_counts[REACH_HEALTHY] / cohort_total_misses if cohort_total_misses else None
        null_share = cohort_null_reachable / cohort_total_misses if cohort_total_misses else None
        # Recompute-style self-check on the share arithmetic.
        numer = sum(e["reachable"] for e in per_commander if e["in_cohort"])
        denom = sum(e["no_rules_misses"] for e in per_commander if e["in_cohort"])
        if numer != cohort_counts[REACH_HEALTHY] or denom != cohort_total_misses:
            raise SelfCheckError("cohort share numerator/denominator recompute diverged")

        result: dict[str, Any] = {
            "provenance": {
                "instrument": "demand_coverage",
                "plan": "2026-07-02-005 Unit 2",
                "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
                "config_hash": config_hash,
                "db_digest": db_digest,
                "fixture": str(fixture_path),
                "commander_filter": sorted(commander_filter) if commander_filter else None,
                "flows_seed": str(flows_seed_path) if flows_seed_path is not None else "packaged default",
                "null_model_seed": seed,
                "independent_check": False,
                "pinned_boundaries": {
                    "activated_candidate_floor": activated_floor,
                    "burial_pool_floor": burial_pool_floor,
                    "supply_pool_cap": supply_pool_cap,
                    "funding_share_bar": FUNDING_SHARE_BAR,
                    "aggregate_label_floor": AGGREGATE_LABEL_FLOOR,
                },
            },
            "flows": {
                name: {
                    "pool_size": len(measured_pools[name]),
                    "bound_failing": name in bound_failing,
                    "null_pool_size": len(null_pools.get(name, frozenset())),
                }
                for name in sorted(flows)
            },
            "bound_failing_flows": bound_failing,
            "per_commander": per_commander,
            "under_served_by_shape": {
                sig: {"ports": slot["ports"], "commanders": len(slot["commanders"])}
                for sig, slot in sorted(under_served_by_shape.items())
            },
            "cohort": {
                "rule": f"activated_candidates < {activated_floor}",
                "commanders": sorted(cohort),
                "named_verification": {
                    name: {
                        "in_fixture": name in set(fixture_commanders),
                        "in_cohort": name in set(cohort),
                    }
                    for name in NAMED_VERIFICATION_COMMANDERS
                },
                "total_no_rules_misses": cohort_total_misses,
                "reachable": cohort_counts[REACH_HEALTHY],
                "burial_excluded": cohort_counts[REACH_BURIAL_EXCLUDED],
                "unreachable": cohort_counts[REACH_NONE],
                "share": share,
                "null_model_reachable": cohort_null_reachable,
                "null_share": null_share,
                "exceeds_null": (share > null_share) if share is not None and null_share is not None else None,
                "meets_share_bar": (share >= FUNDING_SHARE_BAR) if share is not None else None,
            },
            "aggregate": {
                "total_no_rules_misses": aggregate_total_misses,
                "reachable_labels": aggregate_counts[REACH_HEALTHY],
                "burial_excluded": aggregate_counts[REACH_BURIAL_EXCLUDED],
                "unreachable": aggregate_counts[REACH_NONE],
                "null_model_reachable": aggregate_null_reachable,
                "meets_label_floor": aggregate_counts[REACH_HEALTHY] >= AGGREGATE_LABEL_FLOOR,
            },
            "yield_diagnosis": yield_rows,
        }
        return result
    finally:
        conn.close()
        # The ports cache is a WeakKeyDictionary keyed on the connection
        # object; clearing it here isn't strictly required for correctness
        # (the entry is dropped automatically once conn is finalized) but
        # frees the cached rows for this connection promptly.
        clear_ports_cache()
        if forensics_fixture != fixture_path:
            forensics_fixture.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Rendering + CLI
# ---------------------------------------------------------------------------


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable report.md mirror of the JSON report."""
    prov = report["provenance"]
    cohort = report["cohort"]
    agg = report["aggregate"]
    lines: list[str] = [
        "# demand_coverage — Stage 0 read-only instrument (plan 2026-07-02-005 Unit 2)",
        "",
        f"config_hash: `{prov['config_hash']}`",
        f"db_digest: `{prov['db_digest']}`",
        f"fixture: `{prov['fixture']}`"
        + (f" (filtered to {prov['commander_filter']})" if prov["commander_filter"] else ""),
        f"null-model seed: {prov['null_model_seed']}  •  created: {prov['created_at']}",
        "",
        "## Flows (provisional pairing table, R1b-pin)",
        "",
        "| flow | pool size | bound (<= {cap}) | null pool |".format(cap=prov["pinned_boundaries"]["supply_pool_cap"]),
        "|---|---:|---|---:|",
    ]
    for name, row in report["flows"].items():
        bound = "FAIL (excluded)" if row["bound_failing"] else "ok"
        lines.append(f"| {name} | {row['pool_size']} | {bound} | {row['null_pool_size']} |")
    lines += [
        "",
        "## Cohort addressable share (R2)",
        "",
        f"Cohort rule: `{cohort['rule']}` — {len(cohort['commanders'])} commanders.",
        "",
        f"- cohort NO_RULES misses: **{cohort['total_no_rules_misses']}**",
        f"- reachable (healthy under-served demand): **{cohort['reachable']}**",
        f"- burial-excluded (reachable ONLY via IDF-burial ports): {cohort['burial_excluded']}",
        f"- unreachable: {cohort['unreachable']}",
        f"- share: **{_fmt(cohort['share'])}** (funding bar {FUNDING_SHARE_BAR}: "
        f"{_verdict(cohort['meets_share_bar'])})",
        f"- null-model share: {_fmt(cohort['null_share'])} (measured exceeds null: {_verdict(cohort['exceeds_null'])})",
        "",
        "### Named verification commanders (qualitative set — cohort is defined by rule)",
        "",
        "| commander | in fixture | in cohort |",
        "|---|---|---|",
    ]
    for name, row in cohort["named_verification"].items():
        lines.append(f"| {name} | {row['in_fixture']} | {row['in_cohort']} |")
    lines += [
        "",
        "## Aggregate readout (non-gating, feeds the >=100-label floor)",
        "",
        f"- fixture-wide NO_RULES misses: {agg['total_no_rules_misses']}",
        f"- reachable labels: **{agg['reachable_labels']}** "
        f"(floor {AGGREGATE_LABEL_FLOOR}: {_verdict(agg['meets_label_floor'])})",
        f"- burial-excluded: {agg['burial_excluded']}  •  unreachable: {agg['unreachable']}",
        f"- null-model reachable: {agg['null_model_reachable']}",
        "",
        "## Feeder-yield diagnosis (origin R1(a))",
        "",
        "| commander | rule | delivered | in top-30 | pool (fixture-wide) | diagnosis |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in report["yield_diagnosis"]:
        lines.append(
            f"| {row['commander']} | {row['rule_id']} | {row['delivered_candidates']} "
            f"| {row['top30_delivered']} | {row['pool_size_fixture_wide']} | {row['diagnosis']} |"
        )
    if not report["yield_diagnosis"]:
        lines.append("| — | — | — | — | — | — |")
    lines += [
        "",
        "## Under-served demand mass per port shape",
        "",
        "| port shape | under-served ports | commanders |",
        "|---|---:|---:|",
    ]
    for sig, row in report["under_served_by_shape"].items():
        lines.append(f"| {sig} | {row['ports']} | {row['commanders']} |")
    lines += [
        "",
        "## Per-commander three-way classification",
        "",
        "| commander | activated | cohort | unconsumed | starved | material | NO_RULES | reachable | burial-excl |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for entry in report["per_commander"]:
        counts = entry["classification_counts"]
        lines.append(
            f"| {entry['commander']} | {entry['activated_candidates']} | "
            f"{'yes' if entry['in_cohort'] else 'no'} | {counts[CLASS_UNCONSUMED]} | "
            f"{counts[CLASS_STARVED]} | {counts[CLASS_MATERIAL]} | {entry['no_rules_misses']} | "
            f"{entry['reachable']} | {entry['burial_excluded']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _verdict(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "MET" if value else "NOT MET"


_EXIT_CODE_EPILOG = """\
Exit codes (mirrors scripts/portfolio_sim.py's taxonomy):
  0  Run completed; report.json + report.md written to --output.
  1  Named harness failure (malformed fixture, forensics failure, ...).
  2  Missing precondition: missing synergy DB / EDHREC DB / fixture /
     resource-flows seed, stale tensor (config_hash mismatch), or a
     --commander name absent from the fixture list.
  3  Self-check failure: an internal arithmetic invariant diverged.
     Do not trust any report from an exit-3 run.
"""


def _add_common_args(p: argparse.ArgumentParser, *, default_fixture: str) -> None:
    p.add_argument("--db", default="data/synergy.db", help="synergy DB path")
    p.add_argument("--edhrec-db", "--tags-db", dest="edhrec_db", default="data/tags.db", help="EDHREC tags DB path")
    p.add_argument("--fixture", default=default_fixture, help="golden-set fixture (commander names)")
    p.add_argument(
        "--commander",
        metavar="NAME",
        action="append",
        default=None,
        help="restrict to this fixture commander (exact name; repeatable; debugging filter)",
    )
    p.add_argument("--output", default=_DEFAULT_OUTPUT_DIR, help="report output directory")
    p.add_argument(
        "--flows-seed",
        default=None,
        help="override the packaged resource_flows_seed.json (tests/diagnostics only)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demand_coverage.py",
        description="Stage 0 read-only demand-coverage instrument (plan 2026-07-02-005 Unit 2)",
        epilog=_EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_args(parser, default_fixture=_DEFAULT_FIXTURE)
    return parser


def _write_reports(outdir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / "report.json"
    md_path = outdir / "report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    db_path = Path(args.db)
    edhrec_path = Path(args.edhrec_db)
    fixture_path = Path(args.fixture)
    for path, what, remedy in (
        (db_path, "synergy DB", "rebuild via `uv run python scripts/import_cardsfolder.py`"),
        (edhrec_path, "EDHREC DB", "restore data/tags.db (see README for the EDHREC snapshot workflow)"),
        (fixture_path, "fixture", "regenerate via `uv run scripts/bench.py audit --repin --yes`"),
    ):
        if not path.exists():
            print(f"error: {what} {path} not found — {remedy}", file=sys.stderr)
            return 2
    if args.flows_seed is not None and not Path(args.flows_seed).exists():
        print(
            f"error: resource-flows seed {args.flows_seed} not found — "
            "restore src/mtg_synergy_graph/data/resource_flows_seed.json from git "
            "(or drop --flows-seed to use the packaged seed)",
            file=sys.stderr,
        )
        return 2

    # Fixture readability check for BOTH paths (with and without
    # --commander) — a malformed fixture must exit 1 with a named error,
    # never a raw traceback from deep inside the forensics pass.
    try:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        available = {e.get("commander") for e in data.get("entries", [])}
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        print(f"error: fixture {fixture_path}: unreadable ({exc})", file=sys.stderr)
        return 1

    if args.commander:
        missing = sorted(name for name in args.commander if name not in available)
        if missing:
            print(
                f"error: --commander name(s) not in fixture {fixture_path}: {missing} "
                "(exact-name match against the fixture commander list)",
                file=sys.stderr,
            )
            return 2

    started = time.perf_counter()
    try:
        report = run_demand_coverage(
            db_path=db_path,
            tags_path=edhrec_path,
            fixture_path=fixture_path,
            commander_filter=args.commander,
            output_dir=args.output,
            flows_seed_path=args.flows_seed,
        )
    except SelfCheckError as exc:
        print(f"error: self-check failed — do not trust this run: {exc}", file=sys.stderr)
        return 3
    except (DemandPreconditionError, ForensicsPreconditionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ForensicsReconciliationError as exc:
        # The forensics pass's own consistency check failed — same
        # do-not-trust posture as our exit-3 self-checks.
        print(f"error: forensics reconciliation failed — do not trust this run: {exc}", file=sys.stderr)
        return 3
    except DemandCoverageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json_path, md_path = _write_reports(Path(args.output), report)
    cohort = report["cohort"]
    agg = report["aggregate"]
    print(
        f"cohort share: {cohort['reachable']}/{cohort['total_no_rules_misses']} "
        f"= {_fmt(cohort['share'])} (null {_fmt(cohort['null_share'])}, "
        f"exceeds null: {_verdict(cohort['exceeds_null'])})"
    )
    print(
        f"aggregate reach: {agg['reachable_labels']}/{agg['total_no_rules_misses']} labels "
        f"(floor {AGGREGATE_LABEL_FLOOR}: {_verdict(agg['meets_label_floor'])})"
    )
    print(f"reports: {json_path}, {md_path}")
    print(f"total wall clock: {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return 0
