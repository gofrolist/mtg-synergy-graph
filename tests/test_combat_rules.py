"""Tests for combat-related complement matchers.

Uses an in-memory SQLite database with minimal schema to exercise every
branch of the five functions in complement_rules.combat:
  - _find_combat_enhancers
  - _find_evasion_complements
  - _find_sacrifice_outlets
  - _find_changeszone_resonance
  - _find_attack_payoffs
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.combat import (
    _find_attack_payoffs,
    _find_changeszone_resonance,
    _find_combat_enhancers,
    _find_evasion_complements,
    _find_sacrifice_outlets,
    _find_subject_zone_feeders,
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

    def test_counters_filter_trigger_skips_self_sac_without_counter_mechanic(self, conn: sqlite3.Connection) -> None:
        """Marchesa-style commanders (trigger filter requires P1P1 counters)
        gain nothing from self-sac cards that have NO counter mechanic: a
        random self-sac artifact (Pizzasaur) without Persist/Undying/Modular/
        P1P1-PutCounter doesn't trigger Marchesa's recursion. Filter such
        cards out. True outlets (other-sac) are always kept."""
        ports = [
            _port(
                "Marchesa, the Black Rose",
                "trigger",
                "ChangesZone",
                valid_filter="Card.YouCtrl+counters_GE1_P1P1",
                zone_destination="Graveyard",
            ),
        ]
        # Pizzasaur: self-sac, no counter mechanic → skip
        # Viscera Seer: other-sac → keep
        # Birthing Pod: paid other-sac → keep
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Pizzasaur", "cost", "sacrifice", "self", "2 T Sac<1/CARDNAME/this creature>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Viscera Seer", "cost", "sacrifice", "any", "Sac<1/Creature>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Birthing Pod", "cost", "sacrifice", "any", "3 G Sac<1/Creature.YouCtrl>"),
        )
        conn.commit()
        results = _find_sacrifice_outlets(conn, ports, {"Marchesa, the Black Rose"})
        names = {r.candidate for r in results}
        assert "Pizzasaur" not in names
        assert "Viscera Seer" in names
        assert "Birthing Pod" in names

    def test_counters_filter_keeps_self_sac_with_persist(self, conn: sqlite3.Connection) -> None:
        """Glen Elendra Archmage has self-sac (counter target spell) AND
        Persist — when she dies she returns with a -1/-1 counter, and
        eventually combines with Marchesa's +1/+1 dethrone for permanent
        recursion. Self-sac cards WITH a counter mechanic must NOT be
        filtered out."""
        ports = [
            _port(
                "Marchesa, the Black Rose",
                "trigger",
                "ChangesZone",
                valid_filter="Card.YouCtrl+counters_GE1_P1P1",
                zone_destination="Graveyard",
            ),
        ]
        # Glen Elendra: self-sac + Persist keyword → keep despite self_sac
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            (
                "Glen Elendra Archmage",
                "cost",
                "sacrifice",
                "self",
                "1 U Sac<1/CARDNAME>",
            ),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            ("Glen Elendra Archmage", "keyword", "Persist", "Persist"),
        )
        # Iron Apprentice: self-sac (modular) + etbCounter:P1P1:1 → keep
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Iron Apprentice", "cost", "sacrifice", "self", "Sac<1/CARDNAME>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
            (
                "Iron Apprentice",
                "keyword",
                "etbCounter:P1P1:1",
                "etbCounter:P1P1:1",
            ),
        )
        conn.commit()
        results = _find_sacrifice_outlets(conn, ports, {"Marchesa, the Black Rose"})
        names = {r.candidate for r in results}
        assert "Glen Elendra Archmage" in names
        assert "Iron Apprentice" in names

    def test_meren_keeps_self_sac(self, conn: sqlite3.Connection) -> None:
        """Meren-style commanders (trigger fires when ANY creature you
        control dies, no counter requirement) DO benefit from self-sac
        cards: a Spore Frog or Sakura-Tribe Elder dying to its own ability
        triggers Meren's recursion. Self-sac must NOT be filtered out for
        commanders without the counters_GE_P1P1 filter."""
        ports = [
            _port(
                "Meren of Clan Nel Toth",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.Other+YouCtrl",
                zone_destination="Graveyard",
            ),
        ]
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Sakura-Tribe Elder", "cost", "sacrifice", "self", "Sac<1/CARDNAME>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Viscera Seer", "cost", "sacrifice", "any", "Sac<1/Creature>"),
        )
        conn.commit()
        results = _find_sacrifice_outlets(conn, ports, {"Meren of Clan Nel Toth"})
        names = {r.candidate for r in results}
        assert "Sakura-Tribe Elder" in names
        assert "Viscera Seer" in names

    def test_land_trigger_narrows_to_land_sacrifice(self, conn: sqlite3.Connection) -> None:
        """Titania triggers on ``Land.YouCtrl`` dying. Only candidates
        that sacrifice Lands (Sac<N/Land>) actually trigger her; the
        ~2000-card generic sacrifice pool is noise. The general rule
        must extract the subject type from the commander's trigger
        valid_filter and narrow candidates accordingly."""
        ports = [
            _port(
                "Titania, Protector of Argoth",
                "trigger",
                "ChangesZone",
                valid_filter="Land.YouCtrl",
                zone_destination="Graveyard",
            ),
        ]
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Zuran Orb", "cost", "sacrifice", "any", "Sac<1/Land>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Harrow", "cost", "sacrifice", "any", "2 G Sac<1/Land>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Carrion Feeder", "cost", "sacrifice", "any", "Sac<1/Creature>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Krark-Clan Ironworks", "cost", "sacrifice", "any", "Sac<1/Artifact>"),
        )
        conn.commit()
        results = _find_sacrifice_outlets(conn, ports, {"Titania, Protector of Argoth"})
        names = {r.candidate for r in results}
        assert "Zuran Orb" in names
        assert "Harrow" in names
        assert "Carrion Feeder" not in names
        assert "Krark-Clan Ironworks" not in names

    def test_creature_trigger_accepts_creature_and_untyped_sacrifice(self, conn: sqlite3.Connection) -> None:
        """Meren's ``Creature.YouCtrl`` trigger should accept both
        Sac<Creature> (exact match) and Sac<N/Permanent> / untyped
        sacrifices — anything that COULD be a creature sacrifice. But
        Sac<Land> and Sac<Artifact> explicitly exclude creatures, so
        they don't feed Meren's trigger and shouldn't match."""
        ports = [
            _port(
                "Meren",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.YouCtrl",
                zone_destination="Graveyard",
            ),
        ]
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Viscera Seer", "cost", "sacrifice", "any", "Sac<1/Creature>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Zuran Orb", "cost", "sacrifice", "any", "Sac<1/Land>"),
        )
        conn.commit()
        results = _find_sacrifice_outlets(conn, ports, {"Meren"})
        names = {r.candidate for r in results}
        assert "Viscera Seer" in names
        assert "Zuran Orb" not in names

    def test_broad_card_or_permanent_filter_accepts_all_sacrifices(self, conn: sqlite3.Connection) -> None:
        """A trigger with a too-broad subject (Card or Permanent) means
        ANY permanent sacrificing triggers it. Marchesa's
        ``Card.YouCtrl+counters_GE1_P1P1`` is broad on the subject axis
        (any card type with a counter) so all sacrifice targets remain
        eligible (further filtered by the counter-mechanic test)."""
        ports = [
            _port(
                "Marchesa, the Black Rose",
                "trigger",
                "ChangesZone",
                valid_filter="Card.YouCtrl+counters_GE1_P1P1",
                zone_destination="Graveyard",
            ),
        ]
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Viscera Seer", "cost", "sacrifice", "any", "Sac<1/Creature>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Zuran Orb", "cost", "sacrifice", "any", "Sac<1/Land>"),
        )
        conn.commit()
        results = _find_sacrifice_outlets(conn, ports, {"Marchesa, the Black Rose"})
        names = {r.candidate for r in results}
        assert "Viscera Seer" in names
        assert "Zuran Orb" in names

    def test_broad_cmdr_filter_keeps_all_sac_subjects(self, conn: sqlite3.Connection) -> None:
        """A commander with a generic ``Permanent`` filter imposes no
        subject constraint — Sac<Land>, Sac<Creature>, Sac<Artifact> all
        qualify."""
        ports = [
            _port(
                "BroadCmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Permanent.YouCtrl",
                zone_destination="Graveyard",
            ),
        ]
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Zuran Orb", "cost", "sacrifice", "any", "Sac<1/Land>"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, cost_target, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Viscera Seer", "cost", "sacrifice", "any", "Sac<1/Creature>"),
        )
        conn.commit()
        results = _find_sacrifice_outlets(conn, ports, {"BroadCmdr"})
        names = {r.candidate for r in results}
        assert "Zuran Orb" in names
        assert "Viscera Seer" in names


