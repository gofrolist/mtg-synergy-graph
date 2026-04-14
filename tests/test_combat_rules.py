"""Tests for combat-related complement matchers.

Uses an in-memory SQLite database with minimal schema to exercise every
branch of the four functions in complement_rules.combat:
  - _find_combat_enhancers
  - _find_evasion_complements
  - _find_sacrifice_outlets
  - _find_changeszone_resonance
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.combat import (
    _find_changeszone_resonance,
    _find_combat_enhancers,
    _find_evasion_complements,
    _find_sacrifice_outlets,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the minimal schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE cards (
            name       TEXT PRIMARY KEY,
            card_types TEXT,
            types      TEXT,
            subtypes   TEXT
        );
        CREATE TABLE card_ports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name        TEXT NOT NULL,
            port_type        TEXT NOT NULL,
            event_class      TEXT NOT NULL,
            valid_filter     TEXT,
            zone_origin      TEXT,
            zone_destination TEXT,
            affected_scope   TEXT,
            raw_line         TEXT,
            branch_kind      TEXT DEFAULT 'root',
            is_conditional   BOOLEAN DEFAULT FALSE,
            replacement_event TEXT,
            replacement_result TEXT,
            amount           TEXT,
            counter_type     TEXT,
            cost_subtype     TEXT,
            phase            TEXT,
            effect_zone      TEXT,
            cost_target      TEXT
        );
    """)
    return conn


@pytest.fixture()
def conn():
    """Fixture: in-memory DB with schema, auto-closed after each test."""
    c = _make_db()
    yield c
    c.close()


def _port(
    card_name: str,
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
    zone_origin: str = "",
    zone_destination: str = "",
    raw_line: str = "",
    affected_scope: str = "",
    branch_kind: str = "root",
) -> dict:
    """Build a PortRow dict for use as a commander port."""
    return {
        "card_name": card_name,
        "port_type": port_type,
        "event_class": event_class,
        "valid_filter": valid_filter,
        "zone_origin": zone_origin,
        "zone_destination": zone_destination,
        "raw_line": raw_line,
        "affected_scope": affected_scope,
        "branch_kind": branch_kind,
        "is_conditional": False,
        "replacement_event": "",
        "replacement_result": "",
        "amount": "",
        "counter_type": "",
    }


def _insert_port(
    conn: sqlite3.Connection,
    card_name: str,
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
    raw_line: str = "",
    zone_destination: str = "",
    branch_kind: str = "root",
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line, zone_destination, branch_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter, raw_line, zone_destination, branch_kind),
    )


def _insert_card(
    conn: sqlite3.Connection,
    name: str,
    *,
    card_types: str = "",
    types: str = "",
    subtypes: str = "",
) -> None:
    conn.execute(
        "INSERT INTO cards (name, card_types, types, subtypes) VALUES (?, ?, ?, ?)",
        (name, card_types, types, subtypes),
    )


# ===========================================================================
# _find_combat_enhancers
# ===========================================================================


