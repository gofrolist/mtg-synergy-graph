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
    _find_counter_doubler_synergy,
    _find_counter_keyword_synergy,
    _find_etb_self_complements,
    _find_lord_complements,
    _find_scales_with_density,
    _find_scaling_complements,
    _find_spellcast_density_complements,
    _find_tribal_density_complements,
    _find_value_engine_density,
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
    cmc: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, types, supertypes, keywords, color_identity, power, toughness, cmc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, card_types, subtypes, types, supertypes, keywords, color_identity, power, toughness, cmc),
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

    def test_gate5_artifact_archetype_suppresses_incidental_token(self, conn):
        """Commander whose ETB token subtype isn't in its literal
        subtypes or port filters, AND which has ≥2 Artifact port refs
        (Urza-style), should NOT get the token subtype as a tribal axis.
        Without Gate 5 the 232 Constructs in the DB all matched
        tribal_density for Urza and buried Dramatic Reversal / Unwinding
        Clock under a rank-30 floor of 0.50."""
        _insert_card(
            conn,
            "UrzaLike",
            card_types="Creature",
            subtypes="Human Artificer",
            types="Creature",
        )
        _insert_card(conn, "Walking Ballista", card_types="Creature", subtypes="Construct")
        # ETB-self Token effect that creates a Construct token.
        _insert_port(
            conn,
            "UrzaLike",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            raw_line="{'Mode':'ChangesZone','Origin':'Any','Destination':'Battlefield','ValidCard':'Card.Self'}",
        )
        _insert_port(
            conn,
            "UrzaLike",
            "effect",
            "Token",
            raw_line="{'TokenScript': 'c_0_0_a_construct_total_artifacts'}",
        )
        # TWO artifact-referencing ports — the "competing archetype"
        # signal Gate 5 looks for.
        _insert_port(
            conn,
            "UrzaLike",
            "cost",
            "tap_type",
            raw_line="tapXType<1/Artifact>",
        )
        _insert_port(
            conn,
            "UrzaLike",
            "static",
            "Continuous",
            raw_line="{'Mode':'Continuous','Affected':'Artifact.YouCtrl','AddAbility':'T: Add 1'}",
        )

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("UrzaLike",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"UrzaLike"})
        assert "Walking Ballista" not in _candidates(results), (
            "Gate 5 should suppress incidental Construct tribal for artifact-archetype commanders"
        )

    def test_gate5_does_not_affect_real_tribal_commanders(self, conn):
        """Ghave/Krenko-style commanders have 0 Artifact port refs, so
        Gate 5 is a no-op: their token subtype still counts as tribal
        via the existing fall-through path."""
        _insert_card(
            conn,
            "GhaveLike",
            card_types="Creature",
            subtypes="Fungus Shaman",
            types="Creature",
        )
        _insert_card(conn, "Sporemound", card_types="Creature", subtypes="Saproling")
        # No triggers (Ghave's tokens come from an activated ability);
        # the Token effect's TokenScript names Saproling.
        _insert_port(
            conn,
            "GhaveLike",
            "effect",
            "Token",
            raw_line="{'TokenScript': 'g_1_1_saproling'}",
        )
        _insert_port(conn, "GhaveLike", "cost", "sacrifice")
        _insert_port(conn, "GhaveLike", "effect", "PutCounter", valid_filter="Creature")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("GhaveLike",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"GhaveLike"})
        assert "Sporemound" in _candidates(results)

    def test_vanilla_tribal_anchor_fires_on_literal_subtype(self, conn):
        """Keywords-only vanilla anchor (Akroma-style) falls back to literal
        card subtypes for tribal density, since its EDHREC Hi-Syn is
        dominated by the tribe and no other rule would match."""
        _insert_card(
            conn,
            "Akroma",
            card_types="Creature",
            subtypes="Angel",
            types="Legendary Creature",
        )
        _insert_card(conn, "Giada", card_types="Creature", subtypes="Angel")
        _insert_card(conn, "Llanowar Elves", card_types="Creature", subtypes="Elf")
        # Only keyword ports — vanilla anchor
        _insert_port(conn, "Akroma", "keyword", "Flying")
        _insert_port(conn, "Akroma", "keyword", "First Strike")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Akroma",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"Akroma"})
        names = _candidates(results)
        assert "Giada" in names
        assert "Llanowar Elves" not in names

    def test_vanilla_tribal_anchor_skipped_when_mechanical_ports_present(self, conn):
        """If the commander has any non-keyword port (trigger / effect /
        static / replacement / scales_with / cost), the vanilla fallback
        is skipped — the mechanical structure drives the synergy, and
        layering tribal on top would flatten top-30.

        Horobi-style commander: mechanical port exists (BecomesTarget
        trigger + Destroy effect) but neither references his literal
        Spirit subtype, so the standard extraction yields no subtypes.
        The vanilla fallback must stay silent since Horobi has mechanical
        structure (his BecomesTarget engine)."""
        _insert_card(
            conn,
            "Horobi",
            card_types="Creature",
            subtypes="Spirit",
            types="Legendary Creature",
        )
        _insert_card(conn, "Spirit of the Hearth", card_types="Creature", subtypes="Spirit")
        _insert_port(conn, "Horobi", "keyword", "Flying")
        # Mechanical port — ablates the vanilla fallback (doesn't reference "Spirit")
        _insert_port(conn, "Horobi", "trigger", "BecomesTarget")
        _insert_port(conn, "Horobi", "effect", "Destroy", valid_filter="TriggeredTargetLKICopy")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Horobi",)).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"Horobi"})
        assert results == []

    def test_vanilla_tribal_anchor_skips_overbroad_subtypes(self, conn):
        """Human / Warrior / Soldier are too over-represented to drive
        tribal density for a vanilla anchor — 4300 Humans would flatten
        every Human-subtyped anchor's top-30. The skiplist filters these
        out; only the rarer subtype (if any) qualifies."""
        _insert_card(
            conn,
            "Generic Human Warrior",
            card_types="Creature",
            subtypes="Human Warrior",
            types="Legendary Creature",
        )
        _insert_card(conn, "Random Human", card_types="Creature", subtypes="Human Soldier")
        _insert_port(conn, "Generic Human Warrior", "keyword", "Vigilance")

        cmdr_ports = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM card_ports WHERE card_name = ?",
                ("Generic Human Warrior",),
            ).fetchall()
        ]
        results = _find_tribal_density_complements(conn, cmdr_ports, {"Generic Human Warrior"})
        assert results == []


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
        """Commander with LifeOppsLost scales_with should find repeatable drain/damage
        sources (permanents) and exclude one-shot burn (Instant/Sorcery)."""
        _insert_card(conn, "Rakdos", card_types="Creature", types="Creature")
        _insert_card(conn, "Lightning Bolt", card_types="Instant")
        _insert_card(conn, "Drain Life", card_types="Sorcery")
        _insert_card(conn, "Spear Spewer", card_types="Creature")
        _insert_card(conn, "Kokusho, the Evening Star", card_types="Creature")
        _insert_port(conn, "Rakdos", "scales_with", "LifeOppsLostThisTurn")
        _insert_port(conn, "Lightning Bolt", "effect", "DealDamage")
        _insert_port(conn, "Drain Life", "effect", "LoseLife")
        _insert_port(conn, "Spear Spewer", "effect", "DamageAll")
        _insert_port(conn, "Kokusho, the Evening Star", "effect", "LoseLife", valid_filter="Opponent")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Rakdos",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Rakdos"})
        cands = _candidates(results)
        # One-shot burn is excluded — it fires once per cast, not per turn,
        # and flat 0.3 weight drowns true repeatable enablers.
        assert "Lightning Bolt" not in cands
        assert "Drain Life" not in cands
        # Repeatable permanent-based damage is kept.
        assert "Spear Spewer" in cands
        assert "Kokusho, the Evening Star" in cands
        assert any(r.cmdr_event == "scales_opp_life_lost" for r in results)

    def test_creature_count_scaling_emits_token_producers(self, conn):
        """Shanna-style vanilla scaler (scales_with Valid Creature.YouCtrl +
        self-pump Continuous static only) matches token producers, since
        every token gives her another +1/+1."""
        _insert_card(conn, "Shanna", card_types="Creature", types="Creature")
        _insert_card(conn, "Queen Allenal", card_types="Creature")
        _insert_card(conn, "Sundering Growth", card_types="Instant")
        _insert_port(
            conn,
            "Shanna",
            "scales_with",
            "Valid",
            valid_filter="Creature.YouCtrl",
        )
        _insert_port(
            conn,
            "Shanna",
            "static",
            "Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Card.Self', 'AddPower': 'X'}",
        )
        _insert_port(
            conn,
            "Queen Allenal",
            "effect",
            "Token",
            raw_line="{'TokenScript': 'g_1_1_creature'}",
        )
        _insert_port(
            conn,
            "Sundering Growth",
            "effect",
            "CopyPermanent",
            raw_line="{'Populate': 'True'}",
        )

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Shanna",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Shanna"})
        names = _candidates(results)
        assert "Queen Allenal" in names
        assert "Sundering Growth" in names
        assert any(r.cmdr_event == "scales_creature_count" for r in results)

    def test_creature_count_scaling_skips_when_trigger_present(self, conn):
        """Adeline-style (scales_with Creature + Attacks trigger that
        makes tokens) must NOT grab the broad token pool — her Hi-Syn
        is attack payoffs, not raw token producers. Gate rejects any
        trigger / effect / cost / replacement port."""
        _insert_card(conn, "Adeline", card_types="Creature", types="Creature")
        _insert_card(conn, "Queen Allenal", card_types="Creature")
        _insert_port(
            conn,
            "Adeline",
            "scales_with",
            "Valid",
            valid_filter="Creature.YouCtrl",
        )
        _insert_port(
            conn,
            "Adeline",
            "trigger",
            "Attacks",
            valid_filter="Card.Self",
        )
        _insert_port(
            conn,
            "Queen Allenal",
            "effect",
            "Token",
            raw_line="{'TokenScript': 'w_1_1_creature'}",
        )

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Adeline",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Adeline"})
        assert "Queen Allenal" not in _candidates(results)

    def test_creature_count_scaling_skips_reducecost_cmdr(self, conn):
        """Ghalta-style (scales_with Creature.YouCtrl$CardPower + static
        ReduceCost) is a cost-reducer for big creatures, not a token
        producer commander. The self-static gate rejects any static
        whose raw_line doesn't name 'Affected' or 'ValidTarget' Card.Self —
        ReduceCost uses 'ValidCard' Card.Self which is a different slot
        (the cast target, not an affected permanent)."""
        _insert_card(conn, "Ghalta", card_types="Creature", types="Creature")
        _insert_card(conn, "Queen Allenal", card_types="Creature")
        _insert_port(
            conn,
            "Ghalta",
            "scales_with",
            "Valid",
            valid_filter="Creature.YouCtrl$CardPower",
        )
        _insert_port(
            conn,
            "Ghalta",
            "static",
            "ReduceCost",
            raw_line="{'Mode': 'ReduceCost', 'ValidCard': 'Card.Self', 'Amount': 'X'}",
        )
        _insert_port(
            conn,
            "Queen Allenal",
            "effect",
            "Token",
            raw_line="{'TokenScript': 'g_1_1_creature'}",
        )

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Ghalta",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Ghalta"})
        assert "Queen Allenal" not in _candidates(results)

    def test_domain_scaling_matches_land_type_adders(self, conn):
        """Commander scaling with Domain (basic-land-type count) matches
        cards that add basic land types to a permanent — Prismatic
        Omen (all lands all basic types) and Dryad of the Ilysian
        Grove are the canonical Radha / Domain enablers."""
        _insert_card(conn, "Radha", card_types="Creature", types="Creature")
        _insert_card(conn, "Prismatic Omen", card_types="Enchantment")
        _insert_card(conn, "Dryad of the Ilysian Grove", card_types="Creature")
        _insert_card(conn, "Random Creature", card_types="Creature")
        _insert_port(conn, "Radha", "scales_with", "Domain")
        _insert_port(
            conn,
            "Prismatic Omen",
            "static",
            "Continuous",
            raw_line=(
                "{'Mode': 'Continuous', 'Affected': 'Land.YouCtrl',"
                " 'AddType': 'Plains & Island & Swamp & Mountain & Forest'}"
            ),
        )
        _insert_port(
            conn,
            "Dryad of the Ilysian Grove",
            "static",
            "Continuous",
            raw_line=(
                "{'Mode': 'Continuous', 'Affected': 'Land.YouCtrl',"
                " 'AddType': 'Forest & Plains & Island & Swamp & Mountain'}"
            ),
        )

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Radha",)).fetchall()
        ]
        results = _find_scales_with_density(conn, cmdr_ports, {"Radha"})
        names = _candidates(results)
        assert "Prismatic Omen" in names
        assert "Dryad of the Ilysian Grove" in names
        assert "Random Creature" not in names
        assert any(r.cmdr_event == "scales_domain" for r in results)

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


