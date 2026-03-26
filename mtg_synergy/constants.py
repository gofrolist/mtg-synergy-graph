"""Shared constants for the synergy graph system.

SEMANTIC_BRIDGES: Maps (provides_tag, wants_tag) → weight for cross-concept matching.
TRIGGER_EFFECT_BRIDGES: Maps effect_tags → trigger_tags for combo detection.
STAPLE_ROLES: Card roles that should never be cut from a deck.
"""

# Action→event bridges: maps Forge-derived provides actions to wants events.
# These connect what a card DOES (provides) to what another card NEEDS (wants).
SEMANTIC_BRIDGES = {
    ("draw", "card-drawn"): 1.0,
    ("token", "token-created"): 1.0,
    ("token", "enters-battlefield"): 1.0,
    ("deal-damage", "damage-done"): 1.0,
    ("deal-damage-all", "damage-done"): 1.0,
    ("gain-life", "life-gained"): 1.0,
    ("lose-life", "life-lost"): 1.0,
    ("mill", "enters-graveyard"): 1.0,
    ("sacrifice-outlet", "sacrificed"): 1.0,
    ("sacrifice-outlet", "dies"): 1.0,
    ("destroy", "dies"): 1.0,
    ("destroy-all", "dies"): 1.0,
    ("force-sacrifice", "sacrificed"): 1.0,
    ("force-sacrifice", "dies"): 1.0,
    ("put-counter", "counter-added"): 1.0,
    ("put-counter-all", "counter-added"): 1.0,
    ("discard", "discarded"): 1.0,
    ("reanimate", "enters-battlefield"): 1.0,
    ("reanimate", "leaves-graveyard"): 1.0,
    ("goad", "attacks"): 1.0,
    ("surveil", "enters-graveyard"): 1.0,
    ("explore", "enters-graveyard"): 1.0,
    ("proliferate", "counter-added"): 1.0,
    ("force-sacrifice-all", "dies"): 1.0,
    ("force-sacrifice-all", "sacrificed"): 1.0,
    ("connive", "card-drawn"): 1.0,
    ("connive", "discarded"): 1.0,
}

# Backward-compat alias for new code
ACTION_EVENT_BRIDGES = SEMANTIC_BRIDGES


def _provides_satisfies_want(provide_tag: str, want_tag: str) -> float:
    """Check if a provides tag satisfies a wants tag. Returns weight 0.0-1.0.

    With normalized vocabulary, uses exact match + semantic bridges.
    Semantic bridges connect provides concepts to wants concepts that
    the vocabulary normalization can't capture (different word roots).
    """
    # Exact match (most common after normalization)
    if provide_tag == want_tag:
        return 1.0

    # Semantic bridges: provides → wants connections with different names
    # Each entry: (provides_tag, wants_tag) → weight
    pair = (provide_tag, want_tag)
    return SEMANTIC_BRIDGES.get(pair, 0.0)


# Maps effect tags to the trigger tags they would cause in-game.
# Used by find_combos_tiered to expand effect_tags before intersection,
# so that e.g. token (effect) can match enters-battlefield (trigger).
TRIGGER_EFFECT_BRIDGES = {
    "token": {"enters-battlefield", "token-created"},
    "destroy": {"dies"},
    "destroy-all": {"dies"},
    "sacrifice-outlet": {"dies", "sacrificed"},
    "force-sacrifice": {"dies", "sacrificed"},
    "deal-damage": {"damage-done"},
    "deal-damage-all": {"damage-done"},
    "draw": {"card-drawn"},
    "gain-life": {"life-gained"},
    "lose-life": {"life-lost"},
    "put-counter": {"counter-added"},
    "put-counter-all": {"counter-added"},
    "mill": {"enters-graveyard"},
    "discard": {"discarded"},
    "reanimate": {"enters-battlefield", "leaves-graveyard"},
    "proliferate": {"counter-added"},
}

STAPLE_ROLES = {"ramp", "draw", "removal", "protection", "land"}
