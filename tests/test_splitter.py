# tests/test_splitter.py
"""Tests for Pass 1 (split) and Pass 2 (classify) of the oracle parser."""
import pytest
from mtg_synergy.parse.splitter import split_abilities


def test_split_simple_newlines():
    """Multiple abilities separated by newlines."""
    text = "Flying\nWhenever a creature enters the battlefield, draw a card."
    abilities = split_abilities(text)
    assert len(abilities) == 2
    assert abilities[0].kind == "keyword"
    assert abilities[0].raw_text == "Flying"
    assert abilities[1].kind == "triggered"


def test_split_activated():
    """{T}: effect is activated."""
    text = "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "activated"
    assert abilities[0].cost_text == "{T}"
    assert "Create" in abilities[0].effect_text


def test_split_activated_complex_cost():
    """{2}, {T}, Sacrifice a creature: effect."""
    text = "{2}, {T}, Sacrifice a creature: Draw a card."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "activated"
    assert "{2}" in abilities[0].cost_text
    assert "Sacrifice" in abilities[0].cost_text
    assert abilities[0].effect_text == "Draw a card."


def test_split_planeswalker():
    """Loyalty abilities split on +N:, -N:, 0:."""
    text = "+1: Create a 3/3 Kavu creature token with trample.\n\u22123: Put +1/+1 counters on target creature.\n\u22126: Draw five cards."
    abilities = split_abilities(text)
    assert len(abilities) == 3
    assert abilities[0].kind == "activated"
    assert abilities[0].loyalty_cost == 1
    assert abilities[1].loyalty_cost == -3
    assert abilities[2].loyalty_cost == -6


def test_split_saga():
    """Saga chapters split on I \u2014, II \u2014, etc."""
    text = "I \u2014 Destroy target nonland permanent.\nII \u2014 Search your library for a Forest card.\nIII \u2014 Exile this Saga."
    abilities = split_abilities(text)
    assert len(abilities) == 3
    assert abilities[0].kind == "triggered"
    assert abilities[0].chapter == 1
    assert abilities[1].chapter == 2


def test_split_replacement_effect():
    """If...would...instead is a replacement."""
    text = "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "replacement"


def test_split_static():
    """No trigger/cost/if-would \u2192 static."""
    text = "Creatures you control get +1/+1."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "static"


def test_split_trigger_modifier():
    """Panharmonicon-style trigger doublers."""
    text = "If a permanent entering the battlefield causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "trigger_modifier"


def test_split_dfc():
    """Double-faced card: split on ' // '."""
    text = "At the beginning of your upkeep, look at the top card. // Flying"
    abilities = split_abilities(text)
    assert len(abilities) == 2
    assert abilities[0].kind == "triggered"
    assert abilities[1].kind == "keyword"


def test_split_reminder_text_stripped():
    """Reminder text in parens is stripped but preserved."""
    text = "Discover 5 (Exile cards from the top of your library until you exile a nonland card with mana value 5 or less. Cast it without paying its mana cost or put it into your hand. Put the rest on the bottom in a random order.)"
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "keyword"
    assert abilities[0].reminder_text is not None
    assert "Exile cards" in abilities[0].reminder_text


def test_split_modal():
    """'Choose one \u2014' splits into mode lines."""
    text = "Choose one \u2014\n\u2022 Destroy target artifact.\n\u2022 Destroy target enchantment."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "modal"
    assert len(abilities[0].modes) == 2


def test_skip_flavor_rules_text():
    """Commander designation text is skipped."""
    text = "{T}: Add {G}.\nJared Carthalion can be your commander."
    abilities = split_abilities(text)
    assert len(abilities) == 1


def test_split_restriction_detection():
    """Restrictions parsed from ability text."""
    text = "{T}: Draw a card. Activate only once each turn."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].restrictions_text == "Activate only once each turn."


def test_when_whenever_at():
    """All trigger words classify as triggered."""
    for text in [
        "When this creature enters the battlefield, draw a card.",
        "Whenever a creature dies, gain 1 life.",
        "At the beginning of your upkeep, scry 1.",
    ]:
        abilities = split_abilities(text)
        assert abilities[0].kind == "triggered", f"Failed for: {text}"
