"""Tests for Forge filter grammar parser."""
import pytest
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter


def test_simple_card_type():
    f = parse_forge_filter("Creature")
    assert f.card_types == ["Creature"]


def test_type_with_controller():
    f = parse_forge_filter("Creature.YouCtrl")
    assert f.card_types == ["Creature"]
    assert f.controller == "YouCtrl"


def test_type_union():
    f = parse_forge_filter("Instant,Sorcery")
    assert f.card_types == ["Instant", "Sorcery"]


def test_subtype():
    f = parse_forge_filter("Goblin.YouCtrl")
    assert f.subtypes == ["Goblin"]
    assert f.controller == "YouCtrl"


def test_power_ge():
    f = parse_forge_filter("Creature.YouCtrl+powerGE4")
    assert f.card_types == ["Creature"]
    assert f.power_ge == 4


def test_cmc_ge():
    f = parse_forge_filter("Card.cmcGE5")
    assert f.card_types == ["Card"]
    assert f.cmc_ge == 5


def test_attacking():
    f = parse_forge_filter("Creature.attacking")
    assert f.card_types == ["Creature"]
    assert f.is_attacking is True


def test_token():
    f = parse_forge_filter("Creature.token")
    assert f.is_token is True


def test_other():
    f = parse_forge_filter("Creature.Other+YouCtrl")
    assert f.is_other is True
    assert f.controller == "YouCtrl"


def test_self():
    f = parse_forge_filter("Card.Self")
    assert f.is_self is True


def test_legendary():
    f = parse_forge_filter("Creature.Legendary+YouCtrl")
    assert f.is_legendary is True
    assert f.controller == "YouCtrl"


def test_with_keyword():
    f = parse_forge_filter("Creature+withFlying")
    assert f.has_keyword == "Flying"


def test_tapped_untapped():
    f = parse_forge_filter("Creature.untapped+YouCtrl")
    assert f.is_tapped is False
    assert f.controller == "YouCtrl"


def test_complex_filter():
    f = parse_forge_filter("Creature.YouCtrl+powerGE4+attacking+Other")
    assert f.card_types == ["Creature"]
    assert f.controller == "YouCtrl"
    assert f.power_ge == 4
    assert f.is_attacking is True
    assert f.is_other is True


def test_unparsed_stored_in_raw():
    f = parse_forge_filter("Card.IsRemembered+EffectSource")
    assert f.raw is not None
    assert "IsRemembered" in f.raw


def test_empty_string():
    f = parse_forge_filter("")
    assert f.card_types == []


def test_opponent_ctrl():
    f = parse_forge_filter("Creature.OppCtrl")
    assert f.controller == "OppCtrl"
