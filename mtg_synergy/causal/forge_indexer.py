"""Forge-native causal indexer -- indexes forge_abilities by events produced/consumed.

Replaces the old indexer.py which used parsed AST abilities + verb_resolvers.
This version reads directly from forge_abilities table and uses the
verb->event mapping to determine what events each card produces.
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field

from mtg_synergy.causal.verb_event_map import verb_to_events
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter


@dataclass
class ForgeIndex:
    """Index of cards by what trigger events they produce and respond to."""
    # {trigger_mode: [(card_name, ability_idx, event_detail_dict)]}
    _producers: dict = field(default_factory=lambda: defaultdict(list))
    # {trigger_mode: [(card_name, ability_idx, trigger_filter_str, origin, destination)]}
    _responders: dict = field(default_factory=lambda: defaultdict(list))
    producer_counts: dict = field(default_factory=dict)
    responder_counts: dict = field(default_factory=dict)
    total_cards: int = 0

    def producers_for(self, trigger_mode: str) -> list:
        return self._producers.get(trigger_mode, [])

    def responders_for(self, trigger_mode: str) -> list:
        return self._responders.get(trigger_mode, [])

    def compute_event_idf(self) -> dict:
        """Compute IDF multipliers for producer and responder events."""
        result = {"producer": {}, "responder": {}}
        n = max(self.total_cards, 1)
        max_idf = math.log(n) if n > 1 else 1.0
        min_idf = math.log(2) if n > 2 else 0.1
        span = max_idf - min_idf if max_idf > min_idf else 1.0

        for side, counts in [("producer", self.producer_counts),
                             ("responder", self.responder_counts)]:
            for event, count in counts.items():
                raw = math.log(max(n / max(count, 1), 1))
                normalized = 0.3 + 2.7 * (raw - min_idf) / span
                result[side][event] = round(max(0.3, min(3.0, normalized)), 3)
        return result


def build_forge_index(conn) -> ForgeIndex:
    """Build a ForgeIndex from the forge_abilities table.

    Producers: cards with effect verbs that produce trigger events
    Responders: cards with trigger abilities that respond to events
    """
    idx = ForgeIndex()

    # Count distinct cards
    idx.total_cards = conn.execute(
        "SELECT COUNT(DISTINCT card_name) FROM forge_abilities"
    ).fetchone()[0]

    # Index producers: cards with effect verbs -> trigger events they produce
    for row in conn.execute(
        "SELECT card_name, ability_index, verb, target, trigger_origin, "
        "trigger_destination, token_script "
        "FROM forge_abilities WHERE verb IS NOT NULL"
    ).fetchall():
        card_name, ab_idx, verb, target, origin, dest, token_script = row
        events = verb_to_events(verb)
        for event in events:
            mode = event["trigger_mode"]
            # For Token verb, use token_script as target if target is empty
            effective_target = target or token_script
            detail = {
                "verb": verb,
                "target": effective_target,
                "origin": event.get("origin") or origin,
                "destination": event.get("destination") or dest,
            }
            idx._producers[mode].append((card_name, ab_idx, detail))

    # Index responders: cards with trigger abilities
    for row in conn.execute(
        "SELECT card_name, ability_index, trigger_mode, trigger_filter, "
        "trigger_origin, trigger_destination "
        "FROM forge_abilities WHERE trigger_mode IS NOT NULL"
    ).fetchall():
        card_name, ab_idx, trigger_mode, trigger_filter, origin, dest = row
        idx._responders[trigger_mode].append(
            (card_name, ab_idx, trigger_filter or "", origin or "", dest or "")
        )

    # Compute counts
    for mode, entries in idx._producers.items():
        idx.producer_counts[mode] = len({name for name, _, _ in entries})
    for mode, entries in idx._responders.items():
        idx.responder_counts[mode] = len({name for name, _, _, _, _ in entries})

    return idx
