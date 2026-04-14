"""Coverage tests for universal_scorer.py — targets missed lines 84-105, 109, 119, 125."""

from __future__ import annotations

from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.universal_scorer import UniversalScore, _computeidf_weights


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
# UniversalScore.score  (lines 84-105)
# ---------------------------------------------------------------------------


def test_score_empty():
    """Score with no complements equals staple_bonus only."""
    us = UniversalScore(complements=[], staple_bonus=0.0, idf_weights={})
    assert us.score == 0.0


def test_score_single_synergy():
    """Single synergy complement uses IDF weight."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event)
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={key: 0.5})
    assert us.score == 0.5


def test_score_single_anti_synergy():
    """Anti-synergy subtracts from score."""
    c = _comp(direction="anti_synergy")
    key = (c.rule_id, c.cmdr_event, c.cand_event)
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={key: 0.3})
    assert us.score == -0.3


def test_score_synergy_and_anti():
    """Mixed synergy and anti-synergy."""
    syn = _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="B")
    anti = _comp(rule_id="replacement_blocks", direction="anti_synergy", cmdr_event="C", cand_event="D")
    idf = {
        ("trigger_effect", "A", "B"): 0.5,
        ("replacement_blocks", "C", "D"): 0.2,
    }
    us = UniversalScore(complements=[syn, anti], staple_bonus=0.0, idf_weights=idf)
    assert abs(us.score - 0.3) < 1e-9


def test_score_deduplicates_synergy():
    """Duplicate synergy key is counted only once."""
    c1 = _comp()
    c2 = _comp()  # same key
    key = (c1.rule_id, c1.cmdr_event, c1.cand_event)
    us = UniversalScore(complements=[c1, c2], staple_bonus=0.0, idf_weights={key: 0.5})
    assert us.score == 0.5


def test_score_deduplicates_anti_synergy():
    """Duplicate anti-synergy key is counted only once."""
    c1 = _comp(direction="anti_synergy")
    c2 = _comp(direction="anti_synergy")
    key = (c1.rule_id, c1.cmdr_event, c1.cand_event)
    us = UniversalScore(complements=[c1, c2], staple_bonus=0.0, idf_weights={key: 0.4})
    assert us.score == -0.4


def test_score_missing_idf_defaults_to_1():
    """When IDF key is missing, weight defaults to 1.0."""
    c = _comp()
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={})
    assert us.score == 1.0


def test_score_includes_staple_bonus():
    """Staple bonus is added to the score."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event)
    us = UniversalScore(complements=[c], staple_bonus=0.01, idf_weights={key: 0.5})
    assert abs(us.score - 0.51) < 1e-9


def test_score_multi_rule_bonus_3_rules():
    """3 distinct synergy rules get +0.05 bonus."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="X"),
        _comp(rule_id="cost_feeds_trigger", cmdr_event="B", cand_event="Y"),
        _comp(rule_id="sacrifice_cluster", cmdr_event="C", cand_event="Z"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.0, idf_weights=idf)
    # 3 rules -> +0.05 * min(3-2, 3) = +0.05
    assert abs(us.score - 3.05) < 1e-9


def test_score_multi_rule_bonus_5_rules_capped():
    """5 distinct synergy rules get capped bonus of +0.15."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="1"),
        _comp(rule_id="cost_feeds_trigger", cmdr_event="B", cand_event="2"),
        _comp(rule_id="sacrifice_cluster", cmdr_event="C", cand_event="3"),
        _comp(rule_id="effect_resonance", cmdr_event="D", cand_event="4"),
        _comp(rule_id="zone_resonance", cmdr_event="E", cand_event="5"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.0, idf_weights=idf)
    # 5 rules -> +0.05 * min(5-2, 3) = +0.15
    assert abs(us.score - 5.15) < 1e-9


