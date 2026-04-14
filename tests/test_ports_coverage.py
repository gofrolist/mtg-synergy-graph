"""Additional tests for ports.py to cover CI-missed lines."""

from __future__ import annotations

from mtg_synergy_graph.ports import (
    _classify_cost_target,
    extract_all_ports,
    extract_effect_ports,
    extract_keyword_ports,
    extract_replacement_ports,
    extract_scaling_ports,
)

# ---------------------------------------------------------------------------
# _classify_cost_target edge cases (lines 104, 107, 110)
# ---------------------------------------------------------------------------


def test_classify_cost_target_no_slash_returns_none():
    """Line 104: len(parts) < 2 when there is no '/' separator."""
    assert _classify_cost_target("JustOneToken") is None


def test_classify_cost_target_empty_typespec_returns_none():
    """Line 107: typespec is empty after stripping count prefix."""
    assert _classify_cost_target("1/ ") is None
    assert _classify_cost_target("1/") is None


def test_classify_cost_target_empty_alts_returns_none():
    """Line 110: all alternatives are empty after splitting on ';'."""
    assert _classify_cost_target("1/;;") is None


# ---------------------------------------------------------------------------
# GenericChoice modal expansion (lines 324-336)
# ---------------------------------------------------------------------------


def test_generic_choice_walks_choice_svars():
    """Lines 324-336: GenericChoice expands Choices$ SVars into sub-ports."""
    parsed = {
        "_verb": "GenericChoice",
        "Choices": "MakeFood,MakeTreasure",
    }
    svars = {
        "MakeFood": "DB$ Token | TokenAmount$ 1 | TokenScript$ f_a_food",
        "MakeTreasure": "DB$ Token | TokenAmount$ 1 | TokenScript$ t_a_treasure",
    }
    ports = extract_effect_ports("Tireless Provisioner", parsed, svars)
    effect_classes = [p["event_class"] for p in ports if p["port_type"] == "effect"]
    # The GenericChoice itself plus both Token sub-effects.
    assert "GenericChoice" in effect_classes
    assert effect_classes.count("Token") == 2


def test_generic_choice_skips_missing_svars():
    """Lines 327: choice name not in svars is skipped gracefully."""
    parsed = {
        "_verb": "GenericChoice",
        "Choices": "Missing,AlsoMissing",
    }
    ports = extract_effect_ports("X", parsed, {})
    # Only the GenericChoice effect itself, no sub-ports.
    assert len([p for p in ports if p["port_type"] == "effect"]) == 1


# ---------------------------------------------------------------------------
# StaticAbilities$ SVar walking (lines 342-346)
# ---------------------------------------------------------------------------


def test_static_abilities_svar_walking():
    """Lines 342-346: StaticAbilities$ refs expand into static sub-ports."""
    parsed = {
        "_verb": "Pump",
        "StaticAbilities": "PlayAbility",
    }
    svars = {
        "PlayAbility": "Mode$ Continuous | Affected$ Card.YouOwn+nonLand | AddKeyword$ MayPlaySource",
    }
    ports = extract_effect_ports("TestCard", parsed, svars)
    static_ports = [p for p in ports if p["port_type"] == "static"]
    assert len(static_ports) == 1
    assert static_ports[0]["event_class"] == "Continuous"
    assert static_ports[0]["granted_keyword"] == "MayPlaySource"


def test_static_abilities_skips_missing_svar():
    """Line 344: ref_name not in svars is skipped."""
    parsed = {
        "_verb": "Pump",
        "StaticAbilities": "MissingSVar",
    }
    ports = extract_effect_ports("X", parsed, {})
    static_ports = [p for p in ports if p["port_type"] == "static"]
    assert len(static_ports) == 0


# ---------------------------------------------------------------------------
# extract_replacement_ports (lines 474-482)
# ---------------------------------------------------------------------------


def test_replacement_port_basic_fields():
    """Lines 474-482: replacement port captures condition, zone, replace_with."""
    parsed = {
        "Event": "Moved",
        "Prevent": "True",
        "Origin": "Graveyard",
        "Destination": "Battlefield",
        "ValidCard": "Creature",
        "ValidPlayer": "Opponent",
    }
    ports = extract_replacement_ports("Grafdigger's Cage", parsed)
    assert len(ports) == 1
    p = ports[0]
    assert p["port_type"] == "replacement"
    assert p["event_class"] == "Moved"
    assert p["replacement_result"] == "Prevent"
    assert p["zone_origin"] == "Graveyard"
    assert p["zone_destination"] == "Battlefield"
    assert p["replacement_player"] == "Opponent"
    assert p["valid_filter"] == "Creature"
    assert p["is_conditional"] is False
    assert p["branch_kind"] == "root"


