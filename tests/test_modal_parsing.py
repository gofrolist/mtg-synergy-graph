"""Tests for modal ability effect parsing."""
import pytest
from mtg_synergy.parse import parse_card


def test_modal_choose_one():
    oracle = "Choose one \u2014\n\u2022 Create a 1/1 white Human creature token.\n\u2022 Draw a card.\n\u2022 Destroy target enchantment."
    abilities = parse_card(oracle)
    modal = [a for a in abilities if a.kind == "modal"]
    assert len(modal) >= 1
    effects = modal[0].effects
    verbs = {e.verb for e in effects}
    assert "create" in verbs
    assert "draw" in verbs
    assert "destroy" in verbs


def test_modal_choose_two():
    oracle = "Choose two \u2014\n\u2022 Create a 1/1 white Human creature token.\n\u2022 Put a +1/+1 counter on target creature.\n\u2022 You gain 3 life."
    abilities = parse_card(oracle)
    modal = [a for a in abilities if a.kind == "modal"]
    assert len(modal) >= 1
    verbs = {e.verb for e in modal[0].effects}
    assert len(verbs) >= 2


def test_non_modal_unaffected():
    oracle = "Whenever a creature enters the battlefield under your control, draw a card."
    abilities = parse_card(oracle)
    assert any(e.verb == "draw" for a in abilities for e in a.effects)
