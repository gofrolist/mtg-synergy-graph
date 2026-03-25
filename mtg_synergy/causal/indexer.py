"""Index parsed cards by what events they produce and respond to."""
from collections import defaultdict
from dataclasses import dataclass, field
from mtg_synergy.parse.ast_types import Ability
from mtg_synergy.parse.verb_resolvers import resolve_effect, StateChange


@dataclass
class CardIndex:
    _producers: dict = field(default_factory=lambda: defaultdict(list))
    _responders: dict = field(default_factory=lambda: defaultdict(list))
    _consumers: dict = field(default_factory=lambda: defaultdict(list))
    _modifiers: dict = field(default_factory=lambda: defaultdict(list))
    _card_events: dict = field(default_factory=lambda: defaultdict(list))

    def events_produced_by(self, card_id: str) -> list:
        return self._card_events.get(card_id, [])

    def cards_producing(self, event_type: str) -> list[str]:
        return list({cid for cid, _, _ in self._producers.get(event_type, [])})

    def cards_responding_to(self, event_type: str) -> list[str]:
        return list({cid for cid, _, _ in self._responders.get(event_type, [])})

    def cards_consuming(self, resource_type: str) -> list[str]:
        return list({cid for cid, _, _ in self._consumers.get(resource_type, [])})

    def producers_for(self, event_type: str):
        return self._producers.get(event_type, [])

    def responders_for(self, event_type: str):
        return self._responders.get(event_type, [])


def build_index(cards: dict[str, list[Ability]]) -> CardIndex:
    idx = CardIndex()
    for card_id, abilities in cards.items():
        for ab_idx, ability in enumerate(abilities):
            for effect in ability.effects:
                state_changes = resolve_effect(effect)
                for sc in state_changes:
                    idx._producers[sc.event].append((card_id, ab_idx, sc))
                    idx._card_events[card_id].append(sc)
            if ability.trigger:
                idx._responders[ability.trigger.event].append(
                    (card_id, ab_idx, ability.trigger))
            if ability.cost:
                if ability.cost.sacrifice:
                    resource = ability.cost.sacrifice.card_type or "permanent"
                    idx._consumers[resource].append((card_id, ab_idx, ability.cost))
                if ability.cost.mana and ability.cost.mana.total > 0:
                    idx._consumers["mana"].append((card_id, ab_idx, ability.cost))
            if ability.kind in ("replacement", "trigger_modifier"):
                idx._modifiers[ability.kind].append((card_id, ab_idx, ability))
    return idx