# ===========================================================================
# _find_subject_zone_feeders
# ===========================================================================


class TestFindSubjectZoneFeeders:
    """General rule: for a commander with a ChangesZone trigger whose
    valid_filter names a specific subject type (Land, Creature, Artifact,
    Zombie, ...), find candidates whose effect feeds the SAME zone
    transition for that subject. Two axes currently emit:

    - ``sac_type_effect``: ``effect=Sacrifice`` with ``SacValid=<subject>``
      (Scapeshift sacs all your Lands, Lotus Field's ETB-sac 2 Lands,
      mass creature-sac for Meren's filter).
    - ``mass_return_to_battlefield``: ``effect=ChangeZoneAll`` with
      ``ChangeType=<subject>``, ``Origin=Graveyard``,
      ``Destination=Battlefield`` (Splendid Reclamation for Lands,
      Living Death for Creatures).
    """

    def test_no_trigger_subject_skips(self, conn: sqlite3.Connection) -> None:
        """Commanders without a ChangesZone BF→GY trigger on a concrete
        subject (no trigger, or generic ``Card``/``Permanent`` filter)
        don't activate this rule."""
        _insert_port(conn, "Scapeshift", "effect", "Sacrifice", raw_line="{'SP': 'Sacrifice', 'SacValid': 'Land'}")
        ports = [_port("NoDeathTrigger", "trigger", "SpellCast")]
        assert _find_subject_zone_feeders(conn, ports, {"NoDeathTrigger"}) == []

    def test_land_trigger_matches_scapeshift_and_splendid(self, conn: sqlite3.Connection) -> None:
        """Titania's ``Land.YouCtrl`` trigger: Scapeshift's
        ``effect=Sacrifice SacValid=Land`` and Splendid Reclamation's
        ``ChangeZoneAll ChangeType=Land Graveyard→Battlefield`` both
        feed her land-dies axis."""
        ports = [
            _port(
                "Titania",
                "trigger",
                "ChangesZone",
                valid_filter="Land.YouCtrl",
                zone_destination="Graveyard",
            )
        ]
        _insert_port(conn, "Scapeshift", "effect", "Sacrifice", raw_line="{'SP': 'Sacrifice', 'SacValid': 'Land'}")
        _insert_port(
            conn,
            "Splendid Reclamation",
            "effect",
            "ChangeZoneAll",
            raw_line=(
                "{'SP': 'ChangeZoneAll', 'ChangeType': 'Land.YouCtrl',"
                " 'Origin': 'Graveyard', 'Destination': 'Battlefield'}"
            ),
        )
        _insert_port(
            conn,
            "Living Death",
            "effect",
            "ChangeZoneAll",
            raw_line=(
                "{'SP': 'ChangeZoneAll', 'ChangeType': 'Creature', 'Origin': 'Graveyard', 'Destination': 'Battlefield'}"
            ),
        )
        results = _find_subject_zone_feeders(conn, ports, {"Titania"})
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Scapeshift") == "sac_type_effect"
        assert events.get("Splendid Reclamation") == "mass_return_to_battlefield"
        assert "Living Death" not in events  # wrong subject

    def test_creature_trigger_rejects_mass_creature_return(self, conn: sqlite3.Connection) -> None:
        """Creature-subject commanders (Meren, Wilhelt, Slimefoot) do
        NOT surface mass-creature-return (Living Death) via this rule.
        Their individual-recursion toolkit (gy_loader, etb_sac_target)
        already covers per-creature return; mass reanimation is a
        different archetype and injecting it here regressed Hi-Syn on
        those commanders. Creature-subject commanders still receive
        ``sac_type_effect`` matches (Barter in Blood) because those
        genuinely drive their death-trigger count."""
        ports = [
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
            "Living Death",
            "effect",
            "ChangeZoneAll",
            raw_line=(
                "{'SP': 'ChangeZoneAll', 'ChangeType': 'Creature', 'Origin': 'Graveyard', 'Destination': 'Battlefield'}"
            ),
        )
        results = _find_subject_zone_feeders(conn, ports, {"Meren"})
        assert "Living Death" not in _candidates(results)

    def test_creature_subtype_trigger_rejected(self, conn: sqlite3.Connection) -> None:
        """Creature-subtype commanders (Wilhelt's Zombie, Slimefoot's
        Saproling) fall under the creature-family umbrella — they
        already have a rich per-creature toolkit (gy_loader,
        etb_sac_target, dies_drain) and don't need mass sac/return
        effects lifted by this rule. Returns empty."""
        ports = [
            _port(
                "Wilhelt",
                "trigger",
                "ChangesZone",
                valid_filter="Zombie.Other+YouCtrl",
                zone_destination="Graveyard",
            )
        ]
        _insert_port(
            conn, "Barter in Blood", "effect", "Sacrifice", raw_line="{'SP': 'Sacrifice', 'SacValid': 'Creature'}"
        )
        results = _find_subject_zone_feeders(conn, ports, {"Wilhelt"})
        assert results == []

    def test_changezoneall_without_graveyard_origin_skipped(self, conn: sqlite3.Connection) -> None:
        """``ChangeZoneAll Library→Battlefield`` (Warp World style) is
        a different axis — it doesn't return from graveyard. Must skip."""
        ports = [
            _port(
                "Titania",
                "trigger",
                "ChangesZone",
                valid_filter="Land.YouCtrl",
                zone_destination="Graveyard",
            )
        ]
        _insert_port(
            conn,
            "Warp World",
            "effect",
            "ChangeZoneAll",
            raw_line=(
                "{'SP': 'ChangeZoneAll', 'ChangeType': 'Land', 'Origin': 'Library', 'Destination': 'Battlefield'}"
            ),
        )
        results = _find_subject_zone_feeders(conn, ports, {"Titania"})
        assert "Warp World" not in _candidates(results)

    def test_excludes_commander(self, conn: sqlite3.Connection) -> None:
        ports = [
            _port(
                "Titania",
                "trigger",
                "ChangesZone",
                valid_filter="Land.YouCtrl",
                zone_destination="Graveyard",
            )
        ]
        _insert_port(conn, "Titania", "effect", "Sacrifice", raw_line="{'DB': 'Sacrifice', 'SacValid': 'Land'}")
        results = _find_subject_zone_feeders(conn, ports, {"Titania"})
        assert "Titania" not in _candidates(results)

    def test_opponent_forcing_sacrifice_rejected(self, conn: sqlite3.Connection) -> None:
        """Opponent-forcing sacrifice effects (``Defined: Opponent`` /
        ``Player.Opponent``) don't feed a YouCtrl-scoped trigger because
        the opponent's creatures, not yours, enter the graveyard. Use a
        Land-subject commander (Titania) since the rule is restricted
        to non-Creature subjects."""
        ports = [
            _port(
                "Titania",
                "trigger",
                "ChangesZone",
                valid_filter="Land.YouCtrl",
                zone_destination="Graveyard",
            )
        ]
        _insert_port(
            conn,
            "OppLandSac",
            "effect",
            "Sacrifice",
            raw_line="{'DB': 'Sacrifice', 'Defined': 'Opponent', 'SacValid': 'Land'}",
        )
        _insert_port(
            conn,
            "Scapeshift",
            "effect",
            "Sacrifice",
            raw_line="{'SP': 'Sacrifice', 'Defined': 'You', 'SacValid': 'Land'}",
        )
        _insert_port(
            conn,
            "Armageddon",
            "effect",
            "Sacrifice",
            raw_line="{'SP': 'Sacrifice', 'Defined': 'Player', 'SacValid': 'Land'}",
        )
        results = _find_subject_zone_feeders(conn, ports, {"Titania"})
        names = _candidates(results)
        assert "OppLandSac" not in names
        assert "Scapeshift" in names  # You-scoped
        assert "Armageddon" in names  # Player (each player)


