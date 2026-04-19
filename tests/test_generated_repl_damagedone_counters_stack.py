"""AUTO-GENERATED tests for rule: repl_damagedone_counters_stack."""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.generated.repl_damagedone_counters_stack import (
    _find_repl_damagedone_counters_stack,
    _repl_damagedone_counters_stack_gate,
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


def _add_repl(conn, name, event, result):
    conn.execute("INSERT OR IGNORE INTO cards (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, replacement_result) "
        "VALUES (?, 'replacement', ?, ?)",
        (name, event, result),
    )


def _port(**kwargs):
    return dict(kwargs)


class TestGate:
    def test_non_replacement_skips(self):
        assert not _repl_damagedone_counters_stack_gate(
            _port(port_type="trigger", event_class="DamageDone", replacement_result="Counters")
        )

    def test_other_event_skips(self):
        assert not _repl_damagedone_counters_stack_gate(
            _port(port_type="replacement", event_class="OtherEvent", replacement_result="Counters")
        )

    def test_other_result_skips(self):
        assert not _repl_damagedone_counters_stack_gate(
            _port(port_type="replacement", event_class="DamageDone", replacement_result="OtherResult")
        )

    def test_target_shape_matches(self):
        assert _repl_damagedone_counters_stack_gate(
            _port(port_type="replacement", event_class="DamageDone", replacement_result="Counters")
        )


class TestFind:
    def _cmdr_ports(self):
        return [_port(port_type="replacement", event_class="DamageDone", replacement_result="Counters")]

    def test_no_replacement_port_returns_empty(self, conn):
        assert (
            _find_repl_damagedone_counters_stack(conn, [_port(port_type="trigger", event_class="Attacks")], set()) == []
        )

    def test_finds_other_cards_with_same_shape(self, conn):
        _add_repl(conn, "Partner Card A", "DamageDone", "Counters")
        _add_repl(conn, "Partner Card B", "DamageDone", "Counters")
        results = _find_repl_damagedone_counters_stack(conn, self._cmdr_ports(), set())
        names = {r.candidate for r in results}
        assert "Partner Card A" in names
        assert "Partner Card B" in names

    def test_excludes_other_shapes(self, conn):
        _add_repl(conn, "Wrong Result Card", "DamageDone", "OtherResult")
        _add_repl(conn, "Wrong Event Card", "OtherEvent", "Counters")
        results = _find_repl_damagedone_counters_stack(conn, self._cmdr_ports(), set())
        names = {r.candidate for r in results}
        assert "Wrong Result Card" not in names
        assert "Wrong Event Card" not in names

    def test_excludes_commander(self, conn):
        _add_repl(conn, "Self Commander", "DamageDone", "Counters")
        results = _find_repl_damagedone_counters_stack(conn, self._cmdr_ports(), {"Self Commander"})
        names = {r.candidate for r in results}
        assert "Self Commander" not in names

    def test_rule_id(self, conn):
        _add_repl(conn, "Partner Card", "DamageDone", "Counters")
        results = _find_repl_damagedone_counters_stack(conn, self._cmdr_ports(), set())
        assert all(r.rule_id == "repl_damagedone_counters_stack" for r in results)
        assert all(r.cand_event == "same_shape_replacement" for r in results)
