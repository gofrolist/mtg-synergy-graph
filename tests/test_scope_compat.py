"""Tests for scope-aware trigger/effect compatibility.

Covers _parse_trigger_scope, _parse_cand_scope, _scope_compatible — the
helpers that prevent false matches like Tergrid (opp-scoped trigger) +
Lich's Tomb (you-scoped Sacrifice effect).
"""

from __future__ import annotations

import pytest

from mtg_synergy_graph.complement_rules.core import (
    _parse_cand_scope,
    _parse_trigger_scope,
    _scope_compatible,
)


class TestParseTriggerScope:
    """Commander trigger valid_filter → scope classification."""

    def test_opp_ctrl(self):
        assert _parse_trigger_scope({"valid_filter": "Permanent.!token+OppCtrl"}) == "opp"

    def test_opp_own(self):
        assert _parse_trigger_scope({"valid_filter": "Creature.OppOwn"}) == "opp"

    def test_player_opponent(self):
        assert _parse_trigger_scope({"valid_filter": "Player.Opponent"}) == "opp"

    def test_bare_opponent(self):
        assert _parse_trigger_scope({"valid_filter": "Opponent"}) == "opp"

    def test_you_ctrl(self):
        assert _parse_trigger_scope({"valid_filter": "Creature.Other+YouCtrl"}) == "you"

    def test_you_own(self):
        assert _parse_trigger_scope({"valid_filter": "Creature.YouOwn"}) == "you"

    def test_bare_you(self):
        assert _parse_trigger_scope({"valid_filter": "You"}) == "you"

    def test_bare_player(self):
        # "Player" on its own = each/any player
        assert _parse_trigger_scope({"valid_filter": "Player"}) == "any"

    def test_empty(self):
        assert _parse_trigger_scope({"valid_filter": ""}) == "any"

    def test_none(self):
        assert _parse_trigger_scope({"valid_filter": None}) == "any"

    def test_missing_key(self):
        assert _parse_trigger_scope({}) == "any"

    def test_no_player_marker(self):
        # Filter with no controller info -> any
        assert _parse_trigger_scope({"valid_filter": "Card.Self"}) == "any"


class TestParseCandScope:
    """Candidate effect valid_filter → scope classification with event defaults."""

    def test_player_is_any(self):
        assert _parse_cand_scope({"event_class": "Sacrifice", "valid_filter": "Player"}) == "any"

    def test_player_opponent(self):
        assert _parse_cand_scope({"event_class": "LoseLife", "valid_filter": "Player.Opponent"}) == "opp"

    def test_opponent(self):
        assert _parse_cand_scope({"event_class": "Discard", "valid_filter": "Opponent"}) == "opp"

    def test_you(self):
        assert _parse_cand_scope({"event_class": "Draw", "valid_filter": "You"}) == "you"

    def test_empty_sacrifice_defaults_to_you(self):
        # Lich's Tomb, Oath of Lim-Dûl: empty valid_filter on Sacrifice effect
        # means the controller sacrifices.
        assert _parse_cand_scope({"event_class": "Sacrifice", "valid_filter": ""}) == "you"

    def test_empty_discard_defaults_to_you(self):
        assert _parse_cand_scope({"event_class": "Discard", "valid_filter": ""}) == "you"

    def test_empty_draw_defaults_to_you(self):
        assert _parse_cand_scope({"event_class": "Draw", "valid_filter": ""}) == "you"

    def test_empty_gainlife_defaults_to_you(self):
        assert _parse_cand_scope({"event_class": "GainLife", "valid_filter": ""}) == "you"

    def test_empty_loselife_defaults_to_any(self):
        # LoseLife with empty filter is ambiguous (could be targeted) - be permissive
        assert _parse_cand_scope({"event_class": "LoseLife", "valid_filter": ""}) == "any"

    def test_empty_other_defaults_to_any(self):
        assert _parse_cand_scope({"event_class": "SomeOtherEvent", "valid_filter": ""}) == "any"

    def test_triggered_activator_any(self):
        # Oppression: "TriggeredActivator" - whoever cast the spell. Context-dependent.
        assert _parse_cand_scope({"event_class": "Discard", "valid_filter": "TriggeredActivator"}) == "any"

    def test_is_remembered_any(self):
        # Dark Deal: "Player.IsRemembered" cycles through each player - any
        assert _parse_cand_scope({"event_class": "Discard", "valid_filter": "Player.IsRemembered"}) == "any"


class TestScopeCompatible:
    """Cross-product of cmdr_scope × cand_scope."""

    @pytest.mark.parametrize(
        "cmdr,cand,expected",
        [
            ("any", "any", True),
            ("any", "opp", True),
            ("any", "you", True),
            ("opp", "any", True),  # each player includes opp
            ("opp", "opp", True),
            ("opp", "you", False),  # ← Tergrid × Lich's Tomb case
            ("you", "any", True),
            ("you", "opp", False),
            ("you", "you", True),
        ],
    )
    def test_matrix(self, cmdr, cand, expected):
        assert _scope_compatible(cmdr, cand) is expected
