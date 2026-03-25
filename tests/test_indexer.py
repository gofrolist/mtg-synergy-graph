"""Tests for event indexing from parsed abilities."""
import pytest
from mtg_synergy.causal.indexer import CardIndex, build_index
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef, Cost, ManaAmount
)

def _make_purphoros():
    return ("purphoros", [Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", controller="you")),
        effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                        target=ObjectFilter(controller="opponent"))],
    )])

def _make_krenko():
    return ("krenko", [Ability(
        kind="activated", cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value="X"),
                        token=TokenDef(card_type="creature", subtype="Goblin",
                                       power=1, toughness=1, keywords=[], color="red"))],
    )])

def _make_phyrexian_altar():
    return ("altar", [Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )])

def test_build_index_producers():
    cards = dict([_make_krenko()])
    idx = build_index(cards)
    events = idx.events_produced_by("krenko")
    event_names = {e.event for e in events}
    assert "enters_the_battlefield" in event_names
    assert "creature_enters" in event_names

def test_build_index_responders():
    cards = dict([_make_purphoros()])
    idx = build_index(cards)
    assert "purphoros" in idx.cards_responding_to("enters_the_battlefield")

def test_build_index_consumers():
    cards = dict([_make_phyrexian_altar()])
    idx = build_index(cards)
    assert "altar" in idx.cards_consuming("creature")

def test_producers_for_event():
    cards = dict([_make_krenko(), _make_purphoros()])
    idx = build_index(cards)
    producers = idx.cards_producing("creature_enters")
    assert "krenko" in producers
    assert "purphoros" not in producers

def test_index_empty():
    idx = build_index({})
    assert idx.cards_producing("anything") == []
    assert idx.cards_responding_to("anything") == []
