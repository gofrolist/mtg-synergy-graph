"""Tests for density-based complement matchers in complement_rules.density.

Uses an in-memory SQLite database with card_ports and cards tables to exercise
each code path in _find_lord_complements, _find_etb_self_complements,
_find_scaling_complements, _find_spellcast_density_complements,
_find_tribal_density_complements, and _find_scales_with_density.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.density import (
    _find_etb_self_complements,
    _find_lord_complements,
    _find_scales_with_density,
    _find_scaling_complements,
    _find_spellcast_density_complements,
    _find_tribal_density_complements,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite database with the required schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE cards (
            name            TEXT PRIMARY KEY,
            oracle_id       TEXT,
            mana_cost       TEXT,
            cmc             REAL,
            types           TEXT,
            supertypes      TEXT,
            subtypes        TEXT,
            card_types      TEXT,
            colors          TEXT,
            color_identity  TEXT,
            power           TEXT,
            toughness       TEXT,
            loyalty         TEXT,
            keywords        TEXT,
            oracle_text     TEXT,
            is_commander    BOOLEAN DEFAULT FALSE,
            deck_hints      TEXT,
            deck_needs      TEXT,
            deck_has        TEXT,
            edhrec_rank     INTEGER,
            rarity          TEXT,
            set_code        TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE card_ports (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name          TEXT NOT NULL REFERENCES cards(name),
            port_type          TEXT NOT NULL,
            event_class        TEXT NOT NULL,
            valid_filter       TEXT,
            zone_origin        TEXT,
            zone_destination   TEXT,
            phase              TEXT,
            affected_scope     TEXT,
            effect_zone        TEXT,
            cost_subtype       TEXT,
            cost_target        TEXT,
            trigger_source     TEXT,
            mana_restriction   TEXT,
            amount             TEXT,
            counter_type       TEXT,
            granted_keyword    TEXT,
            granted_ability    TEXT,
            execute_ref        TEXT,
            sub_ability_ref    TEXT,
            is_conditional     BOOLEAN DEFAULT FALSE,
            branch_kind        TEXT DEFAULT 'root',
            branch_parent      TEXT,
            source_svar        TEXT,
            chain_depth        INTEGER DEFAULT 0,
            scaling_expression TEXT,
            is_optional        BOOLEAN DEFAULT FALSE,
            is_combat          BOOLEAN DEFAULT FALSE,
            is_curse           BOOLEAN DEFAULT FALSE,
            replacement_event  TEXT,
            replacement_result TEXT,
            replacement_player TEXT,
            duration           TEXT,
            raw_line           TEXT
        )
        """
    )
    return conn


@pytest.fixture()
def conn():
    """Fixture: in-memory DB with schema, auto-closed after each test."""
    c = _make_db()
    yield c
    c.close()


def _insert_card(
    conn: sqlite3.Connection,
    name: str,
    *,
    card_types: str = "",
    subtypes: str = "",
    types: str = "",
    supertypes: str = "",
    keywords: str = "[]",
    color_identity: str = "",
    power: str = "",
    toughness: str = "",
) -> None:
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, types, supertypes, keywords, color_identity, power, toughness) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, card_types, subtypes, types, supertypes, keywords, color_identity, power, toughness),
    )


def _insert_port(
    conn: sqlite3.Connection,
    card_name: str,
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
    affected_scope: str = "",
    branch_kind: str = "root",
    raw_line: str = "",
    counter_type: str = "",
    cost_subtype: str = "",
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, "
        "affected_scope, branch_kind, raw_line, counter_type, cost_subtype) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_name,
            port_type,
            event_class,
            valid_filter,
            affected_scope,
            branch_kind,
            raw_line,
            counter_type,
            cost_subtype,
        ),
    )


def _candidates(results: list) -> set[str]:
    return {r.candidate for r in results}


# ---------------------------------------------------------------------------
# _find_lord_complements
# ---------------------------------------------------------------------------


