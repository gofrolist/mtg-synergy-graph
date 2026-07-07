"""Death-payoff subtype detection — shared by the cohort predicate and the
subtype-supply rule (plan 2026-07-07-001).

Extracted verbatim from ``bench/cohorts.py`` so the scoring path
(``complement_rules/subtype_supply.py``) can reuse the EXACT predicate that
selects the archetype-payoff cohort without importing from ``bench``.
Behavior change here changes BOTH the cohort membership and the rule gate —
the cohort fixture's pinned ``cohort_members`` snapshot is the regression
oracle (tests/test_death_payoff.py::TestCohortUnchanged).
"""

from __future__ import annotations

import sqlite3

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
#: The empty-origin arm is the loosest (it could in principle admit a
#: "put into a graveyard from anywhere" mill payoff), and is intentional per the
#: plan's decision to prefer inclusion; it is currently inert — no cohort
#: commander matches solely via an empty-origin ChangesZone (verified against
#: data/synergy.db). Re-check after a cardsfolder refresh if precision matters.
_BATTLEFIELD_TOLERANT_ORIGINS = frozenset({"", "Battlefield", "Any"})


def token_subtype_vocab(conn: sqlite3.Connection) -> set[str]:
    """The set of token-producible subtypes (``port_attributes`` token rows)."""
    return {
        row[0]
        for row in conn.execute("SELECT DISTINCT attr_value FROM port_attributes WHERE attr_kind = 'token_subtype'")
    }


def valid_filter_subtype_tokens(valid_filter: str) -> list[str]:
    """Every token in a ``valid_filter`` that could name a subtype.

    ``valid_filter`` is a comma-list of Forge filter clauses. A subtype can be
    the clause head (``Insect.YouCtrl`` -> ``Insect``) OR a restriction after
    the ``.`` (``Creature.Zombie+YouCtrl`` -> the head is ``Creature`` but the
    subtype is ``Zombie``). Both forms occur in the corpus, so we emit the head
    plus every ``+``-separated restriction token; the caller filters against the
    token-subtype vocabulary, so non-subtype restrictions (``YouCtrl``,
    ``!token``, ``Other``) simply never match. Negated (``!Zombie``) and
    ``non``-prefixed tokens keep their prefix and so do not false-match a bare
    subtype. ``"Insect.YouCtrl,Creature.Zombie+Other"`` ->
    ``["Insect", "YouCtrl", "Creature", "Zombie", "Other"]``.
    """
    tokens: list[str] = []
    for clause in valid_filter.split(","):
        clause = clause.strip()
        if not clause:
            continue
        head, _, restriction = clause.partition(".")
        head = head.split("+")[0].strip()
        if head:
            tokens.append(head)
        for part in restriction.split("+"):
            part = part.strip()
            if part:
                tokens.append(part)
    return tokens


def reaches_graveyard_from_battlefield(zone_origin: str | None, zone_destination: str | None) -> bool:
    """True when a ChangesZone event is a battlefield->graveyard death.

    Destination must reach the graveyard; origin must be battlefield-tolerant
    (see :data:`_BATTLEFIELD_TOLERANT_ORIGINS`) to exclude mill/tutor shapes.
    """
    if "Graveyard" not in (zone_destination or ""):
        return False
    origin = (zone_origin or "").strip()
    return origin in _BATTLEFIELD_TOLERANT_ORIGINS or "Battlefield" in origin


def is_death_event(event_class: str, zone_origin: str | None, zone_destination: str | None) -> bool:
    """True when a trigger's event fires on a creature/permanent dying."""
    if event_class in _SACRIFICE_EVENTS:
        return True
    if event_class in _CHANGESZONE_EVENTS:
        return reaches_graveyard_from_battlefield(zone_origin, zone_destination)
    return False


def payoff_subtypes_from_ports(conn: sqlite3.Connection, cmdr_ports: list) -> list[str]:
    """Sorted payoff subtypes named by the commander's death-trigger filters.

    A subtype qualifies when (a) some trigger port is a death event
    (:func:`is_death_event`) and (b) its ``valid_filter`` names a token in
    the token-producible vocabulary (:func:`token_subtype_vocab`) — the same
    two conditions as ``bench.cohorts.subtype_death_payoff``, applied to an
    already-loaded port list instead of a DB-wide scan.
    """
    vocab = token_subtype_vocab(conn)
    if not vocab:
        return []
    subs: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        event_class = (p.get("event_class") or "").strip()
        if not is_death_event(event_class, p.get("zone_origin"), p.get("zone_destination")):
            continue
        valid_filter = p.get("valid_filter") or ""
        if not valid_filter:
            continue
        subs.update(t for t in valid_filter_subtype_tokens(valid_filter) if t in vocab)
    return sorted(subs)
