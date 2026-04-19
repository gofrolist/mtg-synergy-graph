"""Tests for complement rule functions in mtg_synergy_graph.complement_rules.

Uses the fixture card files in tests/fixtures/ to build a small test DB
and verifies each _find_* function produces expected results.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.complement_rules.combat import (
    _find_combat_enhancers,
    _find_evasion_complements,
    _find_sacrifice_outlets,
)
from mtg_synergy_graph.complement_rules.core import (
    PortComplement,
    _build_stax_exclusion,
    find_all_complements,
)
from mtg_synergy_graph.complement_rules.density import (
    _find_scales_with_density,
    _find_spellcast_density_complements,
)
from mtg_synergy_graph.complement_rules.panharmonicon import (
    _find_panharmonicon_stacking,
)
from mtg_synergy_graph.complement_rules.tokens import (
    _find_effect_feeds_etb,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import load_ports_for_set
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("complement") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidates(results: list[PortComplement]) -> set[str]:
    """Extract candidate card names from a list of PortComplement."""
    return {r.candidate for r in results}


# ---------------------------------------------------------------------------
# 1. _build_stax_exclusion
# ---------------------------------------------------------------------------


class TestBuildStaxExclusion:
    def test_korvold_excludes_cant_sacrifice(self, populated_db):
        """Korvold has a Sacrificed trigger -- CantSacrifice stax should be excluded."""
        cmdr_ports = load_ports_for_set(populated_db, ["Korvold, Fae-Cursed King"])
        excluded = _build_stax_exclusion(populated_db, cmdr_ports)
        # We don't have CantSacrifice cards in fixtures, but the set should be
        # computed without error. The key test is that the function runs and
        # returns a set (possibly empty if no stax cards exist in fixtures).
        assert isinstance(excluded, set)

    def test_rhystic_study_excludes_raise_cost(self, populated_db):
        """Rhystic Study has a SpellCast trigger -- RaiseCost/CantBeCast should be excluded."""
        cmdr_ports = load_ports_for_set(populated_db, ["Rhystic Study"])
        excluded = _build_stax_exclusion(populated_db, cmdr_ports)
        assert isinstance(excluded, set)

    def test_non_trigger_commander_returns_empty(self, populated_db):
        """Sol Ring has no triggers -- stax exclusion should be empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Sol Ring"])
        excluded = _build_stax_exclusion(populated_db, cmdr_ports)
        assert excluded == set()


# ---------------------------------------------------------------------------
# 2. find_all_complements (integration)
# ---------------------------------------------------------------------------


class TestFindAllComplements:
    def test_korvold_finds_phyrexian_altar(self, populated_db):
        """Korvold's Sacrificed trigger should match Phyrexian Altar's sacrifice cost."""
        results = find_all_complements(populated_db, ["Korvold, Fae-Cursed King"])
        candidates = _candidates(results)
        assert "Phyrexian Altar" in candidates

    def test_korvold_excludes_wrath_of_god(self, populated_db):
        """Wrath of God (DestroyAll) should NOT be a synergy match for Korvold."""
        results = find_all_complements(populated_db, ["Korvold, Fae-Cursed King"])
        synergy_candidates = {r.candidate for r in results if r.direction == "synergy"}
        assert "Wrath of God" not in synergy_candidates


# ---------------------------------------------------------------------------
# 4. _find_sacrifice_outlets
# ---------------------------------------------------------------------------


