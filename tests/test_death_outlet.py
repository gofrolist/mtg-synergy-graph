"""Tests for complement_rules.death_outlet (plan 2026-07-07-002 Task 6).

RULE_PLANNING.md section 4 required cases: flag-off, gate rejection
(ETB-shaped ChangesZone, self-only filter, Sacrificed-port commander),
match case (exact rule_id/direction/cmdr_event, cand_event classification),
dedup, self-exclusion. Plus TestWiring-style assertions (registry gate
present, bucket mapping, dispatch reachable).
"""

from __future__ import annotations

import sqlite3

import pytest

import mtg_synergy_graph.complement_rules.death_outlet as do
from mtg_synergy_graph.complement_rules.death_outlet import (
    _commander_has_death_outlet_gate,
    _find_death_outlet_complements,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name TEXT NOT NULL,
            port_type TEXT NOT NULL,
            event_class TEXT NOT NULL,
            valid_filter TEXT,
            zone_origin TEXT,
            zone_destination TEXT,
            cost_target TEXT,
            raw_line TEXT
        )
        """
    )
    return conn


def _add_port(
    conn,
    card_name,
    port_type,
    event_class,
    valid_filter="",
    zone_origin="",
    zone_destination="",
    cost_target="",
    raw_line="",
):
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, "
        "zone_origin, zone_destination, cost_target, raw_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, cost_target, raw_line),
    )


DEATH_TRIGGER = {
    "port_type": "trigger",
    "event_class": "ChangesZone",
    "valid_filter": "Creature.YouCtrl",
    "zone_origin": "Battlefield",
    "zone_destination": "Graveyard",
}


@pytest.fixture()
def conn():
    c = _make_db()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Rule logic tests run with the flag ON; the default-off contract has
    its own explicit test below."""
    monkeypatch.setattr(do, "_ENABLE_DEATH_OUTLET_FEEDER", True)


def _add_free_outlet(conn, name="Viscera Seer"):
    _add_port(
        conn,
        name,
        "cost",
        "sacrifice",
        cost_target="other",
        raw_line="Sac<1/Creature/Creature>",
    )


class TestFindDeathOutletComplements:
    def test_flag_off_returns_nothing(self, conn, monkeypatch):
        monkeypatch.setattr(do, "_ENABLE_DEATH_OUTLET_FEEDER", False)
        _add_free_outlet(conn)
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER], {"Meren of Clan Nel Toth"})
        assert out == []

    def test_gate_rejects_etb_shaped_changeszone(self, conn):
        _add_free_outlet(conn)
        etb = dict(DEATH_TRIGGER, zone_origin="Any", zone_destination="Battlefield")
        out = _find_death_outlet_complements(conn, [etb], {"Some Commander"})
        assert out == []

    def test_gate_rejects_self_only_filter(self, conn):
        _add_free_outlet(conn)
        self_only = dict(DEATH_TRIGGER, valid_filter="Card.Self")
        out = _find_death_outlet_complements(conn, [self_only], {"Some Commander"})
        assert out == []

    def test_gate_passes_wilhelt_shaped_subtype_cohort_commander(self, conn):
        """Documents the gate-breadth reality (F2, PR #103 review): a
        Wilhelt-shaped commander -- a ChangesZone-to-graveyard death trigger
        with a subtype-naming filter (e.g. "Zombie"), and NO Sacrificed
        trigger of its own -- PASSES ``_commander_has_death_outlet_gate``.

        This is INTENTIONAL documentation, not a regression: the gate
        (``has_changeszone_death_payoff`` AND no Sacrificed trigger) has no
        conjunct excluding ``subtype_death_payoff`` cohort membership at the
        port level -- that exclusion only happens at cohort-ENUMERATION time
        (``bench/cohorts.py::outlet_direction_death_payoff`` subtracts
        ``subtype_death_payoff(conn)``'s members from its own qualifying
        set). A subtype-cohort commander like Wilhelt, the Rotcleaver or
        Slimefoot, the Stowaway is therefore NOT excluded by this port-level
        gate -- it would stack with the shipped ``subtype_supply_*`` rules
        if the flag were ever flipped on, a combination never sweep-tested.
        See the module docstring's re-flip warning (added PR #103 review,
        Phase 2) before ever flipping ``_ENABLE_DEATH_OUTLET_FEEDER``.
        """
        subtype_death_trigger = dict(DEATH_TRIGGER, valid_filter="Zombie.YouCtrl")
        assert _commander_has_death_outlet_gate([subtype_death_trigger]) is True

    def test_gate_rejects_commander_with_sacrificed_port(self, conn):
        """Sacrificed-trigger commanders are served by cost_feeds_trigger already."""
        _add_free_outlet(conn)
        sac_trigger = {
            "port_type": "trigger",
            "event_class": "Sacrificed",
            "valid_filter": "Creature.YouCtrl",
        }
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER, sac_trigger], {"Meren of Clan Nel Toth"})
        assert out == []

    def test_match_case_free_outlet(self, conn):
        _add_free_outlet(conn, "Viscera Seer")
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER], {"Some Commander"})
        assert len(out) == 1
        c = out[0]
        assert c.rule_id == "death_outlet_feeder"
        assert c.direction == "synergy"
        assert c.candidate == "Viscera Seer"
        assert c.cmdr_event == "death_outlet"
        assert c.cand_event in {"free_outlet", "paid_outlet", "self_sac"}
        assert c.cand_event == "free_outlet"

    def test_match_case_paid_outlet(self, conn):
        _add_port(
            conn,
            "Attrition",
            "cost",
            "sacrifice",
            cost_target="other",
            raw_line="Sac<1/Creature/Creature> B",
        )
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER], {"Some Commander"})
        assert len(out) == 1
        assert out[0].cand_event == "paid_outlet"

    def test_match_case_self_sac(self, conn):
        _add_port(
            conn,
            "Sakura-Tribe Elder",
            "cost",
            "sacrifice",
            cost_target="self",
            raw_line="Sac<1/CARDNAME>",
        )
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER], {"Some Commander"})
        assert len(out) == 1
        assert out[0].cand_event == "self_sac"

    def test_dedup_one_complement_per_card(self, conn):
        _add_free_outlet(conn, "Viscera Seer")
        _add_port(
            conn,
            "Viscera Seer",
            "cost",
            "sacrifice",
            cost_target="self",
            raw_line="Sac<1/CARDNAME>",
        )
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER], {"Some Commander"})
        assert len(out) == 1

    def test_commander_self_exclusion(self, conn):
        _add_free_outlet(conn, "Meren of Clan Nel Toth")
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER], {"Meren of Clan Nel Toth"})
        assert out == []

    def test_cand_event_deterministic_across_row_order(self, conn):
        """F9 (PR #103 review): a card with BOTH a free_outlet-shaped and a
        self_sac-shaped cost.sacrifice port must always classify as
        ``free_outlet`` (min(groups) alphabetically) -- never a coin flip
        depending on SQLite's (unordered) row-return order."""
        _add_port(
            conn,
            "Ambiguous Outlet",
            "cost",
            "sacrifice",
            cost_target="self",
            raw_line="Sac<1/CARDNAME>",
        )
        _add_port(
            conn,
            "Ambiguous Outlet",
            "cost",
            "sacrifice",
            cost_target="other",
            raw_line="Sac<1/Creature/Creature>",
        )
        out = _find_death_outlet_complements(conn, [DEATH_TRIGGER], {"Some Commander"})
        assert len(out) == 1
        assert out[0].cand_event == "free_outlet"


