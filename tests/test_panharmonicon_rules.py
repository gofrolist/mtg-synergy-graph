"""Tests for panharmonicon complement matchers.

Builds a small in-memory SQLite database with synthetic card_ports and cards
rows to exercise all three functions in complement_rules/panharmonicon.py:
  - _find_panharmonicon_complements
  - _find_reverse_panharmonicon
  - _find_panharmonicon_stacking
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.panharmonicon import (
    _find_panharmonicon_complements,
    _find_panharmonicon_stacking,
    _find_reverse_panharmonicon,
)

# ---------------------------------------------------------------------------
# Fixture: in-memory DB with schema
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    """Create an in-memory SQLite DB with the minimal schema needed."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE cards (
            name       TEXT PRIMARY KEY,
            subtypes   TEXT,
            types      TEXT
        );
        CREATE TABLE card_ports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name        TEXT NOT NULL,
            port_type        TEXT NOT NULL,
            event_class      TEXT NOT NULL,
            valid_filter     TEXT,
            zone_origin      TEXT,
            zone_destination TEXT,
            branch_kind      TEXT DEFAULT 'root',
            raw_line         TEXT,
            amount           TEXT,
            counter_type     TEXT,
            replacement_event TEXT,
            replacement_result TEXT,
            affected_scope   TEXT,
            is_conditional   BOOLEAN DEFAULT FALSE
        );
        """
    )
    yield c
    c.close()


def _port_row(**kwargs) -> dict:
    """Build a PortRow dict with defaults."""
    defaults = {
        "card_name": "",
        "port_type": "",
        "event_class": "",
        "valid_filter": "",
        "zone_origin": "",
        "zone_destination": "",
        "branch_kind": "root",
        "raw_line": "",
        "amount": "",
        "counter_type": "",
        "replacement_event": "",
        "replacement_result": "",
        "affected_scope": "",
        "is_conditional": False,
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# _find_panharmonicon_complements
# ---------------------------------------------------------------------------


class TestFindPanharmoniconComplements:
    """Test Yarok-style panharmonicon: commander has Panharmonicon static,
    find candidates whose triggers are doubled."""

    def test_basic_etb_doubling(self, conn):
        """Commander with Panharmonicon for ChangesZone should find
        candidates with ChangesZone triggers that have a valuable effect."""
        # Commander: Yarok-like with Panharmonicon for ChangesZone
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        # Candidate: Mulldrifter with ChangesZone ETB trigger + Draw effect
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Mulldrifter",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Mulldrifter", "trigger", "ChangesZone", "Card.Self", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Mulldrifter", "effect", "Draw"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        candidates = {r.candidate for r in results}
        assert "Mulldrifter" in candidates

    def test_non_self_trigger_with_valuable_effect(self, conn):
        """Non-self trigger with a valuable effect should match."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Soul Warden",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Soul Warden", "trigger", "ChangesZone", "Creature.Other", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Soul Warden", "effect", "GainLife"),  # GainLife is in _VALUABLE_EFFECTS
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        candidates = {r.candidate for r in results}
        # GainLife IS in _VALUABLE_EFFECTS, so Soul Warden should match
        assert "Soul Warden" in candidates

    def test_no_panharmonicon_ports_returns_empty(self, conn):
        """Commander without Panharmonicon event_class returns empty."""
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="Sacrificed"),
        ]
        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Korvold"})
        assert results == []

    def test_panharmonicon_raw_line_without_valid_mode(self, conn):
        """Panharmonicon port without ValidMode in raw_line is skipped."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'SomeOther': 'Stuff'}",
            ),
        ]
        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert results == []

    def test_excludes_commander_from_candidates(self, conn):
        """Commander cards should not appear in results."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Yarok",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Yarok", "trigger", "ChangesZone", "Creature.YouCtrl", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Yarok", "effect", "Draw"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        candidates = {r.candidate for r in results}
        assert "Yarok" not in candidates

    def test_zone_destination_filter(self, conn):
        """Panharmonicon with Destination filter (Teysa: Graveyard) should
        exclude candidates with a different zone_destination."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone', 'Destination': 'Graveyard'}",
            ),
        ]
        # Card with ETB (Battlefield) -- wrong destination, should be excluded
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("ETB Card",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ETB Card", "trigger", "ChangesZone", "Creature.Other", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("ETB Card", "effect", "Draw"),
        )
        # Card with death trigger (Graveyard) -- correct destination
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Death Card",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Death Card", "trigger", "ChangesZone", "Creature.Other", "Graveyard", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Death Card", "effect", "Destroy"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Teysa"})
        candidates = {r.candidate for r in results}
        assert "ETB Card" not in candidates
        assert "Death Card" in candidates

    def test_zone_destination_empty_candidate_passes(self, conn):
        """Candidate without zone_destination should pass the zone filter."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone', 'Destination': 'Graveyard'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("NoZone Card",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("NoZone Card", "trigger", "ChangesZone", "Creature.Other", "", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("NoZone Card", "effect", "Token"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Teysa"})
        candidates = {r.candidate for r in results}
        # Empty zone_destination passes the check (cand_zd is falsy)
        assert "NoZone Card" in candidates

    def test_self_etb_splits_by_effect_type_in_cand_event(self, conn):
        """Self-ETB triggers split by effect type (``_etb_Draw``, ``_etb_Dig``)
        so premium Yarok picks don't share a single flooded IDF group."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Mulldrifter",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Mulldrifter", "trigger", "ChangesZone", "Card.Self", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Mulldrifter", "effect", "Draw"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert len(results) == 1
        assert results[0].cand_event == "ChangesZone_etb_Draw"

    def test_non_self_trigger_uses_effect_suffix(self, conn):
        """Non-self triggers should produce cand_event with effect type suffix."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Impact Tremors",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Impact Tremors", "trigger", "ChangesZone", "Creature.Other", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Impact Tremors", "effect", "DealDamage"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert len(results) == 1
        assert results[0].cand_event == "ChangesZone_DealDamage"

    def test_no_valuable_effect_excluded(self, conn):
        """Candidate with trigger but no valuable effect is excluded."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Vanilla Creature",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Vanilla Creature", "trigger", "ChangesZone", "Card.Self", "Battlefield", "root"),
        )
        # No effect port at all
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert results == []

    def test_multiple_valid_modes(self, conn):
        """Panharmonicon with multiple ValidModes matches triggers for any."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone, Sacrificed'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Sac Trigger Card",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, branch_kind) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Sac Trigger Card", "trigger", "Sacrificed", "Creature.YouCtrl", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Sac Trigger Card", "effect", "Draw"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Teysa"})
        candidates = {r.candidate for r in results}
        assert "Sac Trigger Card" in candidates

    def test_result_has_correct_rule_id_and_direction(self, conn):
        """Results should have rule_id='panharmonicon' and direction='synergy'."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Test Card",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Test Card", "trigger", "ChangesZone", "Card.Self", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Test Card", "effect", "Token"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert len(results) == 1
        assert results[0].rule_id == "panharmonicon"
        assert results[0].direction == "synergy"
        assert results[0].cmdr_event == "Panharmonicon_ChangesZone"

    def test_deduplicates_candidates(self, conn):
        """Same card with multiple matching triggers should only appear once."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Multi Trigger",))
        # Two ChangesZone triggers for the same card
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Multi Trigger", "trigger", "ChangesZone", "Card.Self", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Multi Trigger", "trigger", "ChangesZone", "Creature.YouCtrl", "Battlefield", "root"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Multi Trigger", "effect", "Draw"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert len(results) == 1

    def test_no_matching_triggers_returns_empty(self, conn):
        """Panharmonicon for ChangesZone, but no candidates have ChangesZone triggers."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Only Effects",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Only Effects", "effect", "Draw"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert results == []

    def test_branch_kind_preserved(self, conn):
        """branch_kind from the candidate port should be preserved in result."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Branched Card",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, branch_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Branched Card", "trigger", "ChangesZone", "Card.Self", "Battlefield", "sub"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
            ("Branched Card", "effect", "Destroy"),
        )
        conn.commit()

        results = _find_panharmonicon_complements(conn, cmdr_ports, {"Yarok"})
        assert len(results) == 1
        assert results[0].branch_kind == "sub"


