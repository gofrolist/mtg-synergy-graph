from mtg_synergy.constants import SEMANTIC_BRIDGES

def test_action_event_bridge_draw():
    """draw should connect to card-drawn."""
    assert ("draw", "card-drawn") in SEMANTIC_BRIDGES

def test_action_event_bridge_sacrifice():
    """sacrifice-outlet should connect to dies and sacrificed."""
    assert ("sacrifice-outlet", "dies") in SEMANTIC_BRIDGES
    assert ("sacrifice-outlet", "sacrificed") in SEMANTIC_BRIDGES

def test_action_event_bridge_destroy():
    """destroy should connect to dies."""
    assert ("destroy", "dies") in SEMANTIC_BRIDGES

def test_action_event_bridge_mill():
    """mill should connect to enters-graveyard."""
    assert ("mill", "enters-graveyard") in SEMANTIC_BRIDGES

def test_action_event_bridge_token():
    """token should connect to token-created and enters-battlefield."""
    assert ("token", "token-created") in SEMANTIC_BRIDGES
    assert ("token", "enters-battlefield") in SEMANTIC_BRIDGES

def test_bridge_weights_in_range():
    """All bridge weights must be between 0 and 1."""
    for (p, w), weight in SEMANTIC_BRIDGES.items():
        assert 0 < weight <= 1.0, f"Bridge ({p}, {w}) has invalid weight {weight}"
