"""Tests for mtg_synergy_graph.death_payoff (plan 2026-07-07-001 Task 1)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.death_payoff import (
    has_changeszone_death_payoff,
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


class TestHasChangeszoneDeathPayoff:
    """Port-level core shared with bench.cohorts.outlet_direction_death_payoff
    (plan 2026-07-07-002 Task 2) and, per that plan, the death_outlet_feeder
    rule gate + whitelist comparator (Tasks 5/6)."""

    def test_battlefield_to_graveyard_non_self_matches(self):
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "valid_filter": "Creature.Other+YouCtrl",
                "zone_origin": "Battlefield",
                "zone_destination": "Graveyard",
            }
        ]
        assert has_changeszone_death_payoff(cmdr_ports) is True

    def test_etb_shaped_trigger_does_not_match(self):
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "valid_filter": "Creature.Other+YouCtrl",
                "zone_origin": "Any",
                "zone_destination": "Battlefield",
            }
        ]
        assert has_changeszone_death_payoff(cmdr_ports) is False

    def test_self_only_filter_does_not_match(self):
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "ChangesZone",
                "valid_filter": "Card.Self",
                "zone_origin": "Battlefield",
                "zone_destination": "Graveyard",
            }
        ]
        assert has_changeszone_death_payoff(cmdr_ports) is False

    def test_sacrificed_event_alone_does_not_match(self):
        """Sacrificed is a death event but not ChangesZone-shaped; this helper
        deliberately only checks the ChangesZone arm (see its docstring)."""
        cmdr_ports = [
            {
                "port_type": "trigger",
                "event_class": "Sacrificed",
                "valid_filter": "Creature.YouCtrl",
                "zone_origin": None,
                "zone_destination": None,
            }
        ]
        assert has_changeszone_death_payoff(cmdr_ports) is False

    def test_non_trigger_port_ignored(self):
        cmdr_ports = [
            {
                "port_type": "effect",
                "event_class": "ChangesZone",
                "valid_filter": "Creature.Other+YouCtrl",
                "zone_origin": "Battlefield",
                "zone_destination": "Graveyard",
            }
        ]
        assert has_changeszone_death_payoff(cmdr_ports) is False

    def test_empty_ports_does_not_match(self):
        assert has_changeszone_death_payoff([]) is False


FIXTURE = Path(__file__).parent / "fixtures" / "golden_set_archetype_payoff.json"
LIVE_DB = Path(__file__).resolve().parents[1] / "data" / "synergy.db"
LIVE_EDHREC_DB = Path(__file__).resolve().parents[1] / "data" / "tags.db"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_bootstrap_min_high_synergy_rows() -> int:
    """Import ``MIN_HIGH_SYNERGY_ROWS`` from the bootstrap script itself.

    ``scripts/`` is not a package, so we load the module by file path via
    ``importlib``. The script only defines constants/functions at module
    scope and guards its side-effecting work behind ``if __name__ ==
    "__main__":``, so importing it here is safe (no fixture rebuild, no DB
    writes). This keeps the test's filter threshold mechanically tied to
    the bootstrap script's constant instead of a second hardcoded literal
    that could silently drift from it.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bootstrap_apf", REPO_ROOT / "scripts" / "bootstrap_archetype_payoff_fixture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MIN_HIGH_SYNERGY_ROWS


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
        test replicates that same filter (using the bootstrap script's own
        ``MIN_HIGH_SYNERGY_ROWS`` constant, not a hardcoded literal) so it is
        comparing like with like.
        """
        from mtg_synergy_graph.bench.cohorts import archetype_payoff_cohort
        from mtg_synergy_graph.bench.fixture import high_synergy_slug_counts
        from mtg_synergy_graph.db import open_db
        from mtg_synergy_graph.validate import commander_to_slug

        min_high_synergy_rows = _load_bootstrap_min_high_synergy_rows()
        pinned = set(json.loads(FIXTURE.read_text())["cohort_members"])
        cards_conn = open_db(str(LIVE_DB), create=False)
        edhrec_conn = open_db(str(LIVE_EDHREC_DB), create=False)
        try:
            cohort = archetype_payoff_cohort(cards_conn)
            slug_counts = high_synergy_slug_counts(edhrec_conn)
            live = {name for name in cohort if slug_counts.get(commander_to_slug(name), 0) >= min_high_synergy_rows}
        finally:
            cards_conn.close()
            edhrec_conn.close()
        assert live == pinned
