"""Additional coverage tests for heuristics.py — targets the 27 missed lines."""

from __future__ import annotations

from mtg_synergy_graph.heuristics import (
    _card_keywords,
    _cmdr_wants_combat_damage,
    _evasion_boost,
    _has_effect,
    _has_effect_in,
    _lki_scaling_boost,
    _mana_sink_boost,
    _mass_pump_boost,
    _normalise_colours,
    _proliferate_boost,
    _selfmill_boost,
    _token_boost,
    _tribal_density_boost,
    active_rules_for_commander,
    evaluate_strategic_rules,
    staple_bonus,
)

# ---------------------------------------------------------------------------
# _card_keywords  (lines 40-46)
# ---------------------------------------------------------------------------


def test_card_keywords_from_list():
    """When keywords is already a list, return it directly."""
    assert _card_keywords({"keywords": ["Flying", "Menace"]}) == ["Flying", "Menace"]


def test_card_keywords_from_json_string():
    """When keywords is a JSON string, parse it."""
    assert _card_keywords({"keywords": '["Flying", "Trample"]'}) == ["Flying", "Trample"]


def test_card_keywords_from_none():
    """When keywords is None, return empty list."""
    assert _card_keywords({"keywords": None}) == []


def test_card_keywords_missing():
    """When keywords key is absent, return empty list."""
    assert _card_keywords({}) == []


def test_card_keywords_invalid_json():
    """When keywords is a non-JSON string, return empty list (except branch)."""
    assert _card_keywords({"keywords": "not-json"}) == []


# ---------------------------------------------------------------------------
# _evasion_boost  (lines 59-67)
# ---------------------------------------------------------------------------


def test_evasion_boost_low_cmc_with_evasion():
    cand = {"card": {"cmc": 2, "keywords": '["Flying"]'}, "ports": []}
    assert _evasion_boost(cand) is True


def test_evasion_boost_high_cmc_rejected():
    cand = {"card": {"cmc": 5, "keywords": '["Flying"]'}, "ports": []}
    assert _evasion_boost(cand) is False


def test_evasion_boost_low_cmc_no_evasion():
    cand = {"card": {"cmc": 1, "keywords": '["Trample"]'}, "ports": []}
    assert _evasion_boost(cand) is False


def test_evasion_boost_cmc_none():
    """cmc is None => treated as 0."""
    cand = {"card": {"cmc": None, "keywords": '["Menace"]'}, "ports": []}
    assert _evasion_boost(cand) is True


def test_evasion_boost_cmc_invalid_string():
    """cmc is a non-numeric string => treated as 0 via except branch."""
    cand = {"card": {"cmc": "abc", "keywords": '["Shadow"]'}, "ports": []}
    assert _evasion_boost(cand) is True


def test_evasion_boost_keywords_as_list():
    """Keywords already a list (exercises _card_keywords list branch)."""
    cand = {"card": {"cmc": 1, "keywords": ["Skulk"]}, "ports": []}
    assert _evasion_boost(cand) is True


# ---------------------------------------------------------------------------
# _mass_pump_boost  (line 71)
# ---------------------------------------------------------------------------


def test_mass_pump_boost_true():
    cand = {
        "card": {},
        "ports": [{"port_type": "static", "event_class": "Continuous", "affected_scope": "Creature you control"}],
    }
    assert _mass_pump_boost(cand) is True


def test_mass_pump_boost_false():
    cand = {"card": {}, "ports": [{"port_type": "effect", "event_class": "Continuous"}]}
    assert _mass_pump_boost(cand) is False


# ---------------------------------------------------------------------------
# _selfmill_boost  (line 80)
# ---------------------------------------------------------------------------


def test_selfmill_boost_mill():
    cand = {
        "card": {},
        "ports": [{"port_type": "effect", "event_class": "Mill", "valid_filter": "You"}],
    }
    assert _selfmill_boost(cand) is True


def test_selfmill_boost_discard():
    cand = {
        "card": {},
        "ports": [{"port_type": "effect", "event_class": "Discard", "valid_filter": "You"}],
    }
    assert _selfmill_boost(cand) is True


def test_selfmill_boost_opponent_mill():
    cand = {
        "card": {},
        "ports": [{"port_type": "effect", "event_class": "Mill", "valid_filter": "Opponent"}],
    }
    assert _selfmill_boost(cand) is False


