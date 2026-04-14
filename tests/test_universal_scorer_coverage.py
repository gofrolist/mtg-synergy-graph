"""Coverage tests for universal_scorer.py — targets missed lines 84-105, 109, 119, 125."""

from __future__ import annotations

from mtg_synergy_graph.complement_rules.core import PortComplement, _extract_filter_group
from mtg_synergy_graph.universal_scorer import UniversalScore, _compute_idf_weights


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
# _extract_filter_group
# ---------------------------------------------------------------------------


def test_extract_filter_group_subtype_wins():
    """Subtype is more specific than type and wins."""
    assert _extract_filter_group("Creature.Goblin.YouCtrl") == "Goblin"


def test_extract_filter_group_type_fallback():
    """Type is returned when no subtype present."""
    assert _extract_filter_group("Creature.YouCtrl") == "Creature"


def test_extract_filter_group_empty():
    assert _extract_filter_group("") == ""


def test_extract_filter_group_negated_ignored():
    """Negated tokens are not used as filter group."""
    assert _extract_filter_group("Creature.!Goblin") == "Creature"


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
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={key: 0.5})
    assert us.score == 0.5


def test_score_single_anti_synergy():
    """Anti-synergy subtracts from score."""
    c = _comp(direction="anti_synergy")
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={key: 0.3})
    assert us.score == -0.3


def test_score_synergy_and_anti():
    """Mixed synergy and anti-synergy."""
    syn = _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="B")
    anti = _comp(rule_id="replacement_blocks", direction="anti_synergy", cmdr_event="C", cand_event="D")
    idf = {
        ("trigger_effect", "A", "B", ""): 0.5,
        ("replacement_blocks", "C", "D", ""): 0.2,
    }
    us = UniversalScore(complements=[syn, anti], staple_bonus=0.0, idf_weights=idf)
    assert abs(us.score - 0.3) < 1e-9


def test_score_deduplicates_synergy():
    """Duplicate synergy key is counted only once."""
    c1 = _comp()
    c2 = _comp()  # same key
    key = (c1.rule_id, c1.cmdr_event, c1.cand_event, c1.filter_group)
    us = UniversalScore(complements=[c1, c2], staple_bonus=0.0, idf_weights={key: 0.5})
    assert us.score == 0.5


def test_score_deduplicates_anti_synergy():
    """Duplicate anti-synergy key is counted only once."""
    c1 = _comp(direction="anti_synergy")
    c2 = _comp(direction="anti_synergy")
    key = (c1.rule_id, c1.cmdr_event, c1.cand_event, c1.filter_group)
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
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(complements=[c], staple_bonus=0.01, idf_weights={key: 0.5})
    assert abs(us.score - 0.51) < 1e-9


def test_score_multi_rule_bonus_2_rules():
    """2 distinct synergy rules get +0.02 bonus."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="X"),
        _comp(rule_id="cost_feeds_trigger", cmdr_event="B", cand_event="Y"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event, c.filter_group): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.0, idf_weights=idf)
    # 2 rules -> +0.02 * min(2-1, 4) = +0.02
    assert abs(us.score - 2.02) < 1e-9


def test_score_multi_rule_bonus_3_rules():
    """3 distinct synergy rules get +0.04 bonus."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="X"),
        _comp(rule_id="cost_feeds_trigger", cmdr_event="B", cand_event="Y"),
        _comp(rule_id="sacrifice_cluster", cmdr_event="C", cand_event="Z"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event, c.filter_group): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.0, idf_weights=idf)
    # 3 rules -> +0.02 * min(3-1, 4) = +0.04
    assert abs(us.score - 3.04) < 1e-9


