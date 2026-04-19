"""Tests for token-related complement matchers.

Uses an in-memory SQLite database with minimal schema to exercise every
branch of the four functions in complement_rules.tokens:
  - _find_effect_feeds_etb
  - _find_token_producers_for_trigger
  - _find_static_strategy
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.tokens import (
    _find_effect_feeds_etb,
    _find_static_strategy,
    _find_token_producers_for_trigger,
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
    zone_origin: str = "",
    zone_destination: str = "",
    raw_line: str = "",
    affected_scope: str = "",
    branch_kind: str = "root",
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, "
        "valid_filter, zone_origin, zone_destination, raw_line, "
        "affected_scope, branch_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_name,
            port_type,
            event_class,
            valid_filter,
            zone_origin,
            zone_destination,
            raw_line,
            affected_scope,
            branch_kind,
        ),
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
        "INSERT OR IGNORE INTO cards (name, card_types, types, subtypes) VALUES (?, ?, ?, ?)",
        (name, card_types, types, subtypes),
    )


# ===========================================================================
# _find_effect_feeds_etb
# ===========================================================================


class TestFindEffectFeedsEtb:
    """Tests for _find_effect_feeds_etb."""

    def test_no_effect_ports_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander with only trigger ports produces no results."""
        ports = [_port("Cmdr", "trigger", "ChangesZone")]
        assert _find_effect_feeds_etb(conn, ports, {"Cmdr"}) == []

    def test_no_zone_effect_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander with a non-zone effect (e.g. DealDamage) produces no results."""
        ports = [_port("Cmdr", "effect", "DealDamage")]
        assert _find_effect_feeds_etb(conn, ports, {"Cmdr"}) == []

    def test_token_artifact_produces_artifact_etb(self, conn: sqlite3.Connection) -> None:
        """Token effect with artifact marker ('a' in parts[3]) -> line 85.

        Commander: creates artifact tokens (Treasure-style).
        Candidate: triggers on ChangesZone Artifact -> Battlefield.
        """
        # Commander makes artifact tokens: c_0_0_a_treasure
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "Token",
            raw_line="'TokenScript': 'c_0_0_a_treasure'",
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="Dragon")

        # Candidate triggers on Artifact entering
        _insert_port(
            conn,
            "Reckless Fireweaver",
            "trigger",
            "ChangesZone",
            valid_filter="Artifact.YouCtrl",
            zone_destination="Battlefield",
        )

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        assert any(r.candidate == "Reckless Fireweaver" for r in results)

    def test_token_creature_non_tribal_adds_creature_etb(self, conn: sqlite3.Connection) -> None:
        """Non-tribal token commander adds Creature ETB.

        The Locust God (not an Insect) makes Insect tokens -> line 92-93.
        """
        cmdr_port = _port(
            "The Locust God",
            "effect",
            "Token",
            raw_line="'TokenScript': 'u_1_1_insect_flying'",
        )
        _insert_card(conn, "The Locust God", card_types="Creature", subtypes="God")

        # Non-creature payoff (always a genuine payoff)
        _insert_port(
            conn,
            "Impact Tremors",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_card(conn, "Impact Tremors", card_types="Enchantment")

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"The Locust God"})
        assert any(r.candidate == "Impact Tremors" for r in results)

    def test_changezone_effect_extracts_base_type(self, conn: sqlite3.Connection) -> None:
        """ChangeZone effect with valid_filter extracts primary type -> line 102-103.

        Commander has a ChangeZone effect with Land filter and explicit zone_destination.
        """
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "ChangeZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="Elf")

        _insert_port(
            conn,
            "Lotus Cobra",
            "trigger",
            "ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
        )

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        assert any(r.candidate == "Lotus Cobra" for r in results)

    def test_candidate_empty_zone_dest_defaults_to_any(self, conn: sqlite3.Connection) -> None:
        """Candidate with empty zone_destination defaults to 'Any' -> line 132-133."""
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "Token",
            raw_line="'TokenScript': 'u_1_1_insect_flying'",
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="God")

        # Candidate trigger with empty zone_destination (defaults to "Any")
        _insert_port(
            conn,
            "Warstorm Surge",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="",
        )
        _insert_card(conn, "Warstorm Surge", card_types="Enchantment")

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        # "Any" matches any cmdr_zd, so this should match
        assert any(r.candidate == "Warstorm Surge" for r in results)

    def test_too_broad_base_skipped(self, conn: sqlite3.Connection) -> None:
        """Candidate with 'Card' or 'Permanent' base type is skipped -> line 138-139."""
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "Token",
            raw_line="'TokenScript': 'u_1_1_insect_flying'",
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="God")

        # Candidate with too-broad filter
        _insert_port(
            conn,
            "Broad Card",
            "trigger",
            "ChangesZone",
            valid_filter="Card.YouCtrl",
            zone_destination="Battlefield",
        )

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        assert not any(r.candidate == "Broad Card" for r in results)

    def test_changezone_effect_empty_zone_defaults_battlefield(self, conn: sqlite3.Connection) -> None:
        """ChangeZone effect with empty zone_destination defaults to Battlefield -> line 99."""
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "ChangeZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="",  # empty -> defaults to "Battlefield"
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="Elf")

        _insert_port(
            conn,
            "ETB Payoff",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.Other+YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_card(conn, "ETB Payoff", card_types="Enchantment")

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        assert any(r.candidate == "ETB Payoff" for r in results)

    def test_self_only_trigger_candidate_skipped(self, conn: sqlite3.Connection) -> None:
        """Candidate with Card.Self filter is skipped -> line 129-130."""
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "Token",
            raw_line="'TokenScript': 'u_1_1_insect_flying'",
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="God")

        # Candidate with self-only trigger
        _insert_port(
            conn,
            "Self Only Card",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        assert not any(r.candidate == "Self Only Card" for r in results)

    def test_duplicate_candidate_deduplicated(self, conn: sqlite3.Connection) -> None:
        """Same candidate from multiple trigger rows is only returned once -> line 126-127."""
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "Token",
            raw_line="'TokenScript': 'u_1_1_insect_flying'",
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="God")

        # Two trigger ports for the same card
        _insert_port(
            conn,
            "Double Trigger",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.Other+YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_port(
            conn,
            "Double Trigger",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_card(conn, "Double Trigger", card_types="Enchantment")

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        matches = [r for r in results if r.candidate == "Double Trigger"]
        assert len(matches) == 1

    def test_zone_mismatch_skipped(self, conn: sqlite3.Connection) -> None:
        """Candidate with mismatched zone is skipped -> line 144-145."""
        cmdr_port = _port(
            "Cmdr",
            "effect",
            "ChangeZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_card(conn, "Cmdr", card_types="Creature", subtypes="Elf")

        # Candidate triggers on Creature entering Graveyard, not Battlefield
        _insert_port(
            conn,
            "Wrong Zone Card",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="Graveyard",
        )

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"Cmdr"})
        assert not any(r.candidate == "Wrong Zone Card" for r in results)

    def test_creature_other_gate_skips_creature_without_other(self, conn: sqlite3.Connection) -> None:
        """Non-tribal token cmdr: creature triggers without 'Other' are skipped -> line 151-157.

        Non-creature cards (enchantments) are always kept.
        Creature cards without 'Other' in filter are skipped when
        _needs_creature_other_gate is True.
        """
        cmdr_port = _port(
            "The Locust God",
            "effect",
            "Token",
            raw_line="'TokenScript': 'u_1_1_insect_flying'",
        )
        _insert_card(conn, "The Locust God", card_types="Creature", subtypes="God")

        # Creature without "Other" in filter -> should be skipped
        _insert_port(
            conn,
            "Soul Warden",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_card(conn, "Soul Warden", card_types="Creature", subtypes="Human Cleric")

        # Creature WITH "Other" in filter -> should be kept
        _insert_port(
            conn,
            "Soul's Attendant",
            "trigger",
            "ChangesZone",
            valid_filter="Creature.Other+YouCtrl",
            zone_destination="Battlefield",
        )
        _insert_card(conn, "Soul's Attendant", card_types="Creature", subtypes="Human Cleric")

        results = _find_effect_feeds_etb(conn, [cmdr_port], {"The Locust God"})
        candidates = {r.candidate for r in results}
        assert "Soul Warden" not in candidates
        assert "Soul's Attendant" in candidates


# ===========================================================================
# _find_token_producers_for_trigger
# ===========================================================================


class TestFindTokenProducersForTrigger:
    """Tests for _find_token_producers_for_trigger."""

    def test_no_trigger_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander with no triggers produces no results."""
        ports = [_port("Cmdr", "effect", "Token")]
        assert _find_token_producers_for_trigger(conn, ports, {"Cmdr"}) == []

    def test_non_battlefield_zone_skipped(self, conn: sqlite3.Connection) -> None:
        """ChangesZone trigger with zone != Battlefield is skipped -> line 211-212."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.YouCtrl",
                zone_destination="Graveyard",
            )
        ]
        assert _find_token_producers_for_trigger(conn, ports, {"Cmdr"}) == []

    def test_no_token_filter_skipped(self, conn: sqlite3.Connection) -> None:
        """Trigger with !token in filter is skipped -> line 213-214."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.!token+YouCtrl",
                zone_destination="Battlefield",
            )
        ]
        assert _find_token_producers_for_trigger(conn, ports, {"Cmdr"}) == []

    def test_purphoros_finds_token_producers(self, conn: sqlite3.Connection) -> None:
        """Purphoros (Creature ETB trigger) finds token producers."""
        ports = [
            _port(
                "Purphoros",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.Other+YouCtrl",
                zone_destination="Battlefield",
            )
        ]

        _insert_port(conn, "Krenko, Mob Boss", "effect", "Token")
        _insert_port(conn, "Lightning Bolt", "effect", "DealDamage")

        results = _find_token_producers_for_trigger(conn, ports, {"Purphoros"})
        assert len(results) == 1
        assert results[0].candidate == "Krenko, Mob Boss"
        assert results[0].rule_id == "token_producer"

    def test_self_only_trigger_skipped(self, conn: sqlite3.Connection) -> None:
        """Self-only trigger (Card.Self) is skipped -> line 209-210."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Card.Self",
                zone_destination="Battlefield",
            )
        ]
        assert _find_token_producers_for_trigger(conn, ports, {"Cmdr"}) == []

    def test_non_changezone_trigger_skipped(self, conn: sqlite3.Connection) -> None:
        """Non-ChangesZone trigger is skipped -> line 205-206."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "Sacrificed",
                valid_filter="Creature.YouCtrl",
                zone_destination="Battlefield",
            )
        ]
        assert _find_token_producers_for_trigger(conn, ports, {"Cmdr"}) == []

    def test_non_creature_type_alone_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Trigger on only non-Creature type (e.g. Artifact) returns empty
        because the function requires 'Creature' in wanted_types -> line 221."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Artifact.YouCtrl",
                zone_destination="Battlefield",
            )
        ]
        assert _find_token_producers_for_trigger(conn, ports, {"Cmdr"}) == []

    def test_commander_excluded_from_results(self, conn: sqlite3.Connection) -> None:
        """Commander's own cards are excluded from results."""
        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.Other+YouCtrl",
                zone_destination="Battlefield",
            )
        ]
        _insert_port(conn, "Cmdr", "effect", "Token")
        _insert_port(conn, "Other Card", "effect", "Token")

        results = _find_token_producers_for_trigger(conn, ports, {"Cmdr"})
        candidates = {r.candidate for r in results}
        assert "Cmdr" not in candidates
        assert "Other Card" in candidates


