"""End-to-end tests: raw oracle text -> fully parsed Ability list."""
import pytest
from mtg_synergy.parse import parse_card


def test_purphoros():
    abilities = parse_card(
        oracle_text="Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.",
        type_line="Legendary Enchantment Creature — God",
        mana_cost="{3}{R}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "triggered"
    assert a.trigger.event == "enters_the_battlefield"
    assert a.trigger.subject.card_type == "creature"
    assert a.trigger.subject.controller == "you"
    assert a.effects[0].verb == "deal_damage"
    assert a.effects[0].amount.value == 2

def test_krenko():
    abilities = parse_card(
        oracle_text="{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        type_line="Legendary Creature — Goblin Warrior",
        mana_cost="{2}{R}{R}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "activated"
    assert a.cost.tap is True
    assert a.effects[0].verb == "create"
    assert a.effects[0].token.subtype == "Goblin"
    assert a.effects[0].amount.value == "X"
    assert a.effects[0].amount.scales_with is not None

def test_hardened_scales():
    abilities = parse_card(
        oracle_text="If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
        type_line="Enchantment",
        mana_cost="{G}",
    )
    assert len(abilities) >= 1
    assert abilities[0].kind == "replacement"

def test_blood_artist():
    abilities = parse_card(
        oracle_text="Whenever Blood Artist or another creature dies, target player loses 1 life and you gain 1 life.",
        type_line="Creature — Vampire",
        mana_cost="{1}{B}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "triggered"
    assert a.trigger.event == "dies"
    assert len(a.effects) == 2

def test_phyrexian_altar():
    abilities = parse_card(
        oracle_text="Sacrifice a creature: Add one mana of any color.",
        type_line="Artifact",
        mana_cost="{3}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "activated"
    assert a.cost.sacrifice is not None
    assert a.cost.sacrifice.card_type == "creature"
    assert a.effects[0].verb == "add_mana"

def test_syr_konrad():
    abilities = parse_card(
        oracle_text="Whenever another creature dies, or a creature card is put into a graveyard from anywhere other than the battlefield, or a creature card leaves your graveyard, Syr Konrad, the Grim deals 1 damage to each opponent.\n{1}{B}: Each player mills a card.",
        type_line="Legendary Creature — Human Knight",
        mana_cost="{3}{B}{B}",
    )
    assert len(abilities) >= 2
    assert abilities[0].kind == "triggered"
    assert abilities[0].effects[0].verb == "deal_damage"
    assert abilities[1].kind == "activated"
    assert abilities[1].effects[0].verb == "mill"

def test_panharmonicon():
    abilities = parse_card(
        oracle_text="If a permanent entering the battlefield causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time.",
        type_line="Artifact",
        mana_cost="{4}",
    )
    assert len(abilities) >= 1
    assert abilities[0].kind == "trigger_modifier"

def test_rhystic_study():
    abilities = parse_card(
        oracle_text="Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.",
        type_line="Enchantment",
        mana_cost="{2}{U}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "triggered"
    assert a.trigger.event == "cast"
    assert a.trigger.subject.controller == "opponent"
    assert a.effects[0].verb == "draw"

def test_kyler_two_abilities():
    abilities = parse_card(
        oracle_text="Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler.",
        type_line="Legendary Creature — Human Cleric",
        mana_cost="{3}{G}{W}",
    )
    assert len(abilities) == 2
    assert abilities[0].kind == "triggered"
    assert abilities[0].trigger.subject.subtype == "Human"
    assert abilities[1].kind == "static"

def test_jace_planeswalker():
    abilities = parse_card(
        oracle_text="+2: Look at the top card of target player's library. You may put that card on the bottom of that player's library.\n0: Draw three cards, then put two cards from your hand on top of your library.\n\u22121: Return target creature to its owner's hand.\n\u221212: Exile all cards from target player's library, then that player shuffles their hand into their library.",
        type_line="Legendary Planeswalker — Jace",
        mana_cost="{2}{U}{U}",
    )
    assert len(abilities) == 4
    assert abilities[0].cost.loyalty == 2
    assert abilities[1].cost.loyalty == 0
    assert abilities[2].cost.loyalty == -1
    assert abilities[3].cost.loyalty == -12
    assert abilities[2].effects[0].verb == "return"
    assert abilities[2].effects[0].destination == "hand"

def test_once_per_turn_restriction():
    abilities = parse_card(
        oracle_text="{T}: Draw a card. Activate only once each turn.",
        type_line="Artifact",
        mana_cost="{2}",
    )
    assert abilities[0].restrictions is not None
    assert abilities[0].restrictions.once_per_turn is True

def test_sorcery_speed_restriction():
    abilities = parse_card(
        oracle_text="{T}: Add {C}{C}. Activate only as a sorcery.",
        type_line="Land",
        mana_cost="",
    )
    assert abilities[0].restrictions is not None
    assert abilities[0].restrictions.sorcery_speed is True

def test_empty_oracle_text():
    abilities = parse_card(oracle_text="", type_line="Land", mana_cost="")
    assert abilities == []