# ---------------------------------------------------------------------------
# _mana_sink_boost  (lines 108-115)
# ---------------------------------------------------------------------------


def test_mana_sink_boost_scales_with_x():
    cand = {
        "card": {},
        "ports": [{"port_type": "scales_with", "scaling_expression": "X mana"}],
    }
    assert _mana_sink_boost(cand) is True


def test_mana_sink_boost_cost_dollar_x():
    cand = {
        "card": {},
        "ports": [{"port_type": "effect", "raw_line": "AB$ Something | Cost$ X G"}],
    }
    assert _mana_sink_boost(cand) is True


def test_mana_sink_boost_cost_dollar_x_no_space():
    cand = {
        "card": {},
        "ports": [{"port_type": "effect", "raw_line": "AB$ Something | Cost$X"}],
    }
    assert _mana_sink_boost(cand) is True


def test_mana_sink_boost_no_match():
    cand = {
        "card": {},
        "ports": [{"port_type": "effect", "raw_line": "AB$ Something | Cost$ 2 G"}],
    }
    assert _mana_sink_boost(cand) is False


def test_mana_sink_boost_empty_ports():
    cand = {"card": {}, "ports": []}
    assert _mana_sink_boost(cand) is False


# ---------------------------------------------------------------------------
# _tribal_density_boost  (line 125)
# ---------------------------------------------------------------------------


def test_tribal_density_boost_true():
    cand = {
        "card": {},
        "ports": [{"port_type": "static", "raw_line": "ValidCard$ Creature | AddKeyword$ Haste"}],
    }
    assert _tribal_density_boost(cand) is True


def test_tribal_density_boost_false():
    cand = {
        "card": {},
        "ports": [{"port_type": "static", "raw_line": "Mode$ Continuous"}],
    }
    assert _tribal_density_boost(cand) is False


# ---------------------------------------------------------------------------
# _normalise_colours  (line 345)
# ---------------------------------------------------------------------------


def test_normalise_colours_none():
    assert _normalise_colours(None) == frozenset({"C"})


def test_normalise_colours_empty_set():
    assert _normalise_colours(set()) == frozenset({"C"})


def test_normalise_colours_filters_empty_strings():
    """Colours with empty-string elements are filtered out."""
    result = _normalise_colours({"W", ""})
    assert "" not in result
    assert "W" in result


def test_normalise_colours_normal():
    result = _normalise_colours({"W", "U"})
    assert result == frozenset({"W", "U"})


# ---------------------------------------------------------------------------
# staple_bonus with None colours (exercises _normalise_colours path)
# ---------------------------------------------------------------------------


def test_staple_bonus_with_none_colours():
    """None colours → colourless bucket only, Sol Ring should still match."""
    assert staple_bonus("Sol Ring", None) > 0


# ---------------------------------------------------------------------------
# Helpers coverage (_has_effect, _has_effect_in, _cmdr_wants_combat_damage)
# ---------------------------------------------------------------------------


def test_has_effect_true():
    ports = [{"port_type": "effect", "event_class": "Token"}]
    assert _has_effect(ports, "Token") is True


def test_has_effect_false():
    ports = [{"port_type": "trigger", "event_class": "Token"}]
    assert _has_effect(ports, "Token") is False


def test_has_effect_in_true():
    ports = [{"port_type": "effect", "event_class": "Tap"}]
    assert _has_effect_in(ports, frozenset({"Tap", "Untap"})) is True


def test_has_effect_in_false():
    ports = [{"port_type": "effect", "event_class": "Token"}]
    assert _has_effect_in(ports, frozenset({"Tap", "Untap"})) is False


def test_cmdr_wants_combat_damage_true():
    ports = [{"port_type": "trigger", "event_class": "DamageDone", "valid_filter": "Player"}]
    assert _cmdr_wants_combat_damage(ports) is True


def test_cmdr_wants_combat_damage_false():
    ports = [{"port_type": "trigger", "event_class": "DamageDone", "valid_filter": "Creature"}]
    assert _cmdr_wants_combat_damage(ports) is False


# ---------------------------------------------------------------------------
# Strategic rule conditions via active_rules_for_commander
# ---------------------------------------------------------------------------


def test_evasion_rule_activates_for_combat_damage_commander():
    cmdr_ports = [{"port_type": "trigger", "event_class": "DamageDone", "valid_filter": "Player"}]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "evasion_for_combat_damage" in names


