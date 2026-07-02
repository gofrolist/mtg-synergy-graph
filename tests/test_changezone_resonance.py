"""Tests for graveyard-recursion reciprocity via ``effect_resonance.ChangeZone``.

The existing ``effect_resonance`` rule pairs commander effects with
candidate effects of the same event (Mill ↔ Mill, Proliferate ↔
Proliferate, etc.). ``ChangeZone`` was intentionally omitted because the
zone pair and the filter carry most of the semantic load — two
``ChangeZone`` effects are only truly complementary when they move cards
between the same zones AND target overlapping card types.

Target gap:
- Meren (``effect.ChangeZone`` Graveyard→Battlefield, ``Creature.YouOwn``)
  should resonate with Karmic Guide / Sun Titan / Reveillark (all do the
  same thing on their own ETB / LTB trigger).
- Sharuum / Daretti (Artifact.YouCtrl) should resonate with each other
  but NOT with Meren (Creature vs Artifact — disjoint).
- Tergrid's ``TriggeredCard`` filter (opponent's just-discarded card) is
  runtime-scoped and must not resonate with anything.
- Eternal Witness (Graveyard→Hand) must NOT resonate with Meren
  (Graveyard→Battlefield) — different zone pair, different game action.
"""

from __future__ import annotations

from mtg_synergy_graph.complement_rules.core import COMPLEMENT_RULES


def _effect_resonance_rule():
    for rule in COMPLEMENT_RULES:
        if rule.rule_id == "effect_resonance":
            return rule
    raise AssertionError("effect_resonance rule not found in COMPLEMENT_RULES")


class TestChangeZoneResonanceRegistered:
    """The rule dict must carry ChangeZone entries, otherwise the matcher
    loop never considers the pair regardless of what the check does."""

    def test_changezone_is_in_event_pairs(self):
        rule = _effect_resonance_rule()
        assert "ChangeZone" in rule.event_pairs, (
            "effect_resonance must wire ChangeZone — graveyard-recursion "
            "reciprocity (Meren ↔ Karmic Guide) has no other resonance path."
        )
        assert "ChangeZone" in rule.event_pairs["ChangeZone"], "ChangeZone must target its own event (effect↔effect)."

    def test_changezoneall_is_routed_via_changezone(self):
        """``ChangeZoneAll`` is the mass-return variant (Living Death,
        Rise of the Dark Realms). Commander or candidate in that form
        should still resonate with the single-card variant."""
        rule = _effect_resonance_rule()
        assert "ChangeZoneAll" in rule.event_pairs.get("ChangeZone", {}), (
            "ChangeZone on commander should accept ChangeZoneAll candidates."
        )


