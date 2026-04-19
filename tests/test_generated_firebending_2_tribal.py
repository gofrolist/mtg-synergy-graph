"""AUTO-GENERATED tests for rule: firebending_2_tribal."""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.generated.firebending_2_tribal import (
    _find_firebending_2_tribal,
    _firebending_2_tribal_gate,
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


class TestFirebending2Gate:
    def test_non_keyword_port_skips(self):
        assert not _firebending_2_tribal_gate(_port(port_type="trigger", event_class="Firebending:2"))

    def test_other_keyword_skips(self):
        assert not _firebending_2_tribal_gate(_port(port_type="keyword", event_class="Flying"))

    def test_target_keyword_matches(self):
        assert _firebending_2_tribal_gate(_port(port_type="keyword", event_class="Firebending:2"))


class TestFindFirebending2:
    def _cmdr_ports(self):
        return [_port(port_type="keyword", event_class="Firebending:2")]

    def test_no_keyword_port_returns_empty(self, conn):
        assert _find_firebending_2_tribal(conn, [_port(port_type="trigger", event_class="Attacks")], set()) == []

    def test_finds_other_cards_with_keyword(self, conn):
        _add_keyword(conn, "Partner Card A", "Firebending:2")
        _add_keyword(conn, "Partner Card B", "Firebending:2")
        results = _find_firebending_2_tribal(conn, self._cmdr_ports(), set())
        names = {r.candidate for r in results}
        assert "Partner Card A" in names
        assert "Partner Card B" in names

    def test_excludes_other_keyword_cards(self, conn):
        _add_keyword(conn, "Wrong Keyword Card", "Flying")
        results = _find_firebending_2_tribal(conn, self._cmdr_ports(), set())
        names = {r.candidate for r in results}
        assert "Wrong Keyword Card" not in names

    def test_excludes_commander(self, conn):
        _add_keyword(conn, "Self Commander", "Firebending:2")
        results = _find_firebending_2_tribal(conn, self._cmdr_ports(), {"Self Commander"})
        names = {r.candidate for r in results}
        assert "Self Commander" not in names

    def test_rule_id(self, conn):
        _add_keyword(conn, "Partner Card", "Firebending:2")
        results = _find_firebending_2_tribal(conn, self._cmdr_ports(), set())
        assert all(r.rule_id == "firebending_2_tribal" for r in results)
        assert all(r.cand_event == "same_keyword_partner" for r in results)
