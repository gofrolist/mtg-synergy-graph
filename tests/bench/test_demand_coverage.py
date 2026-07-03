"""Stage 0 demand-coverage instrument tests (plan 2026-07-02-005, Unit 2).

Test-first coverage for the funding gate's arithmetic: the consumption
predicate (RULE_GATES minus CARD_LEVEL_RULES, unioned with declarative
interpreter gates), the pinned starved predicate (< 1,000 activated
candidates OR zero top-30 delivery), the pinned IDF-burial /
wrong-supply / pool-size diagnosis, R2 reachability (including the
"reachable ONLY via burial-diagnosed ports" exclusion), the 2,000-card
supply-pool bound, the null model, and the CLI exit-code taxonomy.

DB discipline: every database is created under ``tmp_path`` /
``tmp_path_factory`` (the session conftest sentinel fails the run on
any stray ``*.db`` under the repo root or ``data/``). The synthetic
scenario DB is built from the committed Forge fixture cards plus
hand-inserted commanders/candidates so ``compute_forensics`` runs the
REAL production ranking path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest

from mtg_synergy_graph.bench.demand_coverage import (
    CLASS_MATERIAL,
    CLASS_STARVED,
    CLASS_UNCONSUMED,
    DIAG_IDF_BURIAL,
    DIAG_POOL_SIZE,
    DIAG_WRONG_SUPPLY,
    REACH_BURIAL_EXCLUDED,
    REACH_HEALTHY,
    REACH_NONE,
    build_consumption_gates,
    build_null_pools,
    classify_miss_reachability,
    classify_port_state,
    consuming_rules_for_port,
    diagnose_starved_rule,
    main,
    port_starved_diagnosis,
    render_markdown,
    run_demand_coverage,
    shape_matches_port,
    supplier_pool,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.complement_rules.registry import CARD_LEVEL_RULES, RULE_GATES, RuleGate
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import clear_ports_cache
from mtg_synergy_graph.importer import import_cards_folder
from mtg_synergy_graph.port_graph.resource_flows import PortShape, ResourceFlow
from mtg_synergy_graph.validate import commander_to_slug

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

CMDR_UNCONSUMED = "Cmdr Unconsumed"
CMDR_TWO_PORTS = "Cmdr Two Ports"
CMDR_RICH = "Cmdr Rich"
CMDR_RICH_STARVED = "Cmdr Rich Starved"


# ---------------------------------------------------------------------------
# Pure predicates — the funding gate's arithmetic (test-first)
# ---------------------------------------------------------------------------


class TestClassifyPortState:
    def test_unconsumed_when_no_gate_fires(self) -> None:
        assert classify_port_state(frozenset(), activated_count=5000, rule_top30_counts={"r": 3}) == CLASS_UNCONSUMED

    def test_starved_via_activated_count_floor(self) -> None:
        # Gate fires, delivery into the top-30 exists, but the scored
        # universe collapsed below the pinned 1,000 floor → starved.
        assert classify_port_state(frozenset({"r"}), activated_count=999, rule_top30_counts={"r": 5}) == CLASS_STARVED

    def test_starved_via_zero_top30_or_branch(self) -> None:
        # Count is comfortably above the floor but the consuming rule
        # contributes zero cards to the top-30 → starved via OR branch.
        assert classify_port_state(frozenset({"r"}), activated_count=5000, rule_top30_counts={"r": 0}) == CLASS_STARVED

    def test_material_yield(self) -> None:
        assert classify_port_state(frozenset({"r"}), activated_count=1000, rule_top30_counts={"r": 1}) == CLASS_MATERIAL

    def test_material_requires_only_one_delivering_consumer(self) -> None:
        # Two consumers, one delivers → material (documented reading).
        assert (
            classify_port_state(frozenset({"a", "b"}), activated_count=1000, rule_top30_counts={"a": 0, "b": 2})
            == CLASS_MATERIAL
        )

    def test_skipped_commander_carveout_uses_count_leg_only(self) -> None:
        # top30_available=False: the zero-top-30 leg cannot be
        # evaluated (no live ranking), so only the count leg applies.
        assert (
            classify_port_state(frozenset({"r"}), activated_count=5000, rule_top30_counts={}, top30_available=False)
            == CLASS_MATERIAL
        )


class TestConsumptionPredicate:
    def test_card_level_rule_firing_is_not_consumption(self, tmp_path: Path) -> None:
        # A gate whose rule_id is in CARD_LEVEL_RULES fires on the
        # port, but the subtraction removes it → NOT consumption.
        card_level_id = next(iter(CARD_LEVEL_RULES))
        injected = (RuleGate(card_level_id, lambda port: True),)
        conn = open_db(tmp_path / "gates.db")
        try:
            gates = build_consumption_gates(conn, rule_gates=injected)
        finally:
            conn.close()
        port = {"port_type": "cost", "event_class": "sacrifice"}
        assert consuming_rules_for_port(port, gates) == frozenset()

    def test_real_registry_subtraction_and_no_card_level_leakage(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "gates.db")
        try:
            gates = build_consumption_gates(conn)
        finally:
            conn.close()
        gate_ids = {rule_id for rule_id, _pred in gates}
        assert not gate_ids & CARD_LEVEL_RULES
        # Every non-card-level RULE_GATES entry survives the subtraction.
        assert {g.rule_id for g in RULE_GATES if g.rule_id not in CARD_LEVEL_RULES} <= gate_ids

    def test_declarative_gate_fires_is_consumption(self, tmp_path: Path) -> None:
        # A rules-table row's compiled gate closure participates in the
        # union (the gap_report._scan_universe precedent).
        conn = open_db(tmp_path / "decl.db")
        try:
            gate = {"op": "has_port", "port_type": "cost", "event_class": "sacrifice"}
            conn.execute(
                "INSERT INTO rules (rule_id, family, gate_predicate, commander_port_predicate, "
                "candidate_port_predicate, filter_group, cmdr_event, cand_event, active) "
                "VALUES ('test_decl_rule', 'tribal', ?, ?, ?, '', 'sacrifice', 'sacrifice', 1)",
                (json.dumps(gate), json.dumps(gate), json.dumps(gate)),
            )
            conn.commit()
            gates = build_consumption_gates(conn)
        finally:
            conn.close()
        port = {"port_type": "cost", "event_class": "sacrifice", "cost_target": "other"}
        assert "test_decl_rule" in consuming_rules_for_port(port, gates)

    def test_gy_fuel_feeder_gate_consumes_araumi_shaped_port(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "gates.db")
        try:
            gates = build_consumption_gates(conn)
        finally:
            conn.close()
        port = {"port_type": "cost", "event_class": "exile_from_grave", "cost_target": "any"}
        assert "gy_fuel_feeder" in consuming_rules_for_port(port, gates)
        # A plain non-self sacrifice cost (Yawgmoth's shape) is
        # unconsumed under the current registry.
        sac_port = {"port_type": "cost", "event_class": "sacrifice", "cost_target": "other"}
        assert consuming_rules_for_port(sac_port, gates) == frozenset()


class TestStarvedDiagnosis:
    def test_idf_burial(self) -> None:
        # Delivers candidates, ZERO reach the top-30, pool >= 500.
        assert (
            diagnose_starved_rule(
                delivered=frozenset({"A", "B"}),
                top30_delivered=0,
                pool_size=500,
                no_rules_misses=frozenset(),
            )
            == DIAG_IDF_BURIAL
        )

    def test_wrong_supply_when_pool_below_burial_floor(self) -> None:
        # Delivers, zero top-30, but pool < 500 → not burial; delivered
        # candidates never intersect NO_RULES misses (tensor semantics)
        # → wrong-supply-cards, the origin's "supply cards that are not
        # the labeled misses".
        assert (
            diagnose_starved_rule(
                delivered=frozenset({"A"}),
                top30_delivered=0,
                pool_size=499,
                no_rules_misses=frozenset({"Missed"}),
            )
            == DIAG_WRONG_SUPPLY
        )

    def test_wrong_supply_when_delivery_reaches_top30(self) -> None:
        # Large pool but top-30 delivery exists → burial leg fails.
        assert (
            diagnose_starved_rule(
                delivered=frozenset({"A"}),
                top30_delivered=1,
                pool_size=10_000,
                no_rules_misses=frozenset(),
            )
            == DIAG_WRONG_SUPPLY
        )

    def test_pool_size_when_nothing_delivered(self) -> None:
        assert (
            diagnose_starved_rule(
                delivered=frozenset(),
                top30_delivered=0,
                pool_size=10_000,
                no_rules_misses=frozenset(),
            )
            == DIAG_POOL_SIZE
        )

    def test_pool_size_when_delivery_intersects_no_rules_list(self) -> None:
        # The intersection branch exists verbatim from the pin even
        # though tensor semantics make it empty in production.
        assert (
            diagnose_starved_rule(
                delivered=frozenset({"X"}),
                top30_delivered=1,
                pool_size=100,
                no_rules_misses=frozenset({"X"}),
            )
            == DIAG_POOL_SIZE
        )

    def test_port_level_fold(self) -> None:
        assert port_starved_diagnosis({"a": DIAG_IDF_BURIAL}) == DIAG_IDF_BURIAL
        assert port_starved_diagnosis({"a": DIAG_IDF_BURIAL, "b": DIAG_IDF_BURIAL}) == DIAG_IDF_BURIAL
        # Any non-burial consumer keeps the port out of the burial mass.
        assert port_starved_diagnosis({"a": DIAG_IDF_BURIAL, "b": DIAG_WRONG_SUPPLY}) == DIAG_WRONG_SUPPLY
        assert port_starved_diagnosis({"a": DIAG_IDF_BURIAL, "b": DIAG_POOL_SIZE}) == DIAG_POOL_SIZE
        with pytest.raises(ValueError, match="at least one rule diagnosis"):
            port_starved_diagnosis({})


class TestShapeMatching:
    def test_exact_event_class_and_port_type(self) -> None:
        shape = PortShape(port_type="cost", event_class="sacrifice")
        assert shape_matches_port(shape, {"port_type": "cost", "event_class": "sacrifice"})
        assert not shape_matches_port(shape, {"port_type": "effect", "event_class": "sacrifice"})
        assert not shape_matches_port(shape, {"port_type": "cost", "event_class": "discard"})

    def test_event_class_prefix(self) -> None:
        shape = PortShape(port_type="keyword", event_class_prefix="Flashback")
        assert shape_matches_port(shape, {"port_type": "keyword", "event_class": "Flashback:3"})
        assert shape_matches_port(shape, {"port_type": "keyword", "event_class": "Flashback"})
        assert not shape_matches_port(shape, {"port_type": "keyword", "event_class": "Madness:1"})

    def test_cost_target_not_treats_null_as_passing(self) -> None:
        shape = PortShape(port_type="cost", event_class="sacrifice", cost_target_not="self")
        assert shape_matches_port(shape, {"port_type": "cost", "event_class": "sacrifice", "cost_target": None})
        assert shape_matches_port(shape, {"port_type": "cost", "event_class": "sacrifice", "cost_target": "other"})
        assert not shape_matches_port(shape, {"port_type": "cost", "event_class": "sacrifice", "cost_target": "self"})

    def test_attr_not_in(self) -> None:
        shape = PortShape(
            port_type="effect",
            event_class="Token",
            attr_kind="token_subtype",
            attr_value_not_in=("Treasure",),
        )
        port = {"port_type": "effect", "event_class": "Token"}
        assert shape_matches_port(shape, port, attrs=(("token_subtype", "Soldier"),))
        assert not shape_matches_port(shape, port, attrs=(("token_subtype", "Treasure"),))
        assert not shape_matches_port(shape, port, attrs=())  # must carry the kind

    def test_card_types_not_contains(self) -> None:
        shape = PortShape(port_type="effect", event_class="Token", card_types_not_contains=("Sorcery",))
        port = {"port_type": "effect", "event_class": "Token"}
        assert shape_matches_port(shape, port, card_types="Creature Human")
        assert not shape_matches_port(shape, port, card_types="Sorcery")

    def test_valid_filter_match_and_mismatch(self) -> None:
        shape = PortShape(port_type="cost", event_class="sacrifice", valid_filter="Self")
        base = {"port_type": "cost", "event_class": "sacrifice"}
        assert shape_matches_port(shape, {**base, "valid_filter": "Self"})
        assert not shape_matches_port(shape, {**base, "valid_filter": "Creature.Other"})
        assert not shape_matches_port(shape, {**base, "valid_filter": None})
        assert not shape_matches_port(shape, base)  # key absent == unconstrained port


class TestReachability:
    POOLS: ClassVar[dict[str, frozenset[str]]] = {
        "healthy_flow": frozenset({"M"}),
        "burial_flow": frozenset({"M", "N"}),
    }

    def test_reachable_via_healthy_flow(self) -> None:
        status = {"healthy_flow": "healthy", "burial_flow": "none"}
        assert classify_miss_reachability("M", self.POOLS, status) == REACH_HEALTHY

    def test_burial_only_is_excluded(self) -> None:
        status = {"healthy_flow": "none", "burial_flow": "burial_only"}
        assert classify_miss_reachability("N", self.POOLS, status) == REACH_BURIAL_EXCLUDED

    def test_burial_and_healthy_is_counted(self) -> None:
        # Reachable via a burial-diagnosed AND a healthy under-served
        # port → counted (the "only through" semantics).
        status = {"healthy_flow": "healthy", "burial_flow": "burial_only"}
        assert classify_miss_reachability("M", self.POOLS, status) == REACH_HEALTHY

    def test_unreachable(self) -> None:
        status = {"healthy_flow": "healthy", "burial_flow": "healthy"}
        assert classify_miss_reachability("Absent", self.POOLS, status) == REACH_NONE


class TestNullModel:
    def test_deterministic_under_fixed_seed_and_same_sizes(self) -> None:
        pools = {"a": frozenset({"x", "y"}), "b": frozenset({"z"})}
        universe = [f"card{i}" for i in range(50)]
        first = build_null_pools(pools, universe, seed=17)
        second = build_null_pools(pools, universe, seed=17)
        assert first == second
        assert {name: len(pool) for name, pool in first.items()} == {"a": 2, "b": 1}
        assert build_null_pools(pools, universe, seed=18) != first


# ---------------------------------------------------------------------------
# Supplier pools (SQL) — narrowing semantics
# ---------------------------------------------------------------------------


def _add_card(conn: sqlite3.Connection, name: str, *, types: str = "Creature Ooze") -> None:
    conn.execute(
        "INSERT INTO cards (name, types, supertypes, subtypes, card_types, colors, color_identity, cmc, "
        "legal_commander) VALUES (?, ?, '', 'Ooze', ?, 'B', 'B', 2, 1)",
        (name, types, types.split()[0]),
    )


def _add_port(conn: sqlite3.Connection, card: str, port_type: str, event_class: str, **kw: str) -> int:
    cols = ["card_name", "port_type", "event_class", *kw]
    cur = conn.execute(
        f"INSERT INTO card_ports ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # noqa: S608
        (card, port_type, event_class, *kw.values()),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


class TestSupplierPool:
    def test_attr_type_and_prefix_narrowing(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "pool.db")
        try:
            _add_card(conn, "Soldier Maker")
            pid = _add_port(conn, "Soldier Maker", "effect", "Token")
            conn.execute("INSERT INTO port_attributes VALUES (?, 'token_subtype', 'Soldier', 0)", (pid,))
            _add_card(conn, "Treasure Maker")
            pid = _add_port(conn, "Treasure Maker", "effect", "Token")
            conn.execute("INSERT INTO port_attributes VALUES (?, 'token_subtype', 'Treasure', 0)", (pid,))
            _add_card(conn, "Token Burst", types="Sorcery")
            pid = _add_port(conn, "Token Burst", "effect", "Token")
            conn.execute("INSERT INTO port_attributes VALUES (?, 'token_subtype', 'Soldier', 0)", (pid,))
            _add_card(conn, "Flashback Card", types="Instant")
            _add_port(conn, "Flashback Card", "keyword", "Flashback:3")
            conn.commit()

            flow = ResourceFlow(
                name="test",
                consumers=(PortShape(port_type="cost", event_class="sacrifice"),),
                suppliers=(
                    PortShape(
                        port_type="effect",
                        event_class="Token",
                        attr_kind="token_subtype",
                        attr_value_not_in=("Treasure",),
                        card_types_not_contains=("Sorcery",),
                    ),
                    PortShape(port_type="keyword", event_class_prefix="Flashback"),
                ),
            )
            assert supplier_pool(conn, flow) == frozenset({"Soldier Maker", "Flashback Card"})
        finally:
            conn.close()

    def test_valid_filter_narrowing(self, tmp_path: Path) -> None:
        # 4 committed seed shapes carry valid_filter — the SQL predicate
        # must pool ONLY exact valid_filter matches (NULL and different
        # values are excluded).
        conn = open_db(tmp_path / "pool.db")
        try:
            _add_card(conn, "Self Sac")
            _add_port(conn, "Self Sac", "cost", "sacrifice", valid_filter="Self")
            _add_card(conn, "Other Sac")
            _add_port(conn, "Other Sac", "cost", "sacrifice", valid_filter="Creature.Other")
            _add_card(conn, "Null Sac")
            _add_port(conn, "Null Sac", "cost", "sacrifice")  # NULL valid_filter
            conn.commit()

            flow = ResourceFlow(
                name="test",
                consumers=(PortShape(port_type="cost", event_class="sacrifice"),),
                suppliers=(PortShape(port_type="cost", event_class="sacrifice", valid_filter="Self"),),
            )
            assert supplier_pool(conn, flow) == frozenset({"Self Sac"})
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Synthetic end-to-end scenario environment
# ---------------------------------------------------------------------------


def _seed_tensor_row(
    conn: sqlite3.Connection,
    commander: str,
    candidate: str,
    config_hash: str,
    *,
    rule_id: str = "synthetic_rule",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, config_hash, computed_at) "
        "VALUES (?, ?, ?, 1.0, 1.0, 1, ?, '2026-07-02T00:00:00+00:00')",
        (commander, candidate, rule_id, config_hash),
    )


@pytest.fixture(scope="module")
def scenario_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """synergy.db from the committed Forge fixture cards + synthetic
    commanders/candidates + tensor rows at the CURRENT config hash."""
    db_path = tmp_path_factory.mktemp("demand_coverage") / "synergy.db"
    conn = open_db(db_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)

        # Commander with one unconsumed non-self sacrifice cost
        # (Yawgmoth's demand shape); 3 activated candidates → cohort.
        _add_card(conn, CMDR_UNCONSUMED, types="Creature Ooze")
        _add_port(conn, CMDR_UNCONSUMED, "cost", "sacrifice", cost_target="other")

        # Commander with a gy_fuel_feeder-consumed port (starved,
        # burial-diagnosable) AND an unconsumed sacrifice port.
        _add_card(conn, CMDR_TWO_PORTS, types="Creature Ooze")
        _add_port(conn, CMDR_TWO_PORTS, "cost", "exile_from_grave", cost_target="any")
        _add_port(conn, CMDR_TWO_PORTS, "cost", "sacrifice", cost_target="other")

        # Commander above the 1,000 activated floor whose consuming
        # rule delivers into the live top-30 → material yield.
        _add_card(conn, CMDR_RICH, types="Creature Ooze")
        _add_port(conn, CMDR_RICH, "cost", "exile_from_grave", cost_target="any")

        # Commander above the floor whose consuming rule delivers
        # ZERO cards into the live top-30 → starved via the OR branch.
        _add_card(conn, CMDR_RICH_STARVED, types="Creature Ooze")
        _add_port(conn, CMDR_RICH_STARVED, "cost", "exile_from_grave", cost_target="any")

        # Miss candidates: keyword.Undying is inert for these
        # commanders live (stays unranked → NO_RULES) but sits in the
        # test gy_flow supplier pool.
        _add_card(conn, "Miss UndyingOnly")
        _add_port(conn, "Miss UndyingOnly", "keyword", "Undying")
        _add_card(conn, "Miss UndyingBoth")
        _add_port(conn, "Miss UndyingBoth", "keyword", "Undying")
        _add_port(
            conn, "Miss UndyingBoth", "effect", "ChangeZone", zone_origin="Graveyard", zone_destination="Battlefield"
        )

        config_hash = compute_config_hash()
        for cand in ("Sol Ring", "Panharmonicon", "Rhystic Study"):
            _seed_tensor_row(conn, CMDR_UNCONSUMED, cand, config_hash)
        _seed_tensor_row(conn, CMDR_TWO_PORTS, "Panharmonicon", config_hash, rule_id="gy_fuel_feeder")
        _seed_tensor_row(conn, CMDR_TWO_PORTS, "Rhystic Study", config_hash, rule_id="gy_fuel_feeder")
        for i in range(1200):
            _seed_tensor_row(conn, CMDR_RICH, f"Synthetic Candidate {i}", config_hash)
            _seed_tensor_row(conn, CMDR_RICH_STARVED, f"Synthetic Candidate {i}", config_hash)
        # Sol Ring is the only live-ranked card for these synthetic
        # commanders (staple bonus) → guaranteed inside the top-30.
        _seed_tensor_row(conn, CMDR_RICH, "Sol Ring", config_hash, rule_id="gy_fuel_feeder")
        _seed_tensor_row(conn, CMDR_RICH_STARVED, "Panharmonicon", config_hash, rule_id="gy_fuel_feeder")
        conn.commit()
    finally:
        conn.close()
    clear_ports_cache()
    return db_path


def _make_tags_db(path: Path, rows: list[tuple[str, str, str, float]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)"
        )
        conn.executemany("INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _write_fixture(path: Path, commanders: list[str]) -> None:
    payload = {
        "schema_version": 2,
        "config_hash": "irrelevant-for-demand-coverage",
        "created_at": "2026-07-02T00:00:00+00:00",
        "entries": [{"commander": c, "scores": {}} for c in commanders],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_test_flows(path: Path) -> None:
    """Controlled two-flow seed mirroring the committed grammar."""
    path.write_text(
        json.dumps(
            {
                "_readme": ["test seed"],
                "flows": {
                    "sac_flow": {
                        "consumers": [{"port_type": "cost", "event_class": "sacrifice", "cost_target_not": "self"}],
                        "suppliers": [
                            {
                                "port_type": "effect",
                                "event_class": "ChangeZone",
                                "zone_origin": "Graveyard",
                                "zone_destination": "Battlefield",
                            }
                        ],
                    },
                    "gy_flow": {
                        "consumers": [{"port_type": "cost", "event_class": "exile_from_grave", "cost_target": "any"}],
                        "suppliers": [{"port_type": "keyword", "event_class": "Undying"}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def scenario_paths(scenario_db: Path, tmp_path: Path) -> dict[str, Path]:
    flows = tmp_path / "flows.json"
    _write_test_flows(flows)
    return {"db": scenario_db, "flows": flows, "tmp": tmp_path}


def _entry(report: dict, commander: str) -> dict:
    return next(e for e in report["per_commander"] if e["commander"] == commander)


class TestEndToEndClassification:
    def test_unconsumed_port_and_addressable_miss(self, scenario_paths: dict[str, Path]) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        # Bloodghast (committed Forge fixture card) carries the
        # GY->Battlefield self-recursion effect → sac_flow supplier.
        _make_tags_db(tags, [(commander_to_slug(CMDR_UNCONSUMED), "Bloodghast", "High Synergy Cards", 2.0)])
        _write_fixture(fixture, [CMDR_UNCONSUMED])

        report = run_demand_coverage(
            db_path=scenario_paths["db"],
            tags_path=tags,
            fixture_path=fixture,
            output_dir=tmp / "out",
            flows_seed_path=scenario_paths["flows"],
        )
        entry = _entry(report, CMDR_UNCONSUMED)
        assert entry["classification_counts"] == {CLASS_UNCONSUMED: 1, CLASS_STARVED: 0, CLASS_MATERIAL: 0}
        assert entry["in_cohort"] is True  # 3 activated < 1,000
        assert entry["no_rules_misses"] == 1
        assert entry["reachable"] == 1  # unconsumed demand + supplier-shaped miss
        assert entry["flow_demand_status"]["sac_flow"] == "healthy"
        assert report["cohort"]["share"] == 1.0
        assert "cost sacrifice/other" in report["under_served_by_shape"]

    def test_two_commander_share_cohort_rule_and_label_floor(self, scenario_paths: dict[str, Path]) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(
            tags,
            [
                (commander_to_slug(CMDR_UNCONSUMED), "Bloodghast", "High Synergy Cards", 2.0),
                (commander_to_slug(CMDR_RICH), "Bloodghast", "High Synergy Cards", 2.0),
            ],
        )
        _write_fixture(fixture, [CMDR_UNCONSUMED, CMDR_RICH])

        report = run_demand_coverage(
            db_path=scenario_paths["db"],
            tags_path=tags,
            fixture_path=fixture,
            output_dir=tmp / "out",
            flows_seed_path=scenario_paths["flows"],
        )
        # Cohort is defined BY RULE: CMDR_RICH sits above the 1,000
        # activated floor → excluded from the denominator.
        assert report["cohort"]["commanders"] == [CMDR_UNCONSUMED]
        rich = _entry(report, CMDR_RICH)
        assert rich["in_cohort"] is False
        assert rich["activated_candidates"] >= 1000
        # CMDR_RICH's consumed port delivers Sol Ring into the live
        # top-30 → consumed-with-material-yield, excluded from the
        # under-served mass.
        assert rich["classification_counts"] == {CLASS_UNCONSUMED: 0, CLASS_STARVED: 0, CLASS_MATERIAL: 1}
        assert rich["flow_demand_status"]["gy_flow"] == "none"
        # Exact share ratio: 1 reachable / 1 cohort NO_RULES miss.
        assert report["cohort"]["total_no_rules_misses"] == 1
        assert report["cohort"]["reachable"] == 1
        assert report["cohort"]["share"] == 1.0
        # ≥100-label fixture-wide floor arithmetic: CMDR_RICH's miss is
        # unreachable (no under-served demand), so aggregate reach is 1
        # of 2 and the absolute floor is NOT met.
        assert report["aggregate"]["total_no_rules_misses"] == 2
        assert report["aggregate"]["reachable_labels"] == 1
        assert report["aggregate"]["meets_label_floor"] is False

    def test_starved_via_zero_top30_above_floor(self, scenario_paths: dict[str, Path]) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(tags, [(commander_to_slug(CMDR_RICH_STARVED), "Miss UndyingOnly", "Top Cards", 1.0)])
        _write_fixture(fixture, [CMDR_RICH_STARVED])

        report = run_demand_coverage(
            db_path=scenario_paths["db"],
            tags_path=tags,
            fixture_path=fixture,
            output_dir=tmp / "out",
            flows_seed_path=scenario_paths["flows"],
        )
        entry = _entry(report, CMDR_RICH_STARVED)
        assert entry["activated_candidates"] >= 1000
        # Gate fires + count >= floor + zero top-30 delivery → starved
        # via the OR branch.
        assert entry["classification_counts"][CLASS_STARVED] == 1
        (port,) = [p for p in entry["ports"] if p["classification"] == CLASS_STARVED]
        assert port["consuming_rules"] == ["gy_fuel_feeder"]

    def test_burial_only_excluded_and_dual_path_counted(self, scenario_paths: dict[str, Path]) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(
            tags,
            [
                (commander_to_slug(CMDR_TWO_PORTS), "Miss UndyingOnly", "High Synergy Cards", 2.0),
                (commander_to_slug(CMDR_TWO_PORTS), "Miss UndyingBoth", "Top Cards", 1.5),
            ],
        )
        _write_fixture(fixture, [CMDR_TWO_PORTS])

        report = run_demand_coverage(
            db_path=scenario_paths["db"],
            tags_path=tags,
            fixture_path=fixture,
            output_dir=tmp / "out",
            flows_seed_path=scenario_paths["flows"],
            burial_pool_floor=2,  # test seam: pinned 500 shrunk to fit the tiny DB
        )
        entry = _entry(report, CMDR_TWO_PORTS)
        # gy port: gy_fuel_feeder delivers 2, zero in top-30, pool >= 2
        # → starved, IDF-burial. sacrifice port: unconsumed (healthy).
        gy_port = next(p for p in entry["ports"] if p["signature"] == "cost exile_from_grave/any")
        assert gy_port["classification"] == CLASS_STARVED
        assert gy_port["starved_diagnosis"] == DIAG_IDF_BURIAL
        assert entry["flow_demand_status"] == {"sac_flow": "healthy", "gy_flow": "burial_only"}
        # Miss UndyingOnly is reachable ONLY via the burial-diagnosed
        # port → excluded; Miss UndyingBoth is reachable via burial AND
        # healthy → counted.
        assert entry["burial_excluded"] == 1
        assert entry["reachable"] == 1
        assert entry["unreachable"] == 0
        assert report["cohort"]["share"] == 0.5
        # Yield-diagnosis rows cover the named feeder.
        row = next(r for r in report["yield_diagnosis"] if r["commander"] == CMDR_TWO_PORTS)
        assert row["rule_id"] == "gy_fuel_feeder"
        assert row["delivered_candidates"] == 2
        assert row["top30_delivered"] == 0
        assert row["diagnosis"] == DIAG_IDF_BURIAL

    def test_pool_over_cap_is_excluded_and_reported(self, scenario_paths: dict[str, Path]) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(tags, [(commander_to_slug(CMDR_UNCONSUMED), "Bloodghast", "High Synergy Cards", 2.0)])
        _write_fixture(fixture, [CMDR_UNCONSUMED])

        report = run_demand_coverage(
            db_path=scenario_paths["db"],
            tags_path=tags,
            fixture_path=fixture,
            output_dir=tmp / "out",
            flows_seed_path=scenario_paths["flows"],
            supply_pool_cap=1,  # test seam: pinned 2,000 shrunk below sac_flow's pool
        )
        assert "sac_flow" in report["bound_failing_flows"]
        assert report["flows"]["sac_flow"]["bound_failing"] is True
        # The miss was only reachable through the bound-failing flow →
        # excluded from the numerator.
        assert report["cohort"]["reachable"] == 0
        assert report["cohort"]["share"] == 0.0

    def test_null_model_deterministic_and_verdict_in_report(self, scenario_paths: dict[str, Path]) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(tags, [(commander_to_slug(CMDR_UNCONSUMED), "Bloodghast", "High Synergy Cards", 2.0)])
        _write_fixture(fixture, [CMDR_UNCONSUMED])

        kwargs = dict(
            db_path=scenario_paths["db"],
            tags_path=tags,
            fixture_path=fixture,
            output_dir=tmp / "out",
            flows_seed_path=scenario_paths["flows"],
        )
        first = run_demand_coverage(**kwargs)  # type: ignore[arg-type]
        second = run_demand_coverage(**kwargs)  # type: ignore[arg-type]
        assert first["cohort"]["null_share"] == second["cohort"]["null_share"]
        assert first["cohort"]["null_model_reachable"] == second["cohort"]["null_model_reachable"]
        assert first["cohort"]["exceeds_null"] in (True, False)
        for name in ("sac_flow", "gy_flow"):
            assert first["flows"][name]["null_pool_size"] == first["flows"][name]["pool_size"]


# ---------------------------------------------------------------------------
# CLI exit codes + report files
# ---------------------------------------------------------------------------


class TestCli:
    def test_missing_db_exit_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        fixture = tmp_path / "fixture.json"
        _write_fixture(fixture, ["X"])
        tags = tmp_path / "tags.db"
        _make_tags_db(tags, [])
        rc = main(["--db", str(tmp_path / "absent.db"), "--edhrec-db", str(tags), "--fixture", str(fixture)])
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_missing_flows_seed_exit_2(
        self, scenario_paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(tags, [])
        _write_fixture(fixture, [CMDR_UNCONSUMED])
        rc = main(
            [
                "--db",
                str(scenario_paths["db"]),
                "--edhrec-db",
                str(tags),
                "--fixture",
                str(fixture),
                "--flows-seed",
                str(tmp / "absent_seed.json"),
                "--output",
                str(tmp / "out"),
            ]
        )
        assert rc == 2
        assert "seed" in capsys.readouterr().err

    def test_malformed_fixture_exit_1_with_and_without_commander(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A malformed --fixture must exit 1 with a named error on BOTH
        # paths — previously only the --commander branch validated it
        # and the bare path crashed with a raw traceback later.
        db = tmp_path / "synergy.db"
        open_db(db).close()
        tags = tmp_path / "tags.db"
        _make_tags_db(tags, [])
        fixture = tmp_path / "fixture.json"
        fixture.write_text("{not json", encoding="utf-8")
        base = ["--db", str(db), "--edhrec-db", str(tags), "--fixture", str(fixture)]

        rc = main(base)
        assert rc == 1
        err = capsys.readouterr().err
        assert "fixture" in err and "unreadable" in err

        rc = main([*base, "--commander", "X"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "fixture" in err and "unreadable" in err

    def test_stale_tensor_exit_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # A DB whose tensor rows carry a DIFFERENT config hash than the
        # live one → precondition failure before anything is computed.
        db_path = tmp_path / "stale.db"
        conn = open_db(db_path)
        try:
            _add_card(conn, "Some Cmdr")
            _seed_tensor_row(conn, "Some Cmdr", "Some Card", "0" * 64)
            conn.commit()
        finally:
            conn.close()
        tags = tmp_path / "tags.db"
        _make_tags_db(tags, [])
        fixture = tmp_path / "fixture.json"
        _write_fixture(fixture, ["Some Cmdr"])
        flows = tmp_path / "flows.json"
        _write_test_flows(flows)
        rc = main(
            [
                "--db",
                str(db_path),
                "--edhrec-db",
                str(tags),
                "--fixture",
                str(fixture),
                "--flows-seed",
                str(flows),
                "--output",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 2
        assert "stale" in capsys.readouterr().err

    def test_unknown_commander_filter_exit_2(
        self, scenario_paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(tags, [])
        _write_fixture(fixture, [CMDR_UNCONSUMED])
        rc = main(
            [
                "--db",
                str(scenario_paths["db"]),
                "--edhrec-db",
                str(tags),
                "--fixture",
                str(fixture),
                "--commander",
                "Not In Fixture",
                "--output",
                str(tmp / "out"),
            ]
        )
        assert rc == 2
        assert "Not In Fixture" in capsys.readouterr().err

    def test_happy_path_writes_both_reports(
        self, scenario_paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(tags, [(commander_to_slug(CMDR_UNCONSUMED), "Bloodghast", "High Synergy Cards", 2.0)])
        _write_fixture(fixture, [CMDR_UNCONSUMED, CMDR_RICH])
        out = tmp / "out"
        rc = main(
            [
                "--db",
                str(scenario_paths["db"]),
                "--edhrec-db",
                str(tags),
                "--fixture",
                str(fixture),
                "--commander",
                CMDR_UNCONSUMED,  # exercise the debugging filter
                "--flows-seed",
                str(scenario_paths["flows"]),
                "--output",
                str(out),
            ]
        )
        assert rc == 0
        payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
        # The --commander filter restricted the run.
        assert [e["commander"] for e in payload["per_commander"]] == [CMDR_UNCONSUMED]
        assert payload["provenance"]["commander_filter"] == [CMDR_UNCONSUMED]
        md = (out / "report.md").read_text(encoding="utf-8")
        for section in (
            "## Cohort addressable share (R2)",
            "## Aggregate readout",
            "## Feeder-yield diagnosis",
            "## Under-served demand mass per port shape",
            "## Per-commander three-way classification",
            "Named verification commanders",
        ):
            assert section in md, f"missing section: {section}"
        stdout = capsys.readouterr().out
        assert "cohort share:" in stdout
        # The output dir carries exactly the two reports — the filtered
        # fixture temp file is cleaned up, and .audit/history.csv is
        # never touched (nothing else is written anywhere).
        assert sorted(p.name for p in out.iterdir()) == ["report.json", "report.md"]

    def test_render_markdown_is_pure(self, scenario_paths: dict[str, Path]) -> None:
        tmp = scenario_paths["tmp"]
        tags = tmp / "tags.db"
        fixture = tmp / "fixture.json"
        _make_tags_db(tags, [(commander_to_slug(CMDR_UNCONSUMED), "Bloodghast", "High Synergy Cards", 2.0)])
        _write_fixture(fixture, [CMDR_UNCONSUMED])
        report = run_demand_coverage(
            db_path=scenario_paths["db"],
            tags_path=tags,
            fixture_path=fixture,
            output_dir=tmp / "out",
            flows_seed_path=scenario_paths["flows"],
        )
        assert render_markdown(report) == render_markdown(report)


# ---------------------------------------------------------------------------
# Full run on the real fixture (integration; excluded by default)
# ---------------------------------------------------------------------------


_REAL_DB = Path("data/synergy.db")
_REAL_TAGS = Path("data/tags.db")
_REAL_FIXTURE = Path("tests/fixtures/golden_set_run.json")


def _real_tensor_current() -> bool:
    if not _REAL_DB.exists():
        return False
    conn = sqlite3.connect(_REAL_DB)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM rule_contributions WHERE config_hash = ?",
            (compute_config_hash(),),
        ).fetchone()
        return int(row[0]) > 0
    except sqlite3.Error:
        return False
    finally:
        conn.close()


@pytest.mark.integration
@pytest.mark.skipif(
    not (_REAL_DB.exists() and _REAL_TAGS.exists() and _REAL_FIXTURE.exists()),
    reason="data/synergy.db, data/tags.db, or the golden-set fixture is absent",
)
@pytest.mark.skipif(
    not _real_tensor_current(),
    reason="persisted tensor is stale relative to the current config_hash (re-pin first)",
)
def test_full_run_on_real_fixture(tmp_path: Path) -> None:
    """Full 100-commander run: completes, writes both report files,
    and the packaged provisional seed's pools respect the 2,000 bound.
    Runs live production ranking — minutes, hence the integration
    marker (excluded by the default addopts)."""
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    report = run_demand_coverage(
        db_path=_REAL_DB,
        tags_path=_REAL_TAGS,
        fixture_path=_REAL_FIXTURE,
        output_dir=out,
    )
    json_path = out / "report.json"
    md_path = out / "report.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    assert json_path.exists() and md_path.exists()
    assert len(report["per_commander"]) == 100
    named = report["cohort"]["named_verification"]
    assert all(row["in_fixture"] for row in named.values())
    # Sanity: the classification partition sums per commander.
    for entry in report["per_commander"]:
        assert sum(entry["classification_counts"].values()) == entry["ports_total"]
