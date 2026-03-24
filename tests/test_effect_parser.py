"""Tests for effect verb + target + amount extraction."""
import pytest
from mtg_synergy.parse.effect_parser import parse_effects
from mtg_synergy.parse.ast_types import Effect, Amount, TokenDef, ObjectFilter

def test_create_token():
    effects = parse_effects("Create a 1/1 red Goblin creature token.")
    assert len(effects) == 1
    e = effects[0]
    assert e.verb == "create"
    assert e.amount.value == 1
    assert e.token.subtype == "Goblin"
    assert e.token.power == 1
    assert e.token.toughness == 1

def test_create_multiple_tokens():
    effects = parse_effects("Create two 1/1 white Soldier creature tokens.")
    assert effects[0].amount.value == 2
    assert effects[0].token.subtype == "Soldier"

def test_create_x_tokens():
    effects = parse_effects("Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.")
    e = effects[0]
    assert e.amount.value == "X"
    assert e.amount.scales_with is not None
    assert e.amount.scales_with.how == "linear"
    assert "Goblins" in e.amount.scales_with.what

def test_create_token_with_keywords():
    effects = parse_effects("Create a 3/3 Kavu creature token with trample.")
    assert effects[0].token.keywords == ["trample"]
    assert effects[0].token.power == 3

def test_create_treasure():
    effects = parse_effects("Create a Treasure token.")
    assert effects[0].token.subtype == "Treasure"
    assert effects[0].token.card_type == "artifact"

def test_deal_damage_to_opponent():
    effects = parse_effects("Purphoros deals 2 damage to each opponent.")
    e = effects[0]
    assert e.verb == "deal_damage"
    assert e.amount.value == 2
    assert e.target.controller == "opponent"

def test_draw_card():
    effects = parse_effects("Draw a card.")
    assert effects[0].verb == "draw"
    assert effects[0].amount.value == 1

def test_draw_multiple():
    effects = parse_effects("Draw three cards.")
    assert effects[0].verb == "draw"
    assert effects[0].amount.value == 3

def test_destroy_target():
    effects = parse_effects("Destroy target creature.")
    assert effects[0].verb == "destroy"
    assert effects[0].target.card_type == "creature"

def test_exile():
    effects = parse_effects("Exile target permanent.")
    assert effects[0].verb == "exile"
    assert effects[0].target.card_type == "permanent"

def test_return_to_hand():
    effects = parse_effects("Return target creature to its owner's hand.")
    assert effects[0].verb == "return"
    assert effects[0].destination == "hand"
    assert effects[0].target.card_type == "creature"

def test_return_to_battlefield():
    effects = parse_effects("Return target creature card from your graveyard to the battlefield.")
    e = effects[0]
    assert e.verb == "return"
    assert e.destination == "battlefield"

def test_put_counter():
    effects = parse_effects("Put a +1/+1 counter on target creature.")
    e = effects[0]
    assert e.verb == "put_counter"
    assert e.target.card_type == "creature"

def test_put_counter_each():
    effects = parse_effects("Put a +1/+1 counter on each creature you control.")
    e = effects[0]
    assert e.verb == "put_counter"
    assert e.target.controller == "you"

def test_gain_life():
    effects = parse_effects("You gain 3 life.")
    assert effects[0].verb == "gain_life"
    assert effects[0].amount.value == 3

def test_lose_life():
    effects = parse_effects("Target opponent loses 2 life.")
    assert effects[0].verb == "lose_life"
    assert effects[0].amount.value == 2

def test_sacrifice():
    effects = parse_effects("Each opponent sacrifices a creature.")
    assert effects[0].verb == "sacrifice"
    assert effects[0].target.controller == "opponent"

def test_search_library():
    effects = parse_effects("Search your library for a basic land card, put it onto the battlefield tapped, then shuffle.")
    assert effects[0].verb == "search"

def test_mill():
    effects = parse_effects("Target player mills three cards.")
    assert effects[0].verb == "mill"
    assert effects[0].amount.value == 3

def test_add_mana():
    effects = parse_effects("Add {R}.")
    assert effects[0].verb == "add_mana"

def test_multiple_effects():
    effects = parse_effects("Target player loses 1 life and you gain 1 life.")
    assert len(effects) == 2
    assert effects[0].verb == "lose_life"
    assert effects[1].verb == "gain_life"

def test_pump():
    effects = parse_effects("Creatures you control get +1/+1 until end of turn.")
    assert effects[0].verb == "pump"

def test_grant_keyword():
    effects = parse_effects("Creatures you control have haste.")
    assert effects[0].verb == "grant_keyword"
    assert effects[0].keyword == "haste"

def test_scry():
    effects = parse_effects("Scry 2.")
    assert effects[0].verb == "scry"
    assert effects[0].amount.value == 2

def test_untap():
    effects = parse_effects("Untap target creature.")
    assert effects[0].verb == "untap"
    assert effects[0].target.card_type == "creature"
