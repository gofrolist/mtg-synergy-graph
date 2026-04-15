"""Scoring constants and bucket definitions (SPEC §7).

The legacy hand-tuned scoring pipeline has been replaced by the
universal port-complement matcher in ``universal_scorer.py``. This
module retains only the shared constants (BUCKETS, BRANCH_MULTIPLIER)
and the ``empty_buckets()`` helper used by the active engine.
"""

from __future__ import annotations

from typing import Any

from .parser import parser_branch_kinds  # noqa: F401 — re-exported for public API

PortRow = dict[str, Any]
BucketDict = dict[str, float]

# ---------------------------------------------------------------------------
# §7.2 branch weighting
# ---------------------------------------------------------------------------

BRANCH_MULTIPLIER: dict[str, float] = {
    "root": 1.0,
    "execute": 1.0,
    "subability": 1.0,
    "true": 0.5,
    "false": 0.5,
    "win": 0.5,
    "otherwise": 0.5,
    "repeat": 1.0,
    "change_zone_table": 1.0,
    "static_condition": 0.75,
    "replacement_condition": 0.75,
}


# ---------------------------------------------------------------------------
# Bucket definitions
# ---------------------------------------------------------------------------

BUCKETS: tuple[str, ...] = (
    "port_match",
    "catchall",
    "cost_synergy",
    "sacrifice_synergy",
    "counter_synergy",
    "counter_ecosystem",
    "untap_synergy",
    "token_etb_payoff",
    "graveyard_synergy",
    "trigger_resonance",
    "stat_scaling",
    "etb_value",
    "spellcast_density",
    "opponent_forcing",
    "scaling",
    "deck_hints",
    "chain",
    "lord",
    "amplifier",
    "internal_synergy",
    "graph_metrics",
    "strategic",
    "resource_density",
    "effect_resonance",
    "replacement_resonance",
    "replacement_producer",
    "staple",
    "replacement",
)

_EMPTY_BUCKETS_TEMPLATE: BucketDict = dict.fromkeys(BUCKETS, 0.0)
_EMPTY_BUCKETS_TEMPLATE["total"] = 0.0


def empty_buckets() -> BucketDict:
    """Return a fresh zeroed bucket dict — one allocation per call."""
    return dict(_EMPTY_BUCKETS_TEMPLATE)
