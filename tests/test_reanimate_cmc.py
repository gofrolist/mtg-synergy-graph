"""Tests for reanimate-targeted CMC scaling.

Extends ``_find_cheat_cmc_bonus`` to detect reanimate-on-trigger
patterns like Sharuum's (ETB → reanimate target Artifact from GY).
Sharuum cheats a high-CMC artifact into play for free via her trigger,
just like Kaalia cheats Angels/Demons/Dragons from hand.

Narrow: only fires when the ChangeZone effect is in an execute branch
(free reanimate from a trigger) and the trigger is NOT
effect-conditional (Meren's XP-gated reanimate would falsely match
otherwise).
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


class TestReanimateCmcCheat:
    """Sharuum-style free reanimation should match high-CMC artifacts."""

    def test_sharuum_matches_high_cmc_artifacts(self, conn):
        """Sharuum's ETB returns target Artifact from GY to battlefield.
        High-CMC artifacts should rank higher as cheat-into-play targets."""
        from mtg_synergy_graph.complement_rules.density import _find_cheat_cmc_bonus
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Sharuum the Hegemon"])
        results = _find_cheat_cmc_bonus(conn, ports, {"Sharuum the Hegemon"})
        # Should catch EDHREC picks: Magister Sphinx (CMC 7), Noxious Gearhulk
        # (CMC 6), Sphinx Summoner (CMC 5), Solemn Simulacrum (CMC 4).
        names = {r.candidate for r in results}
        assert "Magister Sphinx" in names
        assert "Noxious Gearhulk" in names
        # CMC 5 falls in cmc_mid bracket (4-5), should be there too
        assert "Sphinx Summoner" in names

    def test_meren_does_not_match(self, conn):
        """Meren's end-step reanimate is effect_conditional (XP ≥ CMC
        gate). She should NOT trigger cheat_cmc — high-CMC targets
        won't reanimate early when XP is low."""
        from mtg_synergy_graph.complement_rules.density import _find_cheat_cmc_bonus
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Meren of Clan Nel Toth"])
        results = _find_cheat_cmc_bonus(conn, ports, {"Meren of Clan Nel Toth"})
        # Meren should not fire cheat_cmc — it's gated by XP counters.
        # Any existing matches from ChangeType (if any) are fine, but
        # the reanimate-on-trigger path must be suppressed.
        # Check that Magister Sphinx (high-CMC generic artifact/creature)
        # is NOT returned from Meren's cheat_cmc (because her ChangeZone
        # is effect_conditional, not from a clean free reanimate).
        from_reanimate = [
            r for r in results if r.cmdr_event == "free_reanimate" or "reanimate" in (r.cmdr_event or "").lower()
        ]
        assert from_reanimate == []

    def test_kaalia_still_matches(self, conn):
        """Existing Kaalia ChangeType-based matches must not regress."""
        from mtg_synergy_graph.complement_rules.density import _find_cheat_cmc_bonus
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Kaalia of the Vast"])
        results = _find_cheat_cmc_bonus(conn, ports, {"Kaalia of the Vast"})
        assert len(results) > 0, "Kaalia should still match via ChangeType path"