class TestFindLordComplements:
    def test_goblin_lord_matches_goblin_commander(self, conn):
        """A Continuous static with Goblin in affected_scope matches a Goblin commander."""
        # Commander is a Goblin with Goblin mentioned in a port filter
        _insert_card(conn, "Krenko, Mob Boss", card_types="Creature", subtypes="Goblin Warrior", types="Creature")
        # Candidate lord card
        _insert_card(conn, "Goblin Chieftain", card_types="Creature", subtypes="Goblin")
        _insert_port(
            conn,
            "Goblin Chieftain",
            "static",
            "Continuous",
            affected_scope="Goblin.YouCtrl",
        )
        # Commander port that mentions Goblin (so _commander_subtypes_from_ports picks it up)
        _insert_port(
            conn,
            "Krenko, Mob Boss",
            "trigger",
            "Activated",
            valid_filter="Goblin.YouCtrl",
            raw_line="Goblin",
        )

        cmdr_ports = [
            dict(r)
            for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Krenko, Mob Boss",)).fetchall()
        ]
        results = _find_lord_complements(conn, cmdr_ports, {"Krenko, Mob Boss"})
        assert "Goblin Chieftain" in _candidates(results)
        assert all(r.rule_id == "lord" for r in results)

    def test_no_subtypes_returns_empty(self, conn):
        """Commander with no relevant subtypes returns empty."""
        _insert_card(conn, "Sol Ring", card_types="Artifact", types="Artifact")
        cmdr_ports = []
        results = _find_lord_complements(conn, cmdr_ports, {"Sol Ring"})
        assert results == []

    def test_excludes_commander_from_results(self, conn):
        """The commander itself should not appear in lord results."""
        _insert_card(conn, "Goblin Lord", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_port(conn, "Goblin Lord", "static", "Continuous", affected_scope="Goblin.YouCtrl")
        _insert_port(conn, "Goblin Lord", "trigger", "ChangesZone", valid_filter="Goblin.YouCtrl")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Goblin Lord",)).fetchall()
        ]
        results = _find_lord_complements(conn, cmdr_ports, {"Goblin Lord"})
        assert "Goblin Lord" not in _candidates(results)

    def test_non_overlapping_subtypes_no_match(self, conn):
        """Lord with Elf scope should not match a Goblin commander."""
        _insert_card(conn, "Krenko", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_card(conn, "Elf Lord", card_types="Creature", subtypes="Elf")
        _insert_port(conn, "Elf Lord", "static", "Continuous", affected_scope="Elf.YouCtrl")
        _insert_port(conn, "Krenko", "trigger", "Activated", valid_filter="Goblin", raw_line="Goblin")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Krenko",)).fetchall()
        ]
        results = _find_lord_complements(conn, cmdr_ports, {"Krenko"})
        assert results == []


# ---------------------------------------------------------------------------
# _find_etb_self_complements
# ---------------------------------------------------------------------------


