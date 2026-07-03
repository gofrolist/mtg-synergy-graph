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

#: Death-trigger event classes. A ``Sacrificed`` / ``SacrificedOnce`` event is
#: unconditionally a death event; ``ChangesZone`` / ``ChangesZoneAll`` counts
#: only when it reaches the graveyard from the battlefield (see
#: :func:`_reaches_graveyard_from_battlefield`).
_SACRIFICE_EVENTS = frozenset({"Sacrificed", "SacrificedOnce"})
_CHANGESZONE_EVENTS = frozenset({"ChangesZone", "ChangesZoneAll"})

#: ``zone_origin`` values tolerated as "from the battlefield" for a
#: ChangesZone death. Real death shapes carry origins including ``Battlefield``,
#: ``Any``, empty, and comma-lists — a strict ``zone_origin='Battlefield'``
#: clause would drop them, so we accept these plus any comma-list *containing*
#: ``Battlefield``. Library/Hand-only origins (mill, tutor) are excluded.
_BATTLEFIELD_TOLERANT_ORIGINS = frozenset({"", "Battlefield", "Any"})


def _token_subtype_vocab(conn: sqlite3.Connection) -> set[str]:
    """The set of token-producible subtypes (``port_attributes`` token rows)."""
    return {
        row[0]
        for row in conn.execute("SELECT DISTINCT attr_value FROM port_attributes WHERE attr_kind = 'token_subtype'")
    }


def _valid_filter_head_tokens(valid_filter: str) -> list[str]:
    """Extract the leading subtype/type token of each comma-separated clause.

    ``valid_filter`` is a comma-list of Forge filter clauses; the head token of a
    clause is the text before the first ``.`` (restriction) or ``+`` (conjoined
    restriction). ``"Insect.YouCtrl+Other,Food.token"`` -> ``["Insect", "Food"]``.
    """
    heads: list[str] = []
    for clause in valid_filter.split(","):
        clause = clause.strip()
        if not clause:
            continue
        head = clause.split(".")[0].split("+")[0].strip()
        if head:
            heads.append(head)
    return heads


def _reaches_graveyard_from_battlefield(zone_origin: str | None, zone_destination: str | None) -> bool:
    """True when a ChangesZone event is a battlefield->graveyard death.

    Destination must reach the graveyard; origin must be battlefield-tolerant
    (see :data:`_BATTLEFIELD_TOLERANT_ORIGINS`) to exclude mill/tutor shapes.
    """
    if "Graveyard" not in (zone_destination or ""):
        return False
    origin = (zone_origin or "").strip()
    return origin in _BATTLEFIELD_TOLERANT_ORIGINS or "Battlefield" in origin


def _is_death_event(event_class: str, zone_origin: str | None, zone_destination: str | None) -> bool:
    """True when a trigger's event fires on a creature/permanent dying."""
    if event_class in _SACRIFICE_EVENTS:
        return True
    if event_class in _CHANGESZONE_EVENTS:
        return _reaches_graveyard_from_battlefield(zone_origin, zone_destination)
    return False


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
        if any(head in vocab for head in _valid_filter_head_tokens(valid_filter)):
            cohort.add(card_name)
    return cohort


#: The archetype-payoff cohort is the union of these predicates. Seeded with one
#: member; append a ``(conn) -> set[str]`` callable to extend (Key Decision 3).
_COHORT_PREDICATES: tuple[Callable[[sqlite3.Connection], set[str]], ...] = (subtype_death_payoff,)


def archetype_payoff_cohort(conn: sqlite3.Connection) -> set[str]:
    """Union of every seeded cohort predicate — the full archetype-payoff cohort."""
    cohort: set[str] = set()
    for predicate in _COHORT_PREDICATES:
        cohort |= predicate(conn)
    return cohort
