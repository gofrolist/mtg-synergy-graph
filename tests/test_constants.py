def test_semantic_bridges_loaded():
    from mtg_synergy.constants import SEMANTIC_BRIDGES
    assert isinstance(SEMANTIC_BRIDGES, dict)
    assert len(SEMANTIC_BRIDGES) > 50
    assert ("counter-placement", "counter-placement-events") in SEMANTIC_BRIDGES
    assert ("token-generation", "creature-etb") in SEMANTIC_BRIDGES

def test_trigger_effect_bridges_loaded():
    from mtg_synergy.constants import TRIGGER_EFFECT_BRIDGES
    assert isinstance(TRIGGER_EFFECT_BRIDGES, dict)
    assert "token-generation" in TRIGGER_EFFECT_BRIDGES

def test_staple_roles_loaded():
    from mtg_synergy.constants import STAPLE_ROLES
    assert "ramp" in STAPLE_ROLES
    assert "draw" in STAPLE_ROLES

def test_provides_satisfies_want():
    from mtg_synergy.constants import _provides_satisfies_want
    assert _provides_satisfies_want("card-draw", "card-draw") == 1.0
    assert _provides_satisfies_want("token-generation", "creature-etb") > 0
    assert _provides_satisfies_want("xyz", "abc") == 0.0