class TestChangeZoneResonanceMatching:
    """Filter- and zone-compatibility semantics for the new rule."""

    def _check(self):
        rule = _effect_resonance_rule()
        return rule.event_pairs["ChangeZone"]["ChangeZone"]

    def test_meren_matches_karmic_guide(self):
        """Meren (Creature.YouOwn, Graveyard→Battlefield) × Karmic Guide
        (Creature.YouCtrl, same zones): canonical recursion reciprocity."""
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        karmic_guide = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(meren, karmic_guide) is True

    def test_meren_matches_sun_titan_via_permanent_superset(self):
        """Sun Titan's ``Permanent.YouCtrl+cmcLE3`` recurs any permanent,
        including creatures. Meren's Creature filter is a subset of
        Permanent → overlap exists."""
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        sun_titan = {
            "event_class": "ChangeZone",
            "valid_filter": "Permanent.YouCtrl+cmcLE3",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(meren, sun_titan) is True

    def test_meren_rejects_lord_windgrace_lands(self):
        """Meren (Creature) × Lord Windgrace (Land): different card types.
        Casting a creature does not feed a land recursion commander."""
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        windgrace = {
            "event_class": "ChangeZone",
            "valid_filter": "Land.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(meren, windgrace) is False

    def test_sharuum_matches_daretti_artifacts(self):
        """Both recur artifacts from graveyard → canonical artifact
        recursion reciprocity."""
        sharuum = {
            "event_class": "ChangeZone",
            "valid_filter": "Artifact.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        daretti = {
            "event_class": "ChangeZone",
            "valid_filter": "Artifact.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(sharuum, daretti) is True

    def test_sharuum_rejects_meren_creature(self):
        """Artifact × Creature: disjoint types, no resonance."""
        sharuum = {
            "event_class": "ChangeZone",
            "valid_filter": "Artifact.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(sharuum, meren) is False

    def test_tergrid_runtime_filter_rejected(self):
        """Tergrid's ``TriggeredCard`` filter is a runtime reference to
        the opponent's just-discarded card — not a card-type family.
        Must not resonate with any recursion candidate, otherwise Tergrid
        gets paired with every GY→BF creature in the pool."""
        tergrid = {
            "event_class": "ChangeZone",
            "valid_filter": "TriggeredCard",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        karmic_guide = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(tergrid, karmic_guide) is False

    def test_marchesa_delaytrigger_filter_rejected(self):
        """Marchesa's ``DelayTriggerRememberedLKI`` reanimates the exact
        creature that died (dethrone mechanic) — runtime reference, not
        a card-type family. Must not resonate."""
        marchesa = {
            "event_class": "ChangeZone",
            "valid_filter": "DelayTriggerRememberedLKI",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        karmic_guide = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(marchesa, karmic_guide) is False

    def test_different_destinations_rejected(self):
        """Meren (GY→Battlefield) vs Eternal Witness (GY→Hand): same
        origin, different destination. These aren't the same game
        action — they shouldn't resonate."""
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        eternal_witness = {
            "event_class": "ChangeZone",
            "valid_filter": "Card.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Hand",
        }
        assert self._check()(meren, eternal_witness) is False

    def test_eternal_witness_matches_regrowth(self):
        """Two GY→Hand effects (Eternal Witness × Regrowth-style) should
        also resonate. The rule covers the Hand destination too, not
        just Battlefield."""
        eternal_witness = {
            "event_class": "ChangeZone",
            "valid_filter": "Card.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Hand",
        }
        regrowth = {
            "event_class": "ChangeZone",
            "valid_filter": "Card.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Hand",
        }
        assert self._check()(eternal_witness, regrowth) is True

    def test_different_origins_rejected(self):
        """Battlefield→Exile (flicker) vs Graveyard→Battlefield
        (reanimate): same destination-family label but completely
        different effects."""
        flicker = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouCtrl",
            "zone_origin": "Battlefield",
            "zone_destination": "Exile",
        }
        reanimate = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(flicker, reanimate) is False

    def test_card_self_filter_rejected(self):
        """``Card.Self`` is a self-bounce / self-flicker (Ephemerate,
        Astral Drift). These aren't reanimators — they act on the
        source card only. Must not resonate with graveyard recursion."""
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        self_port = {
            "event_class": "ChangeZone",
            "valid_filter": "Card.Self",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(meren, self_port) is False

    def test_empty_filter_rejected(self):
        """An empty ``valid_filter`` on a ChangeZone effect is lossy
        data — the scope is runtime-bound (Targeted, etc.). Don't
        resonate on noise."""
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        blank = {
            "event_class": "ChangeZone",
            "valid_filter": "",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert self._check()(meren, blank) is False

    def test_changezoneall_matches_changezone(self):
        """Living Death (ChangeZoneAll Graveyard→Battlefield) resonates
        with Meren's single-card ChangeZone."""
        rule = _effect_resonance_rule()
        check = rule.event_pairs["ChangeZone"]["ChangeZoneAll"]
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        living_death = {
            "event_class": "ChangeZoneAll",
            "valid_filter": "Creature",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        assert check(meren, living_death) is True

    def test_card_prefixed_type_uses_specific_type(self):
        """Ajani's ``Card.Creature+cmcLE2+YouCtrl`` means "creature card",
        not "any card". The type head is ``Card`` but the *specific*
        type lives after the dot. Must narrow to Creature so Ajani
        pairs with Meren (Creature) but NOT with Sharuum (Artifact) or
        Lord Windgrace (Land)."""
        ajani = {
            "event_class": "ChangeZone",
            "valid_filter": "Card.Creature+cmcLE2+YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        sharuum = {
            "event_class": "ChangeZone",
            "valid_filter": "Artifact.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        meren = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouOwn",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        windgrace = {
            "event_class": "ChangeZone",
            "valid_filter": "Land.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Battlefield",
        }
        check = self._check()
        assert check(ajani, meren) is True, "Ajani's Card.Creature should match Meren's Creature"
        assert check(ajani, sharuum) is False, "Ajani's Card.Creature should NOT match Sharuum's Artifact"
        assert check(ajani, windgrace) is False, "Ajani's Card.Creature should NOT match Lord Windgrace's Land"

    def test_card_scope_only_is_universal(self):
        """``Card.YouCtrl`` / ``Card.YouOwn`` with only a scope qualifier
        (no type token after the dot) IS a universal recursion — Eternal
        Witness pattern. Should match any typed peer."""
        eternal_witness = {
            "event_class": "ChangeZone",
            "valid_filter": "Card.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Hand",
        }
        regrowth_creature = {
            "event_class": "ChangeZone",
            "valid_filter": "Creature.YouCtrl",
            "zone_origin": "Graveyard",
            "zone_destination": "Hand",
        }
        check = self._check()
        assert check(eternal_witness, regrowth_creature) is True


class TestZoneEquivalenceClasses:
    """Plan 2026-07-02-002 Unit 9: seed-data zone equivalence classes.

    ``recur_to_battlefield`` = {Graveyard, Exile} → Battlefield;
    ``retrieve_to_hand`` = {Graveyard, Exile} → Hand. Pairs outside
    every class keep exact-equality semantics.
    """

    def _check(self):
        from mtg_synergy_graph.complement_rules.core import (
            _changezone_resonance_check,
        )

        return _changezone_resonance_check

    def _port(self, origin, dest, vf="Creature.YouCtrl"):
        return {
            "zone_origin": origin,
            "zone_destination": dest,
            "valid_filter": vf,
        }

    def test_gy_and_exile_to_battlefield_resonate(self):
        """Meren (GY→BF) × an Exile→BF recursion piece — same class."""
        check = self._check()
        assert check(self._port("Graveyard", "Battlefield"), self._port("Exile", "Battlefield"))

    def test_retrieval_is_not_reanimation(self):
        """GY→Hand vs GY→BF stay distinct — different classes."""
        check = self._check()
        assert not check(self._port("Graveyard", "Hand"), self._port("Graveyard", "Battlefield"))

    def test_exile_to_hand_resonates_with_gy_to_hand(self):
        check = self._check()
        assert check(self._port("Exile", "Hand"), self._port("Graveyard", "Hand"))

    def test_unclassed_pair_keeps_exact_equality(self):
        """Zone pairs outside every class: exact match still resonates,
        mismatch still does not."""
        check = self._check()
        assert check(self._port("Hand", "Library"), self._port("Hand", "Library"))
        assert not check(self._port("Hand", "Library"), self._port("Library", "Hand"))

    def test_type_family_still_required_across_class(self):
        """The class relaxes zones only — disjoint card types still
        block resonance (Meren × Land recursion)."""
        check = self._check()
        assert not check(
            self._port("Graveyard", "Battlefield", vf="Creature.YouOwn"),
            self._port("Exile", "Battlefield", vf="Land.YouCtrl"),
        )

    def test_malformed_seed_row_raises(self, tmp_path, monkeypatch):
        """Loader raises at first use on a malformed class row — the
        interpreter's seed drift-check pattern."""
        import json

        import mtg_synergy_graph.complement_rules.core as core_mod
        import mtg_synergy_graph.port_graph._paths as paths_mod

        bad = tmp_path / "event_match_seed.json"
        bad.write_text(
            json.dumps({"zone_equivalence_classes": [{"class": "x", "origins": [], "destination": "Battlefield"}]})
        )
        monkeypatch.setattr(paths_mod, "default_seed_path", lambda name: bad)
        monkeypatch.setattr(core_mod, "_ZONE_EQUIVALENCE_CACHE", None)
        import pytest

        with pytest.raises(ValueError, match="malformed zone_equivalence_classes"):
            core_mod._zone_equivalence_map()

    def test_class_rows_participate_in_config_digest(self):
        """Editing a class row must flip the scoring config hash — the
        section is part of event_match_seed_digest."""
        from mtg_synergy_graph.universal_scorer import _seed_digest

        with_zones = _seed_digest(
            "event_match_seed.json",
            ("event_match_map", "cost_feeds_trigger", "zone_equivalence_classes"),
        )
        without_zones = _seed_digest(
            "event_match_seed.json",
            ("event_match_map", "cost_feeds_trigger"),
        )
        assert with_zones != without_zones
