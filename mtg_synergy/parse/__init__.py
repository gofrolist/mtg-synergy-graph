"""Oracle text parser — deterministic, zero-cost card text analysis."""
from mtg_synergy.parse.ast_types import Ability, Restrictions
from mtg_synergy.parse.splitter import split_abilities
from mtg_synergy.parse.trigger_parser import parse_trigger
from mtg_synergy.parse.effect_parser import parse_effects
from mtg_synergy.parse.cost_parser import parse_cost
from mtg_synergy.parse.resolver import resolve_references
from mtg_synergy.parse.templates import apply_templates


def parse_card(oracle_text: str, type_line: str = "", mana_cost: str = "") -> list[Ability]:
    if not oracle_text or not oracle_text.strip():
        return []

    raw_abilities = split_abilities(oracle_text)
    abilities = []

    for raw in raw_abilities:
        # Pass 3a: Parse trigger
        trigger = None
        if raw.kind == "triggered" and raw.trigger_text:
            trigger = parse_trigger(raw.trigger_text)

        # Pass 3b: Parse effects
        effects = []
        if raw.effect_text:
            effects = parse_effects(raw.effect_text)

        # Pass 3c: Parse cost
        cost = None
        if raw.cost_text:
            is_loyalty = raw.loyalty_cost is not None
            cost = parse_cost(raw.cost_text, is_loyalty=is_loyalty)
        if raw.loyalty_cost is not None and cost:
            cost.loyalty = raw.loyalty_cost

        # Apply templates for scaling/trigger_modifier detection
        template_result = apply_templates(raw.raw_text)

        # Determine kind — template may override
        kind = raw.kind
        if template_result and template_result.kind:
            kind = template_result.kind

        # Parse restrictions
        restrictions = _parse_restrictions(raw.restrictions_text) if raw.restrictions_text else None

        ability = Ability(
            kind=kind,
            trigger=trigger,
            cost=cost,
            effects=effects,
            restrictions=restrictions,
        )

        # Pass 4: Resolve cross-references
        ability = resolve_references(ability)

        abilities.append(ability)

    return abilities


def _parse_restrictions(text: str) -> Restrictions | None:
    if not text:
        return None
    text_lower = text.lower()
    return Restrictions(
        once_per_turn="once each turn" in text_lower or "once per turn" in text_lower,
        sorcery_speed="as a sorcery" in text_lower,
        once_per_game="only once" in text_lower and "each turn" not in text_lower,
        your_turn_only="only during your turn" in text_lower,
    )