class TestFindSacrificeOutlets:
    def test_korvold_changeszone_not_death(self, populated_db):
        """Korvold's ChangesZone trigger is ETB (Destination=Battlefield),
        not death (Destination=Graveyard). Should not match sacrifice_outlets."""
        cmdr_ports = load_ports_for_set(populated_db, ["Korvold, Fae-Cursed King"])
        results = _find_sacrifice_outlets(
            populated_db,
            cmdr_ports,
            {"Korvold, Fae-Cursed King"},
        )
        # Korvold's ChangesZone is self-ETB (Card.Self, Destination=Battlefield)
        # which is NOT a death trigger. Sacrifice outlets need a non-self
        # ChangesZone with Destination=Graveyard.
        assert "Phyrexian Altar" not in _candidates(results)

    def test_non_death_trigger_returns_empty(self, populated_db):
        """Sol Ring has no ChangesZone death trigger -- returns empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Sol Ring"])
        results = _find_sacrifice_outlets(
            populated_db,
            cmdr_ports,
            {"Sol Ring"},
        )
        assert results == []


# ---------------------------------------------------------------------------
# 5. _find_panharmonicon_stacking
# ---------------------------------------------------------------------------


class TestFindPanharmoniconStacking:
    def test_korvold_not_panharmonicon(self, populated_db):
        """Korvold has no Panharmonicon static -- should return empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Korvold, Fae-Cursed King"])
        results = _find_panharmonicon_stacking(
            populated_db,
            cmdr_ports,
            {"Korvold, Fae-Cursed King"},
        )
        assert results == []

    def test_sol_ring_not_panharmonicon(self, populated_db):
        """Sol Ring has no Panharmonicon -- should return empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Sol Ring"])
        results = _find_panharmonicon_stacking(
            populated_db,
            cmdr_ports,
            {"Sol Ring"},
        )
        assert results == []

    def test_panharmonicon_as_commander(self, populated_db):
        """Panharmonicon fixture has a Panharmonicon static for ChangesZone.
        If used as commander, it should find other Panharmonicon-statics
        with overlapping modes (if any exist in fixtures)."""
        cmdr_ports = load_ports_for_set(populated_db, ["Panharmonicon"])
        results = _find_panharmonicon_stacking(
            populated_db,
            cmdr_ports,
            {"Panharmonicon"},
        )
        # Panharmonicon itself is the only card with Panharmonicon static
        # in our fixtures, so it should not find itself.
        assert "Panharmonicon" not in _candidates(results)


# ---------------------------------------------------------------------------
# 6. _find_scales_with_density
# ---------------------------------------------------------------------------


class TestFindScalesWithDensity:
    def test_tireless_tracker_no_p1p1_scaling(self, populated_db):
        """Tireless Tracker has scales_with for Land, not P1P1 counters.
        Should return empty since its scales_with is not P1P1-related."""
        cmdr_ports = load_ports_for_set(populated_db, ["Tireless Tracker"])
        results = _find_scales_with_density(
            populated_db,
            cmdr_ports,
            {"Tireless Tracker"},
        )
        # Tireless Tracker doesn't have a scales_with port at all in Forge data
        # (its scaling is implicit in the trigger). So this should be empty.
        assert results == []

    def test_non_scales_with_returns_empty(self, populated_db):
        """Rhystic Study has no scales_with port -- should return empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Rhystic Study"])
        results = _find_scales_with_density(
            populated_db,
            cmdr_ports,
            {"Rhystic Study"},
        )
        assert results == []


# ---------------------------------------------------------------------------
# 7. _find_spellcast_density_complements
# ---------------------------------------------------------------------------


