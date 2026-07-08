"""Death-payoff subtype detection — shared by the cohort predicate and the
subtype-supply rule (plan 2026-07-07-001).

Extracted verbatim from ``bench/cohorts.py`` so the scoring path
(``complement_rules/subtype_supply.py``) can reuse the death-shape +
token-subtype-vocab conjunct that selects the archetype-payoff cohort,
without importing from ``bench``. This module reproduces that conjunct
exactly; it does NOT reproduce the cohort predicate's additional SQL-level
``legal_commander`` / ``Legendary`` / ``Creature`` restriction
(``bench/cohorts.py::subtype_death_payoff``), which is an offline
DB-wide-scan bound used only to enumerate cohort membership. The scoring
path intentionally skips that restriction — its contract is to trust that
the commander it is called with (``cmdr_ports``) is already a legal
legendary creature, not to re-verify it per call.
Behavior change here changes BOTH the cohort membership and the rule gate —
the cohort fixture's pinned ``cohort_members`` snapshot is the regression
oracle (tests/test_death_payoff.py::TestCohortUnchanged).

``_trigger_only_matches_self`` is imported from ``graph_engine`` (not
duplicated here) — every sibling ``complement_rules`` module already
imports it directly from there, so this module gains no new import-cycle
exposure by doing the same (PR #103 review, F4).
"""

from __future__ import annotations

import sqlite3

from mtg_synergy_graph.graph_engine import _trigger_only_matches_self

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


def has_sacrificed_trigger(cmdr_ports: list) -> bool:
    """True when any trigger port's event_class is a Sacrificed-family event.

    Factored out (PR #103 review, F4) so the "does this commander already
    have a dedicated sacrifice trigger" conjunct — previously duplicated in
    ``complement_rules/death_outlet.py`` and ``bench/context_sim.py`` — has
    one canonical home.
    """
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() in _SACRIFICE_EVENTS:
            return True
    return False


def has_changeszone_death_payoff(cmdr_ports: list) -> bool:
    """True when some trigger port is a ``ChangesZone``/``ChangesZoneAll``-shaped
    death event (battlefield -> graveyard, :func:`is_death_event`) that is not
    self-only (:func:`_trigger_only_matches_self`).

    This is the port-level core of ``bench.cohorts.outlet_direction_death_payoff``
    (plan 2026-07-07-002 Task 2), factored here so the ``death_outlet_feeder``
    rule gate and its whitelist comparator (Tasks 5/6) can reuse the exact same
    conjunct against an already-loaded ``cmdr_ports`` list instead of a DB-wide
    SQL scan. Deliberately does NOT check for ``Sacrificed``/``SacrificedOnce``
    triggers (those commanders are served by the existing ``cost_feeds_trigger``
    arm) or for ``subtype_death_payoff`` cohort membership — both are
    cohort-enumeration-specific exclusions applied by the cohort predicate
    itself, not part of this port-level gate.
    """
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        event_class = (p.get("event_class") or "").strip()
        if event_class not in _CHANGESZONE_EVENTS:
            continue
        if not is_death_event(event_class, p.get("zone_origin"), p.get("zone_destination")):
            continue
        if _trigger_only_matches_self(p.get("valid_filter")):
            continue
        return True
    return False


def payoff_subtypes_from_ports(conn: sqlite3.Connection, cmdr_ports: list) -> list[str]:
    """Sorted payoff subtypes named by the commander's death-trigger filters.

    A subtype qualifies when (a) some trigger port is a death event
    (:func:`is_death_event`) and (b) its ``valid_filter`` names a token in
    the token-producible vocabulary (:func:`token_subtype_vocab`) — the same
    two conditions as ``bench.cohorts.subtype_death_payoff``, applied to an
    already-loaded port list instead of a DB-wide scan.

    Pass 1 (over ``cmdr_ports``, no DB access) collects every death-trigger
    ``valid_filter``; the great majority of commanders have none, so this
    lets us return ``[]`` before ever calling :func:`token_subtype_vocab`
    (a DB-wide scan) on pass 2.
    """
    death_filters: list[str] = []
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        event_class = (p.get("event_class") or "").strip()
        if not is_death_event(event_class, p.get("zone_origin"), p.get("zone_destination")):
            continue
        valid_filter = p.get("valid_filter") or ""
        if not valid_filter:
            continue
        death_filters.append(valid_filter)
    if not death_filters:
        return []

    vocab = token_subtype_vocab(conn)
    if not vocab:
        return []
    subs: set[str] = set()
    for valid_filter in death_filters:
        subs.update(t for t in valid_filter_subtype_tokens(valid_filter) if t in vocab)
    return sorted(subs)
