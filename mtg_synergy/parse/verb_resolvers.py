"""Verb resolvers: map Effect AST nodes to StateChange events (rules engine).

Each MTG verb (create, destroy, draw, etc.) produces zero or more StateChange
objects describing the game-state transitions that occur when the effect resolves.
These StateChanges are the bridge between parsed oracle text and the synergy
matching engine -- they tell us what events a card *produces* so we can match
them against triggers on other cards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mtg_synergy.parse.ast_types import Amount, Effect, ObjectFilter, TokenDef


@dataclass
class StateChange:
    """A single game-state transition produced by resolving an Effect."""
    event: str = ""
    object: Optional[ObjectFilter] = None
    zone_from: Optional[str] = None
    zone_to: Optional[str] = None
    quantity: Optional[Amount] = None
    controller: str = "any"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _amount(effect: Effect) -> Amount:
    """Return the effect's amount or a sensible default."""
    return effect.amount if effect.amount is not None else Amount(value=1)


def _target(effect: Effect) -> ObjectFilter:
    """Return the effect's target or an empty filter."""
    return effect.target if effect.target is not None else ObjectFilter()


def _is_creature(obj: ObjectFilter) -> bool:
    return obj.card_type == "creature"


def _is_player_target(target: ObjectFilter) -> bool:
    """A target is a player if it has a controller but no card_type."""
    return target.controller is not None and target.card_type is None


# ---------------------------------------------------------------------------
# Verb-specific resolvers
# ---------------------------------------------------------------------------

def _resolve_create(effect: Effect) -> list[StateChange]:
    token = effect.token
    if token is None:
        return []

    obj = ObjectFilter(
        card_type=token.card_type,
        subtype=token.subtype,
        is_token=True,
        color=token.color,
    )
    amt = _amount(effect)

    changes: list[StateChange] = [
        StateChange(
            event="enters_the_battlefield",
            object=obj,
            zone_from=None,
            zone_to="battlefield",
            quantity=amt,
        ),
    ]

    # Type-specific ETB events
    if token.card_type == "creature":
        changes.append(StateChange(
            event="creature_enters",
            object=obj,
            zone_to="battlefield",
            quantity=amt,
        ))
    elif token.card_type == "artifact":
        changes.append(StateChange(
            event="artifact_enters",
            object=obj,
            zone_to="battlefield",
            quantity=amt,
        ))

    return changes


