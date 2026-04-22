"""Canonical port-graph vocabulary (Unit 1 of plan 002).

Closed, versioned sets of identifiers that the entire substrate
depends on:

* ``NODE_KINDS`` — the canonical event-node kinds emitted by the
  ``port_nodes`` view (Unit 2).
* ``MATCH_QUALITIES`` — the enumerated predicate types for
  ``event_match_map`` rows (Unit 3). Every Python lambda in the
  legacy ``graph_engine.EVENT_MATCH_MAP`` maps to one of these.
* ``GATE_OPS`` — the leaf and combinator operators for the
  ``rules`` table's JSON predicate trees (Unit 4).
* ``VOCAB_VERSION`` — bumped on any closed-set change so
  ``compute_config_hash()`` / migration tooling can detect
  vocabulary drift.

Adding a new node kind, match quality, or gate operator is a
vocabulary change — bump ``VOCAB_VERSION`` in the same commit, run
``bench.py audit --expect-identity``, and re-pin.
"""

from __future__ import annotations

#: Version of the canonical vocabulary. Bumped when any of the
#: frozen sets below grows or shrinks. Schema migrations may key on
#: this value; ``compute_config_hash()`` folds it in so stale
#: tensor rows under an older vocabulary are refused.
VOCAB_VERSION: str = "1"

#: Canonical event-node kinds. Projection (Unit 2) maps every
#: ``card_ports`` row to exactly one of these (or to ``"UNKNOWN"``).
#: Order is not meaningful; the set is what matters.
NODE_KINDS: frozenset[str] = frozenset(
    {
        "ETB",
        "LTB",
        "DIES",
        "CAST",
        "RESOLVE",
        "ATTACK",
        "BLOCK",
        "PAYMANA",
        "TAP",
        "UNTAP",
        "COUNTER_PLACED",
        "COUNTER_REMOVED",
        "SACRIFICE",
        "DISCARD",
        "DRAW",
        "DAMAGE",
        "LIFE_CHANGE",
        "ZONECHANGE",
        "STATIC_BUFF",
        "STATIC_REPLACEMENT",
        "SCALES_WITH",
    }
)

#: Enumerated predicate classifications for ``event_match_map`` rows.
#: Each row's ``match_quality`` column is exactly one of these; the
#: interpreter dispatches on the value to a small set of compiled
#: predicates. The five values empirically cover every Python lambda
#: in the legacy ``graph_engine.EVENT_MATCH_MAP`` at the time of
#: writing; adding a new predicate requires a new entry here + a
#: matching interpreter case + a ``VOCAB_VERSION`` bump.
MATCH_QUALITIES: frozenset[str] = frozenset(
    {
        "always",
        "zone_compatible",
        "counter_compatible",
        "tribe_match",
        "color_match",
    }
)

#: Leaf and combinator operators for rule JSON predicate trees. Used
#: by ``rules_schema.validate_gate_predicate`` (Unit 4) and compiled
#: by the interpreter (Unit 5).
#:
#: Combinators:
#:   ``and`` / ``or`` / ``not`` — take ``args`` (list for and/or,
#:   single for not).
#:
#: Leaf ops (each takes its own keyword arguments):
#:   ``has_port`` — match a port by ``port_type`` and/or
#:     ``event_class``.
#:   ``filter_tag`` — match when the port's ``valid_filter``
#:     contains ``tag`` (e.g. ``YouCtrl``).
#:   ``zone_eq`` — match when ``zone_origin`` or
#:     ``zone_destination`` equals a given value.
#:   ``counter_type`` — match when the port's counter type equals
#:     a given value (e.g. ``P1P1``).
#:   ``tribe`` — match on a subtype token extracted from the port's
#:     valid_filter.
#:   ``color`` — match on a color token extracted from the port's
#:     valid_filter.
#:   ``not_in_commander_set`` — candidate-side predicate excluding
#:     the commander(s) themselves from the candidate pool.
GATE_OPS: frozenset[str] = frozenset(
    {
        "and",
        "or",
        "not",
        "has_port",
        "filter_tag",
        "zone_eq",
        "counter_type",
        "tribe",
        "color",
        "not_in_commander_set",
    }
)
