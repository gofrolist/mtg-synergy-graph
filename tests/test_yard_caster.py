"""Tests for the yard_caster rule (Chainer-style cast-from-graveyard).

Chainer, Nightmare Adept has a ``StaticAbilities$ STYardCast`` static
letting him cast a creature spell from the graveyard once per turn
(cost: discard a card). This rule catches the reanimator package that
fills and plays from the graveyard: Buried Alive, Animate Dead,
Living Death, Victimize, Squee / Gravecrawler (self-returning
creatures), Faithless Looting (loot outlets).

Narrow gate: fires only when commander has ``STYardCast`` in a port's
raw_line — Chainer is the only golden-set commander with this pattern.
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


class TestYardCasterRule:
    def test_chainer_catches_reanimator_package(self, conn):
        from mtg_synergy_graph.complement_rules.statics import _find_yard_caster
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Chainer, Nightmare Adept"])
        results = _find_yard_caster(conn, ports, {"Chainer, Nightmare Adept"})
        names = {r.candidate for r in results}
        # GY fillers
        assert "Buried Alive" in names
        # Reanimator spells
        assert "Animate Dead" in names
        assert "Victimize" in names
        # Self-returning creatures
        assert "Squee, Goblin Nabob" in names
        # Loot outlet (draw + discard)
        assert "Faithless Looting" in names

    def test_categories_use_distinct_cand_events(self, conn):
        """Different reanimator patterns get different cand_event tags so
        each gets its own IDF group (narrow matches score higher)."""
        from mtg_synergy_graph.complement_rules.statics import _find_yard_caster
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Chainer, Nightmare Adept"])
        results = _find_yard_caster(conn, ports, {"Chainer, Nightmare Adept"})
        by_cand = {r.candidate: r.cand_event for r in results}
        assert by_cand.get("Buried Alive") == "mill_to_gy"
        assert by_cand.get("Victimize") == "reanimate"
        assert by_cand.get("Squee, Goblin Nabob") == "self_recur"
        assert by_cand.get("Faithless Looting") == "loot_outlet"

    def test_non_yardcast_commander_no_match(self, conn):
        """Commanders without the STYardCast static don't trigger the rule."""
        from mtg_synergy_graph.complement_rules.statics import _find_yard_caster
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        for cmdr in [
            "Korvold, Fae-Cursed King",
            "Selvala, Heart of the Wilds",
            "Sharuum the Hegemon",
            "Marrow-Gnawer",
        ]:
            ports = load_ports_for_set(conn, [cmdr])
            results = _find_yard_caster(conn, ports, {cmdr})
            assert results == [], f"{cmdr} should not trigger yard_caster"

    def test_rule_id(self, conn):
        from mtg_synergy_graph.complement_rules.statics import _find_yard_caster
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Chainer, Nightmare Adept"])
        results = _find_yard_caster(conn, ports, {"Chainer, Nightmare Adept"})
        assert all(r.rule_id == "yard_caster" for r in results)
