"""Tests for cross-reference resolution (Pass 4)."""
import pytest
from mtg_synergy.parse.resolver import resolve_references
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef
)


def test_it_refers_to_trigger_subject():
    """'When a creature dies, return it to the battlefield' -> 'it' = creature."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="return", amount=Amount(value=1),
                        target=ObjectFilter(),
                        destination="battlefield",
                        unresolved_ref="it")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.card_type == "creature"


def test_that_creature():
    """'that creature' refers to the trigger subject."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", subtype="Goblin")),
        effects=[Effect(verb="put_counter", amount=Amount(value=1),
                        target=ObjectFilter(),
                        unresolved_ref="that creature")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.card_type == "creature"


def test_that_creature_preserves_subtype():
    """'that creature' copies subtype from trigger subject."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", subtype="Goblin")),
        effects=[Effect(verb="put_counter", amount=Amount(value=1),
                        target=ObjectFilter(),
                        unresolved_ref="that creature")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.subtype == "Goblin"


def test_those_tokens():
    """'those tokens' refers to tokens created by previous effect."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield", subject=ObjectFilter()),
        effects=[
            Effect(verb="create", amount=Amount(value=2),
                   token=TokenDef(card_type="creature", subtype="Goblin",
                                  power=1, toughness=1, keywords=[], color="red")),
            Effect(verb="pump", amount=Amount(value=1),
                   target=ObjectFilter(),
                   unresolved_ref="those tokens"),
        ],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[1].target.card_type == "creature"


def test_those_tokens_subtype():
    """'those tokens' copies subtype from the token definition."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield", subject=ObjectFilter()),
        effects=[
            Effect(verb="create", amount=Amount(value=2),
                   token=TokenDef(card_type="creature", subtype="Goblin",
                                  power=1, toughness=1, keywords=[], color="red")),
            Effect(verb="pump", amount=Amount(value=1),
                   target=ObjectFilter(),
                   unresolved_ref="those tokens"),
        ],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[1].target.subtype == "Goblin"
    assert resolved.effects[1].target.is_token is True


def test_no_refs_unchanged():
    """Ability with no cross-references passes through unchanged."""
    ability = Ability(
        kind="static",
        effects=[Effect(verb="pump", amount=Amount(value=1),
                        target=ObjectFilter(card_type="creature", controller="you"))],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.card_type == "creature"


def test_does_not_mutate_input():
    """resolve_references returns a new Ability, not mutating the input."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="return", amount=Amount(value=1),
                        target=ObjectFilter(),
                        unresolved_ref="it")],
    )
    resolve_references(ability)
    # Original should be unchanged
    assert ability.effects[0].target.card_type is None
    assert ability.effects[0].unresolved_ref == "it"


def test_that_card():
    """'that card' refers to the trigger subject."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="exile", amount=Amount(value=1),
                        target=ObjectFilter(),
                        unresolved_ref="that card")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.card_type == "creature"


def test_its_controller():
    """'its controller' sets controller field from trigger subject."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="draw", amount=Amount(value=1),
                        target=ObjectFilter(),
                        unresolved_ref="its controller")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.controller is not None


def test_those_creatures():
    """'those creatures' refers to tokens created by a previous create effect."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield", subject=ObjectFilter()),
        effects=[
            Effect(verb="create", amount=Amount(value=3),
                   token=TokenDef(card_type="creature", subtype="Soldier",
                                  power=1, toughness=1, keywords=[], color="white")),
            Effect(verb="pump", amount=Amount(value=2),
                   target=ObjectFilter(),
                   unresolved_ref="those creatures"),
        ],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[1].target.card_type == "creature"
    assert resolved.effects[1].target.subtype == "Soldier"


def test_unresolved_ref_cleared_after_resolution():
    """After resolving, the unresolved_ref field should be None."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="return", amount=Amount(value=1),
                        target=ObjectFilter(),
                        unresolved_ref="it")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].unresolved_ref is None
