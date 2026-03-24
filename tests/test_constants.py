REMOVED_PARENT_TAGS = {
    "creature-pump", "creature-board", "creature-etb",
    "combat-events", "token-generation", "evasion-grant",
}

def test_semantic_bridges_loaded():
    from mtg_synergy.constants import SEMANTIC_BRIDGES
    assert isinstance(SEMANTIC_BRIDGES, dict)
    assert len(SEMANTIC_BRIDGES) > 50
    assert ("counter-placement", "counter-placement-events") in SEMANTIC_BRIDGES

def test_trigger_effect_bridges_loaded():
    from mtg_synergy.constants import TRIGGER_EFFECT_BRIDGES
    assert isinstance(TRIGGER_EFFECT_BRIDGES, dict)
    assert "tokens-creature" in TRIGGER_EFFECT_BRIDGES

def test_staple_roles_loaded():
    from mtg_synergy.constants import STAPLE_ROLES
    assert "ramp" in STAPLE_ROLES
    assert "draw" in STAPLE_ROLES

def test_provides_satisfies_want():
    from mtg_synergy.constants import _provides_satisfies_want
    assert _provides_satisfies_want("card-draw", "card-draw") == 1.0
    assert _provides_satisfies_want("tokens-creature", "etb-value") > 0
    assert _provides_satisfies_want("xyz", "abc") == 0.0

def test_no_parent_tags_in_bridges():
    from mtg_synergy.constants import SEMANTIC_BRIDGES
    for (p, w), weight in SEMANTIC_BRIDGES.items():
        assert p not in REMOVED_PARENT_TAGS, f"Provides '{p}' is a removed parent tag in bridge ({p}, {w})"
        assert w not in REMOVED_PARENT_TAGS, f"Wants '{w}' is a removed parent tag in bridge ({p}, {w})"

def test_key_subtag_bridges_exist():
    from mtg_synergy.constants import SEMANTIC_BRIDGES
    assert ("tokens-creature", "etb-value") in SEMANTIC_BRIDGES
    assert ("tokens-tribal", "etb-tribal") in SEMANTIC_BRIDGES
    assert ("pump-lord", "board-tribal") in SEMANTIC_BRIDGES
    assert ("pump-anthem", "board-go-wide") in SEMANTIC_BRIDGES
    assert ("tokens-creature", "board-tokens") in SEMANTIC_BRIDGES
    assert ("combat-enabler", "combat-attack") in SEMANTIC_BRIDGES

def test_no_parent_tags_in_trigger_bridges():
    from mtg_synergy.constants import TRIGGER_EFFECT_BRIDGES
    for key, vals in TRIGGER_EFFECT_BRIDGES.items():
        assert key not in REMOVED_PARENT_TAGS, f"Key '{key}' is a removed parent tag"
        for v in vals:
            assert v not in REMOVED_PARENT_TAGS, f"Value '{v}' is a removed parent tag in {key}'s set"
