"""Tests for Forge verb → trigger event mapping."""
import pytest
from mtg_synergy_train.causal.verb_event_map import verb_to_events


def test_token_produces_changes_zone():
    events = verb_to_events("Token")
    assert any(e["trigger_mode"] == "ChangesZone" for e in events)
    # Token creates a creature entering the battlefield
    czs = [e for e in events if e["trigger_mode"] == "ChangesZone"]
    assert any(e.get("destination") == "Battlefield" for e in czs)


def test_deal_damage_produces_damage_done():
    events = verb_to_events("DealDamage")
    assert any(e["trigger_mode"] == "DamageDone" for e in events)


def test_destroy_produces_changes_zone_to_graveyard():
    events = verb_to_events("Destroy")
    czs = [e for e in events if e["trigger_mode"] == "ChangesZone"]
    assert any(e.get("destination") == "Graveyard" for e in czs)


def test_sacrifice_produces_sacrificed():
    events = verb_to_events("Sacrifice")
    assert any(e["trigger_mode"] == "Sacrificed" for e in events)


def test_draw_produces_drawn():
    events = verb_to_events("Draw")
    assert any(e["trigger_mode"] == "Drawn" for e in events)


def test_gain_life_produces_life_gained():
    events = verb_to_events("GainLife")
    assert any(e["trigger_mode"] == "LifeGained" for e in events)



def test_unknown_verb():
    events = verb_to_events("SomeUnknownVerb")
    assert events == []


def test_pump_has_no_trigger():
    """Pump doesn't produce a triggerable event."""
    events = verb_to_events("Pump")
    assert events == []