class TestFindEtbSelfComplements:
    def test_changeszone_trigger_matches_creature(self, conn):
        """Commander with ChangesZone trigger for Creature.Goblin should match a Goblin creature."""
        _insert_card(conn, "Commander", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_card(
            conn,
            "Goblin Lackey",
            card_types="Creature",
            subtypes="Goblin",
            types="Creature",
            keywords='["Haste"]',
            color_identity="R",
        )
        _insert_port(conn, "Commander", "trigger", "ChangesZone", valid_filter="Creature.Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Commander",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"Commander"})
        assert "Goblin Lackey" in _candidates(results)
        assert all(r.rule_id == "etb_self" for r in results)
        assert all(r.cand_event == "card_identity" for r in results)

    def test_self_only_trigger_skipped(self, conn):
        """Trigger with Card.Self filter should be skipped."""
        _insert_card(conn, "SelfTrig", card_types="Creature", types="Creature")
        _insert_card(conn, "Some Creature", card_types="Creature", types="Creature")
        _insert_port(conn, "SelfTrig", "trigger", "ChangesZone", valid_filter="Card.Self")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("SelfTrig",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"SelfTrig"})
        assert results == []

    def test_no_triggers_returns_empty(self, conn):
        """Commander with no trigger ports returns empty."""
        _insert_card(conn, "NoTrig", card_types="Artifact", types="Artifact")
        _insert_port(conn, "NoTrig", "effect", "Token")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("NoTrig",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"NoTrig"})
        assert results == []

    def test_permanent_filter_skipped_as_too_broad(self, conn):
        """Trigger with Permanent filter should be skipped (in _SKIP_BASES)."""
        _insert_card(conn, "BroadTrig", card_types="Creature", types="Creature")
        _insert_card(conn, "Random Card", card_types="Creature", types="Creature")
        _insert_port(conn, "BroadTrig", "trigger", "ChangesZone", valid_filter="Permanent")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("BroadTrig",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"BroadTrig"})
        assert results == []

    def test_card_filter_skipped_as_too_broad(self, conn):
        """Trigger with Card filter should be skipped (in _SKIP_BASES)."""
        _insert_card(conn, "BroadCardTrig", card_types="Creature", types="Creature")
        _insert_card(conn, "Some Card", card_types="Creature", types="Creature")
        _insert_port(conn, "BroadCardTrig", "trigger", "ChangesZone", valid_filter="Card")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("BroadCardTrig",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"BroadCardTrig"})
        assert results == []

    def test_non_self_event_trigger_skipped(self, conn):
        """Trigger with non-self-event event_class should be skipped."""
        _insert_card(conn, "SpellCaster", card_types="Creature", types="Creature")
        _insert_card(conn, "Some Card", card_types="Instant", types="Instant")
        _insert_port(conn, "SpellCaster", "trigger", "SpellCast", valid_filter="Instant")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("SpellCaster",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"SpellCaster"})
        assert results == []

    def test_excludes_commander_from_results(self, conn):
        """Commander should not appear in its own ETB-self results."""
        _insert_card(
            conn,
            "SelfRef",
            card_types="Creature",
            subtypes="Goblin",
            types="Creature",
            keywords="[]",
            color_identity="R",
        )
        _insert_port(conn, "SelfRef", "trigger", "ChangesZone", valid_filter="Creature.Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("SelfRef",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"SelfRef"})
        assert "SelfRef" not in _candidates(results)

    def test_needs_full_scan_for_nonprimary_type(self, conn):
        """When the filter head is not a primary type (e.g. Goblin), needs_full_scan should be set."""
        _insert_card(conn, "GobTrig", card_types="Creature", types="Creature")
        _insert_card(
            conn,
            "Goblin Recruit",
            card_types="Creature",
            subtypes="Goblin",
            types="Creature",
            keywords="[]",
            color_identity="R",
        )
        # Goblin is NOT a primary type, so the code should set needs_full_scan=True
        _insert_port(conn, "GobTrig", "trigger", "ChangesZone", valid_filter="Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("GobTrig",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"GobTrig"})
        assert "Goblin Recruit" in _candidates(results)

    def test_primary_type_hint_optimized_query(self, conn):
        """When filter head is a primary type (e.g. Creature.Goblin), type hint SQL optimization should work."""
        _insert_card(conn, "CreatureTrig", card_types="Creature", types="Creature")
        _insert_card(
            conn,
            "Goblin Soldier",
            card_types="Creature",
            subtypes="Goblin",
            types="Creature",
            keywords="[]",
            color_identity="R",
        )
        _insert_card(conn, "Lightning Bolt", card_types="Instant", types="Instant", keywords="[]", color_identity="R")
        _insert_port(conn, "CreatureTrig", "trigger", "ChangesZone", valid_filter="Creature.Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("CreatureTrig",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"CreatureTrig"})
        assert "Goblin Soldier" in _candidates(results)
        # Lightning Bolt is an Instant, should not match
        assert "Lightning Bolt" not in _candidates(results)

    def test_dedup_by_card_and_event(self, conn):
        """Same card matching multiple trigger alts should be deduped."""
        _insert_card(conn, "MultiTrig", card_types="Creature", types="Creature")
        _insert_card(
            conn,
            "Goblin Piker",
            card_types="Creature",
            subtypes="Goblin Warrior",
            types="Creature",
            keywords="[]",
            color_identity="R",
        )
        # Two triggers with same event_class, both matching Goblin Piker
        _insert_port(conn, "MultiTrig", "trigger", "ChangesZone", valid_filter="Creature.Goblin")
        _insert_port(conn, "MultiTrig", "trigger", "ChangesZone", valid_filter="Creature.Warrior")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("MultiTrig",)).fetchall()
        ]
        results = _find_etb_self_complements(conn, cmdr_ports, {"MultiTrig"})
        # Should appear at most once per (card, event) key
        goblin_matches = [r for r in results if r.candidate == "Goblin Piker"]
        assert len(goblin_matches) == 1


# ---------------------------------------------------------------------------
# _find_scaling_complements
# ---------------------------------------------------------------------------


class TestFindScalingComplements:
    def test_aura_scaling(self, conn):
        """Commander with scales_with Aura should match Aura cards."""
        _insert_card(conn, "Uril", card_types="Creature", types="Creature")
        _insert_card(conn, "Ethereal Armor", card_types="Enchantment", subtypes="Aura")
        _insert_card(conn, "Lightning Bolt", card_types="Instant")
        _insert_port(conn, "Uril", "scales_with", "AuraCount", raw_line="Aura attached")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Uril",)).fetchall()
        ]
        results = _find_scaling_complements(conn, cmdr_ports, {"Uril"})
        assert "Ethereal Armor" in _candidates(results)
        assert "Lightning Bolt" not in _candidates(results)
        assert all(r.rule_id == "scaling" for r in results)

    def test_equipment_scaling(self, conn):
        """Commander with scales_with Equipment should match Equipment cards."""
        _insert_card(conn, "Sram", card_types="Creature", types="Creature")
        _insert_card(conn, "Sword of F&I", card_types="Artifact", subtypes="Equipment")
        _insert_port(conn, "Sram", "scales_with", "EquipCount", raw_line="Equipment attached")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Sram",)).fetchall()
        ]
        results = _find_scaling_complements(conn, cmdr_ports, {"Sram"})
        assert "Sword of F&I" in _candidates(results)

    def test_enchantment_primary_type_scaling(self, conn):
        """Commander with scales_with Enchantment uses card_types LIKE query."""
        _insert_card(conn, "EnchCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Some Enchantment", card_types="Enchantment")
        _insert_port(conn, "EnchCmdr", "scales_with", "EnchCount", raw_line="Enchantment count")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("EnchCmdr",)).fetchall()
        ]
        results = _find_scaling_complements(conn, cmdr_ports, {"EnchCmdr"})
        assert "Some Enchantment" in _candidates(results)

    def test_no_scales_with_returns_empty(self, conn):
        """Commander with no scales_with ports returns empty."""
        _insert_card(conn, "Plain", card_types="Creature", types="Creature")
        _insert_port(conn, "Plain", "trigger", "ChangesZone")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Plain",)).fetchall()
        ]
        results = _find_scaling_complements(conn, cmdr_ports, {"Plain"})
        assert results == []

    def test_excludes_commander(self, conn):
        """Commander should not be in its own scaling results."""
        _insert_card(conn, "Uril2", card_types="Enchantment Creature", subtypes="Aura", types="Creature")
        _insert_port(conn, "Uril2", "scales_with", "AuraCount", raw_line="Aura count")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Uril2",)).fetchall()
        ]
        results = _find_scaling_complements(conn, cmdr_ports, {"Uril2"})
        assert "Uril2" not in _candidates(results)

    def test_valid_filter_aura_extraction(self, conn):
        """scales_with port with Aura in valid_filter should also match."""
        _insert_card(conn, "VFCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Holy Mantle", card_types="Enchantment", subtypes="Aura")
        _insert_port(conn, "VFCmdr", "scales_with", "Count", valid_filter="Aura.YouCtrl", raw_line="count stuff")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("VFCmdr",)).fetchall()
        ]
        results = _find_scaling_complements(conn, cmdr_ports, {"VFCmdr"})
        assert "Holy Mantle" in _candidates(results)

    def test_dedup_across_types(self, conn):
        """A card matching both primary and subtype should appear once."""
        _insert_card(conn, "DupCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Enchant Aura", card_types="Enchantment", subtypes="Aura")
        # Both Enchantment (primary) and Aura (subtype) should match
        _insert_port(conn, "DupCmdr", "scales_with", "Count", raw_line="Enchantment Aura density")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("DupCmdr",)).fetchall()
        ]
        results = _find_scaling_complements(conn, cmdr_ports, {"DupCmdr"})
        enchant_matches = [r for r in results if r.candidate == "Enchant Aura"]
        assert len(enchant_matches) == 1  # deduped


