"""Tests for complement_rules.subtype_supply (plan 2026-07-07-001 Task 2).

RULE_PLANNING.md section 4 required cases: gate rejection, qualifier
rejection, per-direction match, per-direction exclusion, dedup, commander
self-exclusion, exact rule_id.
"""

from __future__ import annotations

import sqlite3

import pytest

import mtg_synergy_graph.complement_rules.subtype_supply as ss
from mtg_synergy_graph.complement_rules.subtype_supply import (
    _find_subtype_supply_complements,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE cards (
            name TEXT PRIMARY KEY,
            subtypes TEXT,
            card_types TEXT,
            edhrec_rank INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name TEXT NOT NULL,
            port_type TEXT NOT NULL,
            event_class TEXT NOT NULL,
            valid_filter TEXT,
            zone_origin TEXT,
            zone_destination TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE port_attributes (
            port_id INTEGER NOT NULL,
            attr_kind TEXT NOT NULL,
            attr_value TEXT NOT NULL,
            is_negated BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (port_id, attr_kind, attr_value, is_negated)
        )
        """
    )
    return conn


def _add_card(conn, name, subtypes="", card_types="Creature"):
    conn.execute(
        "INSERT INTO cards (name, subtypes, card_types) VALUES (?, ?, ?)",
        (name, subtypes, card_types),
    )


def _add_producer(conn, name, subtype):
    _add_card(conn, name)
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, 'effect', 'Token')",
        (name,),
    )
    conn.execute(
        "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', ?)",
        (cur.lastrowid, subtype),
    )


DEATH_TRIGGER = {
    "port_type": "trigger",
    "event_class": "ChangesZone",
    "valid_filter": "Saproling.YouCtrl",
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
    monkeypatch.setattr(ss, "_ENABLE_SUBTYPE_SUPPLY", True)


class TestFindSubtypeSupply:
    def test_flag_off_returns_nothing(self, conn, monkeypatch):
        monkeypatch.setattr(ss, "_ENABLE_SUBTYPE_SUPPLY", False)
        _add_producer(conn, "Sprout Swarm", "Saproling")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        assert out == []

    def test_gate_rejects_commander_without_death_trigger(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")
        etb = dict(DEATH_TRIGGER, zone_origin="Any", zone_destination="Battlefield")
        assert _find_subtype_supply_complements(conn, [etb], {"Slimefoot"}) == []

    def test_gate_rejects_subtype_outside_token_vocab(self, conn):
        # No port_attributes rows at all -> empty vocab -> no payoff subtype.
        _add_card(conn, "Some Body", subtypes="Saproling")
        assert _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"}) == []

    def test_producer_direction_matches(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        producers = [c for c in out if c.rule_id == "subtype_supply_producer"]
        assert [c.candidate for c in producers] == ["Sprout Swarm"]
        assert producers[0].direction == "synergy"
        assert producers[0].cmdr_event == "death_payoff"
        assert producers[0].cand_event == "Saproling"

    def test_producer_direction_excludes_other_subtypes(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")  # establishes vocab
        _add_producer(conn, "Krenko", "Goblin")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        names = {c.candidate for c in out if c.rule_id == "subtype_supply_producer"}
        assert names == {"Sprout Swarm"}

    def test_body_direction_matches_exact_token(self, conn):
        _add_producer(conn, "Sprout Swarm", "Saproling")  # establishes vocab
        _add_card(conn, "Mycoloth", subtypes="Fungus Saproling")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        bodies = {c.candidate for c in out if c.rule_id == "subtype_supply_body"}
        assert "Mycoloth" in bodies

    def test_body_direction_is_token_anchored_not_substring(self, conn):
        """The documented Rat-in-Pirate bug: subtype match must split, not LIKE."""
        rat_trigger = dict(DEATH_TRIGGER, valid_filter="Rat.YouCtrl")
        _add_producer(conn, "Rat Producer", "Rat")  # establishes Rat in vocab
        _add_card(conn, "Ruthless Knave", subtypes="Human Pirate")
        out = _find_subtype_supply_complements(conn, [rat_trigger], {"Marrow-Gnawer"})
        bodies = {c.candidate for c in out if c.rule_id == "subtype_supply_body"}
        assert "Ruthless Knave" not in bodies

    def test_dedup_one_complement_per_card_per_rule(self, conn):
        # Card both produces Saproling tokens twice -> still one producer row.
        _add_producer(conn, "Sprout Swarm", "Saproling")
        cur = conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Sprout Swarm', 'effect', 'Token')"
        )
        conn.execute(
            "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', 'Saproling')",
            (cur.lastrowid,),
        )
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        producers = [c for c in out if c.rule_id == "subtype_supply_producer"]
        assert len(producers) == 1

    def test_card_matching_both_directions_gets_both_rule_ids(self, conn):
        _add_producer(conn, "Tender Greenkeeper", "Saproling")
        conn.execute("UPDATE cards SET subtypes = 'Elf Druid Saproling' WHERE name = 'Tender Greenkeeper'")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot"})
        rule_ids = {c.rule_id for c in out if c.candidate == "Tender Greenkeeper"}
        assert rule_ids == {"subtype_supply_producer", "subtype_supply_body"}

    def test_commander_self_exclusion(self, conn):
        _add_producer(conn, "Slimefoot, the Stowaway", "Saproling")
        out = _find_subtype_supply_complements(conn, [DEATH_TRIGGER], {"Slimefoot, the Stowaway"})
        assert out == []


class TestWiring:
    def test_registered_in_card_level_rules(self):
        from mtg_synergy_graph.complement_rules.registry import CARD_LEVEL_RULES

        assert "subtype_supply_producer" in CARD_LEVEL_RULES
        assert "subtype_supply_body" in CARD_LEVEL_RULES

    def test_bucket_mapping(self):
        from mtg_synergy_graph.universal_scorer import _RULE_TO_BUCKET

        assert _RULE_TO_BUCKET["subtype_supply_producer"] == "port_match"
        assert _RULE_TO_BUCKET["subtype_supply_body"] == "port_match"

    def test_dispatched_from_core(self):
        """core.py must call the helper (source-level check keeps the test
        independent of a full engine fixture)."""
        import inspect

        from mtg_synergy_graph.complement_rules import core

        src = inspect.getsource(core)
        assert "_find_subtype_supply_complements" in src
