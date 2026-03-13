"""
Deck configurations for MTG Synergy Graph.

Each deck module defines:
  - COMMANDER: str (card name)
  - EDHREC_SLUG: str (URL path segment for EDHREC API)
  - COLOR_IDENTITY: set[str] (e.g., {"G", "W"})
  - DECKLIST: list[str] (card names in the deck)
  - SYNERGY_PAIRS: list[tuple[str, str, str]] (hand-curated synergy pairs for validation)
  - SUPPLEMENT_FILTERS: list[dict] (Scryfall supplement filter definitions)

Usage:
    from decks import load_deck
    deck = load_deck("kyler")
    print(deck.COMMANDER)
"""

import importlib


def load_deck(name: str):
    """Load a deck config module by name."""
    try:
        return importlib.import_module(f"decks.{name}")
    except ModuleNotFoundError:
        available = list_decks()
        raise ValueError(f"Unknown deck '{name}'. Available: {', '.join(available)}")


def list_decks() -> list[str]:
    """List available deck names."""
    import os
    deck_dir = os.path.dirname(__file__)
    decks = []
    for f in sorted(os.listdir(deck_dir)):
        if f.endswith(".py") and f != "__init__.py":
            decks.append(f[:-3])
    return decks