def test_replacement_port_with_condition():
    """Lines 474, 494: has_condition flips is_conditional and branch_kind."""
    parsed = {
        "Event": "Counter",
        "Condition": "LandfallCondition",
        "ReplaceWith": "Nothing",
    }
    ports = extract_replacement_ports("X", parsed)
    p = ports[0]
    assert p["is_conditional"] is True
    assert p["branch_kind"] == "replacement_condition"
    assert p["replacement_result"] == "Nothing"


def test_replacement_port_with_check_svar_condition():
    """Line 474: CheckSVar also flips has_condition."""
    parsed = {"Event": "Draw", "CheckSVar": "X", "ReplaceWith": "Nothing"}
    ports = extract_replacement_ports("X", parsed)
    assert ports[0]["is_conditional"] is True


def test_replacement_port_replace_with_field():
    """Line 475: ReplaceWith takes priority; Prevent is fallback."""
    parsed = {"Event": "DamageDone", "ReplaceWith": "Counter"}
    ports = extract_replacement_ports("X", parsed)
    assert ports[0]["replacement_result"] == "Counter"


def test_replacement_port_no_replace_with_no_prevent():
    """Line 475: neither ReplaceWith nor Prevent yields empty string."""
    parsed = {"Event": "Draw"}
    ports = extract_replacement_ports("X", parsed)
    assert ports[0]["replacement_result"] == ""


# ---------------------------------------------------------------------------
# extract_keyword_ports — empty line skip (line 514)
# ---------------------------------------------------------------------------


def test_keyword_ports_skips_empty_lines():
    """Line 514: empty/whitespace lines are skipped."""
    ports = extract_keyword_ports("X", ["Flying", "", "  ", "Trample"])
    assert len(ports) == 2
    assert ports[0]["event_class"] == "Flying"
    assert ports[1]["event_class"] == "Trample"


# ---------------------------------------------------------------------------
# extract_scaling_ports — CountValid, Triggered, known metrics (lines 566-572)
# ---------------------------------------------------------------------------


def test_scaling_count_valid():
    """Line 566-567: Count$CountValid branch."""
    svars = {"NumCreatures": "Count$CountValid Creature.YouCtrl"}
    ports = extract_scaling_ports("X", svars)
    assert len(ports) == 1
    p = ports[0]
    assert p["event_class"] == "CountValid"
    assert p["valid_filter"] == "Creature.YouCtrl"


def test_scaling_triggered():
    """Line 569-570: Count$Triggered branch."""
    svars = {"X": "Count$Triggered"}
    ports = extract_scaling_ports("X", svars)
    assert len(ports) == 1
    assert ports[0]["event_class"] == "Triggered"


def test_scaling_known_metric():
    """Lines 571-572: standalone known metric like Power, Toughness."""
    svars = {"PowVal": "Count$Power"}
    ports = extract_scaling_ports("X", svars)
    assert len(ports) == 1
    assert ports[0]["event_class"] == "Power"


def test_scaling_unknown_metric_preserved():
    """Default branch: unknown expression is kept as-is metric."""
    svars = {"Weird": "Count$SomethingNew"}
    ports = extract_scaling_ports("X", svars)
    assert len(ports) == 1
    assert ports[0]["event_class"] == "SomethingNew"


# ---------------------------------------------------------------------------
# _replacement_ext wrapper (lines 608-609)
# ---------------------------------------------------------------------------


def test_replacement_ext_via_extract_all_ports():
    """Lines 608-609: replacement abilities route through _replacement_ext."""
    card = {
        "name": "TestCard",
        "svars": {},
        "abilities": [
            ("replacement", {"Event": "Moved", "ReplaceWith": "Nothing"}),
        ],
        "keywords": [],
    }
    ports = extract_all_ports(card)
    assert len(ports) == 1
    assert ports[0]["port_type"] == "replacement"
    assert ports[0]["event_class"] == "Moved"


# ---------------------------------------------------------------------------
# extract_all_ports — unknown ability_kind (line 634)
# ---------------------------------------------------------------------------


def test_extract_all_ports_skips_unknown_ability_kind():
    """Line 634: unknown ability_kind is silently skipped via continue."""
    card = {
        "name": "TestCard",
        "svars": {},
        "abilities": [
            ("unknown_type", {"_verb": "DoSomething"}),
            ("trigger", {"Mode": "Attacks"}),
        ],
        "keywords": [],
    }
    ports = extract_all_ports(card)
    # Only the trigger port from the known ability kind.
    trigger_ports = [p for p in ports if p["port_type"] == "trigger"]
    assert len(trigger_ports) == 1
    assert trigger_ports[0]["event_class"] == "Attacks"
    # No port from the unknown_type ability.
    assert not any(p.get("event_class") == "DoSomething" for p in ports)
