"""Tests for the Prepared / AlternateMode:Prepare complement rule.

Builds a small in-memory SQLite database with synthetic ``card_ports`` and
``port_attributes`` rows to exercise both detection paths of
``_find_prepared_mechanic_complements`` and its associated registry gate:

  - Cheap path: commander has a ``static AlternateMode Prepare`` port.
  - Slow path: commander has an ``effect AlterAttribute`` port whose
    ``port_attributes`` contains ``(attribute, Prepared)``.

The in-memory schema is the minimum subset the rule's two SQL queries
touch — kept narrow on purpose so a future schema change to ``card_ports``
doesn't drag this test file with it.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.prepared import (
    _commander_has_alternate_mode_prepare,
    _find_prepared_mechanic_complements,
)
from mtg_synergy_graph.complement_rules.registry import RULE_GATES

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    """In-memory SQLite with the minimum schema the rule's SQL touches."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE card_ports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name       TEXT NOT NULL,
            port_type       TEXT NOT NULL,
            event_class     TEXT NOT NULL,
            granted_keyword TEXT
        );
        CREATE TABLE port_attributes (
            port_id    INTEGER NOT NULL,
            attr_kind  TEXT NOT NULL,
            attr_value TEXT NOT NULL,
            is_negated BOOLEAN DEFAULT FALSE
        );
        """
    )
    yield c
    c.close()


def _port_row(**kwargs) -> dict:
    """Build a PortRow dict with neutral defaults."""
    defaults = {
        "card_name": "",
        "port_type": "",
        "event_class": "",
        "granted_keyword": "",
        "valid_filter": "",
        "raw_line": "",
    }
    defaults.update(kwargs)
    return defaults


def _insert_alternate_mode_prepare_port(conn: sqlite3.Connection, card_name: str) -> int:
    """Insert a synthetic ``static AlternateMode Prepare`` port. Returns port id."""
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword) VALUES (?, ?, ?, ?)",
        (card_name, "static", "AlternateMode", "Prepare"),
    )
    return cur.lastrowid


def _insert_alter_attribute_prepared(conn: sqlite3.Connection, card_name: str) -> int:
    """Insert a synthetic ``effect AlterAttribute`` port + matching
    ``port_attributes`` row. Returns port id."""
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword) VALUES (?, ?, ?, ?)",
        (card_name, "effect", "AlterAttribute", None),
    )
    port_id = cur.lastrowid
    conn.execute(
        "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, ?, ?)",
        (port_id, "attribute", "Prepared"),
    )
    return port_id


# ---------------------------------------------------------------------------
# _find_prepared_mechanic_complements — cheap path
# ---------------------------------------------------------------------------


class TestCheapPathPreparedPayoffCommander:
    """Commander has the synthetic ``static AlternateMode Prepare`` port.

    This is the path covering the 47 Prepared payoff creatures including
    the ~20 that self-prepare via ``K:ETBReplacement:Other:DBPrepare``
    (which doesn't surface an AlterAttribute port).
    """

    def test_fires_for_alternate_mode_prepare_commander(self, conn):
        """Abigale-shape commander (AlternateMode:Prepare on its port set)
        should produce complements for every other Prepared payoff card."""
        # Commander has AlternateMode:Prepare on its port set
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="AlternateMode",
                granted_keyword="Prepare",
            ),
        ]
        # Insert three other Prepared payoff candidates
        for name in ("Adventurous Eater", "Cheerful Osteomancer", "Emeritus of Woe"):
            _insert_alternate_mode_prepare_port(conn, name)
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, cmdr_ports, {"Abigale, Poet Laureate"})
        candidates = {r.candidate for r in results}
        assert candidates == {"Adventurous Eater", "Cheerful Osteomancer", "Emeritus of Woe"}

    def test_excludes_self(self, conn):
        """Commander itself must not appear in its own complement set."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="AlternateMode",
                granted_keyword="Prepare",
            ),
        ]
        # Insert the commander itself + one other Prepared card in card_ports
        _insert_alternate_mode_prepare_port(conn, "Abigale, Poet Laureate")
        _insert_alternate_mode_prepare_port(conn, "Adventurous Eater")
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, cmdr_ports, {"Abigale, Poet Laureate"})
        candidates = {r.candidate for r in results}
        assert "Abigale, Poet Laureate" not in candidates
        assert candidates == {"Adventurous Eater"}

    def test_complement_metadata_is_stable(self, conn):
        """Emitted PortComplement carries the expected rule_id / event tags
        so the IDF basis and recommend.py --explain rendering stay
        deterministic across rebuilds.
        """
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="AlternateMode",
                granted_keyword="Prepare",
            ),
        ]
        _insert_alternate_mode_prepare_port(conn, "Adventurous Eater")
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, cmdr_ports, {"Abigale, Poet Laureate"})
        assert len(results) == 1
        c = results[0]
        assert c.rule_id == "prepared_mechanic"
        assert c.direction == "synergy"
        assert c.cmdr_event == "Prepared_ecosystem"
        assert c.cand_event == "AlternateMode_Prepare"

    def test_alternate_mode_with_non_prepare_value_does_not_activate(self, conn):
        """A commander with AlternateMode:Modal (e.g., Tergrid) must NOT be
        treated as a Prepared ecosystem commander. Regression for the
        ``_ALTERNATE_MODE_PORT_VALUES`` narrowing — Modal/Adventure/Split
        synthetic ports shouldn't even exist post-import, but the rule's
        cheap-path predicate still checks ``granted_keyword == 'Prepare'``
        as a defense-in-depth.
        """
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="AlternateMode",
                granted_keyword="Modal",
            ),
        ]
        _insert_alternate_mode_prepare_port(conn, "Adventurous Eater")
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, cmdr_ports, {"Tergrid, God of Fright"})
        assert results == []