# ---------------------------------------------------------------------------
# _find_reverse_panharmonicon
# ---------------------------------------------------------------------------


class TestFindReversePanharmonicon:
    """Test reverse panharmonicon: find candidates with Panharmonicon static
    that double the commander's triggers based on subtype matching."""

    def test_basic_subtype_match(self, conn):
        """Harmonic Prodigy doubles Wizard triggers. Commander is a Wizard
        with triggers -> should match."""
        # Commander: a Wizard with a trigger
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        # Candidate: Harmonic Prodigy with Panharmonicon for Wizards
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Harmonic Prodigy", "Human Wizard", "Creature"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Harmonic Prodigy",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Wizard.Other+YouCtrl', 'ValidMode': 'ChangesZone,SpellCast'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        candidates = {r.candidate for r in results}
        assert "Harmonic Prodigy" in candidates

    def test_no_subtypes_returns_empty(self, conn):
        """Commander without subtypes should return empty."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Sol Ring", None, "Artifact"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="TapsForMana"),
        ]
        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Sol Ring"})
        assert results == []

    def test_no_triggers_returns_empty(self, conn):
        """Commander with subtypes but no triggers should return empty."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Wizard Card", "Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="effect", event_class="Draw"),
        ]
        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Wizard Card"})
        assert results == []

    def test_generic_valid_card_does_not_match(self, conn):
        """ValidCard='Permanent.YouCtrl' (generic) should NOT match."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Panharmonicon", None, "Artifact"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Panharmonicon",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Permanent.YouCtrl', 'ValidMode': 'ChangesZone'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        candidates = {r.candidate for r in results}
        assert "Panharmonicon" not in candidates

    def test_excludes_commander_from_results(self, conn):
        """Commander should not appear in its own results."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Kykar",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Wizard.Other+YouCtrl', 'ValidMode': 'SpellCast'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        candidates = {r.candidate for r in results}
        assert "Kykar" not in candidates

    def test_no_valid_card_in_raw_line_skipped(self, conn):
        """Candidate without ValidCard in raw_line should be skipped."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Weird Card", None, "Artifact"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Weird Card",
                "static",
                "Panharmonicon",
                "{'ValidMode': 'ChangesZone'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        assert results == []

    def test_valid_mode_overlap_with_cmdr_triggers(self, conn):
        """ValidMode must overlap with commander's trigger event classes."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        # Candidate doubles ChangesZone but commander only has SpellCast
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Wrong Mode Card", "Human Wizard", "Creature"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Wrong Mode Card",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Wizard.Other+YouCtrl', 'ValidMode': 'ChangesZone'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        candidates = {r.candidate for r in results}
        assert "Wrong Mode Card" not in candidates

    def test_no_valid_mode_defaults_to_any(self, conn):
        """Missing ValidMode defaults to 'any', which matches all triggers."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("No Mode Card", "Human Wizard", "Creature"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "No Mode Card",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Wizard.Other+YouCtrl'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        candidates = {r.candidate for r in results}
        assert "No Mode Card" in candidates

    def test_result_attributes(self, conn):
        """Results should have correct rule_id, direction, and event names."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Harmonic Prodigy", "Human Wizard", "Creature"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Harmonic Prodigy",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Wizard.Other+YouCtrl', 'ValidMode': 'SpellCast'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        assert len(results) == 1
        r = results[0]
        assert r.rule_id == "panharmonicon"
        assert r.direction == "synergy"
        assert r.cmdr_event == "reverse_panharmonicon"
        assert r.cand_event == "doubles_cmdr_triggers"

    def test_deduplicates_candidates(self, conn):
        """Same card with multiple Panharmonicon statics should appear once."""
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Kykar", "Bird Wizard", "Creature"),
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="SpellCast"),
        ]
        conn.execute(
            "INSERT INTO cards (name, subtypes, types) VALUES (?, ?, ?)",
            ("Dup Card", "Human Wizard", "Creature"),
        )
        # Two Panharmonicon statics for the same card
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Dup Card",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Wizard.Other+YouCtrl', 'ValidMode': 'SpellCast'}",
            ),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Dup Card",
                "static",
                "Panharmonicon",
                "{'ValidCard': 'Bird.Other+YouCtrl', 'ValidMode': 'SpellCast'}",
            ),
        )
        conn.commit()

        results = _find_reverse_panharmonicon(conn, cmdr_ports, {"Kykar"})
        assert len(results) == 1


