"""Tests for event IDF computation in CardIndex."""
import math
import pytest
from mtg_synergy.causal.indexer import CardIndex, build_index
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost
)


def _make_krenko():
    """Creates Goblin tokens -> produces creature_enters + goblin-specific events."""
    return ("krenko", [Ability(
        kind="activated", cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value="X"),
                        token=TokenDef(card_type="creature", subtype="Goblin",
                                       power=1, toughness=1, keywords=[], color="red"))],
    )])


def _make_purphoros():
    """Triggers on creature entering -> responds to enters_the_battlefield."""
    return ("purphoros", [Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", controller="you")),
        effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                        target=ObjectFilter(controller="opponent"))],
    )])


def _make_cathars():
    """Triggers on creature entering -> responds to enters_the_battlefield."""
    return ("cathars", [Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", controller="you")),
        effects=[Effect(verb="put_counter", amount=Amount(value=1),
                        target=ObjectFilter(card_type="creature", controller="you"))],
    )])


def _make_goblin_sharpshooter():
    """Triggers on creature dying -> responds to dies."""
    return ("sharpshooter", [Ability(
        kind="triggered",
        trigger=Trigger(event="dies",
                        subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="untap", target=ObjectFilter(name="self"))],
    )])


def test_producer_counts():
    cards = dict([_make_krenko()])
    idx = build_index(cards)
    assert hasattr(idx, 'producer_counts')
    assert idx.producer_counts.get("creature_enters", 0) >= 1


def test_responder_counts():
    cards = dict([_make_purphoros(), _make_cathars()])
    idx = build_index(cards)
    assert hasattr(idx, 'responder_counts')
    assert idx.responder_counts.get("enters_the_battlefield", 0) == 2


def test_compute_event_idf():
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    idx = build_index(cards)
    idf = idx.compute_event_idf()
    assert "enters_the_battlefield" in idf["responder"]
    assert "dies" in idf["responder"]
    assert idf["responder"]["dies"] > idf["responder"]["enters_the_battlefield"]


def test_idf_range():
    """IDF values must be in 0.3-3.0 range."""
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    idx = build_index(cards)
    idf = idx.compute_event_idf()
    for side in ("producer", "responder"):
        for event, value in idf[side].items():
            assert 0.3 <= value <= 3.0, f"{side} IDF for {event} = {value} out of range"


# ---------------------------------------------------------------------------
# IDF-weighted edge building tests
# ---------------------------------------------------------------------------

from mtg_synergy.causal.graph_builder import build_causal_edges


def test_trigger_edges_use_idf():
    """Rare event edges should have higher strength than common event edges."""
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    edges = build_causal_edges(cards)
    kr_to_purph = [e for e in edges if e.source == "krenko" and e.target == "purphoros"
                   and e.edge_type == "triggers"]
    kr_to_cathars = [e for e in edges if e.source == "krenko" and e.target == "cathars"
                     and e.edge_type == "triggers"]
    assert len(kr_to_purph) >= 1
    assert len(kr_to_cathars) >= 1
    for e in kr_to_purph:
        # creature_enters is common (multiple responders), so IDF dampens it below 0.6
        assert e.strength < 0.6, f"Edge strength {e.strength} should be IDF-dampened below 0.6"


def test_combined_idf_capped():
    """Combined producer × responder IDF product must not exceed 3.0."""
    cards = dict([_make_krenko(), _make_purphoros(), _make_cathars(),
                  _make_goblin_sharpshooter()])
    edges = build_causal_edges(cards)
    for e in edges:
        if e.edge_type == "triggers":
            assert e.strength <= 3.0, f"Edge {e.source}->{e.target} strength {e.strength} exceeds cap"
