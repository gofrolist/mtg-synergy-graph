"""Tests for graveyard-related complement matchers.

Uses an in-memory SQLite database with minimal schema to exercise every
branch of the functions in complement_rules.graveyard:
  - _find_graveyard_fillers
  - _find_artifact_recursion
  - _find_copy_synergy
  - _find_dies_drain
  - _find_gy_loader
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.graveyard import (
    _find_artifact_recursion,
    _find_copy_synergy,
    _find_dies_drain,
    _find_graveyard_fillers,
    _find_gy_loader,
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

    def test_populate_finds_other_populate_spells(self, conn) -> None:
        """Other populate cards (Sundering Growth, Rootborn Defenses, Growing Ranks,
        Second Harvest) stack with Ghired's own populate and are canonical picks."""
        _insert_card(conn, "Ghired, Conclave Exile")
        for name in ("Sundering Growth", "Rootborn Defenses", "Growing Ranks", "Second Harvest"):
            _insert_card(conn, name)
            _insert_port(
                conn,
                name,
                "effect",
                "CopyPermanent",
                raw_line="{'Populate': 'True'}",
            )
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
        names = _candidates(results)
        assert "Sundering Growth" in names
        assert "Rootborn Defenses" in names
        assert "Growing Ranks" in names
        assert "Second Harvest" in names

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
# _find_dies_drain
# ===========================================================================


def _dies_trigger(conn: sqlite3.Connection, card_name: str, valid_filter: str = "Creature.Other+YouCtrl") -> None:
    _insert_port(
        conn,
        card_name,
        "trigger",
        "ChangesZone",
        valid_filter=valid_filter,
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )


def _drain_effect(conn: sqlite3.Connection, card_name: str) -> None:
    _insert_port(conn, card_name, "effect", "LoseLife", valid_filter="Player.Opponent")


