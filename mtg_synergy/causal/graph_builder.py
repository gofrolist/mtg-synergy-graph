"""Build causal edges (triggers / feeds) between parsed card pairs."""
from __future__ import annotations

from mtg_synergy.causal.indexer import build_index, CardIndex
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.parse.ast_types import Ability, ObjectFilter
from mtg_synergy.parse.verb_resolvers import StateChange


# ---------------------------------------------------------------------------
# Filter precision
# ---------------------------------------------------------------------------

def _compute_filter_precision(
    trigger_subject: ObjectFilter | None,
    sc_object: ObjectFilter | None,
) -> str:
    """Compare a trigger's subject filter against a state-change's object.

    Returns one of: "exact", "broad", "unfiltered", "none".
    """
    if trigger_subject is None:
        return "unfiltered"

    # is_token=False explicitly rejects tokens
    if trigger_subject.is_token is False:
        if sc_object is not None and sc_object.is_token:
            return "none"

    # card_type check
    if trigger_subject.card_type:
        if sc_object is None or sc_object.card_type is None:
            return "none"
        if trigger_subject.card_type != sc_object.card_type:
            return "none"
        # card_type matches -- check subtype
        if trigger_subject.subtype:
            if sc_object.subtype and trigger_subject.subtype == sc_object.subtype:
                return "exact"
            # subtype required but not matched
            if sc_object.subtype is None or trigger_subject.subtype != sc_object.subtype:
                return "none"
        # card_type matches, no subtype filter
        return "broad"

    # No card_type filter on trigger
    return "unfiltered"


def _precision_to_strength(precision: str) -> float:
    return {"exact": 1.0, "broad": 0.6, "unfiltered": 0.3, "none": 0.0}[precision]


# ---------------------------------------------------------------------------
# Edge builders
# ---------------------------------------------------------------------------

def _build_trigger_edges(index: CardIndex) -> list[Edge]:
    """Cross-match producers x responders for each event type."""
    edges: list[Edge] = []

    # Collect all event types that have both producers and responders
    all_events = set()
    # Access internal dicts to enumerate event types
    for event in index._producers:
        if event in index._responders:
            all_events.add(event)

    for event in all_events:
        producers = index._producers[event]
        responders = index._responders[event]
        for prod_card, prod_ab, sc in producers:
            for resp_card, resp_ab, trigger in responders:
                if prod_card == resp_card:
                    continue
                precision = _compute_filter_precision(trigger.subject, sc.object)
                strength = _precision_to_strength(precision)
                if strength <= 0:
                    continue
                edges.append(Edge(
                    source=prod_card,
                    target=resp_card,
                    edge_type="triggers",
                    ability_a=prod_ab,
                    ability_b=resp_ab,
                    strength=strength,
                    detail=EdgeDetail(
                        event=event,
                        filter_precision=precision,
                    ),
                ))

    return edges


def _build_feeds_edges(index: CardIndex) -> list[Edge]:
    """Build feeds edges: creature producers → sacrifice consumers, mana → mana."""
    edges: list[Edge] = []

    # Creature feeds: cards producing creature_enters feed sacrifice(creature) consumers
    creature_producers = index._producers.get("creature_enters", [])
    creature_consumers = index._consumers.get("creature", [])

    for prod_card, prod_ab, sc in creature_producers:
        for cons_card, cons_ab, cost in creature_consumers:
            if prod_card == cons_card:
                continue
            edges.append(Edge(
                source=prod_card,
                target=cons_card,
                edge_type="feeds",
                ability_a=prod_ab,
                ability_b=cons_ab,
                strength=0.7,
                detail=EdgeDetail(resource="creature"),
            ))

    # Mana feeds: cards with add_mana effects feed mana consumers
    # add_mana produces no StateChanges, so scan abilities directly
    mana_producers: list[tuple[str, int]] = []
    for card_id, abilities in _all_cards.items():
        for ab_idx, ability in enumerate(abilities):
            for effect in ability.effects:
                if effect.verb == "add_mana":
                    mana_producers.append((card_id, ab_idx))

    mana_consumers = index._consumers.get("mana", [])
    for prod_card, prod_ab in mana_producers:
        for cons_card, cons_ab, cost in mana_consumers:
            if prod_card == cons_card:
                continue
            edges.append(Edge(
                source=prod_card,
                target=cons_card,
                edge_type="feeds",
                ability_a=prod_ab,
                ability_b=cons_ab,
                strength=0.4,
                detail=EdgeDetail(resource="mana"),
            ))

    return edges


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def _dedup_edges(edges: list[Edge]) -> list[Edge]:
    """Keep strongest edge per (source, target, edge_type, ability_b)."""
    best: dict[tuple, Edge] = {}
    for e in edges:
        key = (e.source, e.target, e.edge_type, e.ability_b)
        if key not in best or e.strength > best[key].strength:
            best[key] = e
    return list(best.values())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Module-level ref so _build_feeds_edges can scan for add_mana verbs
_all_cards: dict[str, list[Ability]] = {}


def build_causal_edges(cards: dict[str, list[Ability]]) -> list[Edge]:
    """Build all causal edges (triggers + feeds) between card pairs.

    Args:
        cards: mapping of card_id -> list of Ability AST nodes.

    Returns:
        Deduplicated list of Edge objects.
    """
    global _all_cards
    _all_cards = cards

    index = build_index(cards)
    edges: list[Edge] = []
    edges.extend(_build_trigger_edges(index))
    edges.extend(_build_feeds_edges(index))
    return _dedup_edges(edges)
