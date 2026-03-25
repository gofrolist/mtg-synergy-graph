"""Tests for N-card chain discovery and loop detection."""
import pytest
from mtg_synergy.causal.chain_finder import find_chains, find_loops
from mtg_synergy.causal.types import Edge, EdgeDetail, Chain
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost, Restrictions
)


def _build_edge(src, tgt, event="enters_the_battlefield", strength=1.0,
                edge_type="triggers", precision="exact"):
    return Edge(src, tgt, edge_type, 0, 0, strength,
                EdgeDetail(event=event, filter_precision=precision))


def test_linear_chain_2_cards():
    edges = [_build_edge("commander", "card_a", "enters_the_battlefield")]
    chains = find_chains("commander", edges, max_depth=3)
    assert len(chains) >= 1
    assert chains[0].cards == ["commander", "card_a"]
    assert chains[0].chain_type == "linear"

def test_linear_chain_3_cards():
    edges = [
        _build_edge("commander", "card_a", "enters_the_battlefield"),
        _build_edge("card_a", "card_b", "dies"),
    ]
    chains = find_chains("commander", edges, max_depth=3)
    chain_3 = [c for c in chains if len(c.cards) == 3]
    assert len(chain_3) >= 1
    assert chain_3[0].cards == ["commander", "card_a", "card_b"]

def test_depth_limit():
    edges = [
        _build_edge("c", "a"),
        _build_edge("a", "b"),
        _build_edge("b", "d"),
        _build_edge("d", "e"),
    ]
    chains = find_chains("c", edges, max_depth=3)
    assert all(len(c.cards) <= 3 for c in chains)

def test_no_duplicate_cards_in_chain():
    edges = [
        _build_edge("c", "a"),
        _build_edge("a", "c"),
    ]
    chains = find_chains("c", edges, max_depth=5)
    linear = [c for c in chains if c.chain_type == "linear"]
    for chain in linear:
        assert len(chain.cards) == len(set(chain.cards))

def test_loop_detection():
    edges = [
        _build_edge("a", "b"),
        _build_edge("b", "c"),
        _build_edge("c", "a"),
    ]
    abilities = {
        "a": [Ability(kind="activated", cost=Cost(tap=True),
                       effects=[Effect(verb="create", amount=Amount(value=3),
                                       token=TokenDef("creature", "Goblin", 1, 1, [], "red"))])],
        "b": [Ability(kind="activated",
                       cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
                       effects=[Effect(verb="add_mana", amount=Amount(value=1))])],
        "c": [Ability(kind="triggered",
                       effects=[Effect(verb="untap", amount=Amount(value=1),
                                       target=ObjectFilter(card_type="creature"))])],
    }
    loops = find_loops(edges, abilities, max_loop_size=4)
    assert len(loops) >= 1
    assert loops[0].chain_type == "loop"

def test_once_per_turn_blocks_loop():
    edges = [
        _build_edge("a", "b"),
        _build_edge("b", "a"),
    ]
    abilities = {
        "a": [Ability(kind="activated", cost=Cost(tap=True),
                       effects=[Effect(verb="create", amount=Amount(value=1),
                                       token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
                       restrictions=Restrictions(once_per_turn=True))],
        "b": [Ability(kind="triggered",
                       effects=[Effect(verb="untap", amount=Amount(value=1),
                                       target=ObjectFilter(card_type="creature"))])],
    }
    loops = find_loops(edges, abilities, max_loop_size=4)
    confirmed = [l for l in loops if l.loop_analysis and l.loop_analysis.is_infinite == "confirmed"]
    assert len(confirmed) == 0

def test_chain_ranking():
    edges = [
        _build_edge("c", "a", strength=1.0),
        _build_edge("c", "b", strength=1.0),
        _build_edge("a", "d", strength=1.0),
    ]
    chains = find_chains("c", edges, max_depth=5)
    short = [c for c in chains if len(c.cards) == 2]
    long = [c for c in chains if len(c.cards) == 3]
    if short and long:
        assert max(c.score for c in short) >= max(c.score for c in long)
