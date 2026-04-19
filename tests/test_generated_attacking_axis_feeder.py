"""AUTO-GENERATED tests for rule: attacking_axis_feeder."""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.generated.attacking_axis_feeder import (
    _attacking_axis_feeder_gate,
    _find_attacking_axis_feeder,
)

SCHEMA = """\
CREATE TABLE cards (name TEXT PRIMARY KEY, types TEXT);
CREATE TABLE card_ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name TEXT NOT NULL,
    port_type TEXT NOT NULL,
    event_class TEXT NOT NULL,
    valid_filter TEXT,
    raw_line TEXT,
    counter_type TEXT,
    zone_origin TEXT,
    zone_destination TEXT,
    replacement_event TEXT,
    replacement_result TEXT
);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


def _add_peer_scaler(conn, name):
    """Insert a candidate matching the peer_scaler-style portable tier
    pattern. Most axis_feeder qualifiers include a peer_scaler tier
    (scales_with.Valid with the qualifier), so this fixture works
    across qualifiers without needing per-tier custom SQL.
    """
    conn.execute("INSERT OR IGNORE INTO cards (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter) "
        "VALUES (?, 'scales_with', 'Valid', 'Creature.attacking+YouCtrl')",
        (name,),
    )


def _port(**kwargs):
    return dict(kwargs)


class TestGate:
    def test_qualifier_matches(self):
        assert _attacking_axis_feeder_gate(
            _port(port_type="trigger", event_class="Attacks", valid_filter="Creature.YouCtrl+attacking")
        )

    def test_self_anchored_rejected(self):
        # Self-anchored conditions are commander-level, not payoff axes.
        assert not _attacking_axis_feeder_gate(
            _port(port_type="trigger", event_class="Attacks", valid_filter="Card.Self+attacking")
        )

    def test_oppctrl_scope_rejected(self):
        # Opponent-scoped filters: defensive utilities, not payoff axis.
        assert not _attacking_axis_feeder_gate(
            _port(port_type="effect", event_class="DealDamage", valid_filter="Creature.attacking+OppCtrl")
        )

    def test_unrelated_filter_skips(self):
        assert not _attacking_axis_feeder_gate(
            _port(port_type="trigger", event_class="Attacks", valid_filter="Creature.YouCtrl")
        )

    def test_unrelated_port_type_skips(self):
        assert not _attacking_axis_feeder_gate(_port(port_type="keyword", event_class="Vigilance", valid_filter=""))


class TestFind:
    def _cmdr_ports(self):
        return [_port(port_type="scales_with", event_class="Valid", valid_filter="Creature.YouCtrl+attacking")]

    def test_no_qualifier_port_returns_empty(self, conn):
        assert _find_attacking_axis_feeder(conn, [_port(port_type="trigger", event_class="Attacks")], set()) == []

    def test_finds_peer_scaler_tier(self, conn):
        # Per-tier SQL coverage is left to integration: the peer_scaler
        # tier is the only qualifier-portable shape (the others —
        # anthem / mass_pump / mass_untap / opponent_lock — depend on
        # qualifier-specific candidate ports). Verifying the helper
        # surfaces a peer_scaler hit confirms gate→tier wiring works.
        _add_peer_scaler(conn, "Peer Scaler")
        results = _find_attacking_axis_feeder(conn, self._cmdr_ports(), set())
        names = {r.candidate for r in results}
        assert "Peer Scaler" in names

    def test_excludes_commander(self, conn):
        _add_peer_scaler(conn, "Self Commander")
        results = _find_attacking_axis_feeder(conn, self._cmdr_ports(), {"Self Commander"})
        names = {r.candidate for r in results}
        assert "Self Commander" not in names

    def test_rule_id(self, conn):
        _add_peer_scaler(conn, "Sample Card")
        results = _find_attacking_axis_feeder(conn, self._cmdr_ports(), set())
        assert all(r.rule_id == "attacking_axis_feeder" for r in results)
