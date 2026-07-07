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

Candidate side: every card holding a ``cost.sacrifice`` port -- a sac
outlet -- gets one ``PortComplement`` per card (deduped), classified via
``core._cost_filter_group`` (``free_outlet`` / ``paid_outlet`` /
``self_sac``) so IDF differentiates outlet classes exactly like the
sibling ``cost_feeds_trigger`` arm's enriched filter_group does. The
classification is surfaced as ``cand_event`` (not a filter_group suffix)
since this rule has no commander-side filter_group of its own to enrich.

Flag: ``_ENABLE_DEATH_OUTLET_FEEDER = False`` -- decision-gated, plan
2026-07-07-002. Flips only on the SHIP path, alongside a
``scoring_weights.json`` multiplier entry and (if warranted) a
``ScoringConfigInputs`` field -- neither exists yet, so flipping this
flag today would NOT be hash-neutral. Do not flip without that
choreography; see docs/RULE_PLANNING.md and the plan's Global
Constraints section.
"""

from __future__ import annotations

import sqlite3

from mtg_synergy_graph.death_payoff import has_changeszone_death_payoff

from .core import PortComplement, PortRow, _cost_filter_group

#: Decision-gated (plan 2026-07-07-002) -- flips only on the SHIP path,
#: together with a scoring_weights.json multiplier entry and
#: ScoringConfigInputs registration. Keep False until that choreography
#: lands; see the module docstring.
_ENABLE_DEATH_OUTLET_FEEDER = False

#: Trigger event classes that indicate the commander already has a
#: dedicated sacrifice trigger -- served by the existing
#: ``cost_feeds_trigger`` arm, so excluded here to avoid double-crediting
#: the same commander from two rules.
_SACRIFICE_TRIGGER_EVENTS = frozenset({"Sacrificed", "SacrificedOnce"})


def _commander_has_death_outlet_gate(cmdr_ports: list[PortRow]) -> bool:
    """Port-level commander gate for ``death_outlet_feeder``.

    ``has_changeszone_death_payoff`` AND no ``Sacrificed``/
    ``SacrificedOnce`` trigger port -- composed here rather than in
    ``death_payoff`` because the second conjunct is specific to this
    rule's "don't double-serve the cost_feeds_trigger cohort" contract,
    not a property of the death-payoff shape itself. Mirrors
    ``bench/context_sim.py::outlet_whitelist_scores`` verbatim.
    """
    if not has_changeszone_death_payoff(cmdr_ports):
        return False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() in _SACRIFICE_TRIGGER_EVENTS:
            return False
    return True


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
    """
    if not _ENABLE_DEATH_OUTLET_FEEDER:
        return []
    if not _commander_has_death_outlet_gate(cmdr_ports):
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    cur = conn.execute(
        "SELECT card_name, event_class, cost_target, raw_line FROM card_ports "
        "WHERE port_type = 'cost' AND event_class = 'sacrifice'"
    )
    for row in cur.fetchall():
        name = row["card_name"]
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        cost_port = {
            "event_class": row["event_class"],
            "cost_target": row["cost_target"],
            "raw_line": row["raw_line"],
        }
        filter_group = _cost_filter_group(cost_port)
        results.append(
            PortComplement(
                rule_id="death_outlet_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="death_outlet",
                cand_event=filter_group,
            )
        )
    return results