# ---------------------------------------------------------------------------
# _find_counter_doubler_synergy
# ---------------------------------------------------------------------------


class TestCounterDoublerSynergy:
    def test_counter_trigger_finds_doubler(self, conn):
        """CounterAdded trigger -> finds Hardened Scales (replacement AddCounter)."""
        _insert_card(conn, "Marchesa", card_types="Creature")
        _insert_port(conn, "Marchesa", "trigger", "CounterAdded", counter_type="P1P1")
        _insert_port(
            conn,
            "Hardened Scales",
            "replacement",
            "AddCounter",
            raw_line="{'Event': 'AddCounter', 'ValidCard': 'Creature.YouCtrl'}",
        )
        # Ensure replacement_event is set
        conn.execute("UPDATE card_ports SET replacement_event = 'AddCounter' WHERE card_name = 'Hardened Scales'")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Marchesa",)).fetchall()
        ]
        results = _find_counter_doubler_synergy(conn, cmdr_ports, {"Marchesa"})
        assert len(results) == 1
        assert results[0].candidate == "Hardened Scales"
        assert results[0].rule_id == "counter_doubler"

    def test_scales_with_p1p1_also_triggers(self, conn):
        """scales_with P1P1 -> also detects counter interest."""
        _insert_card(conn, "Ezuri", card_types="Creature")
        _insert_port(conn, "Ezuri", "scales_with", "CardCounters.P1P1")
        _insert_port(
            conn,
            "Doubling Season",
            "replacement",
            "AddCounter",
            raw_line="{'Event': 'AddCounter', 'ValidCard': 'Permanent.YouCtrl'}",
        )
        conn.execute("UPDATE card_ports SET replacement_event = 'AddCounter' WHERE card_name = 'Doubling Season'")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Ezuri",)).fetchall()
        ]
        results = _find_counter_doubler_synergy(conn, cmdr_ports, {"Ezuri"})
        assert len(results) == 1

    def test_p1p1_in_valid_filter_triggers(self, conn):
        """Marchesa pattern: trigger valid_filter with P1P1 -> detects counter interest."""
        _insert_card(conn, "MarchesaLike", card_types="Creature")
        _insert_port(
            conn,
            "MarchesaLike",
            "trigger",
            "ChangesZone",
            valid_filter="Card.YouCtrl+counters_GE1_P1P1",
        )
        _insert_port(
            conn,
            "Branching Evo",
            "replacement",
            "AddCounter",
            raw_line="{'Event': 'AddCounter', 'ValidCard': 'Creature.YouCtrl'}",
        )
        conn.execute("UPDATE card_ports SET replacement_event = 'AddCounter' WHERE card_name = 'Branching Evo'")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("MarchesaLike",)).fetchall()
        ]
        results = _find_counter_doubler_synergy(conn, cmdr_ports, {"MarchesaLike"})
        assert len(results) == 1

    def test_self_only_doubler_skipped(self, conn):
        """Counter doubler targeting Card.Self should be skipped."""
        _insert_card(conn, "Cmdr", card_types="Creature")
        _insert_port(conn, "Cmdr", "trigger", "CounterAdded", counter_type="P1P1")
        _insert_port(
            conn,
            "SelfDoubler",
            "replacement",
            "AddCounter",
            raw_line="{'Event': 'AddCounter', 'ValidCard': 'Card.Self'}",
        )
        conn.execute("UPDATE card_ports SET replacement_event = 'AddCounter' WHERE card_name = 'SelfDoubler'")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Cmdr",)).fetchall()
        ]
        results = _find_counter_doubler_synergy(conn, cmdr_ports, {"Cmdr"})
        assert results == []

    def test_no_counter_interest_returns_empty(self, conn):
        """Commander without counter interest gets no matches."""
        _insert_card(conn, "Talrand", card_types="Creature")
        _insert_port(conn, "Talrand", "trigger", "SpellCast")
        _insert_port(
            conn,
            "Hardened Scales",
            "replacement",
            "AddCounter",
            raw_line="{'Event': 'AddCounter'}",
        )
        conn.execute("UPDATE card_ports SET replacement_event = 'AddCounter' WHERE card_name = 'Hardened Scales'")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Talrand",)).fetchall()
        ]
        results = _find_counter_doubler_synergy(conn, cmdr_ports, {"Talrand"})
        assert results == []

    def test_excludes_commander(self, conn):
        _insert_card(conn, "Cmdr2", card_types="Creature")
        _insert_port(conn, "Cmdr2", "trigger", "CounterAdded", counter_type="P1P1")
        _insert_port(
            conn,
            "Cmdr2",
            "replacement",
            "AddCounter",
            raw_line="{'Event': 'AddCounter'}",
        )
        conn.execute("UPDATE card_ports SET replacement_event = 'AddCounter' WHERE card_name = 'Cmdr2'")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Cmdr2",)).fetchall()
        ]
        results = _find_counter_doubler_synergy(conn, cmdr_ports, {"Cmdr2"})
        assert results == []