def test_score_multi_rule_bonus_not_applied_for_2_rules():
    """2 distinct synergy rules get no multi-rule bonus."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="X"),
        _comp(rule_id="cost_feeds_trigger", cmdr_event="B", cand_event="Y"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.0, idf_weights=idf)
    assert us.score == 2.0


# ---------------------------------------------------------------------------
# UniversalScore.distinct_rules  (line 109)
# ---------------------------------------------------------------------------


def test_distinct_rules_only_synergy():
    """distinct_rules only includes synergy complements, not anti-synergy."""
    comps = [
        _comp(rule_id="trigger_effect"),
        _comp(rule_id="trigger_effect", cmdr_event="X", cand_event="Y"),
        _comp(rule_id="lord", direction="anti_synergy"),
    ]
    us = UniversalScore(complements=comps, idf_weights={})
    assert us.distinct_rules == frozenset({"trigger_effect"})


def test_distinct_rules_empty():
    us = UniversalScore(complements=[], idf_weights={})
    assert us.distinct_rules == frozenset()


# ---------------------------------------------------------------------------
# UniversalScore.to_legacy_buckets  — duplicate skip (line 119)
#                                   — anti_synergy branch (line 125)
# ---------------------------------------------------------------------------


def test_to_legacy_buckets_deduplicates():
    """Duplicate (rule_id, cmdr_event, cand_event, direction) is skipped."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event)
    us = UniversalScore(
        complements=[c, c],  # exact duplicate
        staple_bonus=0.0,
        idf_weights={key: 0.5},
    )
    buckets = us.to_legacy_buckets()
    assert buckets["port_match"] == 0.5  # counted once, not twice


def test_to_legacy_buckets_anti_synergy():
    """Anti-synergy subtracts from the bucket."""
    c = _comp(rule_id="trigger_effect", direction="anti_synergy")
    key = (c.rule_id, c.cmdr_event, c.cand_event)
    us = UniversalScore(
        complements=[c],
        staple_bonus=0.0,
        idf_weights={key: 0.3},
    )
    buckets = us.to_legacy_buckets()
    assert buckets["port_match"] == -0.3


def test_to_legacy_buckets_staple():
    """Staple bonus is placed in the staple bucket."""
    us = UniversalScore(complements=[], staple_bonus=0.01, idf_weights={})
    buckets = us.to_legacy_buckets()
    assert buckets["staple"] == 0.01


def test_to_legacy_buckets_unknown_rule_goes_to_catchall():
    """Unknown rule_id maps to catchall bucket."""
    c = _comp(rule_id="unknown_rule_xyz")
    key = (c.rule_id, c.cmdr_event, c.cand_event)
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={key: 0.7})
    buckets = us.to_legacy_buckets()
    assert buckets["catchall"] == 0.7


def test_to_legacy_buckets_total_sums_all():
    """Total equals sum of all bucket values."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="1"),
        _comp(rule_id="lord", cmdr_event="B", cand_event="2"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.01, idf_weights=idf)
    buckets = us.to_legacy_buckets()
    expected_total = sum(buckets[b] for b in buckets if b != "total")
    assert abs(buckets["total"] - expected_total) < 1e-9


# ---------------------------------------------------------------------------
# _computeidf_weights
# ---------------------------------------------------------------------------


def test_computeidf_flat_rules():
    """Flat-count rules get weight 1.0 (or override)."""
    c_spell = _comp(rule_id="spell_density")
    c_tribal = _comp(rule_id="tribal_density")
    c_evasion = _comp(rule_id="evasion")
    weights = _computeidf_weights([c_spell, c_tribal, c_evasion])
    assert weights[("spell_density", c_spell.cmdr_event, c_spell.cand_event)] == 1.0
    assert weights[("tribal_density", c_tribal.cmdr_event, c_tribal.cand_event)] == 0.5
    assert weights[("evasion", c_evasion.cmdr_event, c_evasion.cand_event)] == 0.15


def test_computeidf_normal_rule():
    """Non-flat rule uses 1/log2(1+N)."""
    import math

    comps = [_comp(candidate=f"Card{i}") for i in range(5)]
    weights = _computeidf_weights(comps)
    key = ("trigger_effect", "Sacrificed", "TokenCreation")
    expected = 1.0 / math.log2(1.0 + 5)
    assert abs(weights[key] - expected) < 1e-9
