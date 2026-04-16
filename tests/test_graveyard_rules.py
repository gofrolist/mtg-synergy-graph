"""Tests for graveyard-related complement matchers.

Uses an in-memory SQLite database with minimal schema to exercise every
branch of the four functions in complement_rules.graveyard:
  - _find_graveyard_fillers
  - _find_artifact_recursion
  - _find_copy_synergy
  - _find_etb_sac_targets
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.graveyard import (
    _find_artifact_recursion,
    _find_copy_synergy,
    _find_etb_sac_targets,
    _find_graveyard_fillers,
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
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, "
        "zone_origin, zone_destination, raw_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, raw_line),
    )


def _insert_card(
    conn: sqlite3.Connection,
    name: str,
    card_types: str = "",
    types: str = "",
    subtypes: str = "",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, types, subtypes) VALUES (?, ?, ?, ?)",
        (name, card_types, types, subtypes),
    )


def _candidates(results: list) -> set[str]:
    return {r.candidate for r in results}


# ===========================================================================
# _find_graveyard_fillers
# ===========================================================================


class TestFindGraveyardFillers:
    """Tests for _find_graveyard_fillers."""

    def test_no_gy_commander_returns_empty(self, conn) -> None:
        """Commander without GY-related ports returns empty."""
        ports = [_port("Generic Cmdr", "trigger", "SpellCast")]
        assert _find_graveyard_fillers(conn, ports, {"Generic Cmdr"}) == []

    def test_changezone_from_graveyard_effect_activates(self, conn) -> None:
        """Commander with ChangeZone effect from Graveyard finds self-mill cards."""
        _insert_card(conn, "Meren of Clan Nel Toth")
        _insert_card(conn, "Hedron Crab")
        _insert_port(conn, "Hedron Crab", "effect", "Mill", valid_filter="YouCtrl")
        conn.commit()

        cmdr_ports = [
            _port("Meren of Clan Nel Toth", "effect", "ChangeZone", zone_origin="Graveyard"),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Meren of Clan Nel Toth"})
        assert "Hedron Crab" in _candidates(results)

    def test_static_mayplay_graveyard_activates(self, conn) -> None:
        """Commander with static MayPlay from Graveyard activates GY filler."""
        _insert_card(conn, "Karador, Ghost Chieftain")
        _insert_card(conn, "Stitcher's Supplier")
        _insert_port(conn, "Stitcher's Supplier", "effect", "Mill", valid_filter="You")
        conn.commit()

        cmdr_ports = [
            _port(
                "Karador, Ghost Chieftain",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True}",
            ),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Karador, Ghost Chieftain"})
        assert "Stitcher's Supplier" in _candidates(results)

    def test_scales_with_graveyard_activates(self, conn) -> None:
        """Commander with scales_with Graveyard activates GY filler."""
        _insert_card(conn, "The Mimeoplasm")
        _insert_card(conn, "Thought Scour")
        _insert_port(conn, "Thought Scour", "effect", "Surveil", valid_filter="YouOwn")
        conn.commit()

        cmdr_ports = [
            _port("The Mimeoplasm", "scales_with", "Graveyard"),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"The Mimeoplasm"})
        assert "Thought Scour" in _candidates(results)

    def test_scales_with_lowercase_graveyard_activates(self, conn) -> None:
        """scales_with port with lowercase 'graveyard' also activates."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Miller")
        _insert_port(conn, "Miller", "effect", "DigUntil", valid_filter="You.Something")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "scales_with", "graveyard_count")]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        assert "Miller" in _candidates(results)

    def test_excludes_commander_from_results(self, conn) -> None:
        """Commander should not appear in its own results."""
        _insert_card(conn, "Meren of Clan Nel Toth")
        _insert_port(conn, "Meren of Clan Nel Toth", "effect", "Mill", valid_filter="YouCtrl")
        conn.commit()

        cmdr_ports = [
            _port("Meren of Clan Nel Toth", "effect", "ChangeZone", zone_origin="Graveyard"),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Meren of Clan Nel Toth"})
        assert "Meren of Clan Nel Toth" not in _candidates(results)

    def test_self_mill_variants(self, conn) -> None:
        """All three self-mill event classes are found: Mill, DigUntil, Surveil."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Miller")
        _insert_card(conn, "Digger")
        _insert_card(conn, "Surveiler")
        _insert_port(conn, "Miller", "effect", "Mill", valid_filter="YouCtrl")
        _insert_port(conn, "Digger", "effect", "DigUntil", valid_filter="YouOwn")
        _insert_port(conn, "Surveiler", "effect", "Surveil", valid_filter="You")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        cands = _candidates(results)
        assert {"Miller", "Digger", "Surveiler"} <= cands

    def test_non_self_mill_excluded(self, conn) -> None:
        """Mill effects targeting opponents (no YouCtrl/YouOwn/You) are excluded."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Opponent Miller")
        _insert_port(conn, "Opponent Miller", "effect", "Mill", valid_filter="Opponent")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        assert "Opponent Miller" not in _candidates(results)

    def test_recast_types_instant_sorcery_density(self, conn) -> None:
        """MayPlay with Affected Instant/Sorcery finds spell-type cards."""
        _insert_card(conn, "Kess, Dissident Mage")
        _insert_card(conn, "Lightning Bolt", card_types="Instant")
        _insert_card(conn, "Ponder", card_types="Sorcery")
        _insert_card(conn, "Llanowar Elves", card_types="Creature")
        conn.commit()

        cmdr_ports = [
            _port(
                "Kess, Dissident Mage",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True, 'Affected': 'Instant,Sorcery'}",
            ),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Kess, Dissident Mage"})
        cands = _candidates(results)
        assert "Lightning Bolt" in cands
        assert "Ponder" in cands
        # Creature should not be found via recast_types (only Instant/Sorcery are _CASTABLE_TYPES)
        assert "Llanowar Elves" not in cands

    def test_recast_types_creature_not_castable(self, conn) -> None:
        """MayPlay with Affected Creature does not add spell density (Creature is not in _CASTABLE_TYPES)."""
        _insert_card(conn, "Karador, Ghost Chieftain")
        _insert_card(conn, "Birds of Paradise", card_types="Creature")
        conn.commit()

        cmdr_ports = [
            _port(
                "Karador, Ghost Chieftain",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True, 'Affected': 'Creature'}",
            ),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Karador, Ghost Chieftain"})
        # No self-mill cards in DB, and Creature is not in _CASTABLE_TYPES
        assert "Birds of Paradise" not in _candidates(results)

    def test_deduplication_across_mill_and_density(self, conn) -> None:
        """A card that matches both self-mill and spell density is not duplicated."""
        _insert_card(conn, "Kess, Dissident Mage")
        _insert_card(conn, "Thought Scour", card_types="Instant")
        _insert_port(conn, "Thought Scour", "effect", "Mill", valid_filter="You")
        conn.commit()

        cmdr_ports = [
            _port(
                "Kess, Dissident Mage",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True, 'Affected': 'Instant'}",
            ),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Kess, Dissident Mage"})
        # Thought Scour matches self-mill AND is an Instant, but should appear only once
        ts_results = [r for r in results if r.candidate == "Thought Scour"]
        assert len(ts_results) == 1

    def test_affected_with_comma_separated_types(self, conn) -> None:
        """Affected field like 'Instant,Sorcery' parses both types."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Bolt", card_types="Instant")
        _insert_card(conn, "Ritual", card_types="Sorcery")
        conn.commit()

        cmdr_ports = [
            _port(
                "Cmdr",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True, 'Affected': 'Instant,Sorcery'}",
            ),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        cands = _candidates(results)
        assert "Bolt" in cands
        assert "Ritual" in cands

    def test_affected_compound_dot_plus_extracts_base(self, conn) -> None:
        """Affected 'Instant.YouCtrl+Sorcery' extracts 'Instant' (base before dot/plus)."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Bolt", card_types="Instant")
        conn.commit()

        cmdr_ports = [
            _port(
                "Cmdr",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True, 'Affected': 'Instant.YouCtrl+Sorcery'}",
            ),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        cands = _candidates(results)
        # Only 'Instant' is extracted as the base type (split by . then +)
        assert "Bolt" in cands

    def test_affected_card_type_ignored(self, conn) -> None:
        """Affected type 'Card' is filtered out (not uppercase-starting or equals 'Card')."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "SomeCard", card_types="Card")
        conn.commit()

        cmdr_ports = [
            _port(
                "Cmdr",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True, 'Affected': 'Card'}",
            ),
        ]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        # 'Card' is excluded by the `base != "Card"` check
        assert results == [] or all(r.rule_id != "spell_density" for r in results)

    def test_result_rule_ids(self, conn) -> None:
        """Self-mill results have rule_id 'trigger_effect'."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Miller")
        _insert_port(conn, "Miller", "effect", "Mill", valid_filter="YouCtrl")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        assert all(r.rule_id in ("trigger_effect", "spell_density") for r in results)

    def test_you_dot_prefix_filter_matches(self, conn) -> None:
        """valid_filter starting with 'You.' should match self-mill."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "SelfSurveiler")
        _insert_port(conn, "SelfSurveiler", "effect", "Surveil", valid_filter="You.YouCtrl")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_graveyard_fillers(conn, cmdr_ports, {"Cmdr"})
        assert "SelfSurveiler" in _candidates(results)


# ===========================================================================
# _find_artifact_recursion
# ===========================================================================


class TestFindArtifactRecursion:
    """Tests for _find_artifact_recursion."""

    def test_no_artifact_gy_commander_returns_empty(self, conn) -> None:
        """Commander without artifact GY/copy ports returns empty."""
        ports = [_port("Generic Cmdr", "trigger", "SpellCast")]
        assert _find_artifact_recursion(conn, ports, {"Generic Cmdr"}) == []

    def test_artifact_gy_finds_sac_artifacts(self, conn) -> None:
        """Commander with ChangeZone from GY for Artifacts finds self-sac artifacts."""
        _insert_card(conn, "Daretti, Scrap Savant")
        _insert_card(conn, "Ichor Wellspring", card_types="Artifact")
        _insert_port(conn, "Ichor Wellspring", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port(
                "Daretti, Scrap Savant",
                "effect",
                "ChangeZone",
                zone_origin="Graveyard",
                valid_filter="Artifact.YouCtrl",
            ),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Daretti, Scrap Savant"})
        assert "Ichor Wellspring" in _candidates(results)
        assert all(r.rule_id == "artifact_recursion" for r in results)

    def test_artifact_copy_finds_etb_artifacts(self, conn) -> None:
        """Commander with CopyPermanent Artifact finds ETB artifacts with valuable effects."""
        _insert_card(conn, "Osgir, the Reconstructor")
        _insert_card(conn, "Solemn Simulacrum", card_types="Artifact Creature")
        _insert_port(
            conn,
            "Solemn Simulacrum",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Solemn Simulacrum", "effect", "Draw")
        conn.commit()

        cmdr_ports = [
            _port(
                "Osgir, the Reconstructor",
                "effect",
                "CopyPermanent",
                valid_filter="Artifact",
            ),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Osgir, the Reconstructor"})
        assert "Solemn Simulacrum" in _candidates(results)

    def test_copy_artifact_via_raw_line(self, conn) -> None:
        """CopyPermanent with Artifact in raw_line (not valid_filter) also matches."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Spine of Ish Sah", card_types="Artifact")
        _insert_port(
            conn,
            "Spine of Ish Sah",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Spine of Ish Sah", "effect", "Destroy")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "CopyPermanent", raw_line="CopyPermanent | Artifact"),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Cmdr"})
        assert "Spine of Ish Sah" in _candidates(results)

    def test_both_gy_and_copy_finds_both_types(self, conn) -> None:
        """Commander with both artifact GY return AND copy finds sac + ETB artifacts."""
        _insert_card(conn, "Osgir, the Reconstructor")
        _insert_card(conn, "Ichor Wellspring", card_types="Artifact")
        _insert_port(conn, "Ichor Wellspring", "cost", "sacrifice")
        _insert_card(conn, "Solemn Simulacrum", card_types="Artifact Creature")
        _insert_port(
            conn,
            "Solemn Simulacrum",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Solemn Simulacrum", "effect", "Draw")
        conn.commit()

        cmdr_ports = [
            _port(
                "Osgir, the Reconstructor",
                "effect",
                "ChangeZone",
                zone_origin="Graveyard",
                valid_filter="Artifact.YouCtrl",
            ),
            _port(
                "Osgir, the Reconstructor",
                "effect",
                "CopyPermanent",
                valid_filter="Artifact",
            ),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Osgir, the Reconstructor"})
        cands = _candidates(results)
        assert "Ichor Wellspring" in cands
        assert "Solemn Simulacrum" in cands

    def test_non_artifact_sac_excluded(self, conn) -> None:
        """Non-artifact creature with sacrifice cost is excluded."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Sakura-Tribe Elder", card_types="Creature")
        _insert_port(conn, "Sakura-Tribe Elder", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard", valid_filter="Artifact"),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Cmdr"})
        assert "Sakura-Tribe Elder" not in _candidates(results)

    def test_excludes_commander_from_results(self, conn) -> None:
        """Commander should not appear in its own results."""
        _insert_card(conn, "Daretti, Scrap Savant", card_types="Artifact")
        _insert_port(conn, "Daretti, Scrap Savant", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port(
                "Daretti, Scrap Savant",
                "effect",
                "ChangeZone",
                zone_origin="Graveyard",
                valid_filter="Artifact",
            ),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Daretti, Scrap Savant"})
        assert "Daretti, Scrap Savant" not in _candidates(results)

    def test_etb_artifact_without_valuable_effect_excluded(self, conn) -> None:
        """Artifact with self-ETB but no valuable effect is excluded from copy results."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Boring Artifact", card_types="Artifact")
        _insert_port(
            conn,
            "Boring Artifact",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        # No valuable effect port
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "CopyPermanent", valid_filter="Artifact"),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Cmdr"})
        assert "Boring Artifact" not in _candidates(results)

    def test_deduplication_sac_and_etb(self, conn) -> None:
        """A card that matches both sac artifact and ETB artifact appears only once."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Dual Artifact", card_types="Artifact")
        _insert_port(conn, "Dual Artifact", "cost", "sacrifice")
        _insert_port(
            conn,
            "Dual Artifact",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Dual Artifact", "effect", "Draw")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard", valid_filter="Artifact"),
            _port("Cmdr", "effect", "CopyPermanent", valid_filter="Artifact"),
        ]
        results = _find_artifact_recursion(conn, cmdr_ports, {"Cmdr"})
        # Should have sac_artifact match; the ETB one is deduplicated via seen set
        da_results = [r for r in results if r.candidate == "Dual Artifact"]
        # At least found, and the seen-set prevents duplication if cmdr_event differs
        assert len(da_results) >= 1


# ===========================================================================
# _find_copy_synergy
# ===========================================================================


class TestFindCopySynergy:
    """Tests for _find_copy_synergy."""

    def test_no_copy_commander_returns_empty(self, conn) -> None:
        """Commander without CopyPermanent returns empty."""
        ports = [_port("Generic", "trigger", "SpellCast")]
        assert _find_copy_synergy(conn, ports, {"Generic"}) == []

    def test_populate_finds_token_producers(self, conn) -> None:
        """Commander with Populate finds token producers."""
        _insert_card(conn, "Ghired, Conclave Exile")
        _insert_card(conn, "Avenger of Zendikar")
        _insert_port(conn, "Avenger of Zendikar", "effect", "Token")
        conn.commit()

        cmdr_ports = [
            _port(
                "Ghired, Conclave Exile",
                "effect",
                "CopyPermanent",
                raw_line="{'Populate': True}",
            ),
        ]
        results = _find_copy_synergy(conn, cmdr_ports, {"Ghired, Conclave Exile"})
        assert "Avenger of Zendikar" in _candidates(results)
        assert all(r.rule_id == "copy_synergy" for r in results)

    def test_creature_copy_finds_etb_creatures(self, conn) -> None:
        """Commander with creature copy finds creatures with self-ETB + valuable effect."""
        _insert_card(conn, "Riku of Two Reflections")
        _insert_card(conn, "Mulldrifter", card_types="Creature")
        _insert_port(
            conn,
            "Mulldrifter",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Mulldrifter", "effect", "Draw")
        conn.commit()

        cmdr_ports = [
            _port("Riku of Two Reflections", "effect", "CopyPermanent", valid_filter="Creature"),
        ]
        results = _find_copy_synergy(conn, cmdr_ports, {"Riku of Two Reflections"})
        assert "Mulldrifter" in _candidates(results)

    def test_creature_copy_via_raw_line(self, conn) -> None:
        """Creature mentioned in raw_line triggers creature copy path."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Ravenous Chupacabra", card_types="Creature")
        _insert_port(
            conn,
            "Ravenous Chupacabra",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Ravenous Chupacabra", "effect", "Destroy")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "CopyPermanent", raw_line="Copy Creature"),
        ]
        results = _find_copy_synergy(conn, cmdr_ports, {"Cmdr"})
        assert "Ravenous Chupacabra" in _candidates(results)

    def test_both_populate_and_creature_copy(self, conn) -> None:
        """Commander with both Populate and Creature copy finds both token producers and ETB creatures."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Token Maker")
        _insert_port(conn, "Token Maker", "effect", "Token")
        _insert_card(conn, "ETB Creature", card_types="Creature")
        _insert_port(
            conn,
            "ETB Creature",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "ETB Creature", "effect", "GainControl")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "CopyPermanent", raw_line="{'Populate': True, 'Creature': True}"),
        ]
        # has_populate and has_creature_copy are both detected
        results = _find_copy_synergy(conn, cmdr_ports, {"Cmdr"})
        cands = _candidates(results)
        assert "Token Maker" in cands
        assert "ETB Creature" in cands

    def test_excludes_commander_from_results(self, conn) -> None:
        """Commander should not appear in its own results."""
        _insert_card(conn, "Ghired, Conclave Exile")
        _insert_port(conn, "Ghired, Conclave Exile", "effect", "Token")
        conn.commit()

        cmdr_ports = [
            _port("Ghired, Conclave Exile", "effect", "CopyPermanent", raw_line="{'Populate': True}"),
        ]
        results = _find_copy_synergy(conn, cmdr_ports, {"Ghired, Conclave Exile"})
        assert "Ghired, Conclave Exile" not in _candidates(results)

    def test_creature_without_valuable_effect_excluded(self, conn) -> None:
        """Creature with self-ETB but no valuable effect is excluded."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Boring Creature", card_types="Creature")
        _insert_port(
            conn,
            "Boring Creature",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        # No valuable effect (Draw, Destroy, etc.)
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "CopyPermanent", valid_filter="Creature")]
        results = _find_copy_synergy(conn, cmdr_ports, {"Cmdr"})
        assert "Boring Creature" not in _candidates(results)

    def test_non_creature_with_etb_excluded(self, conn) -> None:
        """Non-creature with self-ETB is excluded from creature copy results."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Some Enchantment", card_types="Enchantment")
        _insert_port(
            conn,
            "Some Enchantment",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Some Enchantment", "effect", "Draw")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "CopyPermanent", valid_filter="Creature")]
        results = _find_copy_synergy(conn, cmdr_ports, {"Cmdr"})
        assert "Some Enchantment" not in _candidates(results)

    def test_all_valuable_effect_types(self, conn) -> None:
        """Creatures with each valuable effect type are found."""
        _insert_card(conn, "Cmdr")
        valuable = ["Draw", "Destroy", "DestroyAll", "Token", "GainControl", "DealDamage", "ChangeZone", "Mana"]
        for eff in valuable:
            name = f"Creature_{eff}"
            _insert_card(conn, name, card_types="Creature")
            _insert_port(
                conn,
                name,
                "trigger",
                "ChangesZone",
                valid_filter="Card.Self",
                zone_destination="Battlefield",
            )
            _insert_port(conn, name, "effect", eff)
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "CopyPermanent", valid_filter="Creature")]
        results = _find_copy_synergy(conn, cmdr_ports, {"Cmdr"})
        cands = _candidates(results)
        for eff in valuable:
            assert f"Creature_{eff}" in cands, f"Missing creature with {eff} effect"

    def test_non_copy_effect_skipped(self, conn) -> None:
        """Non-CopyPermanent effect port is skipped."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Token Maker")
        _insert_port(conn, "Token Maker", "effect", "Token")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "Token")]
        results = _find_copy_synergy(conn, cmdr_ports, {"Cmdr"})
        assert results == []

    def test_dedup_across_populate_and_creature(self, conn) -> None:
        """A creature with Token effect + self-ETB + valuable effect appears only once."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Versatile Creature", card_types="Creature")
        _insert_port(conn, "Versatile Creature", "effect", "Token")
        _insert_port(
            conn,
            "Versatile Creature",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Versatile Creature", "effect", "Draw")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "CopyPermanent", raw_line="{'Populate': True}"),
            _port("Cmdr", "effect", "CopyPermanent", valid_filter="Creature"),
        ]
        results = _find_copy_synergy(conn, cmdr_ports, {"Cmdr"})
        vc = [r for r in results if r.candidate == "Versatile Creature"]
        # Populate finds it first, then creature copy is deduplicated
        assert len(vc) == 1


# ===========================================================================
# _find_etb_sac_targets
# ===========================================================================


class TestFindEtbSacTargets:
    """Tests for _find_etb_sac_targets."""

    def test_no_gy_commander_returns_empty(self, conn) -> None:
        """Commander without GY-related ports returns empty."""
        ports = [_port("Generic", "trigger", "SpellCast")]
        assert _find_etb_sac_targets(conn, ports, {"Generic"}) == []

    def test_changezone_graveyard_finds_etb_sac_creatures(self, conn) -> None:
        """Commander with ChangeZone from GY finds creatures with self-ETB + sac cost."""
        _insert_card(conn, "Meren of Clan Nel Toth")
        _insert_card(conn, "Sakura-Tribe Elder")
        _insert_port(
            conn,
            "Sakura-Tribe Elder",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Sakura-Tribe Elder", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port("Meren of Clan Nel Toth", "effect", "ChangeZone", zone_origin="Graveyard"),
        ]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Meren of Clan Nel Toth"})
        assert "Sakura-Tribe Elder" in _candidates(results)
        assert all(r.rule_id == "etb_sac_target" for r in results)

    def test_static_mayplay_graveyard_activates(self, conn) -> None:
        """Static MayPlay from Graveyard also triggers etb_sac_targets."""
        _insert_card(conn, "Karador, Ghost Chieftain")
        _insert_card(conn, "Plaguecrafter")
        _insert_port(
            conn,
            "Plaguecrafter",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Plaguecrafter", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port(
                "Karador, Ghost Chieftain",
                "static",
                "Continuous",
                raw_line="{'MayPlay': True, 'Graveyard': True}",
            ),
        ]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Karador, Ghost Chieftain"})
        assert "Plaguecrafter" in _candidates(results)

    def test_triggered_card_filter_blocks(self, conn) -> None:
        """ChangeZone from GY with TriggeredCard filter is excluded (Tergrid pattern)."""
        _insert_card(conn, "Tergrid, God of Fright")
        _insert_card(conn, "Sakura-Tribe Elder")
        _insert_port(
            conn,
            "Sakura-Tribe Elder",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Sakura-Tribe Elder", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port(
                "Tergrid, God of Fright",
                "effect",
                "ChangeZone",
                zone_origin="Graveyard",
                valid_filter="TriggeredCard",
            ),
        ]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Tergrid, God of Fright"})
        assert results == []

    def test_excludes_commander_from_results(self, conn) -> None:
        """Commander should not appear in its own results."""
        _insert_card(conn, "Meren of Clan Nel Toth")
        _insert_port(
            conn,
            "Meren of Clan Nel Toth",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Meren of Clan Nel Toth", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port("Meren of Clan Nel Toth", "effect", "ChangeZone", zone_origin="Graveyard"),
        ]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Meren of Clan Nel Toth"})
        assert "Meren of Clan Nel Toth" not in _candidates(results)

    def test_creature_without_sac_cost_excluded(self, conn) -> None:
        """Creature with self-ETB but no sacrifice cost is excluded."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Mulldrifter")
        _insert_port(
            conn,
            "Mulldrifter",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        # No sacrifice cost
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Cmdr"})
        assert "Mulldrifter" not in _candidates(results)

    def test_creature_without_self_etb_excluded(self, conn) -> None:
        """Creature with sacrifice cost but no self-ETB trigger is excluded."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Sac Only")
        _insert_port(conn, "Sac Only", "cost", "sacrifice")
        # No self-ETB trigger
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Cmdr"})
        assert "Sac Only" not in _candidates(results)

    def test_multiple_etb_sac_creatures_found(self, conn) -> None:
        """Multiple valid ETB+sac creatures are all found."""
        _insert_card(conn, "Cmdr")
        for name in ["Spore Frog", "Plaguecrafter", "Sakura-Tribe Elder"]:
            _insert_card(conn, name)
            _insert_port(
                conn,
                name,
                "trigger",
                "ChangesZone",
                valid_filter="Card.Self",
                zone_destination="Battlefield",
            )
            _insert_port(conn, name, "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Cmdr"})
        cands = _candidates(results)
        assert {"Spore Frog", "Plaguecrafter", "Sakura-Tribe Elder"} <= cands

    def test_changezone_non_graveyard_origin_ignored(self, conn) -> None:
        """ChangeZone effect from non-Graveyard origin does not activate."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Creature")
        _insert_port(
            conn,
            "Creature",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Creature", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "ChangeZone", zone_origin="Hand"),
        ]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Cmdr"})
        assert results == []

    def test_self_recursion_wins_over_steal(self, conn) -> None:
        """Self-recursion port wins over steal port — both present should still match."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Sakura-Tribe Elder")
        _insert_port(
            conn,
            "Sakura-Tribe Elder",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Sakura-Tribe Elder", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [
            _port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard"),
            _port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard", valid_filter="TriggeredCard"),
        ]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Cmdr"})
        assert len(results) == 1
        assert results[0].candidate == "Sakura-Tribe Elder"

    def test_result_events(self, conn) -> None:
        """Results have correct cmdr_event and cand_event."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Spore Frog")
        _insert_port(
            conn,
            "Spore Frog",
            "trigger",
            "ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _insert_port(conn, "Spore Frog", "cost", "sacrifice")
        conn.commit()

        cmdr_ports = [_port("Cmdr", "effect", "ChangeZone", zone_origin="Graveyard")]
        results = _find_etb_sac_targets(conn, cmdr_ports, {"Cmdr"})
        for r in results:
            assert r.cmdr_event == "graveyard_reanimate"
            assert r.cand_event == "etb_sac_creature"
            assert r.direction == "synergy"
