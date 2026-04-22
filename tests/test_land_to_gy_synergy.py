"""Tests for _find_land_to_gy_synergy (Gitrog / Titania Voice of Gaea)."""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.density import _find_land_to_gy_synergy
from mtg_synergy_graph.complement_rules.registry import _land_to_gy_gate

SCHEMA = """\
CREATE TABLE card_ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name TEXT NOT NULL,
    port_type TEXT NOT NULL,
    event_class TEXT NOT NULL,
    valid_filter TEXT,
    raw_line TEXT,
    zone_origin TEXT,
    zone_destination TEXT
);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


def _port(**kwargs):
    return dict(kwargs)


def _add_port(
    conn: sqlite3.Connection,
    name: str,
    port_type: str,
    event_class: str,
    *,
    valid_filter: str | None = None,
    raw_line: str | None = None,
    zone_origin: str | None = None,
    zone_destination: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line, zone_origin, zone_destination) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, port_type, event_class, valid_filter, raw_line, zone_origin, zone_destination),
    )


class TestLandToGyGate:
    def test_gitrog_changeszoneall_land_to_graveyard_matches(self):
        assert _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZoneAll",
                zone_destination="Graveyard",
                valid_filter="Land.YouOwn+!token",
            )
        )

    def test_singular_changeszone_variant_also_matches(self):
        """Some Forge triggers use the singular Mode spelling."""
        assert _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZone",
                zone_destination="Graveyard",
                valid_filter="Land.YouCtrl",
            )
        )

    def test_non_trigger_port_skips(self):
        assert not _land_to_gy_gate(
            _port(
                port_type="effect",
                event_class="ChangesZoneAll",
                zone_destination="Graveyard",
                valid_filter="Land.YouOwn",
            )
        )

    def test_wrong_event_class_skips(self):
        assert not _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="SpellCast",
                zone_destination="Graveyard",
                valid_filter="Land",
            )
        )

    def test_wrong_zone_destination_skips(self):
        """ChangesZone battlefield is landfall, not land-to-graveyard."""
        assert not _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZoneAll",
                zone_destination="Battlefield",
                valid_filter="Land.YouOwn",
            )
        )

    def test_no_land_in_filter_skips(self):
        """A creature-to-graveyard trigger (e.g. dies trigger) is not
        a land-to-graveyard trigger."""
        assert not _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZoneAll",
                zone_destination="Graveyard",
                valid_filter="Creature.YouOwn",
            )
        )

    def test_empty_valid_filter_skips(self):
        assert not _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZoneAll",
                zone_destination="Graveyard",
                valid_filter="",
            )
        )

    def test_nonland_creature_death_trigger_skips(self):
        """Princess Yue's dies trigger on 'Card.Self+Creature+nonLand'
        must NOT activate the land-to-GY gate. Substring 'Land' in
        'nonLand' is a false-positive trap."""
        assert not _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZone",
                zone_destination="Graveyard",
                valid_filter="Card.Self+Creature+nonLand",
            )
        )

    def test_nonland_permanent_dies_trigger_skips(self):
        """Filters like 'Permanent.nonLand+YouCtrl' cover non-land
        permanents dying — not the land-to-GY shape."""
        assert not _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZoneAll",
                zone_destination="Graveyard",
                valid_filter="Permanent.nonLand+YouOwn",
            )
        )

    def test_compound_filter_with_real_land_alt_matches(self):
        """A compound filter like 'Creature.YouOwn,Land.YouOwn' still
        has a real Land alt and must activate the gate (Titania Voice
        of Gaea-style filters that combine creature and land watches)."""
        assert _land_to_gy_gate(
            _port(
                port_type="trigger",
                event_class="ChangesZoneAll",
                zone_destination="Graveyard",
                valid_filter="Creature.YouOwn,Land.YouOwn",
            )
        )


def _gitrog_ports():
    return [
        _port(
            port_type="trigger",
            event_class="ChangesZoneAll",
            zone_destination="Graveyard",
            valid_filter="Land.YouOwn+!token",
        )
    ]


class TestFindLandToGySynergy:
    def test_no_matching_trigger_returns_empty(self, conn):
        """Commander without a land-to-graveyard trigger gets nothing."""
        _add_port(
            conn,
            "Sylvan Safekeeper",
            "cost",
            "sacrifice",
            raw_line="Sac<1/Land>",
        )
        non_gitrog = [_port(port_type="trigger", event_class="Attacks", valid_filter="Card.Self")]
        assert _find_land_to_gy_synergy(conn, non_gitrog, set()) == []

    def test_feeder_pool_catches_sac_land_cost(self, conn):
        _add_port(conn, "Sylvan Safekeeper", "cost", "sacrifice", raw_line="Sac<1/Land>")
        _add_port(conn, "Zuran Orb", "cost", "sacrifice", raw_line="Sac<1/Land>")
        _add_port(conn, "Harrow", "cost", "sacrifice", raw_line="Sac<1/Land>")
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        names = {r.candidate for r in results}
        assert "Sylvan Safekeeper" in names
        assert "Zuran Orb" in names
        assert "Harrow" in names
        # All feeder emissions should carry the feeder filter_group
        feeders = [r for r in results if r.candidate in {"Sylvan Safekeeper", "Zuran Orb", "Harrow"}]
        assert all(r.filter_group == "feeder" for r in feeders)
        assert all(r.cand_event == "feeder" for r in feeders)

    def test_feeder_pool_catches_dredge_keyword(self, conn):
        _add_port(conn, "Life from the Loam", "keyword", "Dredge:3")
        _add_port(conn, "Dakmor Salvage", "keyword", "Dredge:2")
        _add_port(conn, "Golgari Grave-Troll", "keyword", "Dredge:6")
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        dredge_hits = [
            r for r in results if r.candidate in {"Life from the Loam", "Dakmor Salvage", "Golgari Grave-Troll"}
        ]
        names = {r.candidate for r in dredge_hits}
        assert "Life from the Loam" in names
        assert "Dakmor Salvage" in names
        assert "Golgari Grave-Troll" in names
        assert all(r.filter_group == "feeder" for r in dredge_hits)

    def test_feeder_pool_rejects_sac_creature_cost(self, conn):
        """Sac<1/Creature> is a creature outlet, not a land feeder."""
        _add_port(conn, "Viscera Seer", "cost", "sacrifice", raw_line="Sac<1/Creature>")
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        names = {r.candidate for r in results}
        assert "Viscera Seer" not in names

    def test_recursion_pool_catches_mayplay_land_static(self, conn):
        _add_port(
            conn,
            "Ramunap Excavator",
            "static",
            "Continuous",
            raw_line=(
                "{'Mode': 'Continuous', 'Affected': 'Land.YouOwn', 'MayPlay': 'True', 'AffectedZone': 'Graveyard'}"
            ),
        )
        _add_port(
            conn,
            "Crucible of Worlds",
            "static",
            "Continuous",
            raw_line=(
                "{'Mode': 'Continuous', 'Affected': 'Land.YouOwn', 'MayPlay': 'True', 'AffectedZone': 'Graveyard'}"
            ),
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        names = {r.candidate for r in results}
        assert "Ramunap Excavator" in names
        assert "Crucible of Worlds" in names
        recursions = [r for r in results if r.candidate in {"Ramunap Excavator", "Crucible of Worlds"}]
        assert all(r.filter_group == "recursion" for r in recursions)

    def test_recursion_pool_catches_gy_to_hand_land_effect(self, conn):
        """Life from the Loam (GY→Hand land return) qualifies as recursion."""
        _add_port(
            conn,
            "Life from the Loam",
            "effect",
            "ChangeZone",
            valid_filter="Land.YouCtrl",
            zone_origin="Graveyard",
            zone_destination="Hand",
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        by_card = {(r.candidate, r.filter_group) for r in results}
        assert ("Life from the Loam", "recursion") in by_card

    def test_recursion_pool_catches_gy_to_battlefield_land_effect(self, conn):
        """Splendid Reclamation returns all lands from GY to battlefield."""
        _add_port(
            conn,
            "Splendid Reclamation",
            "effect",
            "ChangeZone",
            valid_filter="Land.YouOwn",
            zone_origin="Graveyard",
            zone_destination="Battlefield",
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        by_card = {(r.candidate, r.filter_group) for r in results}
        assert ("Splendid Reclamation", "recursion") in by_card

    def test_recursion_pool_rejects_creature_recursion(self, conn):
        """GY→Battlefield creature recursion (Karmic Guide) is not a
        land-recursion payoff."""
        _add_port(
            conn,
            "Karmic Guide",
            "effect",
            "ChangeZone",
            valid_filter="Creature.YouCtrl",
            zone_origin="Graveyard",
            zone_destination="Battlefield",
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        names = {r.candidate for r in results}
        assert "Karmic Guide" not in names

    def test_recursion_pool_rejects_nonland_reanimator_effect(self, conn):
        """Emeria Shepherd / Moira and Teshar / Pull Through the Weft
        reanimate non-land permanents with filter like
        'Permanent.nonLand+YouOwn'. Substring 'Land' in 'nonLand' is a
        false positive — these must not be tagged as Gitrog recursion."""
        _add_port(
            conn,
            "Emeria Shepherd",
            "effect",
            "ChangeZone",
            valid_filter="Permanent.nonLand+YouOwn",
            zone_origin="Graveyard",
            zone_destination="Battlefield",
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        names = {r.candidate for r in results}
        assert "Emeria Shepherd" not in names

    def test_recursion_pool_rejects_nonland_static(self, conn):
        """Yawgmoth's Will-style static with MayPlay for nonLand cards
        in Graveyard must not be tagged as Gitrog recursion."""
        _add_port(
            conn,
            "Yawgmoth's Will",
            "static",
            "Continuous",
            raw_line=(
                "{'Mode': 'Continuous', 'Affected': 'Card.nonLand+YouOwn', "
                "'MayPlay': 'True', 'AffectedZone': 'Graveyard'}"
            ),
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        names = {r.candidate for r in results}
        assert "Yawgmoth's Will" not in names

    def test_excludes_commander_self(self, conn):
        """The commander shouldn't recommend itself even if its ports
        match the feeder/recursion query."""
        _add_port(
            conn,
            "The Gitrog Monster",
            "keyword",
            "Dredge:3",  # hypothetical — just to test the exclusion
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), {"The Gitrog Monster"})
        names = {r.candidate for r in results}
        assert "The Gitrog Monster" not in names

    def test_dedup_across_feeder_and_recursion(self, conn):
        """Life from the Loam has both Dredge (feeder) and GY→Hand Land
        (recursion). The ``seen`` set dedupes — one PortComplement only,
        first-bucket-wins (feeder runs before recursion)."""
        _add_port(conn, "Life from the Loam", "keyword", "Dredge:3")
        _add_port(
            conn,
            "Life from the Loam",
            "effect",
            "ChangeZone",
            valid_filter="Land.YouCtrl",
            zone_origin="Graveyard",
            zone_destination="Hand",
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        loam_hits = [r for r in results if r.candidate == "Life from the Loam"]
        assert len(loam_hits) == 1
        assert loam_hits[0].filter_group == "feeder"

    def test_rule_id_and_cmdr_event_labels(self, conn):
        _add_port(conn, "Sylvan Safekeeper", "cost", "sacrifice", raw_line="Sac<1/Land>")
        _add_port(
            conn,
            "Crucible of Worlds",
            "static",
            "Continuous",
            raw_line=(
                "{'Mode': 'Continuous', 'Affected': 'Land.YouOwn', 'MayPlay': 'True', 'AffectedZone': 'Graveyard'}"
            ),
        )
        results = _find_land_to_gy_synergy(conn, _gitrog_ports(), set())
        assert results, "rule should emit something"
        assert all(r.rule_id == "land_to_gy_synergy" for r in results)
        assert all(r.cmdr_event == "land_to_graveyard" for r in results)
        assert all(r.direction == "synergy" for r in results)