# ===========================================================================
# _find_static_strategy
# ===========================================================================


class TestFindStaticStrategy:
    """Tests for _find_static_strategy."""

    def test_no_static_ports_returns_empty(self, conn: sqlite3.Connection) -> None:
        """Commander with no relevant statics or keywords returns empty."""
        ports = [_port("Cmdr", "trigger", "ChangesZone")]
        assert _find_static_strategy(conn, ports, {"Cmdr"}) == []

    def test_go_wide_creature_pump_finds_tokens(self, conn: sqlite3.Connection) -> None:
        """Continuous static with AddPower on Creature.YouCtrl -> lines 279-283, 294-301.

        Jetmir-style go-wide pump finds token producers.
        """
        ports = [
            _port(
                "Jetmir",
                "static",
                "Continuous",
                affected_scope="Creature.YouCtrl",
                raw_line="'Creature.YouCtrl' 'AddPower' '2'",
            )
        ]

        _insert_port(conn, "Krenko, Mob Boss", "effect", "Token")
        _insert_port(conn, "Lightning Bolt", "effect", "DealDamage")

        results = _find_static_strategy(conn, ports, {"Jetmir"})
        assert len(results) == 1
        assert results[0].candidate == "Krenko, Mob Boss"
        assert results[0].rule_id == "token_producer"
        assert results[0].cmdr_event == "creature_pump"

    def test_go_wide_add_toughness_also_matches(self, conn: sqlite3.Connection) -> None:
        """AddToughness also triggers the go-wide path -> line 281."""
        ports = [
            _port(
                "Cmdr",
                "static",
                "Continuous",
                raw_line="Creature.YouCtrl 'AddToughness' '1'",
            )
        ]

        _insert_port(conn, "Token Maker", "effect", "Token")

        results = _find_static_strategy(conn, ports, {"Cmdr"})
        assert len(results) == 1
        assert results[0].candidate == "Token Maker"

    def test_voltron_hexproof_finds_auras_equipment(self, conn: sqlite3.Connection) -> None:
        """Hexproof keyword triggers voltron path -> lines 287, 313-320."""
        ports = [_port("Sigarda", "keyword", "Hexproof")]

        _insert_card(conn, "Ethereal Armor", subtypes="Aura")
        _insert_card(conn, "Sword of Fire and Ice", subtypes="Equipment")
        _insert_card(conn, "Lightning Bolt")  # not an Aura/Equipment

        results = _find_static_strategy(conn, ports, {"Sigarda"})
        candidates = {r.candidate for r in results}
        assert "Ethereal Armor" in candidates
        assert "Sword of Fire and Ice" in candidates
        assert "Lightning Bolt" not in candidates
        for r in results:
            assert r.rule_id == "voltron"
            assert r.cmdr_event == "self_protection"
            assert r.cand_event == "Aura_Equipment"

    def test_voltron_exalted_finds_equipment(self, conn: sqlite3.Connection) -> None:
        """Exalted keyword also triggers voltron -> line 287."""
        ports = [_port("Rafiq", "keyword", "Exalted")]

        _insert_card(conn, "Bonesplitter", subtypes="Equipment")

        results = _find_static_strategy(conn, ports, {"Rafiq"})
        assert len(results) == 1
        assert results[0].candidate == "Bonesplitter"

    def test_voltron_shroud_finds_equipment(self, conn: sqlite3.Connection) -> None:
        """Shroud keyword also triggers voltron -> line 287."""
        ports = [_port("Cmdr", "keyword", "Shroud")]

        _insert_card(conn, "Bonesplitter", subtypes="Equipment")

        results = _find_static_strategy(conn, ports, {"Cmdr"})
        assert len(results) == 1

    def test_commander_excluded_from_voltron(self, conn: sqlite3.Connection) -> None:
        """Commander's own name excluded from voltron results."""
        ports = [_port("Sigarda", "keyword", "Hexproof")]

        _insert_card(conn, "Sigarda", subtypes="Aura")
        _insert_card(conn, "Other Aura", subtypes="Aura")

        results = _find_static_strategy(conn, ports, {"Sigarda"})
        candidates = {r.candidate for r in results}
        assert "Sigarda" not in candidates
        assert "Other Aura" in candidates

    def test_go_wide_excludes_commander(self, conn: sqlite3.Connection) -> None:
        """Commander's own name excluded from go-wide results -> line 299."""
        ports = [
            _port(
                "Jetmir",
                "static",
                "Continuous",
                affected_scope="Creature.YouCtrl",
                raw_line="'Creature.YouCtrl' 'AddPower' '2'",
            )
        ]

        _insert_port(conn, "Jetmir", "effect", "Token")
        _insert_port(conn, "Other Token Maker", "effect", "Token")

        results = _find_static_strategy(conn, ports, {"Jetmir"})
        candidates = {r.candidate for r in results}
        assert "Jetmir" not in candidates
        assert "Other Token Maker" in candidates

    def test_keyword_haste_not_voltron(self, conn: sqlite3.Connection) -> None:
        """Non-voltron keywords (Haste, Indestructible) don't trigger voltron."""
        ports = [_port("Cmdr", "keyword", "Haste")]
        _insert_card(conn, "Bonesplitter", subtypes="Equipment")
        assert _find_static_strategy(conn, ports, {"Cmdr"}) == []

    def test_continuous_without_pump_not_go_wide(self, conn: sqlite3.Connection) -> None:
        """Continuous static without AddPower/AddToughness doesn't trigger go-wide."""
        ports = [
            _port(
                "Cmdr",
                "static",
                "Continuous",
                affected_scope="Creature.YouCtrl",
                raw_line="'Creature.YouCtrl' 'AddKeyword' 'Haste'",
            )
        ]
        _insert_port(conn, "Token Maker", "effect", "Token")
        assert _find_static_strategy(conn, ports, {"Cmdr"}) == []


