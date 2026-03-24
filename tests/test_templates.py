"""Tests for template-based pattern matching (the 5% escape hatch)."""
import pytest
from mtg_synergy.parse.templates import apply_templates


def test_for_each_scaling():
    result = apply_templates("deal damage equal to the number of creatures you control")
    assert result.scaling is not None
    assert result.scaling.what == "creatures you control"
    assert result.scaling.how == "linear"

def test_where_x_is():
    result = apply_templates("Create X tokens, where X is the number of Goblins you control")
    assert result.scaling is not None
    assert "Goblins" in result.scaling.what
    assert result.scaling.how == "linear"

def test_double_template():
    result = apply_templates("double the number of +1/+1 counters on target creature")
    assert result.scaling is not None
    assert result.scaling.how == "multiplicative"

def test_that_many_plus_one():
    result = apply_templates("that many plus one +1/+1 counters are placed on it instead")
    assert result.scaling is not None
    assert result.scaling.how == "linear"

def test_twice_that_many():
    result = apply_templates("twice that many of those tokens instead")
    assert result.scaling is not None
    assert result.scaling.how == "multiplicative"

def test_no_template_match():
    result = apply_templates("Draw a card.")
    assert result is None

def test_additional_time():
    result = apply_templates("that ability triggers an additional time")
    assert result.kind == "trigger_modifier"

def test_reminder_text_decomposition():
    from mtg_synergy.parse.templates import decompose_reminder
    effects = decompose_reminder("Draw a card, then discard a card. If you discarded a nonland card, put a +1/+1 counter on this creature.")
    assert len(effects) >= 2
    verbs = [e.verb for e in effects]
    assert "draw" in verbs
    assert "discard" in verbs