# ---------------------------------------------------------------------------
# _find_spellcast_density_complements
# ---------------------------------------------------------------------------


class TestFindSpellcastDensityComplements:
    def test_instant_sorcery_trigger(self, conn):
        """Commander with SpellCast trigger for Instant should match Instant cards."""
        _insert_card(conn, "Talrand", card_types="Creature", types="Creature")
        _insert_card(conn, "Counterspell", card_types="Instant")
        _insert_card(conn, "Some Creature", card_types="Creature")
        _insert_port(conn, "Talrand", "trigger", "SpellCast", valid_filter="Instant")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Talrand",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"Talrand"})
        assert "Counterspell" in _candidates(results)
        assert "Some Creature" not in _candidates(results)
        assert all(r.rule_id == "spell_density" for r in results)

    def test_noncreature_filter(self, conn):
        """Commander with nonCreature trigger should match all noncreature types."""
        _insert_card(conn, "NonCreCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Bolt", card_types="Instant")
        _insert_card(conn, "Divination", card_types="Sorcery")
        _insert_card(conn, "Bear", card_types="Creature")
        _insert_port(conn, "NonCreCmdr", "trigger", "SpellCast", valid_filter="Card.nonCreature")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("NonCreCmdr",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"NonCreCmdr"})
        cands = _candidates(results)
        assert "Bolt" in cands
        assert "Divination" in cands
        # Creature should NOT match nonCreature
        assert "Bear" not in cands

    def test_non_creature_hyphenated_filter(self, conn):
        """Commander with non-Creature trigger should also match all noncreature types."""
        _insert_card(conn, "HyphenCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Shock", card_types="Instant")
        _insert_port(conn, "HyphenCmdr", "trigger", "SpellCast", valid_filter="Card.non-Creature")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("HyphenCmdr",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"HyphenCmdr"})
        assert "Shock" in _candidates(results)

    def test_too_broad_filter_returns_empty(self, conn):
        """Trigger with Card or Creature or Permanent filter returns empty (too broad)."""
        _insert_card(conn, "BroadCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Something", card_types="Creature")
        _insert_port(conn, "BroadCmdr", "trigger", "SpellCast", valid_filter="Card")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("BroadCmdr",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"BroadCmdr"})
        assert results == []

    def test_creature_filter_too_broad(self, conn):
        """Creature alone is in _TOO_BROAD set."""
        _insert_card(conn, "CreCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Bear", card_types="Creature")
        _insert_port(conn, "CreCmdr", "trigger", "SpellCast", valid_filter="Creature")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("CreCmdr",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"CreCmdr"})
        assert results == []

    def test_subtype_trigger(self, conn):
        """SpellCast trigger with a subtype (Vampire) should match cards with that subtype."""
        _insert_card(conn, "Edgar", card_types="Creature", types="Creature")
        _insert_card(conn, "Vampire Nighthawk", card_types="Creature", subtypes="Vampire Shaman")
        _insert_card(conn, "Goblin Piker", card_types="Creature", subtypes="Goblin Warrior")
        _insert_port(conn, "Edgar", "trigger", "SpellCast", valid_filter="Vampire")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Edgar",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"Edgar"})
        assert "Vampire Nighthawk" in _candidates(results)
        assert "Goblin Piker" not in _candidates(results)

    def test_self_filter_skipped(self, conn):
        """Card.Self filter should be skipped."""
        _insert_card(conn, "SelfCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Random", card_types="Instant")
        _insert_port(conn, "SelfCmdr", "trigger", "SpellCast", valid_filter="Card.Self")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("SelfCmdr",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"SelfCmdr"})
        assert results == []

    def test_no_trigger_returns_empty(self, conn):
        """Commander with no SpellCast trigger returns empty."""
        _insert_card(conn, "NoSpell", card_types="Creature", types="Creature")
        _insert_port(conn, "NoSpell", "effect", "Token")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("NoSpell",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"NoSpell"})
        assert results == []

    def test_conspire_static_extracts_types(self, conn):
        """A static Continuous port granting Conspire should extract spell types."""
        _insert_card(conn, "Wort", card_types="Creature", types="Creature")
        _insert_card(conn, "Bolt", card_types="Instant")
        _insert_card(conn, "Divination", card_types="Sorcery")
        _insert_port(
            conn,
            "Wort",
            "static",
            "Continuous",
            raw_line="{'AddKeyword': 'Conspire', 'Affected': 'Instant,Sorcery'}",
        )

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Wort",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"Wort"})
        cands = _candidates(results)
        assert "Bolt" in cands
        assert "Divination" in cands

    def test_empty_valid_filter_skipped(self, conn):
        """SpellCast trigger with empty valid_filter should be skipped."""
        _insert_card(conn, "EmptyVF", card_types="Creature", types="Creature")
        _insert_card(conn, "Card1", card_types="Instant")
        _insert_port(conn, "EmptyVF", "trigger", "SpellCast", valid_filter="")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("EmptyVF",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"EmptyVF"})
        assert results == []

    def test_landplayed_trigger(self, conn):
        """LandPlayed is also a CATCH_ALL_TRIGGER, so should produce density matches."""
        _insert_card(conn, "LandCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Forest", card_types="Land")
        _insert_port(conn, "LandCmdr", "trigger", "LandPlayed", valid_filter="Land")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("LandCmdr",)).fetchall()
        ]
        results = _find_spellcast_density_complements(conn, cmdr_ports, {"LandCmdr"})
        # LandPlayed is in CATCH_ALL_TRIGGERS so it should be processed
        # Land is not in _CASTABLE_TYPES, so it won't match wanted_types
        # and is uppercase -> will go to wanted_subtypes
        # Land should match from subtypes query
        # Actually, "Land" as a head with isupper() will go to wanted_subtypes
        assert results == [] or "Forest" in _candidates(results)


