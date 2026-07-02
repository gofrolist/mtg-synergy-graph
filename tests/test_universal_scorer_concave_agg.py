"""Concave within-family aggregation probe — plan 2026-07-02-002 Unit 4.

The flag ``_ENABLE_CONCAVE_FAMILY_AGG`` (default False) extends the
signal-concentration dampener to single-rule candidates and applies it
at a choke point shared by ``UniversalScore.score`` AND
``to_legacy_buckets()["total"]`` — closing the dual-total split where
production ranking never saw the dampener at all.

Hand-computed pairs follow the color-IDF probe's Unit-2 test pattern.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import mtg_synergy_graph.universal_scorer as us_mod
from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.universal_scorer import UniversalScore


def _comp(
    rule_id: str,
    cand_event: str,
    direction: str = "synergy",
    cmdr_event: str = "Sacrificed",
) -> PortComplement:
    return PortComplement(
        rule_id=rule_id,
        direction=direction,
        candidate="CardA",
        cmdr_event=cmdr_event,
        cand_event=cand_event,
    )


def _weights(comps: list[PortComplement], w: float) -> dict:
    return {(c.rule_id, c.cmdr_event, c.cand_event, c.filter_group): w for c in comps}


class TestFlagDefault:
    def test_flag_default_is_false(self):
        assert us_mod._ENABLE_CONCAVE_FAMILY_AGG is False


class TestFlagOff:
    def test_single_rule_candidate_undampened(self):
        """Flag OFF: single-rule candidate keeps the legacy exemption —
        score is the plain weight sum (0.5), no factor applied."""
        comps = [_comp("tribal_density", "Goblin")]
        s = UniversalScore(complements=comps, idf_weights=_weights(comps, 0.5))
        assert s.score == pytest.approx(0.5)
        assert s.to_legacy_buckets()["total"] == pytest.approx(0.5)

    def test_legacy_buckets_have_no_dampener_flag_off(self):
        """Flag OFF preserves the historic dual-total split: score damps
        a 2-rule concentrated candidate, legacy total does not."""
        comps = [
            _comp("tribal_density", "Goblin"),
            _comp("etb_self", "ETB"),
        ]
        w = _weights([comps[0]], 0.9)
        w.update(_weights([comps[1]], 0.1))
        s = UniversalScore(complements=comps, idf_weights=w)
        # frac = 0.9 -> penalty = 0.2 -> syn 1.0*0.8 = 0.8, + multi-rule
        # bonus 0.02
        assert s.score == pytest.approx(0.8 + 0.02)
        # legacy total: undampened 1.0 + 0.02
        assert s.to_legacy_buckets()["total"] == pytest.approx(1.0 + 0.02)


class TestFlagOn:
    def test_single_rule_candidate_dampened(self):
        """Flag ON: single-rule candidate (frac == 1.0) takes the full
        30% haircut on its synergy sum."""
        comps = [_comp("tribal_density", "Goblin")]
        with patch.object(us_mod, "_ENABLE_CONCAVE_FAMILY_AGG", True):
            s = UniversalScore(complements=comps, idf_weights=_weights(comps, 0.5))
            assert s.score == pytest.approx(0.5 * 0.7)
            assert s.to_legacy_buckets()["total"] == pytest.approx(0.5 * 0.7)

    def test_diversified_candidate_outranks_equal_monoculture(self):
        """Flag ON: three equal families (frac 1/3, no penalty) beat a
        single-family candidate with the same raw sum."""
        mono = [_comp("tribal_density", "Goblin")]
        div = [
            _comp("trigger_effect", "TokenA"),
            _comp("cost_feeds_trigger", "TokenB"),
            _comp("effect_resonance", "TokenC"),
        ]
        with patch.object(us_mod, "_ENABLE_CONCAVE_FAMILY_AGG", True):
            s_mono = UniversalScore(complements=mono, idf_weights=_weights(mono, 0.6))
            s_div = UniversalScore(complements=div, idf_weights=_weights(div, 0.2))
            # mono: 0.6*0.7 = 0.42; div: 0.6 undamped + breadth 0.04
            # (+ any pair bonus >= 0)
            assert s_div.score > s_mono.score

    def test_tiny_single_contribution_not_amplified(self):
        """Monotone, bounded: the factor only shrinks, never grows."""
        comps = [_comp("etb_self", "ETB")]
        with patch.object(us_mod, "_ENABLE_CONCAVE_FAMILY_AGG", True):
            s = UniversalScore(complements=comps, idf_weights=_weights(comps, 0.01))
            assert 0 < s.score <= 0.01

    def test_anti_synergy_not_dampened(self):
        """Factor applies to the synergy side only — anti subtracts at
        full weight, consistent with legacy dampener semantics."""
        comps = [
            _comp("tribal_density", "Goblin"),
            _comp("etb_tapped_stax", "Tapped", direction="anti_synergy"),
        ]
        w = _weights(comps, 0.5)
        with patch.object(us_mod, "_ENABLE_CONCAVE_FAMILY_AGG", True):
            s = UniversalScore(complements=comps, idf_weights=w)
            assert s.score == pytest.approx(0.5 * 0.7 - 0.5)

    def test_dual_totals_agree_flag_on(self):
        """Flag ON unifies the dampening semantics of both totals."""
        comps = [
            _comp("tribal_density", "Goblin"),
            _comp("tribal_density", "Warrior"),
            _comp("etb_self", "ETB"),
        ]
        w = _weights(comps, 0.4)
        with patch.object(us_mod, "_ENABLE_CONCAVE_FAMILY_AGG", True):
            s = UniversalScore(complements=comps, idf_weights=w)
            assert s.to_legacy_buckets()["total"] == pytest.approx(s.score)


class TestFactorHelper:
    def test_below_threshold_no_penalty(self):
        assert us_mod._syn_concentration_factor({"a": 0.5, "b": 0.5}, 1.0) == 1.0

    def test_legacy_min_rules_two_flag_off(self):
        assert us_mod._syn_concentration_factor({"a": 1.0}, 1.0) == 1.0

    def test_min_rules_one_flag_on(self):
        with patch.object(us_mod, "_ENABLE_CONCAVE_FAMILY_AGG", True):
            assert us_mod._syn_concentration_factor({"a": 1.0}, 1.0) == pytest.approx(0.7)

    def test_zero_syn_guard(self):
        assert us_mod._syn_concentration_factor({}, 0.0) == 1.0
