"""
MTG Synergy Graph — backward-compatible entry point.

All logic has moved to the mtg_synergy package. This module re-exports
public symbols so existing ``from synergy_graph import X`` imports work.
"""

# Re-export public symbols
from mtg_synergy.recommend import recommend_cards
from mtg_synergy.cli import run


if __name__ == "__main__":
    run()
