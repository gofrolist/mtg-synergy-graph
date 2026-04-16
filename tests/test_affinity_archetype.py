"""Tests for the affinity_archetype rule.

Commanders with an ``Affinity:X`` keyword care about having many X
permanents — cheaper they cast for each one you control. Emry, Lurker
of the Loch has ``Affinity:Artifact`` and wants cheap artifacts,
artifact lands, and cost reducers.

Three cand_event categories for IDF segmentation:
- ``typed_land`` — artifact lands (~22 cards, very high IDF)
- ``cost_reducer`` — ReduceCost statics targeting the type
- ``cheap_typed`` — CMC 0-1 permanents of the type

Narrow: only fires when commander has an Affinity:X keyword.
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect("data/synergy.db")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class TestAffinityArchetypeRule:
    def test_emry_catches_artifact_archetype(self, conn):
        from mtg_synergy_graph.complement_rules.statics import (
            _find_affinity_archetype,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Emry, Lurker of the Loch"])
        results = _find_affinity_archetype(conn, ports, {"Emry, Lurker of the Loch"})
        names = {r.candidate for r in results}
        # Artifact lands
        assert "Seat of the Synod" in names
        assert "Darksteel Citadel" in names
        # Cost reducers
        assert "Etherium Sculptor" in names
        assert "Foundry Inspector" in names
        # Cheap artifacts
        assert "Mishra's Bauble" in names
        assert "Lotus Petal" in names
        assert "Chromatic Star" in names

    def test_distinct_cand_events_per_category(self, conn):
        from mtg_synergy_graph.complement_rules.statics import (
            _find_affinity_archetype,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Emry, Lurker of the Loch"])
        results = _find_affinity_archetype(conn, ports, {"Emry, Lurker of the Loch"})
        by_cand = {r.candidate: r.cand_event for r in results}
        # Artifact land → typed_land
        assert by_cand.get("Seat of the Synod") == "typed_land"
        # Cost reducer → cost_reducer
        assert by_cand.get("Etherium Sculptor") == "cost_reducer"
        # Cheap artifact → cheap_typed
        assert by_cand.get("Mishra's Bauble") == "cheap_typed"

    def test_non_affinity_commander_no_match(self, conn):
        from mtg_synergy_graph.complement_rules.statics import (
            _find_affinity_archetype,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        for cmdr in [
            "Korvold, Fae-Cursed King",
            "Marrow-Gnawer",
            "Sharuum the Hegemon",
            "Marchesa, the Black Rose",
        ]:
            ports = load_ports_for_set(conn, [cmdr])
            results = _find_affinity_archetype(conn, ports, {cmdr})
            assert results == [], f"{cmdr} should not match affinity_archetype"

    def test_rule_id(self, conn):
        from mtg_synergy_graph.complement_rules.statics import (
            _find_affinity_archetype,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Emry, Lurker of the Loch"])
        results = _find_affinity_archetype(conn, ports, {"Emry, Lurker of the Loch"})
        assert all(r.rule_id == "affinity_archetype" for r in results)
