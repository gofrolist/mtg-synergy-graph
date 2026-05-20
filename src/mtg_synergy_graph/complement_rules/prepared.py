"""Prepared / AlternateMode:Prepare mechanic synergy.

Captures the new mechanic added in the 2026-05-19 Forge refresh
(brainstorm: ``docs/brainstorms/2026-05-19-prepared-mechanic-requirements.md``).

A creature with the top-level header ``AlternateMode:Prepare`` has a
stored back-face spell castable when it becomes "prepared" (a state set
via ``DB$ AlterAttribute | Attributes$ Prepared``). Two roles in the
ecosystem:

1. **Prepared-payoff cards** — creatures with ``AlternateMode:Prepare``.
   The importer emits a synthetic ``static`` port ``event_class='AlternateMode'``
   with ``granted_keyword='Prepare'`` for each one (see ``ports.py``
   ``extract_alternate_mode_ports``).
2. **Prepare-enablers** — abilities that grant the Prepared state via
   ``AlterAttribute``. The importer explodes the ``Attributes$ Prepared``
   value into ``port_attributes`` under ``attr_kind='attribute'`` so the
   rule can join on it.

A commander is in the Prepared ecosystem when **either** signal is
present on its port set. Every other Prepared-payoff card in the legal
universe is a complement — the mechanic is gated by reciprocity
("only creatures with prepare spells can become prepared"), so Prepared
cards co-deck like a tribe.
"""

from __future__ import annotations

import sqlite3

from .core import PortComplement, PortRow

_RULE_ID = "prepared_mechanic"


def _commander_has_alternate_mode_prepare(cmdr_ports: list[PortRow]) -> bool:
    """Cheap path — commander itself is a Prepared-payoff card.

    Covers all 47 self-preparing creatures (including the ~20 that
    self-prepare via ``K:ETBReplacement:Other:DBPrepare`` which doesn't
    surface an AlterAttribute port on the commander side).
    """
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "static":
            continue
        if (p.get("event_class") or "").strip() != "AlternateMode":
            continue
        if (p.get("granted_keyword") or "").strip() == "Prepare":
            return True
    return False


def _commander_prepares_creatures(conn: sqlite3.Connection, cmdr_set: set[str]) -> bool:
    """Slow path — commander has an AlterAttribute effect that grants
    Prepared. Covers prepare-enabler commanders that aren't themselves
    Prepared-payoff cards.

    Runs at most once per commander (gated by the cheap path missing)
    and uses the indexed ``port_attributes`` lookup.
    """
    placeholders = ",".join("?" * len(cmdr_set))
    row = conn.execute(
        f"SELECT 1 FROM card_ports cp "
        f"JOIN port_attributes pa ON pa.port_id = cp.id "
        f"WHERE cp.card_name IN ({placeholders}) "
        f"AND cp.event_class = 'AlterAttribute' "
        f"AND pa.attr_kind = 'attribute' "
        f"AND pa.attr_value = 'Prepared' "
        f"LIMIT 1",
        tuple(cmdr_set),
    ).fetchone()
    return row is not None


def _find_prepared_mechanic_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    in_ecosystem = _commander_has_alternate_mode_prepare(cmdr_ports)
    if not in_ecosystem:
        in_ecosystem = _commander_prepares_creatures(conn, cmdr_set)
    if not in_ecosystem:
        return []

    placeholders = ",".join("?" * len(cmdr_set))
    rows = conn.execute(
        f"SELECT DISTINCT card_name FROM card_ports "
        f"WHERE port_type = 'static' "
        f"AND event_class = 'AlternateMode' "
        f"AND granted_keyword = 'Prepare' "
        f"AND card_name NOT IN ({placeholders})",
        tuple(cmdr_set),
    ).fetchall()

    return [
        PortComplement(
            rule_id=_RULE_ID,
            direction="synergy",
            candidate=r["card_name"],
            cmdr_event="Prepared_ecosystem",
            cand_event="AlternateMode_Prepare",
        )
        for r in rows
    ]
