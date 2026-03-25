"""Tests for causal edge building between card pairs."""
import pytest
from mtg_synergy.causal.graph_builder import build_causal_edges
from mtg_synergy.causal.types import Edge
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost
)

def _make_card(name, abilities):
    return (name, abilities)

def test_trigger_edge_krenko_purphoros():
    cards = dict([
        _make_card("krenko", [Ability(
            kind="activated", cost=Cost(tap=True),
            effects=[Effect(verb="create", amount=Amount(value=2),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
        )]),
        _make_card("purphoros", [Ability(
            kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature", controller="you")),
            effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                            target=ObjectFilter(controller="opponent"))],
        )]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"
                     and e.source == "krenko" and e.target == "purphoros"]
    assert len(trigger_edges) >= 1
    assert trigger_edges[0].detail.event == "enters_the_battlefield"
    assert trigger_edges[0].strength >= 0.5

def test_trigger_edge_exact_subtype():
    cards = dict([
        _make_card("producer", [Ability(kind="activated",
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))])]),
        _make_card("responder", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature", subtype="Goblin")),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) >= 1
    assert trigger_edges[0].detail.filter_precision == "exact"
    assert trigger_edges[0].strength == 1.0

def test_trigger_edge_broad_match():
    cards = dict([
        _make_card("producer", [Ability(kind="activated",
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))])]),
        _make_card("responder", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) >= 1
    assert trigger_edges[0].detail.filter_precision == "broad"
    assert trigger_edges[0].strength == pytest.approx(0.6, abs=0.1)

def test_no_self_edge():
    cards = dict([_make_card("card", [
        Ability(kind="activated",
                effects=[Effect(verb="create", amount=Amount(value=1),
                                token=TokenDef("creature", "Goblin", 1, 1, [], "red"))]),
        Ability(kind="triggered",
                trigger=Trigger(event="enters_the_battlefield",
                                subject=ObjectFilter(card_type="creature")),
                effects=[Effect(verb="draw", amount=Amount(value=1))]),
    ])])
    edges = build_causal_edges(cards)
    assert all(e.source != e.target for e in edges)

def test_no_match_nontoken_filter():
    cards = dict([
        _make_card("producer", [Ability(kind="activated",
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))])]),
        _make_card("responder", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature", is_token=False)),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) == 0

def test_feeds_edge_altar():
    cards = dict([
        _make_card("krenko", [Ability(kind="activated", cost=Cost(tap=True),
            effects=[Effect(verb="create", amount=Amount(value=2),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))])]),
        _make_card("altar", [Ability(kind="activated",
            cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="add_mana", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards)
    feeds = [e for e in edges if e.edge_type == "feeds"
             and e.source == "krenko" and e.target == "altar"]
    assert len(feeds) >= 1
    assert feeds[0].detail.resource == "creature"

def test_no_edges_unrelated():
    cards = dict([
        _make_card("ramp", [Ability(kind="activated",
            effects=[Effect(verb="add_mana", amount=Amount(value=1))])]),
        _make_card("draw", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards)
    trigger_edges = [e for e in edges if e.edge_type == "triggers"]
    assert len(trigger_edges) == 0

def test_bidirectional_edges():
    cards = dict([
        _make_card("a", [Ability(kind="triggered",
            trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Zombie", 2, 2, [], "black"))])]),
        _make_card("b", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="sacrifice", amount=Amount(value=1),
                            target=ObjectFilter(card_type="creature"))])]),
    ])
    edges = build_causal_edges(cards)
    a_to_b = [e for e in edges if e.source == "a" and e.target == "b"]
    b_to_a = [e for e in edges if e.source == "b" and e.target == "a"]
    assert len(a_to_b) >= 1
    assert len(b_to_a) >= 1
