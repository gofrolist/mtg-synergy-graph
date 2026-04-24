"""Integration tests for complement_rules/statics.py."""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.statics import (
    EDICT_GATE_REJECTING_FRAGMENTS,
    _edict_benefits_from_trigger,
    _find_cost_reduction_synergy,
    _find_edict_feeders,
    _find_graveyard_play_synergy,
)


def _make_db() -> sqlite3.Connection:
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
    c = _make_db()
    yield c
    c.close()


def _port(
    card_name: str,
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
    zone_destination: str = "",
    raw_line: str = "",
) -> dict:
    return {
        "card_name": card_name,
        "port_type": port_type,
        "event_class": event_class,
        "valid_filter": valid_filter,
        "zone_origin": "",
        "zone_destination": zone_destination,
        "raw_line": raw_line,
        "affected_scope": "",
        "branch_kind": "root",
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
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line, zone_destination) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter, raw_line, zone_destination),
    )


# ---------------------------------------------------------------------------
# _find_cost_reduction_synergy
# ---------------------------------------------------------------------------


class TestCostReductionSynergy:
    def test_finds_spell_cost_reducer(self, conn: sqlite3.Connection) -> None:
        """SpellCast Instant trigger -> finds Instant cost reducer."""
        cmdr_ports = [_port("Talrand", "trigger", "SpellCast", valid_filter="Instant.YouCtrl")]
        _insert_port(
            conn,
            "Goblin Electromancer",
            "static",
            "ReduceCost",
            raw_line="{'Mode': 'ReduceCost', 'ValidCard': 'Instant,Sorcery', 'Type': 'Spell', 'Amount': '1'}",
        )
        results = _find_cost_reduction_synergy(conn, cmdr_ports, {"Talrand"})
        assert len(results) == 1
        assert results[0].candidate == "Goblin Electromancer"
        assert results[0].rule_id == "cost_reducer"
        assert "Instant" in results[0].cand_event

    def test_skips_self_cost_reducer(self, conn: sqlite3.Connection) -> None:
        """ReduceCost targeting Card.Self should be skipped."""
        cmdr_ports = [_port("Talrand", "trigger", "SpellCast", valid_filter="Instant.YouCtrl")]
        _insert_port(
            conn,
            "SelfReducer",
            "static",
            "ReduceCost",
            raw_line="{'Mode': 'ReduceCost', 'ValidCard': 'Card.Self', 'Type': 'Spell', 'Amount': '1'}",
        )
        results = _find_cost_reduction_synergy(conn, cmdr_ports, {"Talrand"})
        assert results == []

    def test_no_spellcast_trigger_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander without SpellCast trigger gets no matches."""
        cmdr_ports = [_port("Meren", "trigger", "ChangesZone", valid_filter="Creature.YouCtrl")]
        _insert_port(
            conn,
            "Electromancer",
            "static",
            "ReduceCost",
            raw_line="{'Mode': 'ReduceCost', 'ValidCard': 'Instant,Sorcery', 'Type': 'Spell'}",
        )
        results = _find_cost_reduction_synergy(conn, cmdr_ports, {"Meren"})
        assert results == []

    def test_excludes_commander(self, conn: sqlite3.Connection) -> None:
        """Commander card itself is excluded from results."""
        cmdr_ports = [_port("Animar", "trigger", "SpellCast", valid_filter="Creature.YouCtrl")]
        _insert_port(
            conn,
            "Animar",
            "static",
            "ReduceCost",
            raw_line="{'Mode': 'ReduceCost', 'ValidCard': 'Creature', 'Type': 'Spell'}",
        )
        results = _find_cost_reduction_synergy(conn, cmdr_ports, {"Animar"})
        assert results == []

    def test_generic_card_reducer_matches(self, conn: sqlite3.Connection) -> None:
        """ReduceCost with ValidCard='Card' matches any spell commander."""
        cmdr_ports = [_port("Talrand", "trigger", "SpellCast", valid_filter="Instant.YouCtrl")]
        _insert_port(
            conn,
            "Medallion",
            "static",
            "ReduceCost",
            raw_line="{'Mode': 'ReduceCost', 'ValidCard': 'Card', 'Type': 'Spell', 'Amount': '1'}",
        )
        results = _find_cost_reduction_synergy(conn, cmdr_ports, {"Talrand"})
        assert len(results) == 1

    def test_deterministic_type_for_generic_reducer(self, conn: sqlite3.Connection) -> None:
        """Generic 'Card' reducers should get a deterministic matched_type."""
        cmdr_ports = [
            _port("Cmdr", "trigger", "SpellCast", valid_filter="Instant.YouCtrl"),
            _port("Cmdr", "trigger", "SpellCast", valid_filter="Sorcery.YouCtrl"),
        ]
        _insert_port(
            conn,
            "Medallion",
            "static",
            "ReduceCost",
            raw_line="{'Mode': 'ReduceCost', 'ValidCard': 'Card', 'Type': 'Spell'}",
        )
        # Run multiple times — should always get the same result (sorted order)
        results1 = _find_cost_reduction_synergy(conn, cmdr_ports, {"Cmdr"})
        results2 = _find_cost_reduction_synergy(conn, cmdr_ports, {"Cmdr"})
        assert results1[0].filter_group == results2[0].filter_group


# ---------------------------------------------------------------------------
# _find_graveyard_play_synergy
# ---------------------------------------------------------------------------


class TestGraveyardPlaySynergy:
    def test_landfall_finds_crucible(self, conn: sqlite3.Connection) -> None:
        """Landfall commander -> finds Crucible of Worlds."""
        cmdr_ports = [
            _port(
                "Omnath",
                "trigger",
                "ChangesZone",
                valid_filter="Land.YouCtrl",
                zone_destination="Battlefield",
            )
        ]
        _insert_port(
            conn,
            "Crucible of Worlds",
            "static",
            "Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Land.YouCtrl', 'MayPlay': 'True', 'AffectedZone': 'Graveyard'}",
        )
        results = _find_graveyard_play_synergy(conn, cmdr_ports, {"Omnath"})
        assert len(results) == 1
        assert results[0].candidate == "Crucible of Worlds"
        assert results[0].rule_id == "graveyard_play"

    def test_empty_zone_destination_defaults_to_battlefield(self, conn: sqlite3.Connection) -> None:
        """Commander with empty zone_destination is treated as Battlefield."""
        cmdr_ports = [
            _port(
                "Omnath",
                "trigger",
                "ChangesZone",
                valid_filter="Land.YouCtrl",
                zone_destination="",
            )
        ]
        _insert_port(
            conn,
            "Crucible",
            "static",
            "Continuous",
            raw_line="{'MayPlay': 'True', 'AffectedZone': 'Graveyard', 'Affected': 'Land'}",
        )
        results = _find_graveyard_play_synergy(conn, cmdr_ports, {"Omnath"})
        assert len(results) == 1

    def test_non_landfall_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander without landfall trigger gets no matches."""
        cmdr_ports = [
            _port("Meren", "trigger", "ChangesZone", valid_filter="Creature.YouCtrl", zone_destination="Graveyard")
        ]
        _insert_port(
            conn,
            "Crucible",
            "static",
            "Continuous",
            raw_line="{'MayPlay': 'True', 'AffectedZone': 'Graveyard', 'Affected': 'Land'}",
        )
        results = _find_graveyard_play_synergy(conn, cmdr_ports, {"Meren"})
        assert results == []

    def test_excludes_commander(self, conn: sqlite3.Connection) -> None:
        cmdr_ports = [
            _port("Cmdr", "trigger", "ChangesZone", valid_filter="Land.YouCtrl", zone_destination="Battlefield")
        ]
        _insert_port(
            conn,
            "Cmdr",
            "static",
            "Continuous",
            raw_line="{'MayPlay': 'True', 'AffectedZone': 'Graveyard', 'Affected': 'Land'}",
        )
        results = _find_graveyard_play_synergy(conn, cmdr_ports, {"Cmdr"})
        assert results == []


