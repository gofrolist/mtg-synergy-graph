"""Tests for Effect -> StateChange mapping (rules engine)."""
import pytest
from mtg_synergy.parse.verb_resolvers import resolve_effect, StateChange
from mtg_synergy.parse.ast_types import Effect, Amount, TokenDef, ObjectFilter


def test_create_creature_token():
    effect = Effect(
        verb="create", amount=Amount(value=2),
        token=TokenDef(card_type="creature", subtype="Goblin",
                       power=1, toughness=1, keywords=[], color="red"),
    )
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "enters_the_battlefield" in events
    assert "creature_enters" in events
    etb = [sc for sc in changes if sc.event == "enters_the_battlefield"][0]
    assert etb.object.subtype == "Goblin"
    assert etb.object.is_token is True
    assert etb.quantity.value == 2

def test_create_treasure_token():
    effect = Effect(
        verb="create", amount=Amount(value=1),
        token=TokenDef(card_type="artifact", subtype="Treasure",
                       power=None, toughness=None, keywords=[], color=None),
    )
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "enters_the_battlefield" in events
    assert "artifact_enters" in events
    assert "creature_enters" not in events

def test_destroy_creature():
    effect = Effect(verb="destroy", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "dies" in events
    assert "leaves_the_battlefield" in events
    assert "enters_graveyard" in events

def test_destroy_noncreature():
    effect = Effect(verb="destroy", amount=Amount(value=1),
                    target=ObjectFilter(card_type="artifact"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "dies" not in events
    assert "leaves_the_battlefield" in events

def test_sacrifice():
    effect = Effect(verb="sacrifice", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    dies = [sc for sc in changes if sc.event == "dies"]
    assert len(dies) == 1
    assert dies[0].controller == "you"

def test_deal_damage_to_player():
    effect = Effect(verb="deal_damage", amount=Amount(value=3),
                    target=ObjectFilter(controller="opponent"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "damage_dealt" in events
    assert "life_lost" in events

def test_deal_damage_to_creature():
    effect = Effect(verb="deal_damage", amount=Amount(value=3),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "damage_dealt" in events
    assert "may_die" in events
    assert "life_lost" not in events

def test_draw():
    effect = Effect(verb="draw", amount=Amount(value=2))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "card_drawn" in events
    assert changes[0].quantity.value == 2

def test_discard():
    effect = Effect(verb="discard", amount=Amount(value=1))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "card_discarded" in events
    assert "enters_graveyard" in events

def test_exile():
    effect = Effect(verb="exile", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "leaves_the_battlefield" in events
    assert "enters_exile" in events
    assert "dies" not in events

def test_return_to_battlefield():
    effect = Effect(verb="return", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"),
                    destination="battlefield")
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "enters_the_battlefield" in events
    assert "creature_enters" in events

def test_return_to_hand():
    effect = Effect(verb="return", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"),
                    destination="hand")
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "leaves_the_battlefield" in events
    assert "enters_the_battlefield" not in events

def test_put_counter():
    effect = Effect(verb="put_counter", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    assert any(sc.event == "counter_placed" for sc in changes)

def test_gain_life():
    effect = Effect(verb="gain_life", amount=Amount(value=3))
    changes = resolve_effect(effect)
    assert any(sc.event == "life_gained" for sc in changes)

def test_lose_life():
    effect = Effect(verb="lose_life", amount=Amount(value=2),
                    target=ObjectFilter(controller="opponent"))
    changes = resolve_effect(effect)
    assert any(sc.event == "life_lost" for sc in changes)

def test_mill():
    effect = Effect(verb="mill", amount=Amount(value=3),
                    target=ObjectFilter(controller="opponent"))
    changes = resolve_effect(effect)
    assert any(sc.event == "enters_graveyard" for sc in changes)

def test_add_mana():
    effect = Effect(verb="add_mana", amount=Amount(value=1))
    changes = resolve_effect(effect)
    assert len(changes) == 0

def test_untap():
    effect = Effect(verb="untap", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    assert any(sc.event == "untapped" for sc in changes)

def test_unknown_verb():
    effect = Effect(verb="unknown_ability", amount=Amount(value=1))
    changes = resolve_effect(effect)
    assert len(changes) == 0