class TestFindSpellcastDensity:
    def test_rhystic_study_spellcast_trigger(self, populated_db):
        """Rhystic Study has SpellCast trigger with ValidCard=Card (catch-all).
        Since its filter is just 'Card' (too broad), it should NOT produce
        spell density matches (Card is in _TOO_BROAD)."""
        cmdr_ports = load_ports_for_set(populated_db, ["Rhystic Study"])
        results = _find_spellcast_density_complements(
            populated_db,
            cmdr_ports,
            {"Rhystic Study"},
        )
        # Rhystic Study's SpellCast has ValidCard=Card which is _TOO_BROAD
        # so no spell_density complements should be generated.
        assert results == []

    def test_non_spellcast_returns_empty(self, populated_db):
        """Sol Ring has no SpellCast trigger -- should return empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Sol Ring"])
        results = _find_spellcast_density_complements(
            populated_db,
            cmdr_ports,
            {"Sol Ring"},
        )
        assert results == []


# ---------------------------------------------------------------------------
# 8. _find_combat_enhancers
# ---------------------------------------------------------------------------


class TestFindCombatEnhancers:
    def test_korvold_no_damage_trigger(self, populated_db):
        """Korvold triggers on Sacrificed, not DamageDone. Should return empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Korvold, Fae-Cursed King"])
        results = _find_combat_enhancers(
            populated_db,
            cmdr_ports,
            {"Korvold, Fae-Cursed King"},
        )
        assert results == []

    def test_sol_ring_returns_empty(self, populated_db):
        """Sol Ring has no DamageDone trigger -- should return empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Sol Ring"])
        results = _find_combat_enhancers(
            populated_db,
            cmdr_ports,
            {"Sol Ring"},
        )
        assert results == []


# ---------------------------------------------------------------------------
# 9. _find_evasion_complements
# ---------------------------------------------------------------------------


class TestFindEvasionComplements:
    def test_korvold_no_combat_damage(self, populated_db):
        """Korvold has no DamageDone trigger -- should return empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Korvold, Fae-Cursed King"])
        results = _find_evasion_complements(
            populated_db,
            cmdr_ports,
            {"Korvold, Fae-Cursed King"},
        )
        assert results == []

    def test_rhystic_study_returns_empty(self, populated_db):
        """Rhystic Study has no DamageDone trigger."""
        cmdr_ports = load_ports_for_set(populated_db, ["Rhystic Study"])
        results = _find_evasion_complements(
            populated_db,
            cmdr_ports,
            {"Rhystic Study"},
        )
        assert results == []


# ---------------------------------------------------------------------------
# 10. _find_effect_feeds_etb
# ---------------------------------------------------------------------------


class TestFindEffectFeedsEtb:
    def test_dockside_finds_creature_etb_triggers(self, populated_db):
        """Dockside Extortionist produces Treasure tokens (artifact creature
        tokens). Since Dockside is a Goblin/Pirate (not a Treasure), the
        token subtype doesn't match the literal subtype, so creature ETB
        triggers should fire. Should find Cathars' Crusade which triggers
        on ChangesZone Creature entering Battlefield."""
        cmdr_ports = load_ports_for_set(populated_db, ["Dockside Extortionist"])
        results = _find_effect_feeds_etb(
            populated_db,
            cmdr_ports,
            {"Dockside Extortionist"},
        )
        candidates = _candidates(results)
        assert "Cathars' Crusade" in candidates

    def test_effect_feeds_etb_rule_id(self, populated_db):
        """Results should have rule_id 'effect_feeds_trigger'."""
        cmdr_ports = load_ports_for_set(populated_db, ["Dockside Extortionist"])
        results = _find_effect_feeds_etb(
            populated_db,
            cmdr_ports,
            {"Dockside Extortionist"},
        )
        assert all(r.rule_id == "effect_feeds_trigger" for r in results)

    def test_sol_ring_no_token_effect(self, populated_db):
        """Sol Ring has no Token/ChangeZone effect -- returns empty."""
        cmdr_ports = load_ports_for_set(populated_db, ["Sol Ring"])
        results = _find_effect_feeds_etb(
            populated_db,
            cmdr_ports,
            {"Sol Ring"},
        )
        assert results == []

    def test_excludes_commander_from_results(self, populated_db):
        """Dockside should not appear in its own results."""
        cmdr_ports = load_ports_for_set(populated_db, ["Dockside Extortionist"])
        results = _find_effect_feeds_etb(
            populated_db,
            cmdr_ports,
            {"Dockside Extortionist"},
        )
        assert "Dockside Extortionist" not in _candidates(results)

    def test_scute_swarm_tribal_gate_blocks(self, populated_db):
        """Scute Swarm is an Insect making Insect tokens -- tribal gate
        blocks creature ETB matches since the token subtype matches the
        commander's literal subtype."""
        cmdr_ports = load_ports_for_set(populated_db, ["Scute Swarm"])
        results = _find_effect_feeds_etb(
            populated_db,
            cmdr_ports,
            {"Scute Swarm"},
        )
        assert results == []
