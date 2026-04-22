"""``port_nodes`` SQL view — canonical projection over ``card_ports``
(Unit 2 of plan 003).

Thin wrapper over ``card_ports`` that adds two computed columns:

* ``node_kind`` — maps each ``(port_type, event_class)`` pair to a
  value in ``vocabulary.NODE_KINDS``; unmapped pairs get ``'UNKNOWN'``.
* ``subkind`` — always ``port_type || '.' || event_class``. Keys the
  view for downstream consumers (interpreter predicates,
  ``--unknowns`` reporter). Deterministic; makes ``UNKNOWN`` rows
  queryable by their raw shape without a second SQL pass.

All raw ``card_ports`` columns the current rule engine uses are
re-exposed here verbatim so any existing query can switch from
``card_ports`` to ``port_nodes`` without row-shape changes.

The view is defined by repeated CASE branches — not a table lookup —
because the mapping set is tiny (<30 branches) and SQLite's optimizer
handles literal CASE efficiently. The branch set grows in Units 7
and 8 as rule migrations demand coverage of new
``(port_type, event_class)`` pairs.
"""

from __future__ import annotations

import sqlite3

#: DDL text for the ``port_nodes`` view. Sourced from a Python
#: constant so ``schema.sql``, the importer, and test fixtures can
#: all reference the same authoritative text. Ordering of the CASE
#: branches does not matter — SQLite evaluates them top-down but the
#: branches are mutually exclusive by construction.
PORT_NODES_VIEW_SQL: str = """
CREATE VIEW IF NOT EXISTS port_nodes AS
SELECT
    card_name,
    port_type,
    event_class,
    valid_filter,
    zone_origin,
    zone_destination,
    counter_type,
    raw_line,
    (COALESCE(port_type, '') || '.' || COALESCE(event_class, '')) AS subkind,
    CASE
        -- Triggers
        WHEN port_type = 'trigger' AND event_class = 'ChangesZone'
             AND zone_destination = 'Battlefield'
            THEN 'ETB'
        WHEN port_type = 'trigger' AND event_class = 'ChangesZone'
             AND zone_destination = 'Graveyard'
            THEN 'DIES'
        WHEN port_type = 'trigger' AND event_class = 'ChangesZone'
             AND zone_origin = 'Battlefield'
             AND zone_destination IS NOT NULL
             AND zone_destination <> 'Battlefield'
             AND zone_destination <> 'Graveyard'
            THEN 'LTB'
        WHEN port_type = 'trigger' AND event_class = 'ChangesZoneAll'
            THEN 'ZONECHANGE'
        WHEN port_type = 'trigger' AND event_class = 'SpellCast'
            THEN 'CAST'
        WHEN port_type = 'trigger' AND event_class = 'Attacks'
            THEN 'ATTACK'
        WHEN port_type = 'trigger' AND event_class = 'AttackerBlocked'
            THEN 'BLOCK'
        WHEN port_type = 'trigger' AND event_class = 'DamageDone'
            THEN 'DAMAGE'
        WHEN port_type = 'trigger' AND event_class IN ('LifeGained', 'LifeLost')
            THEN 'LIFE_CHANGE'
        WHEN port_type = 'trigger' AND event_class = 'Sacrificed'
            THEN 'SACRIFICE'
        WHEN port_type = 'trigger' AND event_class = 'Discarded'
            THEN 'DISCARD'
        WHEN port_type = 'trigger' AND event_class = 'Drawn'
            THEN 'DRAW'
        WHEN port_type = 'trigger' AND event_class = 'Taps'
            THEN 'TAP'
        WHEN port_type = 'trigger' AND event_class = 'Untaps'
            THEN 'UNTAP'
        WHEN port_type = 'trigger' AND event_class = 'CounterAdded'
            THEN 'COUNTER_PLACED'
        WHEN port_type = 'trigger' AND event_class = 'CounterRemoved'
            THEN 'COUNTER_REMOVED'
        WHEN port_type = 'trigger' AND event_class = 'TapsForMana'
            THEN 'PAYMANA'
        -- Effects — symmetric with triggers on the same event_class
        -- where the semantics align.
        WHEN port_type = 'effect' AND event_class IN ('Token', 'ChangeZone', 'ChangeZoneAll',
                                                       'CopyPermanent', 'Animate')
            THEN 'ZONECHANGE'
        WHEN port_type = 'effect' AND event_class = 'Mana'
            THEN 'PAYMANA'
        WHEN port_type = 'effect' AND event_class IN ('DealDamage', 'DamageAll')
            THEN 'DAMAGE'
        WHEN port_type = 'effect' AND event_class = 'GainLife'
            THEN 'LIFE_CHANGE'
        WHEN port_type = 'effect' AND event_class IN ('Sacrifice', 'SacrificeAll')
            THEN 'SACRIFICE'
        WHEN port_type = 'effect' AND event_class = 'Discard'
            THEN 'DISCARD'
        WHEN port_type = 'effect' AND event_class = 'Draw'
            THEN 'DRAW'
        WHEN port_type = 'effect' AND event_class IN ('Tap', 'TapAll')
            THEN 'TAP'
        WHEN port_type = 'effect' AND event_class IN ('Untap', 'UntapAll', 'TapOrUntap')
            THEN 'UNTAP'
        WHEN port_type = 'effect' AND event_class IN ('PutCounter', 'PutCounterAll',
                                                       'Proliferate', 'MultiplyCounter')
            THEN 'COUNTER_PLACED'
        -- Costs
        WHEN port_type = 'cost' AND event_class = 'sacrifice'
            THEN 'SACRIFICE'
        WHEN port_type = 'cost' AND event_class = 'discard'
            THEN 'DISCARD'
        WHEN port_type = 'cost' AND event_class IN ('exile', 'exile_from_grave',
                                                     'exile_from_hand', 'exile_from_top', 'mill')
            THEN 'ZONECHANGE'
        WHEN port_type = 'cost' AND event_class = 'pay_life'
            THEN 'LIFE_CHANGE'
        WHEN port_type = 'cost' AND event_class IN ('tap', 'tap_type')
            THEN 'TAP'
        -- Statics / replacements / scales_with / keyword
        WHEN port_type = 'static' AND event_class = 'Continuous'
            THEN 'STATIC_BUFF'
        WHEN port_type = 'replacement'
            THEN 'STATIC_REPLACEMENT'
        WHEN port_type = 'scales_with'
            THEN 'SCALES_WITH'
        WHEN port_type = 'keyword'
            THEN 'STATIC_BUFF'
        ELSE 'UNKNOWN'
    END AS node_kind
FROM card_ports
"""


def create_port_nodes_view(conn: sqlite3.Connection) -> None:
    """Create (or replace) the ``port_nodes`` view on ``conn``.

    Safe to call repeatedly — ``CREATE VIEW IF NOT EXISTS`` is a
    no-op if the view is already current. If the view definition
    changes between releases the caller should ``DROP VIEW`` first.
    """
    conn.executescript(PORT_NODES_VIEW_SQL)


def drop_port_nodes_view(conn: sqlite3.Connection) -> None:
    """Drop the view if it exists.

    Used by the importer and migration tooling so a new view
    definition reliably replaces any previous definition.
    """
    conn.execute("DROP VIEW IF EXISTS port_nodes")
