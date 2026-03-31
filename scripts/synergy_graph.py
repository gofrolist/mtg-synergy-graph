"""
MTG Synergy Graph — backward-compatible entry point.

All logic has moved to the mtg_synergy package. This module re-exports
public symbols so existing ``from synergy_graph import X`` imports work.
"""

import os

# Re-export public symbols
from mtg_synergy.constants import STAPLE_ROLES
from mtg_synergy.recommend import recommend_cards
from mtg_synergy.combos import (
    find_combos_tiered, find_partial_combos,
    compute_strategy_relevance, find_anti_synergy,
)
from mtg_synergy.combos.display import show_combos_tiered
from mtg_synergy.analysis.strategy import _detect_deck_types
from mtg_synergy.cli import run


if __name__ == "__main__":
    run()
