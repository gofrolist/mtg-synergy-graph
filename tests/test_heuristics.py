"""Tests for strategic heuristic rules and the staples list (SPEC §7.3, §7.4)."""

from __future__ import annotations

from mtg_synergy_graph import (
    STAPLE_SCORE,
    STAPLES,
    STRATEGIC_RULES,
    active_rules_for_commander,
    evaluate_strategic_rules,
    staple_bonus,
)


# ---------------------------------------------------------------------------
# §7.4 staples
# ---------------------------------------------------------------------------


def test_staples_lookup_includes_sol_ring_for_every_colour():
    for colour in ("W", "U", "B", "R", "G"):
        assert staple_bonus("Sol Ring", {colour}) == STAPLE_SCORE


def test_staples_returns_zero_for_unknown_card():
    assert staple_bonus("Definitely Not A Staple", {"W", "U"}) == 0


def test_staples_match_only_in_correct_colour():
    # Demonic Tutor is in the B bucket; mono-W shouldn't trigger it.
    assert staple_bonus("Demonic Tutor", {"B"}) == STAPLE_SCORE
    assert staple_bonus("Demonic Tutor", {"W"}) == 0


def test_staples_table_keys_are_pip_letters():
    assert set(STAPLES).issubset({"C", "W", "U", "B", "R", "G"})


# ---------------------------------------------------------------------------
# §7.3 strategic rules
# ---------------------------------------------------------------------------


def test_strategic_rules_have_required_fields():
    for rule in STRATEGIC_RULES:
        assert "name" in rule
        assert callable(rule["condition"])
        assert callable(rule["boost"])
        assert isinstance(rule["weight"], int)
        assert isinstance(rule["reason"], str)


def test_active_rules_for_korvold_includes_lki_and_proliferate(korvold):
    """Korvold has Sacrificed/PutCounter triggers + sacrifice effects → at
    least the LKI scaling rule and the proliferate rule should activate."""
    from mtg_synergy_graph import extract_all_ports

    cmdr_ports = extract_all_ports(korvold)
    active_names = {rule["name"] for rule in active_rules_for_commander(cmdr_ports)}
    assert "lki_scaling" in active_names
    assert "proliferate_for_counters" in active_names


def test_evaluate_strategic_rules_token_for_sacrifice_korvold(korvold, scute_swarm):
    """Scute Swarm produces tokens via DBToken (FalseSubAbility) so the
    branch metadata round-trip should still expose the Token effect to the
    rule. Korvold's Sacrificed trigger activates the tokens_for_sacrifice
    rule."""
    from mtg_synergy_graph import extract_all_ports

    cmdr_ports = extract_all_ports(korvold)
    cand_ports = extract_all_ports(scute_swarm)
    active = active_rules_for_commander(cmdr_ports)
    weight, reasons = evaluate_strategic_rules(
        active,
        {"card": {"name": "Scute Swarm", "cmc": 3, "keywords": "[]"}, "ports": cand_ports},
    )
    # tokens_for_sacrifice rule has weight 5
    assert weight >= 5
    assert any("sacrifice" in r.lower() for r in reasons)


# Phase D3's combat-modifier rule was reverted — every tuning cost
# Hi-Syn on the golden set for zero NDCG gain. See the comment block
# above ``_COMBAT_MODIFIER_STATICS`` in heuristics.py for details.


def test_combat_modifier_rule_not_in_strategic_rules():
    """Regression guard: if anyone re-adds the D3 combat modifier rule
    without re-tuning, the tests should catch it and force a re-check
    against the golden set."""
    assert all(
        r["name"] != "combat_modifier_for_attack_triggers"
        for r in STRATEGIC_RULES
    )


def test_anti_stax_rule_negative_weight():
    """If a commander has Tap effects and a candidate has CantTap, the
    anti-stax rule fires with a negative weight."""
    cmdr_ports = [{"port_type": "effect", "event_class": "Tap"}]
    cand_ports = [{"port_type": "static", "event_class": "CantTap"}]
    active = active_rules_for_commander(cmdr_ports)
    assert any(r["name"] == "anti_stax_activated_abilities" for r in active)
    weight, _ = evaluate_strategic_rules(
        active,
        {"card": {"cmc": 0, "keywords": "[]"}, "ports": cand_ports},
    )
    assert weight < 0