# ---------------------------------------------------------------------------
# _find_static_strategy — exalted_density sub-rule
# ---------------------------------------------------------------------------


class TestExaltedDensity:
    """The exalted_density sub-rule inside ``_find_static_strategy``."""

    def test_exalted_commander_finds_other_exalted(self, conn: sqlite3.Connection) -> None:
        """Rafiq-style Exalted commander surfaces every Exalted creature."""
        ports = [_port("Rafiq", "keyword", "Exalted")]
        _insert_card(conn, "Rafiq")
        _insert_card(conn, "Qasali Pridemage")
        _insert_card(conn, "Sublime Archangel")
        _insert_card(conn, "Finest Hour")
        _insert_port(conn, "Qasali Pridemage", "keyword", "Exalted")
        _insert_port(conn, "Sublime Archangel", "keyword", "Exalted")
        _insert_port(conn, "Finest Hour", "keyword", "Exalted")

        results = _find_static_strategy(conn, ports, {"Rafiq"})
        ex = [r for r in results if r.rule_id == "exalted_density"]
        names = {r.candidate for r in ex}
        assert "Qasali Pridemage" in names
        assert "Sublime Archangel" in names
        assert "Finest Hour" in names
        assert "Rafiq" not in names
        for r in ex:
            assert r.cmdr_event == "exalted"
            assert r.cand_event == "exalted_keyword"

    def test_non_exalted_commander_no_density(self, conn: sqlite3.Connection) -> None:
        """Hexproof voltron commander triggers the generic voltron pool
        but NOT exalted_density."""
        ports = [_port("Sigarda", "keyword", "Hexproof")]
        _insert_card(conn, "Qasali Pridemage")
        _insert_port(conn, "Qasali Pridemage", "keyword", "Exalted")

        results = _find_static_strategy(conn, ports, {"Sigarda"})
        assert [r for r in results if r.rule_id == "exalted_density"] == []


