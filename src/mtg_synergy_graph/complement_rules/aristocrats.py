# src/mtg_synergy_graph/complement_rules/aristocrats.py
"""Aristocrats death-bridge rule (spec 2026-07-09).

Bridges sacrifice-outlet / death-engine commanders to the aristocrats
death-value class — death-triggered payoffs (Blood Artist, Zulaport) and
self-recursive fodder (Reassembling Skeleton, Butcher Ghoul) — that the current
vocabulary leaves unranked because the substrate has no
``sacrifice -> ChangesZone(bf->grave)`` equivalence. Dedicated flag-gated,
own-pool implementation: does NOT touch the shared event-match substrate.
Default OFF; config-hash-neutral until a measured SHIP flip.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .core import PortComplement, PortRow

if TYPE_CHECKING:
    from ..penalties import CandidateCache

#: Default-OFF flag. NOT registered in ScoringConfigInputs — flipping it does
#: not change config_hash until the SHIP commit adds the scoring_weights entry.
_ENABLE_ARISTOCRATS_DEATH_BRIDGE: bool = False


def _commander_is_aristocrats(cmdr_ports: list[PortRow]) -> bool:
    """Unit 1 gate: the commander establishes a death/sacrifice engine.

    True iff some port is EITHER
    * a creature sacrifice outlet — ``cost``/``sacrifice`` whose ``cost_subtype``
      references a Creature and whose ``cost_target`` is not ``self``; OR
    * a death-trigger payoff — ``trigger``/``ChangesZone`` Battlefield->Graveyard.
    """
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "cost" and ev == "sacrifice":
            sub = p.get("cost_subtype") or ""
            tgt = (p.get("cost_target") or "").strip()
            if "Creature" in sub and tgt != "self":
                return True
        if pt == "trigger" and ev == "ChangesZone":
            zo = p.get("zone_origin") or ""
            zd = p.get("zone_destination") or ""
            if "Battlefield" in zo and "Graveyard" in zd:
                return True
    return False


def _find_aristocrats_death_bridge(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Aristocrats death-value payoffs for sacrifice/death-engine commanders.

    Fires when the commander passes ``_commander_is_aristocrats``. Emits two IDF
    tiers scanned strong-first with dedup so the stronger credit wins:
    ``death_payoff`` (death-triggered value payoffs, ~218) > ``recursive_fodder``
    (self-returning bodies, ~131). A bounded, mechanically-discriminated subset —
    NOT a flat creature/token-producer flood.
    """
    if not _ENABLE_ARISTOCRATS_DEATH_BRIDGE:
        return []
    if not _commander_is_aristocrats(cmdr_ports):
        return []

    if candidate_cache is not None:
        payoffs = candidate_cache.aristocrats_death_payoff_cards
        fodder = candidate_cache.aristocrats_recursive_fodder_cards
    else:
        from ..penalties import (
            _bulk_load_aristocrats_death_payoff_cards,
            _bulk_load_aristocrats_recursive_fodder_cards,
        )

        payoffs = _bulk_load_aristocrats_death_payoff_cards(conn)
        fodder = _bulk_load_aristocrats_recursive_fodder_cards(conn)

    results: list[PortComplement] = []
    seen: set[str] = set()

    def _emit(name: str, tier: str) -> None:
        if name in cmdr_set or name in seen:
            return
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="aristocrats_death_bridge",
                direction="synergy",
                candidate=name,
                cmdr_event="death_engine",
                cand_event=tier,
            )
        )

    for name in sorted(payoffs):
        _emit(name, "death_payoff")
    for name in sorted(fodder):
        _emit(name, "recursive_fodder")
    return results