# ---------------------------------------------------------------------------
# _find_prepared_mechanic_complements — slow path
# ---------------------------------------------------------------------------


class TestSlowPathPrepareEnablerCommander:
    """Commander has an ``effect AlterAttribute`` port whose port_attributes
    include ``(attribute, Prepared)``, but no ``AlternateMode:Prepare`` of
    its own. Covers hypothetical enabler-only legendary creatures (the only
    current example is Skycoach Waypoint, a land — not a commander).
    """

    def test_fires_via_port_attributes_join(self, conn):
        """Commander has an AlterAttribute Prepared port (no AlternateMode
        static). Rule should fall through to the slow path and still emit
        complements for every Prepared payoff candidate."""
        # Commander has NO AlternateMode static port on its ports list.
        # cmdr_ports passed to the rule is the in-memory list; the slow path
        # queries the DB directly, so we also need the commander's
        # AlterAttribute port in card_ports.
        cmdr_ports: list[dict] = []
        _insert_alter_attribute_prepared(conn, "Synthetic Enabler")
        _insert_alternate_mode_prepare_port(conn, "Adventurous Eater")
        _insert_alternate_mode_prepare_port(conn, "Emeritus of Woe")
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, cmdr_ports, {"Synthetic Enabler"})
        candidates = {r.candidate for r in results}
        assert candidates == {"Adventurous Eater", "Emeritus of Woe"}

    def test_alter_attribute_other_attribute_does_not_activate(self, conn):
        """An AlterAttribute port that grants Suspected (not Prepared) must
        NOT put the commander in the Prepared ecosystem.
        """
        # Insert an AlterAttribute Suspected on the commander side
        cur = conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword) VALUES (?, ?, ?, ?)",
            ("Suspected Tribesman", "effect", "AlterAttribute", None),
        )
        port_id = cur.lastrowid
        conn.execute(
            "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, ?, ?)",
            (port_id, "attribute", "Suspected"),
        )
        # Prepared candidate exists in the universe but should not be paired.
        _insert_alternate_mode_prepare_port(conn, "Adventurous Eater")
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, [], {"Suspected Tribesman"})
        assert results == []


