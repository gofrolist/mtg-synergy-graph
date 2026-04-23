"""Depth-2 self-bridging pathway scoring (``self_bridging_cascade`` rule family).

This module layers a length-<=2 port-graph walker on top of the existing
depth-1 complement pipeline. It detects candidates whose internal ports
form a loop that reinforces two distinct commander-port matches --
engine-grade cascade shapes (Korvold + Bloodghast, Muldrotha +
Gravecrawler, Teysa + aristocrats-chain token-makers).

The walker is a pure function on in-memory port tuples and has no
database dependency. The SQL-backed ``_find_self_bridging_cascade``
helper that wraps it ships in Unit 2 of plan
``docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md``.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Any

from ..graph_engine import COST_FEEDS_TRIGGER, EVENT_MATCH_MAP
from .core import _CHANGEZONE_RUNTIME_HEADS, _changezone_type_set

PortRow = dict[str, Any]

#: Channel identifiers returned by :func:`_walk_self_paths`. Ordered
#: tightest-semantic-first; the walker returns the first matching
#: channel per pair.
_CHANNEL_EVENT_MATCH = "event_match"
_CHANNEL_COST_FEEDS = "cost_feeds"
_CHANNEL_VALID_FILTER = "valid_filter"


def _walk_self_paths(
    ports: Sequence[PortRow],
) -> tuple[PortRow, PortRow, str] | None:
    """Detect a length-<=2 internal edge between two of ``ports``.

    Returns ``(p1, p2, channel)`` on first match, ``None`` otherwise.
    ``channel`` is one of ``"event_match"``, ``"cost_feeds"``, or
    ``"valid_filter"``. The returned port dicts are the original
    objects (not copies).

    Ports are expected to be a candidate's own port rows that have
    already been filtered against a commander's port shapes (the set
    ``M`` in FR1). The walker does not itself validate commander
    compatibility -- only the internal edge on the candidate.
    """
    if len(ports) < 2:
        return None
    for p1, p2 in itertools.combinations(ports, 2):
        if _event_match_edge(p1, p2):
            return (p1, p2, _CHANNEL_EVENT_MATCH)
        if _cost_feeds_edge(p1, p2):
            return (p1, p2, _CHANNEL_COST_FEEDS)
        if _valid_filter_edge(p1, p2):
            return (p1, p2, _CHANNEL_VALID_FILTER)
    return None


def _event_match_edge(p1: PortRow, p2: PortRow) -> bool:
    """True iff either orientation satisfies a canonical
    trigger->effect entry in ``EVENT_MATCH_MAP``.

    Unlike :func:`graph_engine.match_event`, this walker-specific
    variant skips the identity fallback (``t_event == e_event`` for
    events not in the map) -- that fallback encodes trigger<->trigger
    resonance which is a different channel from internal cause->effect
    cascade. For the depth-2 walk we only want the canonical
    trigger->effect edges the seed JSON declares.
    """
    return _canonical_trigger_effect(p1, p2) or _canonical_trigger_effect(p2, p1)


def _canonical_trigger_effect(trigger: PortRow, effect: PortRow) -> bool:
    """True iff ``trigger``'s event_class is an ``EVENT_MATCH_MAP``
    FROM key and ``effect``'s event_class resolves through that key
    (direct match or ``*`` wildcard) with the mapped predicate
    evaluating True."""
    t_event = (trigger.get("event_class") or "").strip()
    e_event = (effect.get("event_class") or "").strip()
    if not t_event or not e_event:
        return False
    targets = EVENT_MATCH_MAP.get(t_event)
    if targets is None:
        return False
    if "*" in targets:
        return targets["*"](trigger, effect)
    check = targets.get(e_event)
    if check is None:
        return False
    return check(trigger, effect)


def _cost_feeds_edge(p1: PortRow, p2: PortRow) -> bool:
    """True iff one port's ``event_class`` is a cost event that feeds
    the other's trigger event per ``COST_FEEDS_TRIGGER``.

    The map is keyed by lowercase cost events (``sacrifice``,
    ``discard``, ``pay_life``, ...) and stores frozensets of
    CamelCase trigger events. The two naming conventions don't
    overlap, so a strict dict lookup is sufficient -- no
    ``port_type`` check required.
    """
    e1 = (p1.get("event_class") or "").strip()
    e2 = (p2.get("event_class") or "").strip()
    if not e1 or not e2:
        return False
    if e2 in COST_FEEDS_TRIGGER.get(e1, frozenset()):
        return True
    return e1 in COST_FEEDS_TRIGGER.get(e2, frozenset())


def _valid_filter_edge(p1: PortRow, p2: PortRow) -> bool:
    """True iff both ports' ``valid_filter`` strings reference
    overlapping card-type families.

    Uses :func:`_changezone_type_set` for ChangeZone-shaped filters
    (Card.X, Permanent->expansion, runtime-bound handling) and a
    simple head-token extractor as fallback for non-ChangeZone
    filters (DamageDone / Sacrificed / static / etc.). Empty or
    purely runtime-bound filters do not form an edge -- an empty
    filter is not a wildcard in this channel.
    """
    set1 = _type_token_set(p1.get("valid_filter"))
    set2 = _type_token_set(p2.get("valid_filter"))
    if set1 is None or set2 is None:
        return False
    return bool(set1 & set2)


def _type_token_set(valid_filter: str | None) -> frozenset[str] | None:
    """Extract the card-type family from any ``valid_filter`` shape.

    Returns ``None`` for empty, runtime-bound, or Card.Self-only
    filters (same contract as :func:`_changezone_type_set`). Falls
    back to a head-token extractor for non-ChangeZone filters --
    ``Creature.YouCtrl+powerGE4`` -> ``{"Creature"}``,
    ``Artifact.YouCtrl`` -> ``{"Artifact"}``.
    """
    if not valid_filter:
        return None
    cz = _changezone_type_set(valid_filter)
    if cz is not None:
        return cz
    types: set[str] = set()
    for alt in valid_filter.split(","):
        alt = alt.strip()
        if not alt:
            continue
        head = alt.split(".", 1)[0].split("+", 1)[0].strip()
        if head and head[0].isupper() and head not in _CHANGEZONE_RUNTIME_HEADS and not head.startswith("DelayTrigger"):
            types.add(head)
    return frozenset(types) if types else None
