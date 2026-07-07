"""Tests for mtg_synergy_graph.death_payoff (plan 2026-07-07-001 Task 1)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.death_payoff import (
    is_death_event,
    payoff_subtypes_from_ports,
    token_subtype_vocab,
    valid_filter_subtype_tokens,
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


@pytest.fixture()
def conn():
    c = _make_db()
    yield c
    c.close()


def _add_port(conn, card, port_type, event_class, valid_filter=None, zo=None, zd=None) -> int:
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (card, port_type, event_class, valid_filter, zo, zd),
    )
    return cur.lastrowid


def _add_token_subtype(conn, port_id, subtype):
    conn.execute(
        "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', ?)",
        (port_id, subtype),
    )


class TestIsDeathEvent:
    def test_sacrificed_is_unconditional(self):
        assert is_death_event("Sacrificed", None, None) is True
        assert is_death_event("SacrificedOnce", "Library", "Exile") is True

    def test_changeszone_needs_graveyard_from_battlefield(self):
        assert is_death_event("ChangesZone", "Battlefield", "Graveyard") is True
        assert is_death_event("ChangesZoneAll", "", "Graveyard") is True
        assert is_death_event("ChangesZone", "Library", "Graveyard") is False  # mill
        assert is_death_event("ChangesZone", "Battlefield", "Exile") is False

    def test_other_events_never_match(self):
        assert is_death_event("SpellCast", None, None) is False


class TestValidFilterSubtypeTokens:
    def test_head_and_restriction_forms(self):
        assert valid_filter_subtype_tokens("Insect.YouCtrl,Creature.Zombie+Other") == [
            "Insect",
            "YouCtrl",
            "Creature",
            "Zombie",
            "Other",
        ]

    def test_negated_tokens_keep_prefix(self):
        assert "!Zombie" in valid_filter_subtype_tokens("Creature.!Zombie")


class TestPayoffSubtypesFromPorts:
    def test_extracts_vocab_intersected_subtype(self, conn):
        pid = _add_port(conn, "Some Producer", "effect", "Token")
        _add_token_subtype(conn, pid, "Saproling")
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "valid_filter": "Saproling.YouCtrl",
                "zone_origin": "Battlefield",
                "zone_destination": "Graveyard",
            }
        ]
        assert payoff_subtypes_from_ports(conn, cmdr_ports) == ["Saproling"]

    def test_non_death_trigger_yields_nothing(self, conn):
        pid = _add_port(conn, "Some Producer", "effect", "Token")
        _add_token_subtype(conn, pid, "Saproling")
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "valid_filter": "Saproling.YouCtrl",
                "zone_origin": "Any",
                "zone_destination": "Battlefield",  # ETB, not a death
            }
        ]
        assert payoff_subtypes_from_ports(conn, cmdr_ports) == []

    def test_subtype_outside_vocab_rejected(self, conn):
        # vocab is empty -> nothing can match
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "Sacrificed",
                "valid_filter": "Saproling.YouCtrl",
                "zone_origin": None,
                "zone_destination": None,
            }
        ]
        assert payoff_subtypes_from_ports(conn, cmdr_ports) == []

    def test_vocab_reads_token_subtype_rows(self, conn):
        pid = _add_port(conn, "Some Producer", "effect", "Token")
        _add_token_subtype(conn, pid, "Zombie")
        assert token_subtype_vocab(conn) == {"Zombie"}


FIXTURE = Path(__file__).parent / "fixtures" / "golden_set_archetype_payoff.json"
LIVE_DB = Path(__file__).resolve().parents[1] / "data" / "synergy.db"
LIVE_EDHREC_DB = Path(__file__).resolve().parents[1] / "data" / "tags.db"


@pytest.mark.skipif(
    not (LIVE_DB.exists() and LIVE_EDHREC_DB.exists()),
    reason="live synergy.db/tags.db not present",
)
class TestCohortUnchanged:
    def test_membership_matches_pinned_snapshot(self):
        """The refactor must not move cohort membership by one card.

        The fixture's ``cohort_members`` snapshot is the *EDHREC High-Synergy
        filtered* subset written by ``scripts/bootstrap_archetype_payoff_fixture.py``
        (33 of the 36 raw ``archetype_payoff_cohort`` members — see CLAUDE.md's
        "dropped: Daryl, Jenny Flint, Miara"), not the raw predicate output. This
        test replicates that same filter so it is comparing like with like.
        """
        from mtg_synergy_graph.bench.cohorts import archetype_payoff_cohort
        from mtg_synergy_graph.bench.fixture import high_synergy_slug_counts
        from mtg_synergy_graph.db import open_db
        from mtg_synergy_graph.validate import commander_to_slug

        pinned = set(json.loads(FIXTURE.read_text())["cohort_members"])
        cards_conn = open_db(str(LIVE_DB), create=False)
        edhrec_conn = open_db(str(LIVE_EDHREC_DB), create=False)
        try:
            cohort = archetype_payoff_cohort(cards_conn)
            slug_counts = high_synergy_slug_counts(edhrec_conn)
            live = {name for name in cohort if slug_counts.get(commander_to_slug(name), 0) >= 1}
        finally:
            cards_conn.close()
            edhrec_conn.close()
        assert live == pinned