# ---------------------------------------------------------------------------
# _find_static_strategy — aura_equipment_support sub-rule
# ---------------------------------------------------------------------------


class TestAuraEquipmentSupport:
    """The aura_equipment_support sub-rule."""

    def test_equipment_scaling_commander_matches(self, conn: sqlite3.Connection) -> None:
        """Wyleth-style commander (scales_with Equipment.Attached)
        surfaces Sigarda's-Aid / Sram / Puresteel-Paladin-style enabler
        cards via the SpellCast and ReduceCost port shapes."""
        ports = [
            _port(
                "Wyleth",
                "scales_with",
                "Valid",
                valid_filter="Equipment.Attached,Aura.Attached",
            )
        ]
        _insert_card(conn, "Sram")
        _insert_port(
            conn,
            "Sram",
            "trigger",
            "SpellCast",
            valid_filter="Aura,Equipment,Vehicle",
        )
        _insert_card(conn, "Danitha")
        _insert_port(
            conn,
            "Danitha",
            "static",
            "ReduceCost",
            raw_line="{'Mode':'ReduceCost','ValidCard':'Aura,Equipment'}",
        )

        results = _find_static_strategy(conn, ports, {"Wyleth"})
        support = [r for r in results if r.rule_id == "aura_equipment_support"]
        names = {r.candidate for r in support}
        assert "Sram" in names
        assert "Danitha" in names

    def test_aura_only_scaling_rejected(self, conn: sqlite3.Connection) -> None:
        """Uril-style pure-Aura ``scales_with Aura.Attached`` does NOT
        activate the rule. The gate requires Equipment mention because
        the support pool is Equipment-heavy; pure-Aura commanders are
        better served by the generic voltron pool."""
        ports = [
            _port(
                "Uril",
                "scales_with",
                "Valid",
                valid_filter="Aura.Attached/Times.2",
            )
        ]
        _insert_card(conn, "Sram")
        _insert_port(
            conn,
            "Sram",
            "trigger",
            "SpellCast",
            valid_filter="Aura,Equipment,Vehicle",
        )

        results = _find_static_strategy(conn, ports, {"Uril"})
        assert [r for r in results if r.rule_id == "aura_equipment_support"] == []

    def test_commander_excluded(self, conn: sqlite3.Connection) -> None:
        """Commander never appears in its own aura_equipment_support
        results even if it matches the candidate shape."""
        ports = [
            _port(
                "Self",
                "scales_with",
                "Valid",
                valid_filter="Equipment.Attached,Aura.Attached",
            )
        ]
        _insert_card(conn, "Self")
        _insert_port(
            conn,
            "Self",
            "trigger",
            "SpellCast",
            valid_filter="Aura,Equipment",
        )

        results = _find_static_strategy(conn, ports, {"Self"})
        assert "Self" not in {r.candidate for r in results}
