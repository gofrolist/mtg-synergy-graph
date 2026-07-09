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

from typing import TYPE_CHECKING

from .core import PortRow

if TYPE_CHECKING:
    pass

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
