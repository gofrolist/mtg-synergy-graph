"""Build causal edges (triggers / feeds / amplifies / enables / tribal) between parsed card pairs."""
from __future__ import annotations

import re
from collections import defaultdict

from mtg_synergy.causal.indexer import build_index, CardIndex
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.parse.ast_types import Ability, ObjectFilter


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

def _build_trigger_edges(index: CardIndex, event_idf: dict) -> list[Edge]:
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
                # Apply combined IDF: dampen common events, boost rare ones
                p_idf = event_idf["producer"].get(event, 1.0)
                r_idf = event_idf["responder"].get(event, 1.0)
                combined_idf = min(p_idf * r_idf, 3.0)
                strength *= combined_idf
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
# Amplifies edges
# ---------------------------------------------------------------------------

_AMPLIFIES_COUNTER_RE = re.compile(r"counter", re.IGNORECASE)
_AMPLIFIES_TOKEN_RE = re.compile(r"token", re.IGNORECASE)

# Map oracle-text keywords to the effect verbs they amplify
_REPLACEMENT_AMPLIFY_MAP = {
    "counter": "put_counter",
    "token": "create",
}

# Map oracle-text keywords to the trigger events they double
_TRIGGER_MODIFIER_EVENT_MAP = {
    "entering": "enters_the_battlefield",
    "enters": "enters_the_battlefield",
    "dying": "dies",
    "dies": "dies",
    "leaves": "leaves_the_battlefield",
    "leaves the battlefield": "leaves_the_battlefield",
    "attacks": "attacks",
    "dealt damage": "dealt_damage",
}


def _detect_replacement_target(oracle_text: str) -> str | None:
    """From oracle text of a replacement effect, detect what it amplifies."""
    text_lower = oracle_text.lower()
    if _AMPLIFIES_TOKEN_RE.search(oracle_text):
        return "create"
    if _AMPLIFIES_COUNTER_RE.search(oracle_text):
        return "put_counter"
    return None


def _detect_modifier_event(oracle_text: str) -> str | None:
    """From oracle text of a trigger modifier, detect which trigger event it doubles."""
    text_lower = oracle_text.lower()
    for keyword, event in _TRIGGER_MODIFIER_EVENT_MAP.items():
        if keyword in text_lower:
            return event
    return None


def _build_amplifies_edges(
    index: CardIndex,
    cards: dict[str, list[Ability]],
    oracle_texts: dict[str, str],
    event_idf: dict,
) -> list[Edge]:
    """Build amplifies edges from replacement effects and trigger modifiers."""
    edges: list[Edge] = []

    for kind in ("replacement", "trigger_modifier"):
        for mod_card, mod_ab, ability in index._modifiers.get(kind, []):
            oracle = oracle_texts.get(mod_card, "")
            if not oracle:
                continue

            if kind == "replacement":
                target_verb = _detect_replacement_target(oracle)
                if not target_verb:
                    continue
                # Find all cards with effects using that verb
                for card_id, abilities in cards.items():
                    if card_id == mod_card:
                        continue
                    for ab_idx, ab in enumerate(abilities):
                        for effect in ab.effects:
                            if effect.verb == target_verb:
                                edges.append(Edge(
                                    source=mod_card,
                                    target=card_id,
                                    edge_type="amplifies",
                                    ability_a=mod_ab,
                                    ability_b=ab_idx,
                                    strength=0.9,
                                    detail=EdgeDetail(
                                        verb_modified=target_verb,
                                    ),
                                ))
                                break  # one edge per ability is enough

            elif kind == "trigger_modifier":
                modified_event = _detect_modifier_event(oracle)
                if not modified_event:
                    continue
                # Apply IDF to trigger modifier strength
                r_idf = event_idf["responder"].get(modified_event, 1.0)
                strength = 0.9 * min(r_idf, 3.0)
                # Find all cards that trigger on that event
                for resp_card, resp_ab, trigger in index._responders.get(modified_event, []):
                    if resp_card == mod_card:
                        continue
                    edges.append(Edge(
                        source=mod_card,
                        target=resp_card,
                        edge_type="amplifies",
                        ability_a=mod_ab,
                        ability_b=resp_ab,
                        strength=strength,
                        detail=EdgeDetail(
                            event=modified_event,
                        ),
                    ))

    return edges


# ---------------------------------------------------------------------------
# Enables edges
# ---------------------------------------------------------------------------