class TestFindCombatEnhancers:
    """Cover lines 24-77: DamageDone trigger detection + AddPhase/DoubleStrike candidates."""

    def test_no_damage_trigger_returns_empty(self, conn: sqlite3.Connection) -> None:
        """No DamageDone trigger -> empty results (line 35)."""
        ports = [_port("Cmdr", "trigger", "Sacrificed", valid_filter="Creature.YouCtrl")]
        assert _find_combat_enhancers(conn, ports, {"Cmdr"}) == []

    def test_non_trigger_port_type_skipped(self, conn: sqlite3.Connection) -> None:
        """port_type != 'trigger' is skipped (line 27)."""
        ports = [_port("Cmdr", "effect", "DamageDone")]
        assert _find_combat_enhancers(conn, ports, {"Cmdr"}) == []

    def test_self_only_damage_trigger_skipped(self, conn: sqlite3.Connection) -> None:
        """DamageDone with Card.Self valid_filter -> self-only, skip (lines 30-32)."""
        ports = [_port("Cmdr", "trigger", "DamageDone", valid_filter="Card.Self")]
        assert _find_combat_enhancers(conn, ports, {"Cmdr"}) == []

    def test_finds_addphase_candidates(self, conn: sqlite3.Connection) -> None:
        """DamageDone trigger -> picks up AddPhase effect candidates (lines 38-57)."""
        ports = [_port("Saskia", "trigger", "DamageDone", valid_filter="Creature.YouCtrl")]
        _insert_port(conn, "Aurelia", "effect", "AddPhase")
        results = _find_combat_enhancers(conn, ports, {"Saskia"})
        assert len(results) == 1
        assert results[0].candidate == "Aurelia"
        assert results[0].rule_id == "combat_enhancer"
        assert results[0].cand_event == "AddPhase"

    def test_finds_double_strike_candidates(self, conn: sqlite3.Connection) -> None:
        """DamageDone trigger -> picks up Double Strike keyword candidates (lines 59-75)."""
        ports = [_port("Saskia", "trigger", "DamageDone", valid_filter="Creature.YouCtrl")]
        _insert_port(conn, "Boros Charm", "keyword", "Double Strike")
        results = _find_combat_enhancers(conn, ports, {"Saskia"})
        assert len(results) == 1
        assert results[0].candidate == "Boros Charm"
        assert results[0].cand_event == "DoubleStrike"

    def test_excludes_commander_from_results(self, conn: sqlite3.Connection) -> None:
        """Commander's own cards excluded from candidates (line 47, 65)."""
        ports = [_port("Saskia", "trigger", "DamageDone", valid_filter="Creature.YouCtrl")]
        _insert_port(conn, "Saskia", "effect", "AddPhase")
        _insert_port(conn, "Aurelia", "effect", "AddPhase")
        results = _find_combat_enhancers(conn, ports, {"Saskia"})
        assert len(results) == 1
        assert results[0].candidate == "Aurelia"

    def test_dedup_across_addphase_and_double_strike(self, conn: sqlite3.Connection) -> None:
        """A card appearing in both AddPhase and DoubleStrike only listed once (seen set)."""
        ports = [_port("Saskia", "trigger", "DamageDone", valid_filter="Creature.YouCtrl")]
        _insert_port(conn, "Weird Card", "effect", "AddPhase")
        _insert_port(conn, "Weird Card", "keyword", "Double Strike")
        results = _find_combat_enhancers(conn, ports, {"Saskia"})
        assert len(results) == 1

    def test_both_addphase_and_double_strike(self, conn: sqlite3.Connection) -> None:
        """Multiple distinct candidates from both queries (lines 42-77)."""
        ports = [_port("Saskia", "trigger", "DamageDone", valid_filter="Creature.YouCtrl")]
        _insert_port(conn, "Aurelia", "effect", "AddPhase")
        _insert_port(conn, "Boros Charm", "keyword", "Double Strike")
        results = _find_combat_enhancers(conn, ports, {"Saskia"})
        names = {r.candidate for r in results}
        assert names == {"Aurelia", "Boros Charm"}


# ===========================================================================
# _find_evasion_complements
# ===========================================================================


