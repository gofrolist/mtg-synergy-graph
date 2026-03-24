"""Tests for trigger event + subject filter extraction."""
import pytest
from mtg_synergy.parse.trigger_parser import parse_trigger
from mtg_synergy.parse.ast_types import Trigger, ObjectFilter, Condition


def test_creature_enters():
    t = parse_trigger("Whenever a creature enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "creature"
    assert t.subject.controller == "you"

def test_goblin_enters():
    t = parse_trigger("Whenever a Goblin enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.subtype == "Goblin"
    assert t.subject.controller == "you"

def test_creature_dies():
    t = parse_trigger("Whenever a creature dies")
    assert t.event == "dies"
    assert t.subject.card_type == "creature"

def test_another_creature_dies():
    t = parse_trigger("Whenever another creature dies")
    assert t.event == "dies"
    assert t.subject.is_another is True
    assert t.subject.card_type == "creature"

def test_nontoken_creature_enters():
    t = parse_trigger("Whenever a nontoken creature enters the battlefield")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "creature"
    assert t.subject.is_token is False

def test_upkeep():
    t = parse_trigger("At the beginning of your upkeep")
    assert t.event == "upkeep"
    assert t.subject is None or t.subject.controller == "you"

def test_end_step():
    t = parse_trigger("At the beginning of each end step")
    assert t.event == "end_step"

def test_deals_combat_damage():
    t = parse_trigger("Whenever this creature deals combat damage to a player")
    assert t.event == "deals_combat_damage"
    assert t.subject is not None

def test_spell_cast_opponent():
    t = parse_trigger("Whenever an opponent casts a spell")
    assert t.event == "cast"
    assert t.subject.controller == "opponent"

def test_spell_cast_typed():
    t = parse_trigger("Whenever you cast an instant or sorcery spell")
    assert t.event == "cast"
    assert t.subject.controller == "you"

def test_attacks():
    t = parse_trigger("Whenever this creature attacks")
    assert t.event == "attacks"

def test_land_enters():
    t = parse_trigger("Whenever a land enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "land"
    assert t.subject.controller == "you"

def test_gain_life():
    t = parse_trigger("Whenever you gain life")
    assert t.event == "life_gained"

def test_discard():
    t = parse_trigger("Whenever a player discards a card")
    assert t.event == "discard"

def test_condition_extracted():
    t = parse_trigger("Whenever a creature enters the battlefield under your control, if you control five or more creatures")
    assert t.event == "enters_the_battlefield"
    assert t.condition is not None
    assert t.condition.restrictiveness in ("mild", "severe")

def test_equipped_creature():
    t = parse_trigger("Whenever equipped creature deals combat damage to a player")
    assert t.event == "deals_combat_damage"

def test_artifact_enters():
    t = parse_trigger("Whenever an artifact enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "artifact"
    assert t.subject.controller == "you"

def test_enchantment_enters():
    t = parse_trigger("Whenever an enchantment enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "enchantment"

def test_counter_placed():
    t = parse_trigger("Whenever one or more +1/+1 counters are put on a creature you control")
    assert t.event == "counter_placed"
    assert t.subject.card_type == "creature"
    assert t.subject.controller == "you"