def _build_enables_edges(
    index: CardIndex,
    cards: dict[str, list[Ability]],
) -> list[Edge]:
    """Build enables edges: haste-granters → tap-ability cards, untap → tap-ability."""
    edges: list[Edge] = []

    # Collect haste granters and untappers
    haste_granters: list[tuple[str, int]] = []
    untappers: list[tuple[str, int]] = []

    for card_id, abilities in cards.items():
        for ab_idx, ability in enumerate(abilities):
            for effect in ability.effects:
                if effect.verb == "grant_keyword" and effect.keyword == "haste":
                    haste_granters.append((card_id, ab_idx))
                    break
                if effect.verb == "untap":
                    untappers.append((card_id, ab_idx))
                    break

    # Collect tap-ability cards
    tap_cards: list[tuple[str, int]] = []
    for card_id, abilities in cards.items():
        for ab_idx, ability in enumerate(abilities):
            if ability.cost and ability.cost.tap:
                tap_cards.append((card_id, ab_idx))
                break  # one entry per card

    # Haste → tap (strength 0.7)
    for h_card, h_ab in haste_granters:
        for t_card, t_ab in tap_cards:
            if h_card == t_card:
                continue
            edges.append(Edge(
                source=h_card,
                target=t_card,
                edge_type="enables",
                ability_a=h_ab,
                ability_b=t_ab,
                strength=0.7,
                detail=EdgeDetail(resource="haste"),
            ))

    # Untap → tap (strength 0.5)
    for u_card, u_ab in untappers:
        for t_card, t_ab in tap_cards:
            if u_card == t_card:
                continue
            edges.append(Edge(
                source=u_card,
                target=t_card,
                edge_type="enables",
                ability_a=u_ab,
                ability_b=t_ab,
                strength=0.5,
                detail=EdgeDetail(resource="untap"),
            ))

    return edges


# ---------------------------------------------------------------------------
# Tribal edges (subtype-aware scope matching)
# ---------------------------------------------------------------------------

def _build_tribal_edges(
    cards: dict[str, list[Ability]],
    type_lines: dict[str, str],
) -> list[Edge]:
    """Build tribal edges: cards that buff/care about a subtype → cards of that subtype.

    'Goblins you control get +1/+1' (target.subtype=Goblin) → every Goblin card.
    These are high-value edges because tribal lords are deck-defining.
    """
    edges: list[Edge] = []

    # 1. Collect cards with subtype-specific effects
    # {subtype: [(card_id, ability_index)]}
    subtype_buffers: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for card_id, abilities in cards.items():
        for ab_idx, ability in enumerate(abilities):
            for effect in ability.effects:
                if effect.target and effect.target.subtype:
                    subtype_buffers[effect.target.subtype.lower()].append((card_id, ab_idx))
            # Also check trigger subject subtypes
            if ability.trigger and ability.trigger.subject and ability.trigger.subject.subtype:
                subtype_buffers[ability.trigger.subject.subtype.lower()].append((card_id, ab_idx))

    if not subtype_buffers:
        return edges

    # 2. Index cards by their creature subtypes (from type_line)
    # {subtype: [card_id]}
    cards_of_type: dict[str, list[str]] = defaultdict(list)
    for card_id, type_line in type_lines.items():
        if not type_line or "—" not in type_line:
            continue
        # Extract subtypes after the dash
        try:
            subtypes_part = type_line.split("—")[1].strip()
            for subtype in subtypes_part.split():
                cards_of_type[subtype.lower()].append(card_id)
        except (IndexError, AttributeError):
            continue

    # 3. Create edges: buffer → card of matching type
    for subtype, buffers in subtype_buffers.items():
        matching_cards = cards_of_type.get(subtype, [])
        for buffer_card, buffer_ab in buffers:
            for target_card in matching_cards:
                if buffer_card == target_card:
                    continue
                edges.append(Edge(
                    source=buffer_card,
                    target=target_card,
                    edge_type="enables",  # tribal synergy is a form of enabling
                    ability_a=buffer_ab,
                    ability_b=0,
                    strength=0.9,  # tribal match is a strong signal
                    detail=EdgeDetail(resource=f"tribal:{subtype}"),
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


def build_causal_edges(
    cards: dict[str, list[Ability]],
    oracle_texts: dict[str, str] | None = None,
    type_lines: dict[str, str] | None = None,
) -> list[Edge]:
    """Build all causal edges (triggers + feeds + amplifies + enables + tribal).

    Args:
        cards: mapping of card_id -> list of Ability AST nodes.
        oracle_texts: mapping of card_id -> oracle text string.
            Required for amplifies edges (replacement/trigger_modifier).
            If None, amplifies edges are skipped.
        type_lines: mapping of card_id -> type_line string.
            Required for tribal edges. If None, tribal edges are skipped.

    Returns:
        Deduplicated list of Edge objects.
    """
    global _all_cards
    _all_cards = cards

    index = build_index(cards)
    event_idf = index.compute_event_idf()
    edges: list[Edge] = []
    edges.extend(_build_trigger_edges(index, event_idf))
    edges.extend(_build_feeds_edges(index))
    if oracle_texts:
        edges.extend(_build_amplifies_edges(index, cards, oracle_texts, event_idf))
    edges.extend(_build_enables_edges(index, cards))
    if type_lines:
        edges.extend(_build_tribal_edges(cards, type_lines))
    return _dedup_edges(edges)