# ---------------------------------------------------------------------------
# _find_prepared_mechanic_complements — negative paths
# ---------------------------------------------------------------------------


class TestNonPreparedCommander:
    def test_no_prepared_signals_returns_empty(self, conn):
        """Korvold-shape commander (no Prepared signal anywhere) returns []
        even when Prepared candidates exist in the universe."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Sacrificed",
                valid_filter="Permanent.YouCtrl",
            ),
        ]
        _insert_alternate_mode_prepare_port(conn, "Adventurous Eater")
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, cmdr_ports, {"Korvold, Fae-Cursed King"})
        assert results == []

    def test_empty_universe_returns_empty(self, conn):
        """Commander IS Prepared but no other Prepared cards exist —
        result is empty, not crash."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="AlternateMode",
                granted_keyword="Prepare",
            ),
        ]
        # Don't insert any Prepared candidates.
        conn.commit()

        results = _find_prepared_mechanic_complements(conn, cmdr_ports, {"Abigale, Poet Laureate"})
        assert results == []


# ---------------------------------------------------------------------------
# _commander_has_alternate_mode_prepare — cheap-path predicate
# ---------------------------------------------------------------------------


class TestCheapPathPredicate:
    def test_matches_static_alternate_mode_prepare(self):
        ports = [
            _port_row(
                port_type="static",
                event_class="AlternateMode",
                granted_keyword="Prepare",
            ),
        ]
        assert _commander_has_alternate_mode_prepare(ports) is True

    def test_rejects_alternate_mode_with_other_value(self):
        ports = [
            _port_row(
                port_type="static",
                event_class="AlternateMode",
                granted_keyword="Modal",
            ),
        ]
        assert _commander_has_alternate_mode_prepare(ports) is False

    def test_rejects_non_static_port_type(self):
        ports = [
            _port_row(
                port_type="trigger",
                event_class="AlternateMode",
                granted_keyword="Prepare",
            ),
        ]
        assert _commander_has_alternate_mode_prepare(ports) is False

    def test_rejects_non_alternate_mode_event_class(self):
        ports = [
            _port_row(
                port_type="static",
                event_class="Continuous",
                granted_keyword="Prepare",
            ),
        ]
        assert _commander_has_alternate_mode_prepare(ports) is False

    def test_handles_empty_port_list(self):
        assert _commander_has_alternate_mode_prepare([]) is False


# ---------------------------------------------------------------------------
# RuleGate registry entry
# ---------------------------------------------------------------------------


class TestPreparedMechanicRuleGate:
    """The registry gate predicate is what the auditor uses to attribute
    rule firings to a specific commander port. It must agree with the
    cheap-path predicate above on its single-port signature.
    """

    @pytest.fixture()
    def gate(self):
        for g in RULE_GATES:
            if g.rule_id == "prepared_mechanic":
                return g.predicate
        raise AssertionError("prepared_mechanic gate not registered in RULE_GATES")

    def test_gate_matches_alternate_mode_prepare_port(self, gate):
        port = _port_row(
            port_type="static",
            event_class="AlternateMode",
            granted_keyword="Prepare",
        )
        assert gate(port) is True

    def test_gate_rejects_alternate_mode_modal(self, gate):
        port = _port_row(
            port_type="static",
            event_class="AlternateMode",
            granted_keyword="Modal",
        )
        assert gate(port) is False

    def test_gate_rejects_non_static_port(self, gate):
        port = _port_row(
            port_type="trigger",
            event_class="AlternateMode",
            granted_keyword="Prepare",
        )
        assert gate(port) is False

    def test_gate_rejects_unrelated_static_port(self, gate):
        port = _port_row(
            port_type="static",
            event_class="Continuous",
            granted_keyword="",
        )
        assert gate(port) is False