# ---------------------------------------------------------------------------
# _find_counter_keyword_synergy
# ---------------------------------------------------------------------------


class TestCounterKeywordSynergy:
    def test_counter_trigger_finds_modular(self, conn):
        """CounterAdded trigger -> finds Modular creature."""
        _insert_card(conn, "Marchesa", card_types="Creature")
        _insert_port(conn, "Marchesa", "trigger", "CounterAdded", counter_type="P1P1")
        _insert_port(conn, "Arcbound Ravager", "keyword", "Modular")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Marchesa",)).fetchall()
        ]
        results = _find_counter_keyword_synergy(conn, cmdr_ports, {"Marchesa"})
        assert len(results) == 1
        assert results[0].candidate == "Arcbound Ravager"
        assert results[0].rule_id == "counter_keyword"
        assert results[0].cand_event == "Modular"

    def test_finds_undying_and_persist(self, conn):
        """Multiple counter keywords are matched."""
        _insert_card(conn, "Cmdr", card_types="Creature")
        _insert_port(conn, "Cmdr", "trigger", "CounterAdded", counter_type="P1P1")
        _insert_port(conn, "Geralf's Messenger", "keyword", "Undying")
        _insert_port(conn, "Kitchen Finks", "keyword", "Persist")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Cmdr",)).fetchall()
        ]
        results = _find_counter_keyword_synergy(conn, cmdr_ports, {"Cmdr"})
        assert len(results) == 2
        assert _candidates(results) == {"Geralf's Messenger", "Kitchen Finks"}

    def test_no_counter_interest_returns_empty(self, conn):
        """Commander without counter interest gets no matches."""
        _insert_card(conn, "Talrand", card_types="Creature")
        _insert_port(conn, "Talrand", "trigger", "SpellCast")
        _insert_port(conn, "Arcbound Worker", "keyword", "Modular")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Talrand",)).fetchall()
        ]
        results = _find_counter_keyword_synergy(conn, cmdr_ports, {"Talrand"})
        assert results == []

    def test_excludes_commander(self, conn):
        _insert_card(conn, "Cmdr3", card_types="Creature")
        _insert_port(conn, "Cmdr3", "trigger", "CounterAdded", counter_type="P1P1")
        _insert_port(conn, "Cmdr3", "keyword", "Undying")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Cmdr3",)).fetchall()
        ]
        results = _find_counter_keyword_synergy(conn, cmdr_ports, {"Cmdr3"})
        assert results == []

    def test_deduplicates_multi_keyword(self, conn):
        """Card with multiple counter keywords only appears once."""
        _insert_card(conn, "Cmdr4", card_types="Creature")
        _insert_port(conn, "Cmdr4", "trigger", "CounterAdded", counter_type="P1P1")
        _insert_port(conn, "DualKeyword", "keyword", "Modular")
        _insert_port(conn, "DualKeyword", "keyword", "Fabricate")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Cmdr4",)).fetchall()
        ]
        results = _find_counter_keyword_synergy(conn, cmdr_ports, {"Cmdr4"})
        assert len(results) == 1