class TestWiring:
    def test_registry_gate_present(self):
        from mtg_synergy_graph.complement_rules.registry import _CARD_ATTR_GATES

        rule_ids = {g.rule_id for g in _CARD_ATTR_GATES}
        assert "death_outlet_feeder" in rule_ids

    def test_gate_predicate_matches_death_shaped_trigger(self):
        from mtg_synergy_graph.complement_rules.registry import _CARD_ATTR_GATES

        gate = next(g for g in _CARD_ATTR_GATES if g.rule_id == "death_outlet_feeder")
        assert gate.predicate(DEATH_TRIGGER) is True
        etb = dict(DEATH_TRIGGER, zone_origin="Any", zone_destination="Battlefield")
        assert gate.predicate(etb) is False

    def test_gate_is_flag_aware(self, monkeypatch):
        """The gate must read ``death_outlet._ENABLE_DEATH_OUTLET_FEEDER`` at
        CALL time, not capture it at import time -- otherwise gap_report /
        demand_coverage / rule_quality_gate would misattribute the unserved
        ChangesZone-death signature as already covered by a rule that never
        fires (final-review B1)."""
        from mtg_synergy_graph.complement_rules.registry import _CARD_ATTR_GATES

        gate = next(g for g in _CARD_ATTR_GATES if g.rule_id == "death_outlet_feeder")

        monkeypatch.setattr(do, "_ENABLE_DEATH_OUTLET_FEEDER", False)
        assert gate.predicate(DEATH_TRIGGER) is False

        monkeypatch.setattr(do, "_ENABLE_DEATH_OUTLET_FEEDER", True)
        assert gate.predicate(DEATH_TRIGGER) is True

    def test_not_in_card_level_rules(self):
        from mtg_synergy_graph.complement_rules.registry import CARD_LEVEL_RULES

        assert "death_outlet_feeder" not in CARD_LEVEL_RULES

    def test_bucket_mapping(self):
        from mtg_synergy_graph.universal_scorer import _RULE_TO_BUCKET

        assert _RULE_TO_BUCKET["death_outlet_feeder"] == "cost_synergy"

    def test_dispatched_from_core(self):
        """core.py must call the helper (source-level check keeps the test
        independent of a full engine fixture)."""
        import inspect

        from mtg_synergy_graph.complement_rules import core

        src = inspect.getsource(core)
        assert "_find_death_outlet_complements" in src

    def test_flag_default_off(self):
        """Read the module source rather than the live attribute -- the
        file-level ``_enable_flag`` autouse fixture monkeypatches the flag to
        True for the duration of every test in this module (including this
        one), so ``do._ENABLE_DEATH_OUTLET_FEEDER`` would read True here even
        though the module's own default is False.
        """
        import inspect

        src = inspect.getsource(do)
        assert "_ENABLE_DEATH_OUTLET_FEEDER = False" in src
