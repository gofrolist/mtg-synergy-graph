"""AUTO-GENERATED tests for rule: cascade_tribal."""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.generated.cascade_tribal import (
    _cascade_tribal_gate,
    _find_cascade_tribal,
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


def _add_keyword(conn, name, keyword):
    conn.execute("INSERT OR IGNORE INTO cards (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, 'keyword', ?)",
        (name, keyword),
    )


def _port(**kwargs):
    return dict(kwargs)


class TestCascadeGate:
    def test_non_keyword_port_skips(self):
        assert not _cascade_tribal_gate(_port(port_type="trigger", event_class="Cascade"))

    def test_other_keyword_skips(self):
        assert not _cascade_tribal_gate(_port(port_type="keyword", event_class="Flying"))

    def test_target_keyword_matches(self):
        assert _cascade_tribal_gate(_port(port_type="keyword", event_class="Cascade"))


class TestFindCascade:
    def _cmdr_ports(self):
        return [_port(port_type="keyword", event_class="Cascade")]

    def test_no_keyword_port_returns_empty(self, conn):
        assert _find_cascade_tribal(conn, [_port(port_type="trigger", event_class="Attacks")], set()) == []

    def test_finds_other_cards_with_keyword(self, conn):
        """Maelstrom Wanderer (Cascade) should pair with Apex Devastator
        and Bloodbraid Elf — the canonical cascade peers."""
        _add_keyword(conn, "Apex Devastator", "Cascade")
        _add_keyword(conn, "Bloodbraid Elf", "Cascade")
        results = _find_cascade_tribal(conn, self._cmdr_ports(), set())
        names = {r.candidate for r in results}
        assert "Apex Devastator" in names
        assert "Bloodbraid Elf" in names

    def test_excludes_other_keyword_cards(self, conn):
        _add_keyword(conn, "Shivan Dragon", "Flying")
        results = _find_cascade_tribal(conn, self._cmdr_ports(), set())
        names = {r.candidate for r in results}
        assert "Shivan Dragon" not in names

    def test_excludes_commander(self, conn):
        """Maelstrom Wanderer shouldn't recommend herself."""
        _add_keyword(conn, "Maelstrom Wanderer", "Cascade")
        results = _find_cascade_tribal(conn, self._cmdr_ports(), {"Maelstrom Wanderer"})
        names = {r.candidate for r in results}
        assert "Maelstrom Wanderer" not in names

    def test_rule_id(self, conn):
        _add_keyword(conn, "Bituminous Blast", "Cascade")
        results = _find_cascade_tribal(conn, self._cmdr_ports(), set())
        assert all(r.rule_id == "cascade_tribal" for r in results)
        assert all(r.cand_event == "same_keyword_partner" for r in results)

    def test_dedup_multiple_cascade_ports_same_card(self, conn):
        """Apex Devastator has four ``Cascade`` keyword ports. We want a
        single complement per candidate, not four."""
        _add_keyword(conn, "Apex Devastator", "Cascade")
        _add_keyword(conn, "Apex Devastator", "Cascade")
        _add_keyword(conn, "Apex Devastator", "Cascade")
        results = _find_cascade_tribal(conn, self._cmdr_ports(), set())
        apex_count = sum(1 for r in results if r.candidate == "Apex Devastator")
        assert apex_count == 1