def _candidates(results: list) -> set[str]:
    return {r.candidate for r in results}


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


# ===========================================================================
# _find_attack_payoffs
# ===========================================================================


def _attack_token_card(conn: sqlite3.Connection, card_name: str) -> None:
    """Insert an ``Attacks``-triggered ``Token``-producing card
    (Mardu Ascendancy shape)."""
    _insert_port(conn, card_name, "trigger", "Attacks", valid_filter="Creature.YouCtrl")
    _insert_port(conn, card_name, "effect", "Token")


class TestFindAttackPayoffs:
    def test_no_attacks_panharmonicon_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander without an Isshin-style Panharmonicon static for
        Attacks+Creature is gated out — even if it has an Attacks
        trigger of its own. That case is served by other rules."""
        _insert_card(conn, "Adeline")
        _attack_token_card(conn, "Adeline")
        # Commander has Attacks trigger but NOT a Panharmonicon static.
        ports = [
            _port("Edgar Markov", "trigger", "Attacks", valid_filter="Creature.YouCtrl"),
        ]
        assert _find_attack_payoffs(conn, ports, {"Edgar Markov"}) == []

    def test_isshin_panharmonicon_activates(self, conn: sqlite3.Connection) -> None:
        """Isshin's ``static: Panharmonicon`` with ``ValidMode: Attacks``
        and ``ValidCause: Creature`` fires the rule."""
        _insert_card(conn, "Mardu Ascendancy")
        _attack_token_card(conn, "Mardu Ascendancy")
        conn.commit()

        ports = [
            _port(
                "Isshin, Two Heavens as One",
                "static",
                "Panharmonicon",
                raw_line=(
                    "{'Mode':'Panharmonicon',"
                    " 'ValidMode':'Attacks,AttackersDeclared,AttackersDeclaredOneTarget',"
                    " 'ValidCard':'Permanent.YouCtrl',"
                    " 'ValidCause':'Creature'}"
                ),
            )
        ]
        results = _find_attack_payoffs(conn, ports, {"Isshin, Two Heavens as One"})
        names = {r.candidate for r in results}
        assert "Mardu Ascendancy" in names
        assert all(r.rule_id == "attack_payoffs" for r in results)

    def test_non_creature_cause_rejected(self, conn: sqlite3.Connection) -> None:
        """A Panharmonicon static with a non-Creature ``ValidCause``
        (e.g. land-dies doubler) doesn't qualify as an attack-trigger
        doubler."""
        _insert_card(conn, "Mardu Ascendancy")
        _attack_token_card(conn, "Mardu Ascendancy")
        conn.commit()

        ports = [
            _port(
                "LandDoubler",
                "static",
                "Panharmonicon",
                raw_line=("{'Mode':'Panharmonicon', 'ValidMode':'ChangesZone', 'ValidCause':'Land'}"),
            )
        ]
        assert _find_attack_payoffs(conn, ports, {"LandDoubler"}) == []

    def test_commander_excluded_from_results(self, conn: sqlite3.Connection) -> None:
        """Commander doesn't appear in its own recommendations even when
        it matches the candidate shape."""
        _insert_card(conn, "IsshinClone")
        # Same card is both commander and candidate shape.
        _attack_token_card(conn, "IsshinClone")
        _insert_port(
            conn,
            "IsshinClone",
            "static",
            "Panharmonicon",
            raw_line=("{'Mode':'Panharmonicon','ValidMode':'Attacks', 'ValidCause':'Creature'}"),
        )
        conn.commit()

        ports = [
            _port(
                "IsshinClone",
                "static",
                "Panharmonicon",
                raw_line=("{'Mode':'Panharmonicon','ValidMode':'Attacks', 'ValidCause':'Creature'}"),
            )
        ]
        results = _find_attack_payoffs(conn, ports, {"IsshinClone"})
        assert "IsshinClone" not in {r.candidate for r in results}

    def test_complement_metadata(self, conn: sqlite3.Connection) -> None:
        """Complements carry the expected rule_id / event pair."""
        _insert_card(conn, "Adeline")
        _attack_token_card(conn, "Adeline")
        conn.commit()

        ports = [
            _port(
                "Isshin",
                "static",
                "Panharmonicon",
                raw_line=("{'Mode':'Panharmonicon','ValidMode':'Attacks', 'ValidCause':'Creature'}"),
            )
        ]
        results = _find_attack_payoffs(conn, ports, {"Isshin"})
        adeline = next(r for r in results if r.candidate == "Adeline")
        assert adeline.rule_id == "attack_payoffs"
        assert adeline.direction == "synergy"
        assert adeline.cmdr_event == "creature_attacks"
        assert adeline.cand_event == "attack_payoff"
