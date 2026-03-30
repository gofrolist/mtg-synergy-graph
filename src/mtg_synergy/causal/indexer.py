"""Index parsed cards by what events they produce and respond to."""
from collections import defaultdict
from dataclasses import dataclass, field
from mtg_synergy.causal.idf import compute_event_idf as _compute_event_idf
from mtg_synergy.parse.ast_types import Ability
from mtg_synergy.parse.verb_resolvers import resolve_effect


@dataclass
class CardIndex:
    _producers: dict = field(default_factory=lambda: defaultdict(list))
    _responders: dict = field(default_factory=lambda: defaultdict(list))
    _consumers: dict = field(default_factory=lambda: defaultdict(list))
    _modifiers: dict = field(default_factory=lambda: defaultdict(list))
    _card_events: dict = field(default_factory=lambda: defaultdict(list))
    producer_counts: dict = field(default_factory=dict)   # {event: num_unique_cards}
    responder_counts: dict = field(default_factory=dict)  # {event: num_unique_cards}
    total_cards: int = 0

    def producers_for(self, event_type: str):
        return self._producers.get(event_type, [])

    def responders_for(self, event_type: str):
        return self._responders.get(event_type, [])

    def compute_event_idf(self) -> dict:
        """Compute IDF multipliers for producer and responder events.

        Rare events get higher weight (up to 3.0x), common events get lower (down to 0.3x).
        """
        return _compute_event_idf(self.total_cards, self.producer_counts,
                                  self.responder_counts)


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

    # Compute unique card counts per event
    for event, entries in idx._producers.items():
        idx.producer_counts[event] = len({cid for cid, _, _ in entries})
    for event, entries in idx._responders.items():
        idx.responder_counts[event] = len({cid for cid, _, _ in entries})
    idx.total_cards = len(cards)

    return idx