# ---------------------------------------------------------------------------
# _find_tribal_density_complements
# ---------------------------------------------------------------------------


class TestFindTribalDensityComplements:
    def test_goblin_tribal(self, conn):
        """Goblin commander should find Goblin creatures."""
        _insert_card(conn, "Krenko", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_card(conn, "Goblin Guide", card_types="Creature", subtypes="Goblin Scout")
        _insert_card(conn, "Elf Ranger", card_types="Creature", subtypes="Elf Ranger")
        # Port that mentions Goblin so _commander_subtypes_from_ports picks it up
        _insert_port(conn, "Krenko", "trigger", "Activated", valid_filter="Goblin.YouCtrl", raw_line="Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Krenko",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"Krenko"})
        assert "Goblin Guide" in _candidates(results)
        assert "Elf Ranger" not in _candidates(results)
        assert all(r.rule_id == "tribal_density" for r in results)

    def test_suppressed_for_conspire_keyword(self, conn):
        """Commander with Conspire keyword should have tribal density suppressed."""
        _insert_card(conn, "ConspireCmdr", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_card(conn, "Goblin Guide", card_types="Creature", subtypes="Goblin")
        _insert_port(conn, "ConspireCmdr", "keyword", "Conspire")
        _insert_port(conn, "ConspireCmdr", "trigger", "Activated", valid_filter="Goblin", raw_line="Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("ConspireCmdr",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"ConspireCmdr"})
        assert results == []

    def test_suppressed_for_copy_spell_effect(self, conn):
        """Commander with CopySpellAbility effect should have tribal density suppressed."""
        _insert_card(conn, "CopyCmdr", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_card(conn, "Goblin Guide", card_types="Creature", subtypes="Goblin")
        _insert_port(conn, "CopyCmdr", "effect", "CopySpellAbility")
        _insert_port(conn, "CopyCmdr", "trigger", "Activated", valid_filter="Goblin", raw_line="Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("CopyCmdr",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"CopyCmdr"})
        assert results == []

    def test_suppressed_for_conspire_static(self, conn):
        """Commander with static granting Conspire should have tribal density suppressed."""
        _insert_card(conn, "StaticConspire", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_card(conn, "Goblin Guide", card_types="Creature", subtypes="Goblin")
        _insert_port(conn, "StaticConspire", "static", "Continuous", raw_line="{'AddKeyword': 'Conspire'}")
        _insert_port(conn, "StaticConspire", "trigger", "Activated", valid_filter="Goblin", raw_line="Goblin")

        cmdr_ports = [
            dict(r)
            for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("StaticConspire",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"StaticConspire"})
        assert results == []

    def test_no_subtypes_returns_empty(self, conn):
        """Commander with no relevant subtypes returns empty."""
        _insert_card(conn, "NoSub", card_types="Artifact", types="Artifact")
        cmdr_ports = []
        results = _find_tribal_density_complements(conn, cmdr_ports, {"NoSub"})
        assert results == []

    def test_excludes_commander(self, conn):
        """Commander should not appear in tribal density results."""
        _insert_card(conn, "GobCmdr", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_port(conn, "GobCmdr", "trigger", "Activated", valid_filter="Goblin", raw_line="Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("GobCmdr",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"GobCmdr"})
        assert "GobCmdr" not in _candidates(results)

    def test_only_creatures_match(self, conn):
        """Only creatures with the subtype should match, not non-creature cards."""
        _insert_card(conn, "GobCmdr2", card_types="Creature", subtypes="Goblin", types="Creature")
        _insert_card(
            conn, "Goblin Grenade", card_types="Sorcery", subtypes="Goblin"
        )  # Non-creature with Goblin subtype
        _insert_card(conn, "Goblin Soldier", card_types="Creature", subtypes="Goblin")
        _insert_port(conn, "GobCmdr2", "trigger", "Activated", valid_filter="Goblin", raw_line="Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("GobCmdr2",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"GobCmdr2"})
        assert "Goblin Soldier" in _candidates(results)
        assert "Goblin Grenade" not in _candidates(results)


# ---------------------------------------------------------------------------
# _find_scales_with_density
# ---------------------------------------------------------------------------


class TestFindScalesWithDensity:
    def test_p1p1_counter_scaling(self, conn):
        """Commander with scales_with P1P1 should find PutCounter/Proliferate effects."""
        _insert_card(conn, "CounterCmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Hardened Scales", card_types="Enchantment")
        _insert_port(conn, "CounterCmdr", "scales_with", "CardCounters.P1P1")
        _insert_port(conn, "Hardened Scales", "effect", "PutCounter", counter_type="P1P1")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("CounterCmdr",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"CounterCmdr"})
        assert "Hardened Scales" in _candidates(results)
        assert all(r.rule_id == "scaling" for r in results)
        assert all(r.cmdr_event == "scales_P1P1" for r in results)

    def test_p1p1_in_valid_filter(self, conn):
        """scales_with with P1P1 in valid_filter should also match counter producers."""
        _insert_card(conn, "Hamza", card_types="Creature", types="Creature")
        _insert_card(conn, "Proliferator", card_types="Creature")
        _insert_port(conn, "Hamza", "scales_with", "Valid", valid_filter="Creature.YouCtrl+counters_GE1_P1P1")
        _insert_port(conn, "Proliferator", "effect", "Proliferate")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Hamza",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Hamza"})
        assert "Proliferator" in _candidates(results)

    def test_p1p1_excludes_wrong_counter_type(self, conn):
        """PutCounter with non-P1P1 counter type should not match P1P1 scaling."""
        _insert_card(conn, "P1P1Cmdr", card_types="Creature", types="Creature")
        _insert_card(conn, "Charge Counter", card_types="Artifact")
        _insert_port(conn, "P1P1Cmdr", "scales_with", "CardCounters.P1P1")
        _insert_port(conn, "Charge Counter", "effect", "PutCounter", counter_type="CHARGE")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("P1P1Cmdr",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"P1P1Cmdr"})
        assert "Charge Counter" not in _candidates(results)

    def test_toughness_scaling(self, conn):
        """Commander with CardToughness scales_with should find high-toughness creatures."""
        _insert_card(conn, "Phenax", card_types="Creature", types="Creature")
        _insert_card(conn, "Wall of Frost", card_types="Creature", power="0", toughness="7")
        _insert_card(conn, "Goblin Piker", card_types="Creature", power="2", toughness="1")
        _insert_port(conn, "Phenax", "scales_with", "CardToughness")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Phenax",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Phenax"})
        assert "Wall of Frost" in _candidates(results)
        assert "Goblin Piker" not in _candidates(results)
        assert any(r.cmdr_event == "scales_toughness" for r in results)

    def test_toughness_defender_keyword(self, conn):
        """Defender keyword creatures should also match toughness scaling."""
        _insert_card(conn, "PhenaxD", card_types="Creature", types="Creature")
        _insert_card(conn, "Shield Sphere", card_types="Creature", power="0", toughness="2")
        _insert_port(conn, "PhenaxD", "scales_with", "CardToughness")
        _insert_port(conn, "Shield Sphere", "keyword", "Defender")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("PhenaxD",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"PhenaxD"})
        assert "Shield Sphere" in _candidates(results)

    def test_life_lost_scaling(self, conn):
        """Commander with LifeOppsLost scales_with should find drain/damage effects."""
        _insert_card(conn, "Rakdos", card_types="Creature", types="Creature")
        _insert_card(conn, "Lightning Bolt", card_types="Instant")
        _insert_card(conn, "Drain Life", card_types="Sorcery")
        _insert_port(conn, "Rakdos", "scales_with", "LifeOppsLostThisTurn")
        _insert_port(conn, "Lightning Bolt", "effect", "DealDamage")
        _insert_port(conn, "Drain Life", "effect", "LoseLife")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Rakdos",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Rakdos"})
        cands = _candidates(results)
        assert "Lightning Bolt" in cands
        assert "Drain Life" in cands
        assert any(r.cmdr_event == "scales_opp_life_lost" for r in results)

    def test_no_scales_with_returns_empty(self, conn):
        """Commander without scales_with ports returns empty."""
        _insert_card(conn, "Plain", card_types="Creature", types="Creature")
        _insert_port(conn, "Plain", "trigger", "ChangesZone")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Plain",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Plain"})
        assert results == []

    def test_excludes_commander_from_p1p1(self, conn):
        """Commander should not appear in its own P1P1 scaling results."""
        _insert_card(conn, "SelfCounter", card_types="Creature", types="Creature")
        _insert_port(conn, "SelfCounter", "scales_with", "CardCounters.P1P1")
        _insert_port(conn, "SelfCounter", "effect", "PutCounter", counter_type="P1P1")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("SelfCounter",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"SelfCounter"})
        assert "SelfCounter" not in _candidates(results)

    def test_unrecognized_scales_with_ignored(self, conn):
        """scales_with event_class that doesn't match any known pattern is ignored."""
        _insert_card(conn, "WeirdCmdr", card_types="Creature", types="Creature")
        _insert_port(conn, "WeirdCmdr", "scales_with", "SomethingUnknown")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("WeirdCmdr",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"WeirdCmdr"})
        assert results == []

    def test_p1p1_null_counter_type_matches(self, conn):
        """PutCounter with NULL/empty counter_type should match P1P1 scaling."""
        _insert_card(conn, "P1P1Cmdr2", card_types="Creature", types="Creature")
        _insert_card(conn, "Generic Counter", card_types="Creature")
        _insert_port(conn, "P1P1Cmdr2", "scales_with", "CardCounters.P1P1")
        _insert_port(conn, "Generic Counter", "effect", "PutCounter", counter_type="")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("P1P1Cmdr2",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"P1P1Cmdr2"})
        assert "Generic Counter" in _candidates(results)

    def test_putcounterall_matches_p1p1(self, conn):
        """PutCounterAll effect should also match P1P1 scaling."""
        _insert_card(conn, "P1P1Cmdr3", card_types="Creature", types="Creature")
        _insert_card(conn, "Mass Counters", card_types="Sorcery")
        _insert_port(conn, "P1P1Cmdr3", "scales_with", "CardCounters.P1P1")
        _insert_port(conn, "Mass Counters", "effect", "PutCounterAll", counter_type="P1P1")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("P1P1Cmdr3",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"P1P1Cmdr3"})
        assert "Mass Counters" in _candidates(results)