def test_mass_pump_rule_activates_for_token_commander():
    cmdr_ports = [{"port_type": "effect", "event_class": "Token"}]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "mass_pump_for_tokens" in names


def test_selfmill_rule_activates_for_graveyard_commander():
    cmdr_ports = [
        {"port_type": "trigger", "event_class": "ChangesZone", "zone_destination": "Graveyard"},
    ]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "selfmill_for_graveyard" in names


def test_tokens_for_sacrifice_activates():
    cmdr_ports = [{"port_type": "trigger", "event_class": "Sacrificed"}]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "tokens_for_sacrifice" in names


def test_counters_for_proliferate_activates():
    cmdr_ports = [{"port_type": "effect", "event_class": "Proliferate"}]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "counters_for_proliferate" in names


def test_mana_sink_rule_activates_for_mana_commander():
    cmdr_ports = [{"port_type": "effect", "event_class": "Mana"}]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "mana_sink" in names


def test_mana_sink_rule_activates_for_treasure_commander():
    cmdr_ports = [{"port_type": "effect", "event_class": "Treasure"}]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "mana_sink" in names


def test_tribal_density_rule_activates():
    cmdr_ports = [{"port_type": "trigger", "valid_filter": "Creature you control"}]
    names = {r["name"] for r in active_rules_for_commander(cmdr_ports)}
    assert "tribal_density" in names


# ---------------------------------------------------------------------------
# counters_for_proliferate boost lambda
# ---------------------------------------------------------------------------


def test_counters_for_proliferate_boost_fires():
    cmdr_ports = [{"port_type": "effect", "event_class": "Proliferate"}]
    active = active_rules_for_commander(cmdr_ports)
    rule = next(r for r in active if r["name"] == "counters_for_proliferate")
    cand = {"card": {}, "ports": [{"port_type": "effect", "event_class": "PutCounter"}]}
    assert rule["boost"](cand) is True


def test_counters_for_proliferate_boost_no_fire():
    cmdr_ports = [{"port_type": "effect", "event_class": "Proliferate"}]
    active = active_rules_for_commander(cmdr_ports)
    rule = next(r for r in active if r["name"] == "counters_for_proliferate")
    cand = {"card": {}, "ports": [{"port_type": "effect", "event_class": "Token"}]}
    assert rule["boost"](cand) is False


# ---------------------------------------------------------------------------
# _lki_scaling_boost and _proliferate_boost and _token_boost
# ---------------------------------------------------------------------------


def test_lki_scaling_boost_true():
    cand = {"card": {}, "ports": [{"port_type": "scales_with", "scaling_expression": "LKI power"}]}
    assert _lki_scaling_boost(cand) is True


def test_lki_scaling_boost_false():
    cand = {"card": {}, "ports": [{"port_type": "scales_with", "scaling_expression": "CMC"}]}
    assert _lki_scaling_boost(cand) is False


def test_proliferate_boost_true():
    cand = {"card": {}, "ports": [{"port_type": "effect", "event_class": "Proliferate"}]}
    assert _proliferate_boost(cand) is True


def test_token_boost_true():
    cand = {"card": {}, "ports": [{"port_type": "effect", "event_class": "Token"}]}
    assert _token_boost(cand) is True


# ---------------------------------------------------------------------------
# Full rule evaluation for evasion path
# ---------------------------------------------------------------------------


def test_evaluate_evasion_rule_fires():
    cmdr_ports = [{"port_type": "trigger", "event_class": "DamageDone", "valid_filter": "Player"}]
    active = active_rules_for_commander(cmdr_ports)
    cand = {"card": {"cmc": 1, "keywords": '["Flying"]'}, "ports": []}
    weight, reasons = evaluate_strategic_rules(active, cand)
    assert weight >= 5
    assert any("evasion" in r.lower() for r in reasons)


def test_evaluate_selfmill_rule_fires():
    cmdr_ports = [
        {"port_type": "trigger", "event_class": "ChangesZone", "zone_destination": "Graveyard"},
    ]
    active = active_rules_for_commander(cmdr_ports)
    cand = {
        "card": {},
        "ports": [{"port_type": "effect", "event_class": "Mill", "valid_filter": "You"}],
    }
    weight, reasons = evaluate_strategic_rules(active, cand)
    assert weight >= 4
    assert any("graveyard" in r.lower() for r in reasons)
