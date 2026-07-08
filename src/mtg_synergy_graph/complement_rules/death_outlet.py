"""Death-outlet-feeder complement rule (plan 2026-07-07-002 Task 6).

Commander gate: a ChangesZone/ChangesZoneAll-shaped death trigger
(``death_payoff.has_changeszone_death_payoff``) that carries no
``Sacrificed``/``SacrificedOnce`` trigger port. That second conjunct is
NOT part of ``has_changeszone_death_payoff`` itself -- its docstring is
explicit that it "deliberately does NOT check for Sacrificed ...
triggers" -- it is composed here to match
``bench.cohorts.outlet_direction_death_payoff``'s port-level semantics
exactly: commanders with a Sacrificed-shaped trigger are already served
by the existing ``cost_feeds_trigger`` arm (``combat.py:441``,
``_invert_cost_feeds``), so this rule only fires for the *other* half of
the outlet cohort -- commanders whose death payoff triggers off
ChangesZone-to-graveyard but who have no dedicated sacrifice trigger of
their own. Mirrors ``bench/context_sim.py::outlet_whitelist_scores``'s
gate composition (Task 5's whitelist comparator) verbatim.

CORRECTED (2026-07-08, PR #103 review): the paragraph above's framing
is misleading -- it reads as though ONLY the Sacrificed-trigger half of
the cohort is already served and this rule's ChangesZone half is
genuinely unserved territory. In fact ``combat.py``'s ChangesZone-gated
arm (``_find_sacrifice_outlets``) ALSO emits ``cost_feeds_trigger``
complements for ChangesZone-to-graveyard death commanders (Judith,
Marchesa, Meren, and the rest of this rule's own cohort) -- this rule
overlaps that arm rather than filling a gap next to it. See
``docs/solutions/best-practices/death-outlet-feeder-null-result-2026-07-07.md``'s
CORRECTION block for the full incident.

Candidate side: every card holding a ``cost.sacrifice`` port -- a sac
outlet -- gets one ``PortComplement`` per card (deduped), classified via
``core._cost_filter_group`` (``free_outlet`` / ``paid_outlet`` /
``self_sac``) so IDF differentiates outlet classes exactly like the
sibling ``cost_feeds_trigger`` arm's enriched filter_group does. The
classification is surfaced as ``cand_event`` (not a filter_group suffix)
since this rule has no commander-side filter_group of its own to enrich.

Flag: ``_ENABLE_DEATH_OUTLET_FEEDER = False`` -- DECLINED at
pre-registered gates (plan 2026-07-07-002; full record at
``docs/solutions/best-practices/death-outlet-feeder-null-result-2026-07-07.md``).
The flag stays ``False`` permanently absent a new plan. Any re-flip
must additionally account for two issues discovered in review, neither
swept by the original cycle:

1. The live gate is BROADER than the measured cohort -- it also fires
   for ``subtype_death_payoff`` members (e.g. Wilhelt, the Rotcleaver;
   Slimefoot, the Stowaway), stacking with the shipped
   ``subtype_supply_*`` rules, a combination never measured.
2. The rule overlaps ``combat.py``'s ``cost_feeds_trigger`` arm on
   ChangesZone-death commanders (see the CORRECTED paragraph above),
   double-crediting the same pair from two rule_ids at once.

A re-flip therefore requires a new plan with re-derived cohort
exclusions (both the subtype-cohort overlap and the cost_feeds_trigger
overlap) and fresh gates -- do not simply flip the flag back on top of
the old measurements. If flipped, the choreography is: flag ``True``
alongside a ``scoring_weights.json`` multiplier entry and (if
warranted) a ``ScoringConfigInputs`` field -- neither exists yet, so
flipping this flag today would NOT be hash-neutral. See
docs/RULE_PLANNING.md and the plan's Global Constraints section.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from mtg_synergy_graph.death_payoff import has_changeszone_death_payoff, has_sacrificed_trigger

from .core import PortComplement, PortRow, _cost_filter_group

#: DECLINED at pre-registered gates (plan 2026-07-07-002) -- stays False
#: permanently absent a new plan. See the module docstring's re-flip
#: warning (gate breadth vs. the subtype cohort, overlap with
#: combat.py's cost_feeds_trigger arm) before ever considering a flip,
#: and docs/solutions/best-practices/death-outlet-feeder-null-result-2026-07-07.md
#: for the full null-result record.
_ENABLE_DEATH_OUTLET_FEEDER = False


def _commander_has_death_outlet_gate(cmdr_ports: list[PortRow]) -> bool:
    """Port-level commander gate for ``death_outlet_feeder``.

    ``has_changeszone_death_payoff`` AND no ``Sacrificed``/
    ``SacrificedOnce`` trigger port (:func:`death_payoff.has_sacrificed_trigger`)
    -- composed here rather than in ``death_payoff`` because the second
    conjunct is specific to this rule's "don't double-serve the
    cost_feeds_trigger cohort" contract, not a property of the death-payoff
    shape itself. Mirrors ``bench/context_sim.py::outlet_whitelist_scores``
    verbatim.
    """
    if not has_changeszone_death_payoff(cmdr_ports):
        return False
    return not has_sacrificed_trigger(cmdr_ports)


def _find_death_outlet_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Sac-outlet candidates for outlet-cohort ChangesZone-death commanders.

    See the module docstring for the commander gate and candidate-side
    classification. One ``PortComplement`` per candidate card holding a
    ``cost.sacrifice`` port (deduped), with ``cand_event`` set to the
    ``_cost_filter_group`` classification (``free_outlet`` /
    ``paid_outlet`` / ``self_sac``) so IDF differentiates outlet classes.

    Determinism (PR #103 review, F9): a card can hold more than one
    ``cost.sacrifice`` port (e.g. one free_outlet-shaped ability and one
    self_sac-shaped ability on the same card). The SQL query below carries
    no ``ORDER BY``, so which row SQLite returns first is not guaranteed
    stable across DB rebuilds -- picking "whichever classification arrived
    first" would make ``cand_event`` a coin flip. Instead every one of a
    card's matching ports is classified and ``min()`` (alphabetical) picks
    the representative group, matching the same
    take-the-alphabetically-least-match precedent used by
    ``subtype_supply``'s ``min(matched)``. Candidate iteration itself is
    also sorted by name for a fully deterministic ``results`` order.
    """
    if not _ENABLE_DEATH_OUTLET_FEEDER:
        return []
    if not _commander_has_death_outlet_gate(cmdr_ports):
        return []

    groups_by_card: dict[str, set[str]] = defaultdict(set)
    cur = conn.execute(
        "SELECT card_name, event_class, cost_target, raw_line FROM card_ports "
        "WHERE port_type = 'cost' AND event_class = 'sacrifice'"
    )
    for row in cur.fetchall():
        name = row["card_name"]
        if name in cmdr_set:
            continue
        cost_port = {
            "event_class": row["event_class"],
            "cost_target": row["cost_target"],
            "raw_line": row["raw_line"],
        }
        groups_by_card[name].add(_cost_filter_group(cost_port))

    results: list[PortComplement] = []
    for name in sorted(groups_by_card):
        results.append(
            PortComplement(
                rule_id="death_outlet_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="death_outlet",
                cand_event=min(groups_by_card[name]),
            )
        )
    return results