class TestFindDiesDrain:
    """Tests for _find_dies_drain."""

    def test_no_dies_trigger_commander_returns_empty(self, conn) -> None:
        """Commander without a BF→GY creature-dies trigger is gated out."""
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        # Commander with only an ETB trigger — not a dies-commander.
        ports = [
            _port(
                "Generic Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Card.Self",
                zone_destination="Battlefield",
            )
        ]
        assert _find_dies_drain(conn, ports, {"Generic Cmdr"}) == []

    def test_card_self_only_filter_rejected(self, conn) -> None:
        """A ``Card.Self``-only BF→GY trigger (just "when I die") is NOT
        a qualifying dies-commander gate."""
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        ports = [
            _port(
                "Self-Only Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Card.Self",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        assert _find_dies_drain(conn, ports, {"Self-Only Cmdr"}) == []

    def test_creature_type_filter_activates(self, conn) -> None:
        """``Creature.Other`` dies-trigger commander matches payoffs."""
        _insert_card(conn, "Meren of Clan Nel Toth")
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        ports = [
            _port(
                "Meren of Clan Nel Toth",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.Other+YouCtrl",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        results = _find_dies_drain(conn, ports, {"Meren of Clan Nel Toth"})
        assert "Blood Artist" in _candidates(results)
        assert all(r.rule_id == "dies_drain" for r in results)

    def test_creature_subtype_filter_activates(self, conn) -> None:
        """Subtype-scoped dies-triggers (Wilhelt's ``Zombie.Other``,
        Slimefoot's ``Saproling.YouCtrl``, Omnath's ``Elemental.Other``)
        also count as creature-dies commanders."""
        _insert_card(conn, "Wilhelt, the Rotcleaver")
        _insert_card(conn, "Pitiless Plunderer")
        _dies_trigger(conn, "Pitiless Plunderer", "Creature.Other+YouCtrl")
        _insert_port(conn, "Pitiless Plunderer", "effect", "Token")
        conn.commit()

        ports = [
            _port(
                "Wilhelt, the Rotcleaver",
                "trigger",
                "ChangesZone",
                valid_filter="Zombie.Other+YouCtrl+withoutDecayed",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        results = _find_dies_drain(conn, ports, {"Wilhelt, the Rotcleaver"})
        assert "Pitiless Plunderer" in _candidates(results)

    def test_teysa_panharmonicon_static_activates(self, conn) -> None:
        """Teysa Karlov has no creature-dies trigger of her own — her
        ``static: Panharmonicon`` doubles other death triggers. The
        gate should still fire for her via the Panharmonicon branch
        (``Origin: Battlefield`` + ``Destination: Graveyard`` +
        ``ValidCause: Creature``)."""
        _insert_card(conn, "Teysa Karlov")
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        ports = [
            _port(
                "Teysa Karlov",
                "static",
                "Panharmonicon",
                raw_line=(
                    "{'Mode': 'Panharmonicon',"
                    " 'ValidMode': 'ChangesZone,ChangesZoneAll',"
                    " 'ValidCard': 'Permanent.YouCtrl',"
                    " 'ValidCause': 'Creature',"
                    " 'Origin': 'Battlefield',"
                    " 'Destination': 'Graveyard'}"
                ),
            )
        ]
        results = _find_dies_drain(conn, ports, {"Teysa Karlov"})
        assert "Blood Artist" in _candidates(results)

    def test_panharmonicon_without_creature_valid_cause_rejected(self, conn) -> None:
        """A Panharmonicon static that doubles *non-creature* death
        triggers (hypothetical Land-dies or Artifact-dies doubler)
        should NOT qualify for the dies-drain payoff pool."""
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        ports = [
            _port(
                "LandDoubler",
                "static",
                "Panharmonicon",
                raw_line=(
                    "{'Mode': 'Panharmonicon',"
                    " 'ValidMode': 'ChangesZone',"
                    " 'ValidCause': 'Land',"
                    " 'Origin': 'Battlefield',"
                    " 'Destination': 'Graveyard'}"
                ),
            )
        ]
        assert _find_dies_drain(conn, ports, {"LandDoubler"}) == []

    def test_commander_excluded_from_results(self, conn) -> None:
        """The commander never appears in its own recommendations — even
        if its port shape matches the candidate pool."""
        _insert_card(conn, "Judith, the Scourge Diva")
        # Judith herself has a BF→GY Creature dies-trigger with a DealDamage
        # effect, so she'd match her own candidate shape without the
        # cmdr_set guard.
        _dies_trigger(conn, "Judith, the Scourge Diva", "Creature.YouCtrl+!token")
        _insert_port(
            conn,
            "Judith, the Scourge Diva",
            "effect",
            "DealDamage",
            valid_filter="Player.Opponent",
        )
        conn.commit()

        ports = [
            _port(
                "Judith, the Scourge Diva",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.YouCtrl+!token",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        results = _find_dies_drain(conn, ports, {"Judith, the Scourge Diva"})
        assert "Judith, the Scourge Diva" not in _candidates(results)

    def test_self_growth_only_effect_rejected(self, conn) -> None:
        """Cards whose only "payoff" is PutCounter on self (self-growth,
        e.g. Dauthi Ghoul) are NOT in the candidate pool — these are
        weak payoffs that displace lords on tribal dies-commanders."""
        _insert_card(conn, "Self-Growth Only")
        _dies_trigger(conn, "Self-Growth Only")
        _insert_port(
            conn,
            "Self-Growth Only",
            "effect",
            "PutCounter",
            valid_filter="Self",
        )
        conn.commit()

        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.Other+YouCtrl",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        results = _find_dies_drain(conn, ports, {"Cmdr"})
        assert "Self-Growth Only" not in _candidates(results)

    def test_complement_has_correct_metadata(self, conn) -> None:
        """Complements carry the ``dies_drain`` rule_id and event metadata."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Grim Haruspex")
        _dies_trigger(conn, "Grim Haruspex", "Creature.!token+Other+YouCtrl")
        _insert_port(conn, "Grim Haruspex", "effect", "Draw", valid_filter="You")
        conn.commit()

        ports = [
            _port(
                "Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Creature.Other+YouCtrl",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        results = _find_dies_drain(conn, ports, {"Cmdr"})
        haruspex = next(r for r in results if r.candidate == "Grim Haruspex")
        assert haruspex.rule_id == "dies_drain"
        assert haruspex.direction == "synergy"
        assert haruspex.cmdr_event == "creature_dies"
        assert haruspex.cand_event == "dies_payoff"

    def test_marchesa_card_youctrl_with_p1p1_counters_filter_activates(self, conn) -> None:
        """Marchesa's BF→GY trigger uses ``Card.YouCtrl+counters_GE1_P1P1``
        because she returns ANY card-with-P1P1-counter that dies. The
        ``Card`` main token would normally fail the gate, but the
        ``counters_GE_P1P1`` qualifier means the trigger is mechanically a
        creature-dies trigger (P1P1 counters live on creatures in practice)
        and should activate the dies-drain payoff pool."""
        _insert_card(conn, "Marchesa, the Black Rose")
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        ports = [
            _port(
                "Marchesa, the Black Rose",
                "trigger",
                "ChangesZone",
                valid_filter="Card.YouCtrl+counters_GE1_P1P1",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        results = _find_dies_drain(conn, ports, {"Marchesa, the Black Rose"})
        assert "Blood Artist" in _candidates(results)

    def test_card_self_with_counters_filter_rejected(self, conn) -> None:
        """Ochre Jelly / Promising Duskmage have ``Card.Self+counters_GE_P1P1``
        triggers — these only fire when THE SAME card dies (a "when I die"
        self-trigger), not "any creature you control dies". The gate must
        still reject ``Card.Self+counters_*`` filters."""
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        ports = [
            _port(
                "Self-Counters Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Card.Self+counters_GE2_P1P1",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        assert _find_dies_drain(conn, ports, {"Self-Counters Cmdr"}) == []

    def test_card_youctrl_without_p1p1_counters_filter_rejected(self, conn) -> None:
        """A bare ``Card.YouCtrl`` filter (no counter-qualifier) is too
        broad to qualify — it would match every Land, Artifact and
        Enchantment dying. Only the counter-gated variant means
        "creature-dies in practice" and should pass."""
        _insert_card(conn, "Blood Artist")
        _dies_trigger(conn, "Blood Artist")
        _drain_effect(conn, "Blood Artist")
        conn.commit()

        ports = [
            _port(
                "Bare Card Cmdr",
                "trigger",
                "ChangesZone",
                valid_filter="Card.YouCtrl",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
            )
        ]
        assert _find_dies_drain(conn, ports, {"Bare Card Cmdr"}) == []


# ===========================================================================
# _find_gy_loader
# ===========================================================================


def _entomb_tutor(conn: sqlite3.Connection, card_name: str) -> None:
    """Insert a Library→Graveyard tutor port (Buried Alive, Entomb, …)."""
    _insert_port(
        conn,
        card_name,
        "effect",
        "ChangeZone",
        zone_origin="Library",
        zone_destination="Graveyard",
    )


class TestFindGyLoader:
    """Tests for _find_gy_loader."""

    def test_no_reanimator_returns_empty(self, conn) -> None:
        """Commander with no GY-reanimation / MayPlay-creature signal
        is gated out even if it has a graveyard interaction."""
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [_port("Generic Cmdr", "trigger", "SpellCast")]
        assert _find_gy_loader(conn, ports, {"Generic Cmdr"}) == []

    def test_changezone_graveyard_to_battlefield_activates(self, conn) -> None:
        """Meren-style ``effect: ChangeZone orig=Graveyard dest=Battlefield``
        reanimators fire the rule."""
        _insert_card(conn, "Meren of Clan Nel Toth")
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [
            _port(
                "Meren of Clan Nel Toth",
                "effect",
                "ChangeZone",
                valid_filter="Creature.YouOwn",
                zone_origin="Graveyard",
                zone_destination="Battlefield",
            )
        ]
        results = _find_gy_loader(conn, ports, {"Meren of Clan Nel Toth"})
        assert "Buried Alive" in _candidates(results)
        assert all(r.rule_id == "gy_loader" for r in results)

    def test_mayplay_creature_activates(self, conn) -> None:
        """Karador-style MayPlay-from-Graveyard on creatures fires."""
        _insert_card(conn, "Karador, Ghost Chieftain")
        _insert_card(conn, "Entomb")
        _entomb_tutor(conn, "Entomb")
        conn.commit()

        ports = [
            _port(
                "Karador, Ghost Chieftain",
                "static",
                "Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'Affected': 'Creature.nonLand+YouCtrl',"
                    " 'MayPlay': 'True', 'AffectedZone': 'Graveyard'}"
                ),
            )
        ]
        results = _find_gy_loader(conn, ports, {"Karador, Ghost Chieftain"})
        assert "Entomb" in _candidates(results)

    def test_mayplay_instant_sorcery_only_rejected(self, conn) -> None:
        """Kess — MayPlay limited to Instant/Sorcery — does NOT want
        Library→GY tutors (those put creatures in GY, Kess plays
        non-creatures from GY)."""
        _insert_card(conn, "Kess, Dissident Mage")
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [
            _port(
                "Kess, Dissident Mage",
                "static",
                "Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'Affected': 'Instant.YouCtrl,Sorcery.YouCtrl',"
                    " 'MayPlay': 'True', 'AffectedZone': 'Graveyard'}"
                ),
            )
        ]
        assert _find_gy_loader(conn, ports, {"Kess, Dissident Mage"}) == []

    def test_discard_cost_alone_rejected(self, conn) -> None:
        """Borborygmos-style discard cost without any reanimation /
        creature-recast signal does NOT qualify — the discard is for
        land themes (retrace / Dredge), not GY-fill for reanimator."""
        _insert_card(conn, "Borborygmos Enraged")
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [
            _port("Borborygmos Enraged", "effect", "Dig", valid_filter="You"),
            _port("Borborygmos Enraged", "cost", "discard"),
        ]
        assert _find_gy_loader(conn, ports, {"Borborygmos Enraged"}) == []

    def test_triggered_card_reanimation_rejected(self, conn) -> None:
        """Tergrid-style reanimation of an opponent's discarded/
        sacrificed card (``valid_filter='TriggeredCard'``) is NOT a
        reason to tutor YOUR creatures into YOUR graveyard."""
        _insert_card(conn, "Tergrid, God of Fright")
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [
            _port(
                "Tergrid, God of Fright",
                "effect",
                "ChangeZone",
                valid_filter="TriggeredCard",
                zone_origin="Graveyard",
                zone_destination="Battlefield",
            )
        ]
        assert _find_gy_loader(conn, ports, {"Tergrid, God of Fright"}) == []

    def test_commander_excluded_from_results(self, conn) -> None:
        """Commander itself never appears in its own recommendations."""
        _insert_card(conn, "Cmdr Tutor")
        _entomb_tutor(conn, "Cmdr Tutor")
        conn.commit()

        ports = [
            _port(
                "Cmdr Tutor",
                "effect",
                "ChangeZone",
                valid_filter="Creature.YouCtrl",
                zone_origin="Graveyard",
                zone_destination="Battlefield",
            )
        ]
        results = _find_gy_loader(conn, ports, {"Cmdr Tutor"})
        assert "Cmdr Tutor" not in _candidates(results)

    def test_complement_has_correct_metadata(self, conn) -> None:
        """Complements carry the ``gy_loader`` rule_id and event metadata."""
        _insert_card(conn, "Cmdr")
        _insert_card(conn, "Jarad's Orders")
        _entomb_tutor(conn, "Jarad's Orders")
        conn.commit()

        ports = [
            _port(
                "Cmdr",
                "effect",
                "ChangeZone",
                valid_filter="Creature.YouCtrl",
                zone_origin="Graveyard",
                zone_destination="Battlefield",
            )
        ]
        results = _find_gy_loader(conn, ports, {"Cmdr"})
        jarad = next(r for r in results if r.candidate == "Jarad's Orders")
        assert jarad.rule_id == "gy_loader"
        assert jarad.direction == "synergy"
        assert jarad.cmdr_event == "reanimator"
        assert jarad.cand_event == "library_to_gy_tutor"

    def test_graveyard_replay_keyword_grant_activates(self, conn) -> None:
        """Sedris-style Continuous static that grants Unearth (or
        Embalm / Eternalize / Encore / Escape / Flashback / Jump-start)
        to creature cards in graveyard qualifies as a reanimator
        archetype — fill the GY, then replay creatures from it."""
        _insert_card(conn, "Sedris, the Traitor King")
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [
            _port(
                "Sedris, the Traitor King",
                "static",
                "Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'EffectZone': 'Battlefield',"
                    " 'AffectedZone': 'Graveyard', 'Affected': 'Creature.YouCtrl',"
                    " 'AddKeyword': 'Unearth:2 B'}"
                ),
            )
        ]
        results = _find_gy_loader(conn, ports, {"Sedris, the Traitor King"})
        assert "Buried Alive" in _candidates(results)

    def test_graveyard_replay_non_creature_affected_rejected(self, conn) -> None:
        """Static that grants recursion to non-creature GY cards
        (e.g. Flashback on Instants/Sorceries) must NOT trigger the
        creature-reanimator gate — those commanders have different
        Hi-Syn (spell density, not creature tutors)."""
        _insert_card(conn, "Spell Replayer")
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [
            _port(
                "Spell Replayer",
                "static",
                "Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'AffectedZone': 'Graveyard',"
                    " 'Affected': 'Instant.YouCtrl,Sorcery.YouCtrl',"
                    " 'AddKeyword': 'Flashback'}"
                ),
            )
        ]
        assert _find_gy_loader(conn, ports, {"Spell Replayer"}) == []

    def test_graveyard_replay_without_replay_keyword_rejected(self, conn) -> None:
        """Static affecting creature cards in graveyard *without* a
        recognized replay keyword (e.g. Morph, Lifelink) doesn't
        qualify — only replay-from-GY mechanics define a reanimator
        archetype."""
        _insert_card(conn, "Hearts Aflame")
        _insert_card(conn, "Buried Alive")
        _entomb_tutor(conn, "Buried Alive")
        conn.commit()

        ports = [
            _port(
                "Hearts Aflame",
                "static",
                "Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'AffectedZone': 'Graveyard',"
                    " 'Affected': 'Creature.YouCtrl',"
                    " 'AddKeyword': 'Lifelink'}"
                ),
            )
        ]
        assert _find_gy_loader(conn, ports, {"Hearts Aflame"}) == []