# ---------------------------------------------------------------------------
# _find_panharmonicon_stacking
# ---------------------------------------------------------------------------


class TestFindPanharmoniconStacking:
    """Test stacking: commander IS a Panharmonicon, find other Panharmonicon
    cards with overlapping ValidMode."""

    def test_basic_stacking(self, conn):
        """Yarok (Panharmonicon for ChangesZone) should find Panharmonicon
        artifact which also doubles ChangesZone."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Panharmonicon",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Panharmonicon",
                "static",
                "Panharmonicon",
                "{'ValidMode': 'ChangesZone'}",
            ),
        )
        conn.commit()

        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        candidates = {r.candidate for r in results}
        assert "Panharmonicon" in candidates

    def test_non_overlapping_modes_excluded(self, conn):
        """Candidate with non-overlapping ValidMode should not match."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Attack Doubler",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Attack Doubler",
                "static",
                "Panharmonicon",
                "{'ValidMode': 'Attacks'}",
            ),
        )
        conn.commit()

        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        assert results == []

    def test_no_panharmonicon_static_returns_empty(self, conn):
        """Commander without Panharmonicon static returns empty."""
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="Sacrificed"),
        ]
        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Korvold"})
        assert results == []

    def test_excludes_commander_from_results(self, conn):
        """Commander should not appear in its own stacking results."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Yarok",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Yarok",
                "static",
                "Panharmonicon",
                "{'ValidMode': 'ChangesZone'}",
            ),
        )
        conn.commit()

        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        candidates = {r.candidate for r in results}
        assert "Yarok" not in candidates

    def test_candidate_without_valid_mode_excluded(self, conn):
        """Candidate without ValidMode in raw_line should be excluded."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("No Mode",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "No Mode",
                "static",
                "Panharmonicon",
                "{'SomeOtherKey': 'value'}",
            ),
        )
        conn.commit()

        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        assert results == []

    def test_partial_overlap_matches(self, conn):
        """Candidate with partially overlapping modes should match."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone, Sacrificed'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Partial Overlap",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Partial Overlap",
                "static",
                "Panharmonicon",
                "{'ValidMode': 'Sacrificed, Attacks'}",
            ),
        )
        conn.commit()

        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        candidates = {r.candidate for r in results}
        assert "Partial Overlap" in candidates

    def test_result_attributes(self, conn):
        """Results should have correct event names for stacking."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Panharmonicon",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Panharmonicon",
                "static",
                "Panharmonicon",
                "{'ValidMode': 'ChangesZone'}",
            ),
        )
        conn.commit()

        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        assert len(results) == 1
        r = results[0]
        assert r.rule_id == "panharmonicon"
        assert r.direction == "synergy"
        assert r.cmdr_event == "Panharmonicon_stack"
        assert r.cand_event == "Panharmonicon"

    def test_cmdr_non_static_panharmonicon_ignored(self, conn):
        """Only port_type='static' on commander should be considered."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",  # trigger, not static
                event_class="Panharmonicon",
                raw_line="{'ValidMode': 'ChangesZone'}",
            ),
        ]
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("Panharmonicon",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Panharmonicon",
                "static",
                "Panharmonicon",
                "{'ValidMode': 'ChangesZone'}",
            ),
        )
        conn.commit()

        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        assert results == []

    def test_cmdr_raw_line_without_valid_mode(self, conn):
        """Commander Panharmonicon static without ValidMode returns empty."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Panharmonicon",
                raw_line="{'SomeKey': 'value'}",
            ),
        ]
        results = _find_panharmonicon_stacking(conn, cmdr_ports, {"Yarok"})
        assert results == []
