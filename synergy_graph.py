"""
MTG Synergy Graph — backward-compatible entry point.

All logic has moved to the mtg_synergy package. This module re-exports
public symbols so existing ``from synergy_graph import X`` imports work.
"""

import os

# Re-export all public symbols
from mtg_synergy.constants import (
    SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES,
    _provides_satisfies_want,
)
from mtg_synergy.graph import build_graph
from mtg_synergy.recommend import recommend_cards, suggest_swaps, show_swaps
from mtg_synergy.recommend.engine import _deck_card_scores, _candidate_scores
from mtg_synergy.recommend.swaps import _classify_card_slot
from mtg_synergy.recommend.affinity import _compute_commander_affinity
from mtg_synergy.combos import (
    find_combos, find_combos_tiered, find_partial_combos,
    compute_strategy_relevance, find_anti_synergy,
)
from mtg_synergy.combos.display import show_combos, show_combos_tiered, validate_against_curated
from mtg_synergy.analysis import (
    show_card_synergies, show_deck_synergies, show_deck_analysis,
    load_merged, build_from_commander,
)
from mtg_synergy.analysis.strategy import (
    _detect_deck_types, _filter_candidates, _find_embedding_candidates,
)
from mtg_synergy.analysis.visualization import generate_visualization
from mtg_synergy.cli import run

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

if __name__ == "__main__":
    run()
