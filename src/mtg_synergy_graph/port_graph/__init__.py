"""Typed port-graph substrate (plan 002).

Canonical vocabulary + projection + event-map tables + rules
interpreter. See
``docs/plans/2026-04-22-002-feat-typed-port-graph-substrate-plan.md``
for design, units, and migration roadmap.

Phase 1 of the plan only re-exports the vocabulary. Subsequent units
wire up ``projection``, ``event_maps``, ``rules_schema``, and
``interpreter``; they become additional re-exports here as they land.
"""

from .vocabulary import (
    GATE_OPS,
    MATCH_QUALITIES,
    NODE_KINDS,
    VOCAB_VERSION,
)

__all__ = [
    "GATE_OPS",
    "MATCH_QUALITIES",
    "NODE_KINDS",
    "VOCAB_VERSION",
]
