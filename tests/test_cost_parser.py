"""Tests for activation/casting cost parsing."""
import pytest
from mtg_synergy.parse.cost_parser import parse_cost
from mtg_synergy.parse.ast_types import Cost, ManaAmount, ObjectFilter


def test_tap_only():
    c = parse_cost("{T}")
    assert c.tap is True
    assert c.mana is None
    assert c.sacrifice is None

def test_mana_only():
    c = parse_cost("{2}{G}")
    assert c.mana.total == 3
    assert c.mana.colors == {"G": 1, "generic": 2}
    assert c.tap is False

def test_mana_and_tap():
    c = parse_cost("{2}{G}{W}, {T}")
    assert c.mana.total == 4
    assert c.mana.colors == {"G": 1, "W": 1, "generic": 2}
    assert c.tap is True

def test_sacrifice_creature():
    c = parse_cost("Sacrifice a creature")
    assert c.sacrifice is not None
    assert c.sacrifice.card_type == "creature"

def test_sacrifice_typed():
    c = parse_cost("Sacrifice a Goblin")
    assert c.sacrifice.subtype == "Goblin"
    assert c.sacrifice.card_type == "creature"

def test_complex_cost():
    c = parse_cost("{2}, {T}, Sacrifice a creature")
    assert c.mana.total == 2
    assert c.tap is True
    assert c.sacrifice.card_type == "creature"

def test_pay_life():
    c = parse_cost("Pay 3 life")
    assert c.pay_life == 3

def test_discard():
    c = parse_cost("Discard a card")
    assert c.discard is not None

def test_exile_from_graveyard():
    c = parse_cost("Exile a creature card from your graveyard")
    assert c.exile is not None
    assert c.exile.card_type == "creature"
    assert c.exile.zone == "graveyard"

def test_loyalty_cost():
    c = parse_cost("+1", is_loyalty=True)
    assert c.loyalty == 1
    assert c.tap is False

def test_loyalty_negative():
    c = parse_cost("\u22123", is_loyalty=True)
    assert c.loyalty == -3

def test_mana_symbols():
    c = parse_cost("{W}{U}{B}{R}{G}")
    assert c.mana.total == 5
    assert c.mana.colors == {"W": 1, "U": 1, "B": 1, "R": 1, "G": 1}

def test_hybrid_mana():
    c = parse_cost("{W/U}{W/U}")
    assert c.mana.total == 2

def test_phyrexian_mana():
    c = parse_cost("{B/P}{B/P}")
    assert c.mana.total == 2

def test_x_mana():
    """{X} is stored as colors["X"] = 1. Total mana excludes X (only fixed costs)."""
    c = parse_cost("{X}{R}")
    assert c.mana.colors.get("R") == 1
    assert c.mana.colors.get("X") == 1
    assert c.mana.total == 1

