"""Subtype-supply complement rules (plan 2026-07-07-001).

Commander gate: a subtype-keyed death-payoff trigger — the EXACT predicate
that selects the archetype-payoff cohort (via
``death_payoff.payoff_subtypes_from_ports``). Two candidate directions with
independent rule_ids so their quality multipliers tune independently
(whitelist evidence: flooding bodies dilutes; producers are the tougher,
cleaner signal — see deck-context-null-result-2026-07-06.md):

- ``subtype_supply_producer`` — candidate has a port that produces tokens of
  the payoff subtype (``port_attributes`` ``attr_kind='token_subtype'``).
- ``subtype_supply_body`` — candidate's ``cards.subtypes`` contains the
  payoff subtype (space-split exact-token membership, NOT LIKE — the
  documented Rat-substring-of-Pirate bug).

Both are IDF-weighted like any other rule via the
``(rule_id, cmdr_event, cand_event, filter_group)`` key with
``cand_event=<subtype>``, so rare payoff subtypes (Saproling) weigh more
than common ones (Zombie) automatically. NOT a flat bonus.

Flag-gated default-OFF until the plan's decision gates pass (S1-S6);
registration inputs (scoring_weights entries) land only on the SHIP path so
the Task 3 commit is config-hash-neutral.
"""

from __future__ import annotations

import sqlite3

from mtg_synergy_graph.death_payoff import payoff_subtypes_from_ports

from .core import PortComplement, PortRow

#: Shipped (plan 2026-07-07-001 Task 6, 2026-07-07) at operating point
#: (producer=1.5, body=0.5) — verdict PARTIAL, human-approved SHIP on the
#: Pareto-dominance rationale (beats both whitelist variants at <=1 cliff;
#: see docs/RULE_HISTORY.md 2026-07-07 entry and .audit/subtype_supply/
#: decision.md for the full gate table).
_ENABLE_SUBTYPE_SUPPLY = True


def _find_subtype_supply_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Producer + body supply for subtype-keyed death-payoff commanders."""
    if not _ENABLE_SUBTYPE_SUPPLY:
        return []
    subs = payoff_subtypes_from_ports(conn, cmdr_ports)
    if not subs:
        return []

    results: list[PortComplement] = []

    seen_producer: set[str] = set()
    for sub in subs:
        cur = conn.execute(
            "SELECT DISTINCT p.card_name FROM card_ports p "
            "JOIN port_attributes a ON a.port_id = p.id "
            "WHERE a.attr_kind = 'token_subtype' AND a.attr_value = ?",
            (sub,),
        )
        for r in cur.fetchall():
            name = r["card_name"]
            if name in cmdr_set or name in seen_producer:
                continue
            seen_producer.add(name)
            results.append(
                PortComplement(
                    rule_id="subtype_supply_producer",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="death_payoff",
                    cand_event=sub,
                )
            )

    sub_set = set(subs)
    seen_body: set[str] = set()
    cur = conn.execute("SELECT name, subtypes FROM cards WHERE subtypes IS NOT NULL AND subtypes != ''")
    for r in cur.fetchall():
        name = r["name"]
        if name in cmdr_set or name in seen_body:
            continue
        matched = sub_set & set((r["subtypes"] or "").split())
        if not matched:
            continue
        seen_body.add(name)
        results.append(
            PortComplement(
                rule_id="subtype_supply_body",
                direction="synergy",
                candidate=name,
                cmdr_event="death_payoff",
                cand_event=min(matched),
            )
        )

    return results
