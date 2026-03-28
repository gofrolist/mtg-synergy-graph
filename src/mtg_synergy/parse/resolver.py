"""Cross-reference resolver (Pass 4).

Resolves pronouns and anaphoric references in parsed ability ASTs.
For example, "When a creature dies, return it to the battlefield" —
the 'it' in the effect refers back to the trigger subject 'creature'.
"""
from __future__ import annotations

import copy
from dataclasses import fields as dc_fields
from typing import Optional

from mtg_synergy.parse.ast_types import Ability, Effect, ObjectFilter, TokenDef

# References that point back to the trigger subject
_TRIGGER_SUBJECT_REFS = {"it", "that creature", "that card"}

# References that point to tokens/objects created by a previous effect
_PREVIOUS_OUTPUT_REFS = {"those tokens", "those creatures"}

# References that extract controller info from the trigger subject
_CONTROLLER_REFS = {"its controller"}


def _copy_filter(src: ObjectFilter) -> ObjectFilter:
    """Create a shallow copy of an ObjectFilter."""
    kwargs = {}
    for f in dc_fields(ObjectFilter):
        val = getattr(src, f.name)
        if val is not None:
            kwargs[f.name] = val
    return ObjectFilter(**kwargs)


def _filter_from_token(token: TokenDef) -> ObjectFilter:
    """Build an ObjectFilter from a TokenDef."""
    return ObjectFilter(
        card_type=token.card_type,
        subtype=token.subtype,
        color=token.color,
        is_token=True,
    )


def _find_previous_token(effects: list[Effect], index: int) -> Optional[TokenDef]:
    """Find the most recent token definition before the given index."""
    for i in range(index - 1, -1, -1):
        if effects[i].token is not None:
            return effects[i].token
    return None


def _resolve_effect(
    effect: Effect,
    trigger_subject: Optional[ObjectFilter],
    effects: list[Effect],
    index: int,
) -> Effect:
    """Resolve a single effect's unresolved_ref, returning a new Effect."""
    ref = effect.unresolved_ref
    if ref is None:
        return effect

    # Deep copy so we don't mutate the original
    new_effect = copy.deepcopy(effect)
    ref_lower = ref.lower().strip()

    if ref_lower in _TRIGGER_SUBJECT_REFS and trigger_subject is not None:
        new_effect.target = _copy_filter(trigger_subject)
        new_effect.unresolved_ref = None

    elif ref_lower in _PREVIOUS_OUTPUT_REFS:
        token = _find_previous_token(effects, index)
        if token is not None:
            new_effect.target = _filter_from_token(token)
            new_effect.unresolved_ref = None

    elif ref_lower in _CONTROLLER_REFS and trigger_subject is not None:
        target = _copy_filter(trigger_subject) if new_effect.target is None else copy.deepcopy(new_effect.target)
        # Set controller to a reference marker; the trigger subject's controller
        target.controller = trigger_subject.controller or "trigger_subject_controller"
        new_effect.target = target
        new_effect.unresolved_ref = None

    return new_effect


def resolve_references(ability: Ability) -> Ability:
    """Resolve cross-references in an Ability AST.

    Walks through each effect and resolves ``unresolved_ref`` fields:
    - "it", "that creature", "that card" -> copy trigger subject's ObjectFilter
    - "those tokens", "those creatures" -> copy previous effect's token/output filter
    - "its controller" -> set controller field from trigger subject

    Returns a **new** Ability with resolved targets; the input is not mutated.
    """
    # Build context: trigger subject is primary referent
    trigger_subject = None
    if ability.trigger is not None and ability.trigger.subject is not None:
        trigger_subject = ability.trigger.subject

    # If no effects need resolution, still return a copy
    new_effects = []
    for i, effect in enumerate(ability.effects):
        new_effects.append(_resolve_effect(effect, trigger_subject, ability.effects, i))

    # Build a new Ability with resolved effects (shallow copy other fields)
    new_ability = copy.deepcopy(ability)
    new_ability.effects = new_effects
    return new_ability
