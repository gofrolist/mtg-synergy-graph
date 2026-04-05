"""Tests for Forge-native AST types."""
import pytest
from mtg_synergy_train.parse.forge_types import ForgeFilter, ForgeTrigger, ForgeEffect


def test_forge_filter_defaults():
    f = ForgeFilter()
    assert f.card_types == []
    assert f.controller is None
    assert f.raw is None


def test_forge_filter_creature():
    f = ForgeFilter(card_types=["Creature"], controller="YouCtrl")
    assert f.card_types == ["Creature"]
    assert f.controller == "YouCtrl"


def test_forge_filter_multi_type():
    f = ForgeFilter(card_types=["Instant", "Sorcery"])
    assert len(f.card_types) == 2


def test_forge_filter_with_stats():
    f = ForgeFilter(card_types=["Creature"], power_ge=4, is_attacking=True)
    assert f.power_ge == 4
    assert f.is_attacking is True


def test_forge_trigger_changes_zone():
    t = ForgeTrigger(mode="ChangesZone", origin="Any", destination="Battlefield")
    assert t.mode == "ChangesZone"
    assert t.destination == "Battlefield"


def test_forge_trigger_spell_cast():
    t = ForgeTrigger(mode="SpellCast",
                     valid_card=ForgeFilter(card_types=["Card"]),
                     trigger_zones=["Battlefield"])
    assert t.mode == "SpellCast"
    assert t.valid_card.card_types == ["Card"]


def test_forge_effect_deal_damage():
    e = ForgeEffect(verb="DealDamage", num_damage=3,
                    target=ForgeFilter(card_types=["Creature"]))
    assert e.verb == "DealDamage"
    assert e.num_damage == 3


def test_forge_effect_draw():
    e = ForgeEffect(verb="Draw", num_cards=1, defined="You")
    assert e.verb == "Draw"
    assert e.num_cards == 1


def test_forge_effect_token():
    e = ForgeEffect(verb="Token", token_script="g_1_1_goblin")
    assert e.verb == "Token"


def test_forge_effect_change_zone():
    e = ForgeEffect(verb="ChangeZone", zone_origin="Graveyard",
                    zone_destination="Battlefield")
    assert e.zone_origin == "Graveyard"


def test_forge_effect_optional():
    e = ForgeEffect(verb="Draw", optional=True)
    assert e.optional is True


