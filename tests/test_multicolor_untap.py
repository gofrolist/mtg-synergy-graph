"""Tests for the multicolor_untap rule.

Commanders with ``TapOrUntap`` / ``Untap`` effects can repeatedly untap
permanents. The most valuable targets are creatures that produce mana
in every color among permanents you control — Bloom Tender, Faeburrow
Elder. Untapping them again = more multicolor mana. Derevi, Empyrial
Tactician's combat-damage TapOrUntap trigger is the primary case.

Narrow: only 4 cards in the DB produce ``EachColorAmong`` mana.
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


class TestMulticolorUntapRule:
    def test_derevi_matches_eachcolor_dorks(self, conn):
        from mtg_synergy_graph.complement_rules.utility import (
            _find_multicolor_untap,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Derevi, Empyrial Tactician"])
        results = _find_multicolor_untap(conn, ports, {"Derevi, Empyrial Tactician"})
        names = {r.candidate for r in results}
        assert "Bloom Tender" in names
        assert "Faeburrow Elder" in names

    def test_no_untap_effect_no_match(self, conn):
        """Commanders with no TapOrUntap/Untap effect don't trigger."""
        from mtg_synergy_graph.complement_rules.utility import (
            _find_multicolor_untap,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        for cmdr in [
            "Korvold, Fae-Cursed King",
            "Marrow-Gnawer",
            "Marchesa, the Black Rose",
        ]:
            ports = load_ports_for_set(conn, [cmdr])
            results = _find_multicolor_untap(conn, ports, {cmdr})
            assert results == []

    def test_rule_id(self, conn):
        from mtg_synergy_graph.complement_rules.utility import (
            _find_multicolor_untap,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Derevi, Empyrial Tactician"])
        results = _find_multicolor_untap(conn, ports, {"Derevi, Empyrial Tactician"})
        assert all(r.rule_id == "multicolor_untap" for r in results)
