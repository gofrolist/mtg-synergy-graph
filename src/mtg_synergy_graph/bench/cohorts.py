"""Archetype-payoff cohort selection (eval-harness instrument, plan 2026-07-03-001).

Zero scoring-path impact. This is a pure read-side partition of commanders used
by the cohort fixture bootstrap (Unit 2) and the per-commander NDCG cohort slice
(Unit 3): the golden-100/500 audit fixtures hold only a couple of the
archetype-payoff commanders, so a cohort effect is invisible in a 100/500-cmdr
aggregate. This module names the cohort so a future mechanism's effect on it can
be measured undiluted.

A cohort is the **union of a module-level tuple of predicate callables**. Only
``subtype_death_payoff`` is seeded now (Key Decision 3): a tuple + union, NOT a
name->function registry with a ``predicates`` selector — that dispatch machinery
is unearned at one member and should be added when a second predicate needs
distinguishing. Extensibility (the tuple) is kept because the outlet-direction
survivor will need a broader death-payoff predicate.

Instrument caveat (binding on any *future* mechanism judged here): a
cohort-NDCG gain is necessary-but-not-sufficient — the cohort is selected by the
same predicate a rule would key on, so a disguised whitelist scores maximally by
construction. See ``docs/plans/2026-07-03-001-feat-archetype-payoff-cohort-fixture-plan.md``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from mtg_synergy_graph.death_payoff import (
    is_death_event as _is_death_event,
)
from mtg_synergy_graph.death_payoff import (
    token_subtype_vocab as _token_subtype_vocab,
)
from mtg_synergy_graph.death_payoff import (
    valid_filter_subtype_tokens as _valid_filter_subtype_tokens,
)
from mtg_synergy_graph.graph_engine import _trigger_only_matches_self

#: Death-trigger event classes used by ``outlet_direction_death_payoff``.
_CHANGESZONE_EVENTS = frozenset({"ChangesZone", "ChangesZoneAll"})
_SACRIFICE_EVENTS = frozenset({"Sacrificed", "SacrificedOnce"})


def subtype_death_payoff(conn: sqlite3.Connection) -> set[str]:
    """Legal legendary-creature commanders with a subtype-keyed death trigger.

    A commander qualifies when it has a ``trigger`` port whose event is a death
    (Sacrificed, or ChangesZone reaching the graveyard from the battlefield) and
    whose ``valid_filter`` names a token-producible subtype (Saproling, Zombie,
    Treasure, ...). This is the seeded archetype-payoff predicate.
    """
    vocab = _token_subtype_vocab(conn)
    if not vocab:
        return set()

    rows = conn.execute(
        "SELECT p.card_name, p.event_class, p.valid_filter, p.zone_origin, p.zone_destination "
        "FROM card_ports p "
        "JOIN cards c ON c.name = p.card_name "
        "WHERE p.port_type = 'trigger' "
        "AND p.valid_filter IS NOT NULL AND p.valid_filter != '' "
        "AND c.legal_commander = 1 "
        "AND c.supertypes LIKE '%Legendary%' "
        "AND c.card_types LIKE '%Creature%'"
    )

    cohort: set[str] = set()
    for card_name, event_class, valid_filter, zone_origin, zone_destination in rows:
        if not _is_death_event(event_class, zone_origin, zone_destination):
            continue
        if any(token in vocab for token in _valid_filter_subtype_tokens(valid_filter)):
            cohort.add(card_name)
    return cohort


def outlet_direction_death_payoff(conn: sqlite3.Connection) -> set[str]:
    """Legal legendary-creature commanders with an outlet-direction death trigger.

    A commander qualifies when it has a ``ChangesZone``/``ChangesZoneAll``
    trigger port whose event is a battlefield->graveyard death
    (:func:`death_payoff.is_death_event`), whose ``valid_filter`` is not
    self-only (:func:`graph_engine._trigger_only_matches_self`), it has NO
    trigger port on ``Sacrificed``/``SacrificedOnce`` (those commanders are
    already served by the existing ``cost_feeds_trigger`` arm), and it is not
    already claimed by :func:`subtype_death_payoff` (that predicate owns its
    own members). Named by plan 2026-07-07-002 (the outlet-direction
    death-payoff cycle) as a candidate second archetype-payoff cohort for the
    ``death_outlet_feeder`` rule under development in that plan.

    Deliberately NOT appended to ``_COHORT_PREDICATES``: appending would
    change ``archetype_payoff_cohort()``'s union, silently mutate the pinned
    ``golden_set_archetype_payoff.json`` fixture's ``cohort_members``
    snapshot on the next bootstrap, and break
    ``tests/test_death_payoff.py::TestCohortUnchanged`` plus its pinned noise
    bands. This function names a candidate cohort for a *separate* fixture
    (``golden_set_outlet_payoff.json``, plan 2026-07-07-002 Task 3);
    promoting it into the shared union is a distinct, deliberate decision
    (its own plan/PR), never a side effect of merely defining the predicate.
    """
    rows = conn.execute(
        "SELECT p.card_name, p.event_class, p.valid_filter, p.zone_origin, p.zone_destination "
        "FROM card_ports p "
        "JOIN cards c ON c.name = p.card_name "
        "WHERE p.port_type = 'trigger' "
        "AND c.legal_commander = 1 "
        "AND c.supertypes LIKE '%Legendary%' "
        "AND c.card_types LIKE '%Creature%'"
    )

    qualifying: set[str] = set()
    has_sacrifice: set[str] = set()
    for card_name, event_class, valid_filter, zone_origin, zone_destination in rows:
        if event_class in _SACRIFICE_EVENTS:
            has_sacrifice.add(card_name)
            continue
        if event_class not in _CHANGESZONE_EVENTS:
            continue
        if not _is_death_event(event_class, zone_origin, zone_destination):
            continue
        if _trigger_only_matches_self(valid_filter):
            continue
        qualifying.add(card_name)

    return qualifying - has_sacrifice - subtype_death_payoff(conn)


#: The archetype-payoff cohort is the union of these predicates. Seeded with one
#: member; append a ``(conn) -> set[str]`` callable to extend (Key Decision 3).
#: ``outlet_direction_death_payoff`` above is intentionally NOT a member of this
#: tuple — see its docstring.
_COHORT_PREDICATES: tuple[Callable[[sqlite3.Connection], set[str]], ...] = (subtype_death_payoff,)


def archetype_payoff_cohort(conn: sqlite3.Connection) -> set[str]:
    """Union of every seeded cohort predicate — the full archetype-payoff cohort."""
    cohort: set[str] = set()
    for predicate in _COHORT_PREDICATES:
        cohort |= predicate(conn)
    return cohort
