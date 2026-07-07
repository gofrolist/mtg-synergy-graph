"""Subtype-supply complement rules (plan 2026-07-07-001).

Commander gate: a subtype-keyed death-payoff trigger, via
``death_payoff.payoff_subtypes_from_ports``. That helper reproduces the
death-shape + token-subtype-vocab conjunct of the archetype-payoff cohort
predicate (``bench/cohorts.py::subtype_death_payoff``) exactly, but NOT
the cohort predicate's SQL-level ``legal_commander`` / ``Legendary`` /
``Creature`` restriction — that restriction is an offline DB-wide-scan
bound used to enumerate cohort membership, and is intentionally not
re-checked per-commander here; the engine's contract is to trust that the
commander it was called with is already a legal legendary creature. Two
candidate directions with independent rule_ids so their quality
multipliers tune independently (whitelist evidence: flooding bodies
dilutes; producers are the tougher, cleaner signal — see
deck-context-null-result-2026-07-06.md):

- ``subtype_supply_producer`` — candidate has a port that produces tokens of
  the payoff subtype (``port_attributes`` ``attr_kind='token_subtype'``).
- ``subtype_supply_body`` — candidate's ``cards.subtypes`` contains the
  payoff subtype (space-split exact-token membership, NOT LIKE — the
  documented Rat-substring-of-Pirate bug).

Both are IDF-weighted like any other rule via the
``(rule_id, cmdr_event, cand_event, filter_group)`` key with
``cand_event=<subtype>``, so rare payoff subtypes (Saproling) weigh more
than common ones (Zombie) automatically. NOT a flat bonus.

Shipped 2026-07-07 at operating point (producer=1.5, body=0.5) — verdict
PARTIAL, human-approved on a Pareto-dominance rationale (S1 cohort ΔNDCG
+0.0650 fell short of the +0.0697 SHIP bar; S2-S6 all passed; see
docs/RULE_HISTORY.md 2026-07-07 entry for the full gate table and
.audit/subtype_supply/decision.md for sweep provenance). The flag stays
default-True as a kill-switch — flip to ``False`` to fully disable both
rule_ids without touching ``scoring_weights.json``.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import TYPE_CHECKING

from mtg_synergy_graph.death_payoff import payoff_subtypes_from_ports

from .core import PortComplement, PortRow

if TYPE_CHECKING:
    from ..penalties import CandidateCache

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
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Producer + body supply for subtype-keyed death-payoff commanders.

    When ``candidate_cache`` is provided, the body direction iterates the
    shared in-memory ``candidate_attr_rows`` instead of re-issuing the
    full ``cards`` table scan for every commander in a batch run (see
    ``density._find_etb_self_complements`` for the established pattern).
    """
    if not _ENABLE_SUBTYPE_SUPPLY:
        return []
    subs = payoff_subtypes_from_ports(conn, cmdr_ports)
    if not subs:
        return []

    results: list[PortComplement] = []

    # Producer direction: one query for all payoff subtypes at once,
    # keyed by subtype so we can credit each card to the FIRST subtype
    # (in sorted order) it produces -- matches the previous per-subtype
    # query loop's dedup semantics exactly.
    seen_producer: set[str] = set()
    placeholders = ",".join("?" * len(subs))
    producers_by_sub: dict[str, list[str]] = defaultdict(list)
    cur = conn.execute(
        "SELECT DISTINCT p.card_name, a.attr_value FROM card_ports p "
        "JOIN port_attributes a ON a.port_id = p.id "
        f"WHERE a.attr_kind = 'token_subtype' AND a.attr_value IN ({placeholders})",
        tuple(subs),
    )
    for row in cur.fetchall():
        producers_by_sub[row["attr_value"]].append(row["card_name"])

    for sub in subs:
        for name in producers_by_sub.get(sub, ()):
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
    if candidate_cache is not None:
        body_rows: list[tuple[str, str]] = [
            (row["name"], row["subtypes"] or "") for row in candidate_cache.candidate_attr_rows.values()
        ]
    else:
        body_rows = [
            (r["name"], r["subtypes"] or "")
            for r in conn.execute(
                "SELECT name, subtypes FROM cards WHERE subtypes IS NOT NULL AND subtypes != ''"
            ).fetchall()
        ]
    for name, subtypes in body_rows:
        if not subtypes:
            continue
        if name in cmdr_set or name in seen_body:
            continue
        matched = sub_set & set(subtypes.split())
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
