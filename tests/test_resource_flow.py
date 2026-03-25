"""Tests for resource cost/production tracking in loops."""
import pytest
from mtg_synergy.causal.resource_flow import compute_ability_resources, compute_cycle_delta
from mtg_synergy.causal.types import ResourceDelta
from mtg_synergy.parse.ast_types import (
    Ability, Effect, Amount, TokenDef, Cost, ObjectFilter, ManaAmount
)

def test_krenko_resources():
    ability = Ability(
        kind="activated", cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value="X"),
                        token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
    )
    cost, prod = compute_ability_resources(ability)
    assert cost.tap is True
    assert cost.mana == 0
    assert prod.creatures == "X"

def test_altar_resources():
    ability = Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )
    cost, prod = compute_ability_resources(ability)
    assert cost.creatures == 1
    assert prod.mana == 1

def test_cycle_delta_positive():
    krenko = Ability(
        kind="activated", cost=Cost(tap=True),
        effects=[Effect(verb="create", amount=Amount(value=3),
                        token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
    )
    altar = Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )
    delta = compute_cycle_delta([krenko, altar])
    assert delta.creatures > 0  # 3 - 1 = +2
    assert delta.mana > 0       # 1 - 0 = +1

def test_cycle_delta_negative():
    expensive = Ability(
        kind="activated",
        cost=Cost(mana=ManaAmount(total=5, colors={"generic": 5})),
        effects=[Effect(verb="create", amount=Amount(value=1),
                        token=TokenDef("creature", "Goblin", 1, 1, [], "red"))],
    )
    altar = Ability(
        kind="activated",
        cost=Cost(sacrifice=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="add_mana", amount=Amount(value=1))],
    )
    delta = compute_cycle_delta([expensive, altar])
    assert delta.mana < 0  # 1 - 5 = -4

def test_draw_production():
    ability = Ability(
        kind="triggered",
        effects=[Effect(verb="draw", amount=Amount(value=2))],
    )
    _, prod = compute_ability_resources(ability)
    assert prod.cards == 2

def test_life_cost():
    ability = Ability(
        kind="activated",
        cost=Cost(pay_life=3),
        effects=[Effect(verb="draw", amount=Amount(value=1))],
    )
    cost, prod = compute_ability_resources(ability)
    assert cost.life == 3
    assert prod.cards == 1
