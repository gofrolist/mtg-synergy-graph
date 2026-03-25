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


# ---------------------------------------------------------------------------
# Amplifies edges
# ---------------------------------------------------------------------------

def test_amplifies_counter_replacement():
    """Hardened Scales amplifies cards that put counters."""
    cards = dict([
        _make_card("scales", [Ability(kind="replacement", effects=[])]),
        _make_card("counter_card", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="put_counter", amount=Amount(value=1),
                            target=ObjectFilter(card_type="creature"))])]),
    ])
    edges = build_causal_edges(cards, oracle_texts={"scales": "If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead."})
    amp_edges = [e for e in edges if e.edge_type == "amplifies" and e.source == "scales"]
    assert len(amp_edges) >= 1
    assert amp_edges[0].target == "counter_card"


def test_amplifies_token_replacement():
    """Anointed Procession amplifies token creators."""
    cards = dict([
        _make_card("procession", [Ability(kind="replacement", effects=[])]),
        _make_card("token_maker", [Ability(kind="activated",
            effects=[Effect(verb="create", amount=Amount(value=1),
                            token=TokenDef("creature", "Goblin", 1, 1, [], "red"))])]),
    ])
    edges = build_causal_edges(cards, oracle_texts={"procession": "If an effect would create one or more tokens under your control, it creates twice that many of those tokens instead."})
    amp_edges = [e for e in edges if e.edge_type == "amplifies"]
    assert len(amp_edges) >= 1


def test_amplifies_trigger_modifier():
    """Panharmonicon amplifies ETB triggered abilities."""
    cards = dict([
        _make_card("panharmonicon", [Ability(kind="trigger_modifier", effects=[])]),
        _make_card("etb_card", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards, oracle_texts={"panharmonicon": "If an artifact or creature entering causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time."})
    amp_edges = [e for e in edges if e.edge_type == "amplifies"]
    assert len(amp_edges) >= 1
    assert amp_edges[0].source == "panharmonicon"
    assert amp_edges[0].target == "etb_card"


def test_no_amplifies_wrong_event():
    """Teysa Karlov (dies modifier) doesn't amplify ETB triggers."""
    cards = dict([
        _make_card("teysa", [Ability(kind="trigger_modifier", effects=[])]),
        _make_card("etb_card", [Ability(kind="triggered",
            trigger=Trigger(event="enters_the_battlefield",
                            subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards, oracle_texts={"teysa": "If a creature dying causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time."})
    amp_edges = [e for e in edges if e.edge_type == "amplifies"
                 and e.source == "teysa" and e.target == "etb_card"]
    assert len(amp_edges) == 0


# ---------------------------------------------------------------------------
# Enables edges
# ---------------------------------------------------------------------------

def test_enables_haste_for_tap():
    """Haste granter enables tap-ability cards."""
    cards = dict([
        _make_card("haste_giver", [Ability(kind="static",
            effects=[Effect(verb="grant_keyword", amount=Amount(value=1),
                            keyword="haste")])]),
        _make_card("tap_card", [Ability(kind="activated", cost=Cost(tap=True),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards)
    enable_edges = [e for e in edges if e.edge_type == "enables"
                    and e.source == "haste_giver" and e.target == "tap_card"]
    assert len(enable_edges) >= 1


def test_no_enables_no_tap():
    """Haste granter doesn't enable non-tap abilities."""
    cards = dict([
        _make_card("haste_giver", [Ability(kind="static",
            effects=[Effect(verb="grant_keyword", amount=Amount(value=1),
                            keyword="haste")])]),
        _make_card("no_tap", [Ability(kind="triggered",
            trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
            effects=[Effect(verb="draw", amount=Amount(value=1))])]),
    ])
    edges = build_causal_edges(cards)
    enable_edges = [e for e in edges if e.edge_type == "enables"]
    assert len(enable_edges) == 0
