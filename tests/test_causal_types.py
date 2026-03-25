# tests/test_causal_types.py
"""Tests for causal graph dataclasses."""
import json
import pytest


def test_edge_detail():
    from mtg_synergy.causal.types import EdgeDetail
    d = EdgeDetail(event="enters_the_battlefield", filter_precision="exact")
    assert d.event == "enters_the_battlefield"
    assert d.filter_precision == "exact"
    assert d.resource is None

def test_edge():
    from mtg_synergy.causal.types import Edge, EdgeDetail
    e = Edge(source="card-a", target="card-b", edge_type="triggers",
             ability_a=0, ability_b=0, strength=1.0,
             detail=EdgeDetail(event="dies", filter_precision="exact"))
    assert e.source == "card-a"
    assert e.edge_type == "triggers"
    assert e.detail.event == "dies"

def test_resource_delta():
    from mtg_synergy.causal.types import ResourceDelta
    rd = ResourceDelta(mana=1, creatures=-1, cards=0, life=0)
    assert not rd.is_positive  # creatures negative

def test_resource_delta_positive():
    from mtg_synergy.causal.types import ResourceDelta
    rd = ResourceDelta(mana=1, creatures=2, cards=0, life=0)
    assert rd.is_positive

def test_resource_delta_negative():
    from mtg_synergy.causal.types import ResourceDelta
    rd = ResourceDelta(mana=-2, creatures=0, cards=0, life=0)
    assert not rd.is_positive

def test_loop_analysis():
    from mtg_synergy.causal.types import LoopAnalysis
    la = LoopAnalysis(is_infinite="confirmed", min_board_requirement="2+ Goblins",
                      resource_deltas={"mana": 1, "creatures": 1}, growth_pattern="exponential")
    assert la.is_infinite == "confirmed"
    assert la.growth_pattern == "exponential"

def test_chain():
    from mtg_synergy.causal.types import Chain, Edge, EdgeDetail, ResourceDelta
    c = Chain(
        cards=["commander", "card-a", "card-b"],
        edges=[Edge("commander", "card-a", "triggers", 0, 0, 1.0,
                     EdgeDetail(event="enters_the_battlefield", filter_precision="exact")),
               Edge("card-a", "card-b", "triggers", 0, 0, 0.8,
                     EdgeDetail(event="dies", filter_precision="broad"))],
        chain_type="linear", output="damage",
        resource_delta=ResourceDelta(mana=0, creatures=0, cards=0, life=0))
    assert len(c.cards) == 3
    assert c.chain_type == "linear"

def test_edge_to_dict():
    from mtg_synergy.causal.types import Edge, EdgeDetail
    e = Edge("a", "b", "triggers", 0, 1, 0.9, EdgeDetail(event="dies", filter_precision="exact"))
    d = e.to_dict()
    assert d["source"] == "a"
    assert d["detail"]["event"] == "dies"
    j = json.dumps(d)
    assert "dies" in j
