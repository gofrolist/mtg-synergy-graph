from mtg_synergy.constants import SEMANTIC_BRIDGES

def test_damage_token_bridge():
    """damage-dealing should connect to token-events for Impact Tremors pattern."""
    assert ("damage-dealing", "token-events") in SEMANTIC_BRIDGES

def test_sacrifice_graveyard_bridge():
    """sacrifice-outlet should connect to graveyard-filling."""
    assert ("sacrifice-outlet", "graveyard-filling") in SEMANTIC_BRIDGES

def test_untap_tap_combo_bridge():
    """untap should connect to tap-combo for commanders with tap abilities."""
    assert ("untap", "tap-combo") in SEMANTIC_BRIDGES

def test_mill_graveyard_bridge():
    """mill should connect to graveyard-filling."""
    assert ("mill", "graveyard-filling") in SEMANTIC_BRIDGES

def test_board_wide_pump_attack():
    """board-wide-pump should connect to attack-events."""
    assert ("board-wide-pump", "attack-events") in SEMANTIC_BRIDGES

def test_bridge_weights_in_range():
    """All bridge weights must be between 0 and 1."""
    for (p, w), weight in SEMANTIC_BRIDGES.items():
        assert 0 < weight <= 1.0, f"Bridge ({p}, {w}) has invalid weight {weight}"
