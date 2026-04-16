"""Tests for scope-aware trigger/effect compatibility.

Covers _parse_trigger_scope, _parse_cand_scope, _scope_compatible — the
helpers that prevent false matches like Tergrid (opp-scoped trigger) +
Lich's Tomb (you-scoped Sacrifice effect).
"""

from __future__ import annotations

import pytest

from mtg_synergy_graph.complement_rules.core import (
    COMPLEMENT_RULES,
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


# ---------------------------------------------------------------------------
# Integration: trigger_effect rule should reject scope-incompatible pairs
# ---------------------------------------------------------------------------


def _trigger_effect_rule():
    for rule in COMPLEMENT_RULES:
        if rule.rule_id == "trigger_effect":
            return rule
    raise AssertionError("trigger_effect rule not found in COMPLEMENT_RULES")


class TestTriggerEffectScope:
    """The trigger_effect rule must respect player scope on player-centric events."""

    def test_tergrid_rejects_you_sacrifice(self):
        """Tergrid (opp-scoped Sacrificed) × Lich's Tomb (you-scoped Sacrifice)
        must NOT match. Before this fix the pair matched via trigger_effect
        and Lich's Tomb ranked as Tergrid's #1 pick — a false synergy."""
        rule = _trigger_effect_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrifice"]
        tergrid_trigger = {
            "event_class": "Sacrificed",
            "valid_filter": "Permanent.!token+OppCtrl",
        }
        lichs_tomb_effect = {"event_class": "Sacrifice", "valid_filter": ""}
        assert check(tergrid_trigger, lichs_tomb_effect) is False

    def test_tergrid_accepts_each_player_sacrifice(self):
        """Tergrid × Smallpox ("Each player sacrifices") must match:
        forcing all players to sac includes opponents."""
        rule = _trigger_effect_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrifice"]
        tergrid_trigger = {
            "event_class": "Sacrificed",
            "valid_filter": "Permanent.!token+OppCtrl",
        }
        smallpox_effect = {"event_class": "Sacrifice", "valid_filter": "Player"}
        assert check(tergrid_trigger, smallpox_effect) is True

    def test_tergrid_accepts_opponent_discard(self):
        """Tergrid × Burglar Rat (opponent discards): opp ↔ opp matches."""
        rule = _trigger_effect_rule()
        check = rule.event_pairs["Discarded"]["Discard"]
        tergrid_trigger = {
            "event_class": "Discarded",
            "valid_filter": "Permanent.!token+OppCtrl",
        }
        opp_discard = {"event_class": "Discard", "valid_filter": "Opponent"}
        assert check(tergrid_trigger, opp_discard) is True

    def test_meren_accepts_each_player_sacrifice(self):
        """Meren (you-scoped creature-dies) × Smallpox must match — each-player
        sac includes your own."""
        rule = _trigger_effect_rule()
        # Meren's trigger is ChangesZone death, not Sacrificed — exercise
        # the Sacrificed axis with a hypothetical you-scoped commander that
        # triggers on your permanents being sacrificed.
        check = rule.event_pairs["Sacrificed"]["Sacrifice"]
        you_scoped = {
            "event_class": "Sacrificed",
            "valid_filter": "Permanent.YouCtrl",
        }
        smallpox_effect = {"event_class": "Sacrifice", "valid_filter": "Player"}
        assert check(you_scoped, smallpox_effect) is True

    def test_you_scoped_rejects_opp_only_sacrifice(self):
        """You-scoped sac trigger × opp-only Sacrifice effect → no match."""
        rule = _trigger_effect_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrifice"]
        you_scoped = {
            "event_class": "Sacrificed",
            "valid_filter": "Permanent.YouCtrl",
        }
        opp_only = {"event_class": "Sacrifice", "valid_filter": "Opponent"}
        assert check(you_scoped, opp_only) is False

    def test_any_scoped_trigger_matches_anything(self):
        """A trigger with no player filter (any) accepts all candidate scopes."""
        rule = _trigger_effect_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrifice"]
        any_trigger = {"event_class": "Sacrificed", "valid_filter": ""}
        for vf in ("", "Player", "Opponent", "You"):
            eff = {"event_class": "Sacrifice", "valid_filter": vf}
            assert check(any_trigger, eff) is True, f"any trigger should match cand_vf={vf!r}"

    def test_non_player_events_unchanged(self):
        """ChangesZone and counter events should NOT gain a scope filter —
        their compatibility is handled by zone/counter logic."""
        rule = _trigger_effect_rule()
        # ChangesZone → ChangeZone still uses zone compatibility (no scope).
        # We verify that passing YouCtrl/OppCtrl filters doesn't flip the
        # result for zone pairs — the zone check is what matters.
        check = rule.event_pairs["ChangesZone"]["ChangeZone"]
        etb_trigger = {
            "event_class": "ChangesZone",
            "valid_filter": "Creature.OppCtrl",
            "zone_origin": "Any",
            "zone_destination": "Battlefield",
        }
        etb_effect = {
            "event_class": "ChangeZone",
            "valid_filter": "",
            "zone_origin": "Any",
            "zone_destination": "Battlefield",
        }
        # Zone-compatible; scope difference must not filter this out.
        assert check(etb_trigger, etb_effect) is True


# ---------------------------------------------------------------------------
# Integration: cost_feeds_trigger rule — costs are always paid by 'you'
# ---------------------------------------------------------------------------


def _cost_feeds_trigger_rule():
    for rule in COMPLEMENT_RULES:
        if rule.rule_id == "cost_feeds_trigger":
            return rule
    raise AssertionError("cost_feeds_trigger rule not found in COMPLEMENT_RULES")


class TestCostFeedsTriggerScope:
    """Costs are paid by the activator (controller = you). So a commander
    trigger scoped to ``opp`` cannot be fed by any candidate cost."""

    def test_opp_trigger_rejects_self_sac_cost(self):
        """Tergrid (opp-sac trigger) × Gonti's Machinations (self-sac cost)
        must NOT match. Before this fix the pair matched via
        cost_feeds_trigger and Gonti's Machinations ranked #1 for Tergrid."""
        rule = _cost_feeds_trigger_rule()
        check = rule.event_pairs["Sacrificed"]["sacrifice"]
        tergrid_trigger = {
            "event_class": "Sacrificed",
            "valid_filter": "Permanent.!token+OppCtrl",
        }
        self_sac_cost = {
            "event_class": "sacrifice",
            "cost_subtype": "1/CARDNAME/self",
        }
        assert check(tergrid_trigger, self_sac_cost) is False

    def test_opp_trigger_rejects_creature_sac_cost(self):
        """Tergrid × Ashnod's Altar (sac creature cost) must NOT match —
        the activator sacs their own creature, not the opponent's."""
        rule = _cost_feeds_trigger_rule()
        check = rule.event_pairs["Sacrificed"]["sacrifice"]
        tergrid_trigger = {
            "event_class": "Sacrificed",
            "valid_filter": "Permanent.!token+OppCtrl",
        }
        sac_cost = {"event_class": "sacrifice", "cost_subtype": "1/Creature"}
        assert check(tergrid_trigger, sac_cost) is False

    def test_you_trigger_accepts_sac_cost(self):
        """Korvold (you-sac trigger) × Ashnod's Altar must match."""
        rule = _cost_feeds_trigger_rule()
        check = rule.event_pairs["Sacrificed"]["sacrifice"]
        korvold_trigger = {
            "event_class": "Sacrificed",
            "valid_filter": "Permanent.YouCtrl",
        }
        sac_cost = {"event_class": "sacrifice", "cost_subtype": "1/Creature"}
        assert check(korvold_trigger, sac_cost) is True

    def test_any_trigger_accepts_sac_cost(self):
        """Unscoped Sacrificed trigger accepts any cost feeder."""
        rule = _cost_feeds_trigger_rule()
        check = rule.event_pairs["Sacrificed"]["sacrifice"]
        any_trigger = {"event_class": "Sacrificed", "valid_filter": ""}
        sac_cost = {"event_class": "sacrifice", "cost_subtype": "1/Creature"}
        assert check(any_trigger, sac_cost) is True

    def test_opp_discard_trigger_rejects_discard_cost(self):
        """Opp-Discarded trigger × candidate discard cost must not match."""
        rule = _cost_feeds_trigger_rule()
        check = rule.event_pairs["Discarded"]["discard"]
        opp_trigger = {"event_class": "Discarded", "valid_filter": "Card.OppOwn"}
        discard_cost = {"event_class": "discard", "cost_subtype": "1/Card"}
        assert check(opp_trigger, discard_cost) is False

    def test_opp_life_lost_trigger_rejects_pay_life_cost(self):
        """Opp-LifeLost trigger × pay-life cost must not match (you pay, not opp)."""
        rule = _cost_feeds_trigger_rule()
        check = rule.event_pairs["LifeLost"]["pay_life"]
        opp_trigger = {"event_class": "LifeLost", "valid_filter": "Player.Opponent"}
        pay_life_cost = {"event_class": "pay_life", "cost_subtype": "2"}
        assert check(opp_trigger, pay_life_cost) is False

    def test_non_scoped_events_unchanged(self):
        """Taps/Exiled/Milled cost feeders are NOT scope-checked —
        leaving their semantics alone."""
        rule = _cost_feeds_trigger_rule()
        # Taps isn't in _SCOPED_TRIGGER_EVENTS, so opp-tap trigger + tap cost
        # should still match via the original _always check.
        check = rule.event_pairs["Taps"]["tap"]
        opp_trigger = {"event_class": "Taps", "valid_filter": "Creature.OppCtrl"}
        tap_cost = {"event_class": "tap"}
        assert check(opp_trigger, tap_cost) is True


# ---------------------------------------------------------------------------
# Integration: trigger_resonance rule — trigger ↔ trigger scope check
# ---------------------------------------------------------------------------


def _trigger_resonance_rule():
    for rule in COMPLEMENT_RULES:
        if rule.rule_id == "trigger_resonance":
            return rule
    raise AssertionError("trigger_resonance rule not found in COMPLEMENT_RULES")


class TestTriggerResonanceScope:
    """Both sides of trigger_resonance are triggers — both filters specify
    the player scope of the event being watched. Scopes must agree."""

    def test_opp_rejects_you_resonance(self):
        """Tergrid (opp-Sacrificed) × Blood Aspirant (you-Sacrificed) → no match.
        Tergrid's trigger fires when opps sacrifice; Blood Aspirant fires
        when YOU sacrifice — they watch different events."""
        rule = _trigger_resonance_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrificed"]
        tergrid = {"event_class": "Sacrificed", "valid_filter": "Permanent.!token+OppCtrl"}
        blood_aspirant = {"event_class": "Sacrificed", "valid_filter": "Permanent.YouCtrl"}
        assert check(tergrid, blood_aspirant) is False

    def test_opp_accepts_opp_resonance(self):
        """Tergrid × Waste Not (opp-Discarded): opp↔opp matches."""
        rule = _trigger_resonance_rule()
        check = rule.event_pairs["Discarded"]["Discarded"]
        tergrid = {"event_class": "Discarded", "valid_filter": "Permanent.!token+OppCtrl"}
        waste_not = {"event_class": "Discarded", "valid_filter": "Card.nonLand+nonCreature+OppOwn"}
        assert check(tergrid, waste_not) is True

    def test_opp_accepts_any_resonance(self):
        """An opp-scoped commander matches a scopeless candidate trigger
        (the candidate fires on any player's event, including opp's)."""
        rule = _trigger_resonance_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrificed"]
        tergrid = {"event_class": "Sacrificed", "valid_filter": "Permanent.!token+OppCtrl"}
        unscoped = {"event_class": "Sacrificed", "valid_filter": ""}
        assert check(tergrid, unscoped) is True

    def test_you_accepts_you_resonance(self):
        """You-scoped commander × you-scoped candidate: match."""
        rule = _trigger_resonance_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrificed"]
        you_cmdr = {"event_class": "Sacrificed", "valid_filter": "Permanent.YouCtrl"}
        you_cand = {"event_class": "Sacrificed", "valid_filter": "Permanent.YouCtrl"}
        assert check(you_cmdr, you_cand) is True

    def test_you_rejects_opp_resonance(self):
        """You-scoped commander × opp-only candidate: no match."""
        rule = _trigger_resonance_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrificed"]
        you_cmdr = {"event_class": "Sacrificed", "valid_filter": "Permanent.YouCtrl"}
        opp_cand = {"event_class": "Sacrificed", "valid_filter": "Permanent.OppCtrl"}
        assert check(you_cmdr, opp_cand) is False

    def test_non_scoped_event_unchanged(self):
        """Proliferate / DamageDone / Taps / Untaps / Investigated / Surveil
        are NOT in _SCOPED_TRIGGER_EVENTS and keep _always semantics."""
        rule = _trigger_resonance_rule()
        check = rule.event_pairs["DamageDone"]["DamageDone"]
        opp_trigger = {"event_class": "DamageDone", "valid_filter": "Creature.OppCtrl"}
        you_trigger = {"event_class": "DamageDone", "valid_filter": "Creature.YouCtrl"}
        # DamageDone filters refer to the source creature, not a player event —
        # scope logic doesn't apply. _always should still match.
        assert check(opp_trigger, you_trigger) is True


# ---------------------------------------------------------------------------
# Integration: sacrifice_cluster rule — trigger↔trigger + trigger↔effect
# ---------------------------------------------------------------------------


def _sacrifice_cluster_rule():
    for rule in COMPLEMENT_RULES:
        if rule.rule_id == "sacrifice_cluster":
            return rule
    raise AssertionError("sacrifice_cluster rule not found in COMPLEMENT_RULES")


class TestSacrificeClusterScope:
    """sacrifice_cluster pairs death triggers with death triggers or death
    effects. Scope must agree on both cmdr side and cand side."""

    def test_opp_trigger_rejects_you_trigger_cluster(self):
        """Tergrid × Blood Aspirant (you-sacrificed trigger): no match."""
        rule = _sacrifice_cluster_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrificed"]
        tergrid = {"event_class": "Sacrificed", "valid_filter": "Permanent.!token+OppCtrl"}
        blood_aspirant = {"event_class": "Sacrificed", "valid_filter": "Permanent.YouCtrl"}
        assert check(tergrid, blood_aspirant) is False

    def test_opp_trigger_rejects_you_sacrifice_effect(self):
        """Tergrid × self-Sacrifice effect: no match (candidate effect is
        you-scoped via empty-filter default)."""
        rule = _sacrifice_cluster_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrifice"]
        tergrid = {"event_class": "Sacrificed", "valid_filter": "Permanent.!token+OppCtrl"}
        self_sac = {"event_class": "Sacrifice", "valid_filter": ""}
        assert check(tergrid, self_sac) is False

    def test_opp_trigger_accepts_each_player_sacrifice(self):
        """Tergrid × Smallpox (each-player Sacrifice): match."""
        rule = _sacrifice_cluster_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrifice"]
        tergrid = {"event_class": "Sacrificed", "valid_filter": "Permanent.!token+OppCtrl"}
        smallpox = {"event_class": "Sacrifice", "valid_filter": "Player"}
        assert check(tergrid, smallpox) is True

    def test_you_trigger_accepts_you_trigger_cluster(self):
        """Korvold × Blood Artist: you↔any matches (Blood Artist is
        scopeless creature-dies)."""
        rule = _sacrifice_cluster_rule()
        check = rule.event_pairs["Sacrificed"]["Sacrificed"]
        korvold = {"event_class": "Sacrificed", "valid_filter": "Permanent.YouCtrl"}
        any_scope = {"event_class": "Sacrificed", "valid_filter": ""}
        assert check(korvold, any_scope) is True