# ---------------------------------------------------------------------------
# _find_edict_feeders
# ---------------------------------------------------------------------------


class TestEdictFeeders:
    def test_death_trigger_finds_plaguecrafter(self, conn: sqlite3.Connection) -> None:
        """Meren-like death trigger -> finds Plaguecrafter."""
        cmdr_ports = [
            _port(
                "Meren",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.Other+YouCtrl",
                zone_destination="Graveyard",
            )
        ]
        _insert_port(
            conn,
            "Plaguecrafter",
            "effect",
            "Sacrifice",
            valid_filter="Player",
        )
        results = _find_edict_feeders(conn, cmdr_ports, {"Meren"})
        assert len(results) == 1
        assert results[0].candidate == "Plaguecrafter"
        assert results[0].rule_id == "edict_feeder"
        assert results[0].cmdr_event == "ChangesZone_death"

    def test_sacrificed_trigger_uses_sacrificed_event(self, conn: sqlite3.Connection) -> None:
        """Korvold-like Sacrificed trigger -> uses 'Sacrificed' cmdr_event."""
        cmdr_ports = [_port("Korvold", "trigger", "Sacrificed")]
        _insert_port(
            conn,
            "Plaguecrafter",
            "effect",
            "Sacrifice",
            valid_filter="Player",
        )
        results = _find_edict_feeders(conn, cmdr_ports, {"Korvold"})
        assert len(results) == 1
        assert results[0].cmdr_event == "Sacrificed"

    def test_sacrifice_all_also_matched(self, conn: sqlite3.Connection) -> None:
        """SacrificeAll effects are also matched."""
        cmdr_ports = [
            _port("Meren", "trigger", "ChangesZone", valid_filter="Creature.YouCtrl", zone_destination="Graveyard")
        ]
        _insert_port(
            conn,
            "Grave Pact",
            "effect",
            "SacrificeAll",
            valid_filter="Opponent",
        )
        results = _find_edict_feeders(conn, cmdr_ports, {"Meren"})
        assert len(results) == 1

    def test_self_only_trigger_skipped(self, conn: sqlite3.Connection) -> None:
        """Card.Self death trigger should not activate edict matching."""
        cmdr_ports = [
            _port("SelfDie", "trigger", "ChangesZone", valid_filter="Card.Self", zone_destination="Graveyard")
        ]
        _insert_port(conn, "Plaguecrafter", "effect", "Sacrifice", valid_filter="Player")
        results = _find_edict_feeders(conn, cmdr_ports, {"SelfDie"})
        assert results == []

    def test_no_death_trigger_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander without death/sacrifice trigger gets no matches."""
        cmdr_ports = [_port("Talrand", "trigger", "SpellCast", valid_filter="Instant.YouCtrl")]
        _insert_port(conn, "Plaguecrafter", "effect", "Sacrifice", valid_filter="Player")
        results = _find_edict_feeders(conn, cmdr_ports, {"Talrand"})
        assert results == []

    def test_excludes_commander(self, conn: sqlite3.Connection) -> None:
        cmdr_ports = [_port("Cmdr", "trigger", "Sacrificed")]
        _insert_port(conn, "Cmdr", "effect", "Sacrifice", valid_filter="Player")
        results = _find_edict_feeders(conn, cmdr_ports, {"Cmdr"})
        assert results == []


class TestEdictBenefitsFromTrigger:
    """Unit tests for the gate predicate shared by registry + helper.

    Each named case maps to a real commander whose audit displacement
    motivated adding the corresponding rejecting fragment in the
    2026-04-24 gate refactor.
    """

    @pytest.mark.parametrize(
        "vf,expected",
        [
            # Plain creature filters — pass.
            ("Creature.Other+YouCtrl", True),
            ("Creature.OppCtrl", True),
            ("Permanent.!token+OppCtrl", True),
            ("Creature.Other+YouCtrl,Vehicle.YouCtrl", True),
            # Counter-gated triggers — Toxrill (slime), Donatello (HasCounters).
            ("Creature.YouDontCtrl+counters_GE1_SLIME", False),
            ("Artifact.YouCtrl+HasCounters", False),
            ("Creature+counters_EQ0_P1P1", False),
            # Equipment / aura gating — Koll the Forgemaster.
            ("Creature.Other+!token+enchanted+YouCtrl", False),
            ("Creature.Other+!token+equipped", False),
            # Token-subtype filters — Astrid Peth (Food/Clue) etc.
            ("Food.YouCtrl,Clue.YouCtrl", False),
            ("Treasure.OppCtrl", False),
            # Non-creature artifact / enchantment subtypes — Jaws.
            ("Artifact.nonCreature", False),
            # But artifact creatures DO satisfy.
            ("Artifact.Creature+OppCtrl", True),
            # Enchantment without Creature -> reject; with Creature -> accept.
            ("Enchantment", False),
            ("Enchantment.Creature+OppCtrl", True),
        ],
    )
    def test_predicate_cases(self, vf: str, expected: bool) -> None:
        assert _edict_benefits_from_trigger(vf) is expected

    def test_substring_creature_in_noncreature_does_not_count(self) -> None:
        """Regression: 'Creature' inside 'nonCreature' must not satisfy
        the creature-type requirement (the 2026-04-24 bugfix).
        """
        assert _edict_benefits_from_trigger("Artifact.nonCreature") is False

    def test_fragment_list_immutable(self) -> None:
        """Defensive: rejecting-fragment list is a tuple (immutable
        constant) and contains the named cases the audit identified.
        """
        assert isinstance(EDICT_GATE_REJECTING_FRAGMENTS, tuple)
        for fragment in (
            "counters_GE",
            "counters_EQ",
            "counters_LE",
            "+enchanted",
            "+equipped",
            "HasCounters",
            "Food",
            "Clue",
            "Treasure",
            "Blood",
        ):
            assert fragment in EDICT_GATE_REJECTING_FRAGMENTS