def _resolve_destroy(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    changes: list[StateChange] = [
        StateChange(
            event="leaves_the_battlefield",
            object=target,
            zone_from="battlefield",
            zone_to="graveyard",
            quantity=amt,
        ),
    ]

    if _is_creature(target):
        changes.append(StateChange(
            event="dies",
            object=target,
            zone_from="battlefield",
            zone_to="graveyard",
            quantity=amt,
        ))

    changes.append(StateChange(
        event="enters_graveyard",
        object=target,
        zone_from="battlefield",
        zone_to="graveyard",
        quantity=amt,
    ))

    return changes


def _resolve_sacrifice(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    changes: list[StateChange] = [
        StateChange(
            event="leaves_the_battlefield",
            object=target,
            zone_from="battlefield",
            zone_to="graveyard",
            quantity=amt,
            controller="you",
        ),
    ]

    if _is_creature(target):
        changes.append(StateChange(
            event="dies",
            object=target,
            zone_from="battlefield",
            zone_to="graveyard",
            quantity=amt,
            controller="you",
        ))

    changes.append(StateChange(
        event="enters_graveyard",
        object=target,
        zone_from="battlefield",
        zone_to="graveyard",
        quantity=amt,
        controller="you",
    ))

    return changes


def _resolve_deal_damage(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    changes: list[StateChange] = [
        StateChange(
            event="damage_dealt",
            object=target,
            quantity=amt,
        ),
    ]

    if _is_player_target(target):
        changes.append(StateChange(
            event="life_lost",
            object=target,
            quantity=amt,
            controller=target.controller or "any",
        ))
    elif target.card_type is not None:
        # Damage to a permanent -- creature may die
        changes.append(StateChange(
            event="may_die",
            object=target,
            quantity=amt,
        ))

    return changes


def _resolve_draw(effect: Effect) -> list[StateChange]:
    amt = _amount(effect)
    return [
        StateChange(
            event="card_drawn",
            zone_from="library",
            zone_to="hand",
            quantity=amt,
        ),
    ]


def _resolve_discard(effect: Effect) -> list[StateChange]:
    amt = _amount(effect)
    target = _target(effect)
    return [
        StateChange(
            event="card_discarded",
            object=target,
            zone_from="hand",
            zone_to="graveyard",
            quantity=amt,
        ),
        StateChange(
            event="enters_graveyard",
            object=target,
            zone_from="hand",
            zone_to="graveyard",
            quantity=amt,
        ),
    ]


def _resolve_exile(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    return [
        StateChange(
            event="leaves_the_battlefield",
            object=target,
            zone_from="battlefield",
            zone_to="exile",
            quantity=amt,
        ),
        StateChange(
            event="enters_exile",
            object=target,
            zone_from="battlefield",
            zone_to="exile",
            quantity=amt,
        ),
    ]


def _resolve_return(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    dest = effect.destination or "hand"

    changes: list[StateChange] = []

    if dest == "battlefield":
        obj = target
        changes.append(StateChange(
            event="enters_the_battlefield",
            object=obj,
            zone_to="battlefield",
            quantity=amt,
        ))
        if _is_creature(target):
            changes.append(StateChange(
                event="creature_enters",
                object=obj,
                zone_to="battlefield",
                quantity=amt,
            ))
    else:
        # Returning to hand or library means leaving the battlefield
        changes.append(StateChange(
            event="leaves_the_battlefield",
            object=target,
            zone_from="battlefield",
            zone_to=dest,
            quantity=amt,
        ))

    return changes


def _resolve_put_counter(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    return [
        StateChange(
            event="counter_placed",
            object=target,
            quantity=amt,
        ),
    ]


def _resolve_gain_life(effect: Effect) -> list[StateChange]:
    amt = _amount(effect)
    return [
        StateChange(
            event="life_gained",
            quantity=amt,
            controller="you",
        ),
    ]


def _resolve_lose_life(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    return [
        StateChange(
            event="life_lost",
            object=target,
            quantity=amt,
            controller=target.controller or "any",
        ),
    ]


def _resolve_mill(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    return [
        StateChange(
            event="enters_graveyard",
            object=target,
            zone_from="library",
            zone_to="graveyard",
            quantity=amt,
            controller=target.controller or "any",
        ),
    ]


def _resolve_add_mana(effect: Effect) -> list[StateChange]:
    # Mana is a resource, not a game-state event that triggers abilities.
    return []


def _resolve_untap(effect: Effect) -> list[StateChange]:
    target = _target(effect)
    amt = _amount(effect)
    return [
        StateChange(
            event="untapped",
            object=target,
            quantity=amt,
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_VERB_RESOLVERS: dict[str, callable] = {
    "create": _resolve_create,
    "destroy": _resolve_destroy,
    "sacrifice": _resolve_sacrifice,
    "deal_damage": _resolve_deal_damage,
    "draw": _resolve_draw,
    "discard": _resolve_discard,
    "exile": _resolve_exile,
    "return": _resolve_return,
    "put_counter": _resolve_put_counter,
    "gain_life": _resolve_gain_life,
    "lose_life": _resolve_lose_life,
    "mill": _resolve_mill,
    "add_mana": _resolve_add_mana,
    "untap": _resolve_untap,
}


def resolve_effect(effect: Effect) -> list[StateChange]:
    """Resolve an Effect AST node into a list of StateChange events.

    Unknown verbs produce an empty list -- this is intentional so that
    the caller can safely resolve any effect without error handling.
    """
    resolver = _VERB_RESOLVERS.get(effect.verb)
    if resolver is None:
        return []
    return resolver(effect)
