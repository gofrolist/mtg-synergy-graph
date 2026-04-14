"""Tests for circuit bonus and CMC micro-score in universal_scorer."""

from __future__ import annotations

from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.universal_scorer import (
    _FED_BY_COMMANDER_RULES,
    _FEEDS_COMMANDER_RULES,
    UniversalScore,
)


def _comp(
    rule_id: str = "trigger_effect",
    direction: str = "synergy",
    candidate: str = "CardA",
    cmdr_event: str = "Sacrificed",
    cand_event: str = "TokenCreation",
) -> PortComplement:
    return PortComplement(
        rule_id=rule_id,
        direction=direction,
        candidate=candidate,
        cmdr_event=cmdr_event,
        cand_event=cand_event,
    )


# ---------------------------------------------------------------------------
# Circuit bonus
# ---------------------------------------------------------------------------


def test_circuit_bonus_added_to_score():
    """Circuit bonus is included in the final score."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(
        complements=[c],
        idf_weights={key: 0.5},
        circuit_bonus=0.05,
    )
    assert abs(us.score - 0.55) < 1e-9


def test_no_circuit_bonus_zero():
    """Score without circuit bonus."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(
        complements=[c],
        idf_weights={key: 0.5},
        circuit_bonus=0.0,
    )
    assert abs(us.score - 0.5) < 1e-9


def test_circuit_rule_sets_non_overlapping():
    """Feed and fed rule sets don't overlap (ensures clean classification)."""
    assert not (_FEEDS_COMMANDER_RULES & _FED_BY_COMMANDER_RULES)


def test_circuit_requires_both_directions():
    """A card with only feed-direction rules is not a circuit."""
    feed = _comp(rule_id="cost_feeds_trigger")
    key = (feed.rule_id, feed.cmdr_event, feed.cand_event, feed.filter_group)
    us = UniversalScore(
        complements=[feed],
        idf_weights={key: 0.5},
        circuit_bonus=0.0,  # Not a circuit — only one direction
    )
    assert us.circuit_bonus == 0.0


def test_circuit_bonus_in_legacy_buckets():
    """Circuit bonus is included in legacy bucket total."""
    us = UniversalScore(complements=[], idf_weights={}, circuit_bonus=0.05)
    buckets = us.to_legacy_buckets()
    assert abs(buckets["total"] - 0.05) < 1e-9


# ---------------------------------------------------------------------------
# CMC micro-score
# ---------------------------------------------------------------------------


def test_cmc_bonus_zero_cmc():
    """CMC 0 gets maximum bonus of 0.01."""
    us = UniversalScore(
        complements=[],
        idf_weights={},
        cmc_bonus=0.01 * max(0.0, (7.0 - 0.0)) / 7.0,
    )
    assert abs(us.cmc_bonus - 0.01) < 1e-9


def test_cmc_bonus_cmc_3():
    """CMC 3 gets partial bonus."""
    cmc = 3.0
    expected = 0.01 * (7.0 - cmc) / 7.0
    us = UniversalScore(
        complements=[],
        idf_weights={},
        cmc_bonus=expected,
    )
    assert abs(us.cmc_bonus - expected) < 1e-9
    assert us.cmc_bonus > 0.0


def test_cmc_bonus_cmc_7_or_higher():
    """CMC 7+ gets zero bonus."""
    for cmc in (7.0, 10.0, 99.0):
        bonus = 0.01 * max(0.0, (7.0 - cmc)) / 7.0
        us = UniversalScore(complements=[], idf_weights={}, cmc_bonus=bonus)
        assert us.cmc_bonus == 0.0


def test_cmc_bonus_in_score():
    """CMC bonus is added to the final score."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    cmc_bonus = 0.01 * (7.0 - 2.0) / 7.0  # CMC 2
    us = UniversalScore(
        complements=[c],
        idf_weights={key: 0.5},
        cmc_bonus=cmc_bonus,
    )
    assert abs(us.score - (0.5 + cmc_bonus)) < 1e-9


def test_cmc_bonus_in_legacy_buckets():
    """CMC bonus is included in legacy bucket total."""
    us = UniversalScore(complements=[], idf_weights={}, cmc_bonus=0.008)
    buckets = us.to_legacy_buckets()
    assert abs(buckets["total"] - 0.008) < 1e-9


# ---------------------------------------------------------------------------
# Rank micro-bonus
# ---------------------------------------------------------------------------


def test_rank_bonus_added_to_score():
    """Rank bonus is included in the final score."""
    us = UniversalScore(complements=[], idf_weights={}, rank_bonus=0.004)
    assert abs(us.score - 0.004) < 1e-9


def test_rank_bonus_formula():
    """Rank 0 gets max bonus, rank 30000+ gets zero."""
    # rank 0 → 0.005
    assert abs(0.005 * max(0.0, 1.0 - 0 / 30000.0) - 0.005) < 1e-9
    # rank 15000 → 0.0025
    assert abs(0.005 * max(0.0, 1.0 - 15000 / 30000.0) - 0.0025) < 1e-9
    # rank 30000 → 0
    assert abs(0.005 * max(0.0, 1.0 - 30000 / 30000.0)) < 1e-9
    # rank 99999 → 0
    assert abs(0.005 * max(0.0, 1.0 - 99999 / 30000.0)) < 1e-9


def test_rank_bonus_in_legacy_buckets():
    """Rank bonus is included in legacy bucket total."""
    us = UniversalScore(complements=[], idf_weights={}, rank_bonus=0.003)
    buckets = us.to_legacy_buckets()
    assert abs(buckets["total"] - 0.003) < 1e-9


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------


def test_all_bonuses_combine():
    """Circuit, CMC, rank, staple, and IDF all add to the score."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(
        complements=[c],
        idf_weights={key: 0.5},
        staple_bonus=0.01,
        circuit_bonus=0.05,
        cmc_bonus=0.007,
        rank_bonus=0.003,
    )
    expected = 0.5 + 0.01 + 0.05 + 0.007 + 0.003
    assert abs(us.score - expected) < 1e-9
