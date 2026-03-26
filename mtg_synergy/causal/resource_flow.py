"""Resource cost/production tracking for loop validation."""
from dataclasses import dataclass
from mtg_synergy.causal.types import ResourceDelta
from mtg_synergy.parse.ast_types import Ability


@dataclass
class ResourceCostSimple:
    mana: int = 0
    creatures: int = 0
    cards: int = 0
    life: int = 0
    tap: bool = False

@dataclass
class ResourceProductionSimple:
    mana: int | str = 0      # int or "X"
    creatures: int | str = 0  # int or "X"
    cards: int | str = 0
    life: int | str = 0


def compute_ability_resources(ability: Ability) -> tuple[ResourceCostSimple, ResourceProductionSimple]:
    """Extract what an ability costs and produces."""
    cost = ResourceCostSimple()
    prod = ResourceProductionSimple()

    # Costs
    if ability.cost:
        if ability.cost.tap:
            cost.tap = True
        if ability.cost.mana and ability.cost.mana.total > 0:
            cost.mana = ability.cost.mana.total
        if ability.cost.sacrifice:
            cost.creatures = 1
        if ability.cost.pay_life:
            cost.life = ability.cost.pay_life
        if ability.cost.discard:
            cost.cards = 1

    # Productions from effects
    for effect in ability.effects:
        amount_val = effect.amount.value if effect.amount else 1
        if effect.verb == "create" and effect.token:
            if effect.token.card_type == "creature":
                prod.creatures = amount_val
        elif effect.verb == "add_mana":
            prod.mana = amount_val if isinstance(amount_val, int) else 1
        elif effect.verb == "draw":
            prod.cards = amount_val
        elif effect.verb == "gain_life":
            prod.life = amount_val

    return cost, prod


def compute_cycle_delta(abilities: list[Ability]) -> ResourceDelta:
    """Sum production - cost around a cycle of abilities."""
    total_mana = 0
    total_creatures = 0
    total_cards = 0
    total_life = 0

    for ability in abilities:
        cost, prod = compute_ability_resources(ability)
        # Subtract costs (only fixed ints)
        total_mana -= cost.mana
        total_creatures -= cost.creatures
        total_cards -= cost.cards
        total_life -= cost.life
        # Add production (handle "X" as large positive)
        total_mana += prod.mana if isinstance(prod.mana, int) else 10
        total_creatures += prod.creatures if isinstance(prod.creatures, int) else 10
        total_cards += prod.cards if isinstance(prod.cards, int) else 10
        total_life += prod.life if isinstance(prod.life, int) else 10

    return ResourceDelta(
        mana=total_mana,
        creatures=total_creatures,
        cards=total_cards,
        life=total_life,
    )
