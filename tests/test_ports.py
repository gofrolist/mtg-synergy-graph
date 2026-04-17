"""Tests for port extractors against the 5 reference cards (SPEC §5.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import (
    extract_all_ports,
    extract_cost_ports,
    extract_effect_ports,
    extract_trigger_ports,
    parse_card_file,
)


def _by_type(ports, port_type):
    return [p for p in ports if p["port_type"] == port_type]


# ---------------------------------------------------------------------------
# Cathars' Crusade — ETB trigger → PutCounter chain
# ---------------------------------------------------------------------------


def test_cathars_crusade_trigger_and_chained_effect(cathars_crusade):
    ports = extract_all_ports(cathars_crusade)
    triggers = _by_type(ports, "trigger")
    effects = _by_type(ports, "effect")

    assert len(triggers) == 1
    t = triggers[0]
    assert t["event_class"] == "ChangesZone"
    assert t["zone_destination"] == "Battlefield"
    assert t["valid_filter"] == "Creature.YouCtrl"
    assert t["execute_ref"] == "CatharsCounters"

    # The Execute$ chain should produce one effect: PutCounterAll P1P1.
    effect_classes = {e["event_class"] for e in effects}
    assert "PutCounterAll" in effect_classes
    pc = next(e for e in effects if e["event_class"] == "PutCounterAll")
    assert pc["counter_type"] == "P1P1"
    assert pc["branch_kind"] == "execute"
    assert pc["source_svar"] == "CatharsCounters"
    assert pc["is_conditional"] is False


# ---------------------------------------------------------------------------
# Korvold — sacrifice trigger → put counter → draw chain
# ---------------------------------------------------------------------------


def test_korvold_sacrifice_chain_emits_putcounter_and_draw(korvold):
    ports = extract_all_ports(korvold)
    effects = _by_type(ports, "effect")
    classes = {e["event_class"] for e in effects}

    assert "PutCounter" in classes
    assert "Draw" in classes
    assert "Sacrifice" in classes

    # Draw should be reached via SubAbility$ from TrigPutCounter
    draw = next(e for e in effects if e["event_class"] == "Draw")
    assert draw["branch_kind"] == "subability"
    assert draw["branch_parent"] == "TrigPutCounter"
    assert draw["source_svar"] == "DBDraw"
    assert draw["chain_depth"] == 2  # trigger → TrigPutCounter (1) → DBDraw (2)


def test_korvold_has_three_triggers_and_flying_keyword(korvold):
    ports = extract_all_ports(korvold)
    triggers = _by_type(ports, "trigger")
    keywords = _by_type(ports, "keyword")

    # 3 triggers per the .txt file (ETB+attack-primary, attack-secondary, sacrifice)
    assert len(triggers) == 3
    assert any(p["event_class"] == "Sacrificed" for p in triggers)
    assert any(p["event_class"] == "Attacks" for p in triggers)
    assert any(k["event_class"] == "Flying" for k in keywords)


# ---------------------------------------------------------------------------
# Panharmonicon — single Static$Panharmonicon port
# ---------------------------------------------------------------------------


def test_panharmonicon_static_port(panharmonicon):
    ports = extract_all_ports(panharmonicon)
    statics = _by_type(ports, "static")
    assert len(statics) == 1
    s = statics[0]
    assert s["event_class"] == "Panharmonicon"
    assert s["is_conditional"] is False
    assert s["branch_kind"] == "root"


# ---------------------------------------------------------------------------
# Rhystic Study — SpellCast trigger + Draw effect
# ---------------------------------------------------------------------------


def test_rhystic_study_spellcast_to_draw(rhystic_study):
    ports = extract_all_ports(rhystic_study)
    triggers = _by_type(ports, "trigger")
    effects = _by_type(ports, "effect")

    assert len(triggers) == 1
    assert triggers[0]["event_class"] == "SpellCast"

    assert any(e["event_class"] == "Draw" for e in effects)
    draw = next(e for e in effects if e["event_class"] == "Draw")
    assert draw["amount"] == "1"
    assert draw["branch_kind"] == "execute"


# ---------------------------------------------------------------------------
# Scute Swarm — branching landfall trigger
# ---------------------------------------------------------------------------


def test_scute_swarm_branch_emits_conditional_token_and_copy(scute_swarm):
    ports = extract_all_ports(scute_swarm)
    effects = _by_type(ports, "effect")
    classes = {e["event_class"] for e in effects}

    # Branch parent appears as an effect; both children must be present.
    assert "Branch" in classes
    assert "Token" in classes
    assert "CopyPermanent" in classes

    token = next(e for e in effects if e["event_class"] == "Token")
    copy = next(e for e in effects if e["event_class"] == "CopyPermanent")

    # DBToken is FalseSubAbility — flagged conditional, branch_kind="false".
    assert token["is_conditional"] is True
    assert token["branch_kind"] == "false"
    assert token["branch_parent"] == "TrigBranch"

    # DBCopy is TrueSubAbility — flagged conditional, branch_kind="true".
    assert copy["is_conditional"] is True
    assert copy["branch_kind"] == "true"
    assert copy["branch_parent"] == "TrigBranch"


def test_scute_swarm_scaling_port_for_landfall_x(scute_swarm):
    ports = extract_all_ports(scute_swarm)
    scaling = [p for p in ports if p["port_type"] == "scales_with"]
    assert len(scaling) == 1
    s = scaling[0]
    assert s["event_class"] == "Valid"
    assert s["valid_filter"] == "Land.YouCtrl"
    assert s["source_svar"] == "X"


# ---------------------------------------------------------------------------
# Phase A1 — sacrifice / discard / exile / return cost target classification
# ---------------------------------------------------------------------------


def _sac_port(cost_str: str) -> dict:
    ports = extract_cost_ports("X", cost_str)
    sac = [p for p in ports if p["event_class"] == "sacrifice"]
    assert len(sac) == 1, f"expected exactly one sacrifice port for {cost_str!r}"
    return sac[0]


def test_cost_target_self_when_typespec_is_cardname():
    # Suspend-style "Sacrifice CARDNAME" — only the source qualifies.
    p = _sac_port("Sac<1/CARDNAME>")
    assert p["cost_target"] == "self"


def test_cost_target_other_when_typespec_carries_dot_other():
    # Korvold-class outlet that explicitly excludes the source.
    p = _sac_port("Sac<1/Creature.Other>")
    assert p["cost_target"] == "other"


def test_cost_target_any_for_viscera_seer_pattern():
    # ``Sac<1/Creature>`` — Viscera Seer / Goblin Bombardment. The source
    # MAY pick itself, so it's a generic outlet (`any`), not strictly other.
    p = _sac_port("Sac<1/Creature>")
    assert p["cost_target"] == "any"


def test_cost_target_any_for_greater_gargadon_multi_typespec():
    # Multiple alternatives separated by ``;`` plus a description suffix.
    p = _sac_port("Sac<1/Artifact;Creature;Land/artifact, creature or land>")
    assert p["cost_target"] == "any"


def test_cost_target_self_for_bare_sacrifice_keyword():
    # Plain ``Sacrifice`` with no bracket — keyword form, e.g. inside a K:
    # line that has been promoted to a cost. Defaults to ``self`` per the
    # implicit "sacrifice CARDNAME" rule.
    p = _sac_port("Sacrifice")
    assert p["cost_target"] == "self"


def test_cost_target_propagates_to_discard_exile_return():
    cost_str = "Discard<1/Card.Other> Return<1/Creature.YouCtrl+inGY> ExileFromGrave<1/CARDNAME>"
    ports = extract_cost_ports("X", cost_str)
    by_class = {p["event_class"]: p for p in ports}
    assert by_class["discard"]["cost_target"] == "other"
    # ``Creature.YouCtrl`` has no ``.Other`` and isn't CARDNAME → any.
    assert by_class["return"]["cost_target"] == "any"
    assert by_class["exile_from_grave"]["cost_target"] == "self"


def test_cost_target_none_for_non_targeted_costs():
    # PayLife / tap have no permanent picker — cost_target stays unset.
    paylife = extract_cost_ports("X", "PayLife<2>")
    assert paylife and paylife[0]["cost_target"] is None
    tap = extract_cost_ports("X", "T")
    assert tap and tap[0]["cost_target"] is None


def test_korvold_sacrifice_trigger_cost_target_recorded(korvold):
    # Korvold's activated sac outlet is ``Sacrificed`` trigger, not a cost
    # itself; just verify any cost ports we *do* extract carry the new field
    # so downstream matchers can rely on it being present (even if None).
    ports = extract_all_ports(korvold)
    cost_ports = [p for p in ports if p["port_type"] == "cost"]
    for p in cost_ports:
        assert "cost_target" in p


# ---------------------------------------------------------------------------
# Phase A2 — trigger metadata: ValidTarget$ + FirstTime$
# ---------------------------------------------------------------------------


def test_becomes_target_first_time_records_trigger_source():
    # Real Forge trigger from valiant_rescuer.txt. FirstTime$ True must
    # surface as ``trigger_source='first_time'``. The ValidTarget$ pivot
    # is deferred to ship with D3 — see extract_trigger_ports docstring.
    parsed = {
        "Mode": "BecomesTarget",
        "ValidTarget": "Card.Self",
        "ValidSource": "SpellAbility.YouCtrl",
        "TriggerZones": "Battlefield",
        "FirstTime": "True",
        "Execute": "TrigToken",
    }
    ports = extract_trigger_ports("Valiant Rescuer", parsed, {})
    trig = ports[0]
    assert trig["trigger_source"] == "first_time"


def test_trigger_first_time_flag_recorded():
    parsed = {"Mode": "DiscardedAll", "ValidPlayer": "You", "FirstTime": "True"}
    ports = extract_trigger_ports("X", parsed, {})
    assert ports[0]["trigger_source"] == "first_time"


def test_trigger_no_first_time_leaves_source_none():
    parsed = {"Mode": "ChangesZone", "ValidCard": "Creature.YouCtrl"}
    ports = extract_trigger_ports("X", parsed, {})
    assert ports[0]["trigger_source"] is None
    assert ports[0]["valid_filter"] == "Creature.YouCtrl"


# ---------------------------------------------------------------------------
# Phase A3 — DB$ Mana RestrictValid$
# ---------------------------------------------------------------------------


def test_mana_effect_captures_restrict_valid():
    # Real Nexos card: AB$ Mana | Cost$ T | Produced$ C | RestrictValid$ CostContainsX
    parsed = {
        "_verb": "Mana",
        "Cost": "T",
        "Produced": "C",
        "Amount": "2",
        "RestrictValid": "CostContainsX",
    }
    ports = extract_effect_ports("Nexos", parsed, {})
    effect = next(p for p in ports if p["port_type"] == "effect")
    assert effect["mana_restriction"] == "CostContainsX"


def test_non_mana_effect_has_empty_restriction():
    parsed = {"_verb": "Draw", "NumCards": "1"}
    ports = extract_effect_ports("X", parsed, {})
    assert ports[0]["mana_restriction"] == ""


# ---------------------------------------------------------------------------
# Phase A4 — additional cost types from corpus inventory
# ---------------------------------------------------------------------------


def test_cost_pattern_draw():
    p = extract_cost_ports("X", "Draw<1>")
    assert any(c["event_class"] == "draw_cost" for c in p)


def test_cost_pattern_damage_you():
    p = extract_cost_ports("X", "DamageYou<2>")
    assert any(c["event_class"] == "damage_self" for c in p)


def test_cost_pattern_collect_evidence():
    p = extract_cost_ports("X", "CollectEvidence<6>")
    assert any(c["event_class"] == "collect_evidence" for c in p)


def test_scute_swarm_emits_combo_primitive_for_branch_verb(scute_swarm):
    # Phase B3: any card whose effect verb is in COMBO_PRIMITIVE_VERBS
    # also gets a synthetic ``event_class='combo_primitive'`` port. Scute
    # Swarm uses ``DB$ Branch`` for its landfall true/false split.
    ports = extract_all_ports(scute_swarm)
    primitives = [p for p in ports if p["event_class"] == "combo_primitive"]
    assert len(primitives) == 1
    p = primitives[0]
    assert p["port_type"] == "effect"
    assert p["granted_ability"] == "Branch"


def test_combo_primitive_only_emitted_for_known_verbs():
    # Sanity: a non-combo verb doesn't get the synthetic port.
    ports = extract_effect_ports(
        "X",
        {"_verb": "Draw", "NumCards": "1"},
        {},
    )
    assert not any(p["event_class"] == "combo_primitive" for p in ports)


def test_cost_pattern_exile_any_grave_does_not_collide_with_exile_from_grave():
    # ``ExileAnyGrave`` is the delve-class "from any graveyard" cost.
    # ``ExileFromGrave`` is the older "from your graveyard" cost. Both
    # must be detected independently — neither is a substring of the other.
    p = extract_cost_ports("X", "ExileAnyGrave<1/Creature>")
    classes = {c["event_class"] for c in p}
    assert "exile_any_grave" in classes
    assert "exile_from_grave" not in classes


# ---------------------------------------------------------------------------
# Exponential SubAbility re-walk regression tests (A1)
# ---------------------------------------------------------------------------

FORGE_CARDSFOLDER = Path(__file__).parent.parent / "data" / "forge" / "forge-gui" / "res" / "cardsfolder"

#: These regression tests assert on actual Forge DSL files. Tagged
#: `integration` so CI (which runs `-m "not integration"`) skips them
#: at collection time; skipif is a fallback for local `-m integration`
#: runs without the ~80 MB cardsfolder cloned.
_integration = pytest.mark.integration
_requires_cardsfolder = pytest.mark.skipif(
    not FORGE_CARDSFOLDER.exists(),
    reason="requires data/forge cardsfolder (see scripts/import_cardsfolder.py)",
)


@_integration
@_requires_cardsfolder
def test_akroma_vision_of_ixidor_does_not_explode():
    card_path = FORGE_CARDSFOLDER / "a" / "akroma_vision_of_ixidor.txt"
    card = parse_card_file(card_path)
    ports = extract_all_ports(card)
    pump_all_ports = [p for p in ports if p["port_type"] == "effect" and p["event_class"] == "PumpAll"]
    assert len(pump_all_ports) == 14, f"Akroma should emit exactly 14 PumpAll ports, got {len(pump_all_ports)}"
    assert len(ports) < 50, f"Akroma total port count should be small; got {len(ports)}"


@_integration
@_requires_cardsfolder
def test_nature_demands_an_offering_does_not_explode():
    card_path = FORGE_CARDSFOLDER / "n" / "nature_demands_an_offering.txt"
    card = parse_card_file(card_path)
    ports = extract_all_ports(card)
    assert len(ports) > 0, "expected at least some ports"
    assert len(ports) < 30, f"Nature Demands an Offering port count exploded: {len(ports)}"


@_integration
@_requires_cardsfolder
def test_largepox_does_not_explode():
    card_path = FORGE_CARDSFOLDER / "l" / "largepox.txt"
    card = parse_card_file(card_path)
    ports = extract_all_ports(card)
    assert len(ports) > 0, "expected at least some ports"
    assert len(ports) < 30, f"Largepox port count exploded: {len(ports)}"
