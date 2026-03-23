"""Deck analysis, strategy detection, and visualization."""
from mtg_synergy.analysis.deck import (
    show_card_synergies, show_deck_synergies, show_deck_analysis, load_merged,
)
from mtg_synergy.analysis.strategy import (
    _detect_deck_types, _filter_candidates, _find_embedding_candidates,
    build_from_commander,
)

__all__ = [
    "show_card_synergies", "show_deck_synergies", "show_deck_analysis",
    "load_merged", "build_from_commander",
]
