"""Tests for port extractors against the 5 reference cards (SPEC §5.5)."""

from __future__ import annotations

from mtg_synergy_graph import extract_all_ports


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