# ---------------------------------------------------------------------------
# _find_value_engine_density
# ---------------------------------------------------------------------------


class TestValueEngineDensity:
    def test_changetype_subtype_matches(self, conn):
        """Kaalia pattern: ChangeType with Creature.Angel -> finds Angels."""
        _insert_card(conn, "Kaalia", card_types="Creature")
        _insert_port(
            conn,
            "Kaalia",
            "effect",
            "ChangeZone",
            raw_line="{'ChangeType': 'Creature.Angel+YouCtrl,Creature.Demon+YouCtrl'}",
        )
        _insert_card(conn, "Avacyn", card_types="Creature", subtypes="Angel")
        _insert_card(conn, "Goblin", card_types="Creature", subtypes="Goblin")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Kaalia",)).fetchall()
        ]
        results = _find_value_engine_density(conn, cmdr_ports, {"Kaalia"})
        assert "Avacyn" in _candidates(results)
        assert "Goblin" not in _candidates(results)
        assert all(r.rule_id == "value_engine" for r in results)

    def test_changetype_base_type_matches(self, conn):
        """Zur pattern: ChangeType Enchantment.cmcLE3 -> finds Enchantments."""
        _insert_card(conn, "Zur", card_types="Creature")
        _insert_port(
            conn,
            "Zur",
            "effect",
            "ChangeZone",
            raw_line="{'ChangeType': 'Enchantment.cmcLE3'}",
        )
        _insert_card(conn, "Grasp of Fate", card_types="Enchantment")
        _insert_card(conn, "Lightning Bolt", card_types="Instant")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Zur",)).fetchall()
        ]
        results = _find_value_engine_density(conn, cmdr_ports, {"Zur"})
        assert "Grasp of Fate" in _candidates(results)
        assert "Lightning Bolt" not in _candidates(results)

    def test_historic_spellcast_matches_artifacts(self, conn):
        """Jhoira pattern: SpellCast Card.Historic -> finds Artifacts."""
        _insert_card(conn, "Jhoira", card_types="Creature")
        _insert_port(conn, "Jhoira", "trigger", "SpellCast", valid_filter="Card.Historic")
        _insert_card(conn, "Sol Ring", card_types="Artifact")
        _insert_card(conn, "Lightning Bolt", card_types="Instant")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Jhoira",)).fetchall()
        ]
        results = _find_value_engine_density(conn, cmdr_ports, {"Jhoira"})
        assert "Sol Ring" in _candidates(results)
        assert "Lightning Bolt" not in _candidates(results)

    def test_no_changetype_returns_empty(self, conn):
        """Commander without ChangeType or Historic gets no matches."""
        _insert_card(conn, "Talrand", card_types="Creature")
        _insert_port(conn, "Talrand", "trigger", "SpellCast", valid_filter="Instant")

        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Talrand",)).fetchall()
        ]
        results = _find_value_engine_density(conn, cmdr_ports, {"Talrand"})
        assert results == []

    def test_excludes_commander(self, conn):
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="Angel")
        _insert_port(
            conn,
            "Cmdr",
            "effect",
            "ChangeZone",
            raw_line="{'ChangeType': 'Creature.Angel+YouCtrl'}",
        )
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("Cmdr",)).fetchall()
        ]
        results = _find_value_engine_density(conn, cmdr_ports, {"Cmdr"})
        assert results == []