def test_score_multi_rule_bonus_5_rules_capped():
    """5 distinct synergy rules get capped bonus of +0.08."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="1"),
        _comp(rule_id="cost_feeds_trigger", cmdr_event="B", cand_event="2"),
        _comp(rule_id="sacrifice_cluster", cmdr_event="C", cand_event="3"),
        _comp(rule_id="effect_resonance", cmdr_event="D", cand_event="4"),
        _comp(rule_id="zone_resonance", cmdr_event="E", cand_event="5"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event, c.filter_group): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.0, idf_weights=idf)
    # 5 rules -> +0.02 * min(5-1, 4) = +0.08
    assert abs(us.score - 5.08) < 1e-9


def test_score_multi_rule_bonus_not_applied_for_1_rule():
    """1 rule gets no multi-rule bonus."""
    c = _comp()
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={key: 1.0})
    assert us.score == 1.0


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
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
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
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
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
    key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights={key: 0.7})
    buckets = us.to_legacy_buckets()
    assert buckets["catchall"] == 0.7


def test_to_legacy_buckets_total_matches_score():
    """Total in legacy buckets matches the score property."""
    comps = [
        _comp(rule_id="trigger_effect", cmdr_event="A", cand_event="1"),
        _comp(rule_id="lord", cmdr_event="B", cand_event="2"),
    ]
    idf = {(c.rule_id, c.cmdr_event, c.cand_event, c.filter_group): 1.0 for c in comps}
    us = UniversalScore(complements=comps, staple_bonus=0.01, idf_weights=idf)
    buckets = us.to_legacy_buckets()
    assert abs(buckets["total"] - us.score) < 1e-9


# ---------------------------------------------------------------------------
# _compute_idf_weights
# ---------------------------------------------------------------------------


def test_computeidf_flat_rules():
    """Flat-count rules get weight 1.0 (or override)."""
    c_spell = _comp(rule_id="spell_density")
    c_tribal = _comp(rule_id="tribal_density")
    c_evasion = _comp(rule_id="evasion")
    weights = _compute_idf_weights([c_spell, c_tribal, c_evasion])
    assert weights[("spell_density", c_spell.cmdr_event, c_spell.cand_event, c_spell.filter_group)] == 1.0
    assert weights[("tribal_density", c_tribal.cmdr_event, c_tribal.cand_event, c_tribal.filter_group)] == 0.5
    assert weights[("evasion", c_evasion.cmdr_event, c_evasion.cand_event, c_evasion.filter_group)] == 0.15


def test_computeidf_normal_rule():
    """Non-flat rule uses 1/log2(1+N)."""
    import math

    comps = [_comp(candidate=f"Card{i}") for i in range(5)]
    weights = _compute_idf_weights(comps)
    key = ("trigger_effect", "Sacrificed", "TokenCreation", "")
    expected = 1.0 / math.log2(1.0 + 5)
    assert abs(weights[key] - expected) < 1e-9


def test_computeidf_filter_group_segments():
    """Different filter_groups produce separate IDF buckets."""
    import math

    # 3 candidates with filter_group="Creature", 1 with "Goblin"
    creature_comps = [
        PortComplement(
            rule_id="trigger_effect",
            direction="synergy",
            candidate=f"Card{i}",
            cmdr_event="ChangesZone",
            cand_event="Token",
            filter_group="Creature",
        )
        for i in range(3)
    ]
    goblin_comp = PortComplement(
        rule_id="trigger_effect",
        direction="synergy",
        candidate="GoblinCard",
        cmdr_event="ChangesZone",
        cand_event="Token",
        filter_group="Goblin",
    )
    weights = _compute_idf_weights([*creature_comps, goblin_comp])
    creature_key = ("trigger_effect", "ChangesZone", "Token", "Creature")
    goblin_key = ("trigger_effect", "ChangesZone", "Token", "Goblin")
    # Creature group: N=3, IDF=1/log2(4)
    assert abs(weights[creature_key] - 1.0 / math.log2(4.0)) < 1e-9
    # Goblin group: N=1, IDF=1/log2(2)=1.0
    assert abs(weights[goblin_key] - 1.0) < 1e-9
    # Goblin IDF > Creature IDF (more specific = higher weight)
    assert weights[goblin_key] > weights[creature_key]


def test_score_uses_filter_group_for_idf_lookup():
    """Score correctly looks up IDF by filter_group."""
    c = PortComplement(
        rule_id="trigger_effect",
        direction="synergy",
        candidate="TestCard",
        cmdr_event="ChangesZone",
        cand_event="Token",
        filter_group="Artifact",
    )
    idf = {("trigger_effect", "ChangesZone", "Token", "Artifact"): 0.42}
    us = UniversalScore(complements=[c], staple_bonus=0.0, idf_weights=idf)
    assert abs(us.score - 0.42) < 1e-9