class TestFindEvasionComplements:
    """Cover lines 98-139: combat-damage trigger -> unblockable creatures."""

    def test_no_combat_damage_returns_empty(self, conn: sqlite3.Connection) -> None:
        """No DamageDone with CombatDamage in raw_line -> empty (line 110)."""
        ports = [_port("Cmdr", "trigger", "DamageDone", raw_line="something else")]
        assert _find_evasion_complements(conn, ports, {"Cmdr"}) == []

    def test_non_trigger_skipped(self, conn: sqlite3.Connection) -> None:
        """port_type != 'trigger' skipped (line 100)."""
        ports = [_port("Cmdr", "effect", "DamageDone", raw_line="'CombatDamage': 'True'")]
        assert _find_evasion_complements(conn, ports, {"Cmdr"}) == []

    def test_non_damage_event_skipped(self, conn: sqlite3.Connection) -> None:
        """event_class != 'DamageDone' skipped (line 102)."""
        ports = [_port("Cmdr", "trigger", "Sacrificed", raw_line="'CombatDamage': 'True'")]
        assert _find_evasion_complements(conn, ports, {"Cmdr"}) == []

    def test_self_only_combat_damage_skipped(self, conn: sqlite3.Connection) -> None:
        """Card.Self valid_filter -> self-only combat trigger, skip (line 106)."""
        ports = [
            _port(
                "Brago",
                "trigger",
                "DamageDone",
                valid_filter="Card.Self",
                raw_line="'CombatDamage': 'True'",
            ),
        ]
        assert _find_evasion_complements(conn, ports, {"Brago"}) == []

    def test_tribal_commander_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander with creature subtypes -> tribal, skip evasion (lines 116-118)."""
        _insert_card(conn, "Yuriko", types="Creature", subtypes="Ninja Human")
        # Give Yuriko a port that mentions Ninja in valid_filter so subtypes are relevant
        _insert_port(conn, "Yuriko", "trigger", "DamageDone", valid_filter="Ninja.YouCtrl")
        ports = [
            _port(
                "Yuriko",
                "trigger",
                "DamageDone",
                valid_filter="Ninja.YouCtrl",
                raw_line="'CombatDamage': 'True'",
            ),
        ]
        result = _find_evasion_complements(conn, ports, {"Yuriko"})
        assert result == []

    def test_finds_unblockable_creatures(self, conn: sqlite3.Connection) -> None:
        """Non-tribal combat-damage commander -> unblockable creatures (lines 120-139)."""
        _insert_card(conn, "Saskia", types="Creature", subtypes="")
        ports = [
            _port(
                "Saskia",
                "trigger",
                "DamageDone",
                valid_filter="Creature.YouCtrl",
                raw_line="'CombatDamage': 'True'",
            ),
        ]
        _insert_port(
            conn,
            "Invisible Stalker",
            "static",
            "CantBlockBy",
            raw_line="Creature.Self can't be blocked",
        )
        result = _find_evasion_complements(conn, ports, {"Saskia"})
        assert len(result) == 1
        assert result[0].candidate == "Invisible Stalker"
        assert result[0].rule_id == "evasion"

    def test_excludes_commander_from_evasion(self, conn: sqlite3.Connection) -> None:
        """Commander excluded from candidate results (line 128)."""
        _insert_card(conn, "Saskia", types="Creature", subtypes="")
        ports = [
            _port(
                "Saskia",
                "trigger",
                "DamageDone",
                valid_filter="Creature.YouCtrl",
                raw_line="'CombatDamage': 'True'",
            ),
        ]
        _insert_port(
            conn,
            "Saskia",
            "static",
            "CantBlockBy",
            raw_line="Creature.Self can't be blocked",
        )
        result = _find_evasion_complements(conn, ports, {"Saskia"})
        assert result == []

    def test_non_self_unblockable_excluded(self, conn: sqlite3.Connection) -> None:
        """CantBlockBy without Creature.Self/Card.Self in raw_line -> excluded by SQL."""
        _insert_card(conn, "Saskia", types="Creature", subtypes="")
        ports = [
            _port(
                "Saskia",
                "trigger",
                "DamageDone",
                valid_filter="Creature.YouCtrl",
                raw_line="'CombatDamage': 'True'",
            ),
        ]
        _insert_port(
            conn,
            "Glaring Spotlight",
            "static",
            "CantBlockBy",
            raw_line="Creatures you control can't be blocked",
        )
        result = _find_evasion_complements(conn, ports, {"Saskia"})
        assert result == []


# ===========================================================================
# _find_sacrifice_outlets
# ===========================================================================


class TestFindSacrificeOutlets:
    """Cover lines 158-195: ChangesZone death trigger -> sacrifice cost candidates."""

    def test_no_death_trigger_returns_empty(self, conn: sqlite3.Connection) -> None:
        """No ChangesZone trigger with Graveyard destination -> empty (line 174)."""
        ports = [_port("Cmdr", "trigger", "Sacrificed", valid_filter="Creature.YouCtrl")]
        assert _find_sacrifice_outlets(conn, ports, {"Cmdr"}) == []

    def test_non_trigger_skipped(self, conn: sqlite3.Connection) -> None:
        """port_type != 'trigger' skipped (line 160)."""
        ports = [_port("Cmdr", "effect", "ChangesZone", zone_destination="Graveyard")]
        assert _find_sacrifice_outlets(conn, ports, {"Cmdr"}) == []

    def test_non_changeszone_skipped(self, conn: sqlite3.Connection) -> None:
        """event_class != 'ChangesZone' skipped (line 163)."""
        ports = [_port("Cmdr", "trigger", "DamageDone", zone_destination="Graveyard")]
        assert _find_sacrifice_outlets(conn, ports, {"Cmdr"}) == []

    def test_self_only_death_trigger_skipped(self, conn: sqlite3.Connection) -> None:
        """Card.Self valid_filter -> self-death, skip (line 166)."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Card.Self",
                zone_destination="Graveyard",
            ),
        ]
        assert _find_sacrifice_outlets(conn, ports, {"Cmdr"}) == []

    def test_non_graveyard_destination_skipped(self, conn: sqlite3.Connection) -> None:
        """zone_destination != 'Graveyard' -> not a death trigger (line 170)."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.YouCtrl",
                zone_destination="Exile",
            ),
        ]
        assert _find_sacrifice_outlets(conn, ports, {"Cmdr"}) == []

    def test_finds_sacrifice_cost_candidates(self, conn: sqlite3.Connection) -> None:
        """Death trigger -> sacrifice cost cards found (lines 178-195)."""
        ports = [
            _port(
                "Meren",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.YouCtrl",
                zone_destination="Graveyard",
            ),
        ]
        _insert_port(conn, "Viscera Seer", "cost", "sacrifice")
        _insert_port(conn, "Ashnod's Altar", "cost", "sacrifice")
        results = _find_sacrifice_outlets(conn, ports, {"Meren"})
        names = {r.candidate for r in results}
        assert names == {"Viscera Seer", "Ashnod's Altar"}
        assert all(r.rule_id == "cost_feeds_trigger" for r in results)

    def test_excludes_commander_from_sac_outlets(self, conn: sqlite3.Connection) -> None:
        """Commander excluded from sacrifice outlet results (line 184)."""
        ports = [
            _port(
                "Meren",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.YouCtrl",
                zone_destination="Graveyard",
            ),
        ]
        _insert_port(conn, "Meren", "cost", "sacrifice")
        _insert_port(conn, "Viscera Seer", "cost", "sacrifice")
        results = _find_sacrifice_outlets(conn, ports, {"Meren"})
        assert len(results) == 1
        assert results[0].candidate == "Viscera Seer"


# ===========================================================================
# _find_changeszone_resonance
# ===========================================================================


class TestFindChangeszoneResonance:
    """Cover lines 210-266: zone-filtered ChangesZone resonance."""

    def test_no_changeszone_trigger_returns_empty(self, conn: sqlite3.Connection) -> None:
        """No ChangesZone trigger -> empty (line 234)."""
        ports = [_port("Cmdr", "trigger", "DamageDone", valid_filter="Creature.YouCtrl")]
        assert _find_changeszone_resonance(conn, ports, {"Cmdr"}) == []

    def test_non_trigger_port_skipped_in_scan(self, conn: sqlite3.Connection) -> None:
        """Non-trigger ports are skipped when scanning for cmdr_types (line 222)."""
        ports = [
            _port("Cmdr", "effect", "ChangesZone", valid_filter="Land.YouCtrl"),
            _port("Cmdr", "trigger", "ChangesZone", valid_filter="Land.YouCtrl"),
        ]
        _insert_port(conn, "Cand", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")
        results = _find_changeszone_resonance(conn, ports, {"Cmdr"})
        assert len(results) == 1

    def test_self_only_changeszone_skipped(self, conn: sqlite3.Connection) -> None:
        """Card.Self valid_filter -> self-only, skip (line 227)."""
        ports = [_port("Cmdr", "trigger", "ChangesZone", valid_filter="Card.Self")]
        assert _find_changeszone_resonance(conn, ports, {"Cmdr"}) == []

    def test_empty_valid_filter_skipped(self, conn: sqlite3.Connection) -> None:
        """Empty valid_filter -> skip (line 227)."""
        ports = [_port("Cmdr", "trigger", "ChangesZone", valid_filter="")]
        assert _find_changeszone_resonance(conn, ports, {"Cmdr"}) == []

    def test_non_primary_type_skipped(self, conn: sqlite3.Connection) -> None:
        """valid_filter with non-primary type base -> no cmdr_types (line 231)."""
        ports = [_port("Cmdr", "trigger", "ChangesZone", valid_filter="Token.YouCtrl")]
        assert _find_changeszone_resonance(conn, ports, {"Cmdr"}) == []

    def test_finds_land_resonance(self, conn: sqlite3.Connection) -> None:
        """Tatyova-like landfall -> finds other landfall triggers (lines 237-266)."""
        ports = [_port("Tatyova", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")]
        _insert_port(
            conn,
            "Lotus Cobra",
            "trigger",
            "ChangesZone",
            valid_filter="Land.YouCtrl",
            branch_kind="root",
        )
        results = _find_changeszone_resonance(conn, ports, {"Tatyova"})
        assert len(results) == 1
        assert results[0].candidate == "Lotus Cobra"
        assert results[0].rule_id == "zone_resonance"
        assert results[0].cand_event == "Land_Battlefield"
        assert results[0].filter_group == "Land"

    def test_excludes_commander_from_resonance(self, conn: sqlite3.Connection) -> None:
        """Commander excluded from candidate results (line 247)."""
        ports = [_port("Tatyova", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")]
        _insert_port(conn, "Tatyova", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")
        results = _find_changeszone_resonance(conn, ports, {"Tatyova"})
        assert results == []

    def test_self_only_candidate_skipped(self, conn: sqlite3.Connection) -> None:
        """Candidate with Card.Self valid_filter -> excluded (line 250)."""
        ports = [_port("Tatyova", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")]
        _insert_port(conn, "SelfCard", "trigger", "ChangesZone", valid_filter="Card.Self")
        results = _find_changeszone_resonance(conn, ports, {"Tatyova"})
        assert results == []

    def test_dedup_candidates(self, conn: sqlite3.Connection) -> None:
        """Same candidate with two matching valid_filters only counted once (seen set, line 255)."""
        ports = [_port("Tatyova", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")]
        _insert_port(conn, "Lotus Cobra", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")
        _insert_port(conn, "Lotus Cobra", "trigger", "ChangesZone", valid_filter="Land.OppCtrl")
        results = _find_changeszone_resonance(conn, ports, {"Tatyova"})
        assert len(results) == 1

    def test_creature_resonance(self, conn: sqlite3.Connection) -> None:
        """Creature-type ChangesZone resonance (lines 254-266)."""
        ports = [_port("Omnath", "trigger", "ChangesZone", valid_filter="Creature.YouCtrl")]
        _insert_port(
            conn,
            "Panharmonicon",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.YouCtrl",
            branch_kind="sub",
        )
        results = _find_changeszone_resonance(conn, ports, {"Omnath"})
        assert len(results) == 1
        assert results[0].cand_event == "Creature_Battlefield"
        assert results[0].filter_group == "Creature"
        assert results[0].branch_kind == "sub"

    def test_multi_type_valid_filter(self, conn: sqlite3.Connection) -> None:
        """Commander with composite valid_filter like 'Land+Creature.YouCtrl'."""
        ports = [_port("Cmdr", "trigger", "ChangesZone", valid_filter="Land+Creature.YouCtrl")]
        _insert_port(conn, "Cand1", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")
        results = _find_changeszone_resonance(conn, ports, {"Cmdr"})
        assert len(results) == 1
        assert results[0].cand_event == "Land_Battlefield"

    def test_no_matching_type_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Candidate's type doesn't match commander's -> empty."""
        ports = [_port("Tatyova", "trigger", "ChangesZone", valid_filter="Land.YouCtrl")]
        _insert_port(conn, "Cand", "trigger", "ChangesZone", valid_filter="Creature.YouCtrl")
        results = _find_changeszone_resonance(conn, ports, {"Tatyova"})
        assert results == []
