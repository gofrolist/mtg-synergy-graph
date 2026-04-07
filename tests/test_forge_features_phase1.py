"""Phase 1 Forge DSL extraction tests.

Covers combat trigger filter extraction (ValidAttacker$ / ValidBlocker$)
from T: ability raw_line rows. Inputs are verbatim samples from
data/tags.db forge_abilities.raw_line rows.
"""

from mtg_synergy.recommend.forge_features import ForgeFeatureContext


class TestCombatTriggerFilters:
    # Verbatim T: row from DB (wooden_stake, AttackerBlockedByCreature, row with
    # ValidBlocker$ Creature.Vampire):
    ROW_VAMPIRE_BLOCKER = (
        "T:Mode$ AttackerBlockedByCreature | ValidCard$ Card.AttachedBy | "
        "ValidBlocker$ Creature.Vampire | Execute$ TrigDestroyBlocker | "
        "Secondary$ True | TriggerDescription$ Whenever equipped creature "
        "blocks or becomes blocked by a Vampire, destroy that creature. "
        "It can't be regenerated."
    )

    # Verbatim T: row from DB (wall_of_frost, AttackerBlocked, ValidBlocker$ Card.Self):
    ROW_WALL_OF_FROST = (
        "T:Mode$ AttackerBlocked | ValidBlocker$ Card.Self | Execute$ TrigPump | "
        "TriggerDescription$ Whenever CARDNAME blocks a creature, that creature "
        "doesn't untap during its controller's next untap step."
    )

    # Verbatim T: row from DB — has ValidBlocker$ with no ValidAttacker$ field,
    # trigger filter is a plain value (Creature):
    ROW_BLACK_RED_PUMP = (
        "T:Mode$ AttackerBlocked | ValidCard$ Creature.Black,Creature.Red | "
        "ValidBlocker$ Creature | TriggerZones$ Battlefield | Execute$ TrigPump | "
        "TriggerDescription$ Whenever a creature blocks a black or red creature, "
        "the blocking creature gets +1/+1 until end of turn. |EXEC| DB$ Pump | "
        "NumAtt$ +1 | NumDef$ +1 | Defined$ TriggeredBlockerLKICopy | "
        "SpellDescription$ The blocking creature gets +1/+1 until end of turn."
    )

    # Verbatim S: row (not a T: row but helps sanity-check the regex doesn't
    # over-match — the helper itself parses any raw_line; wiring restricts to T:)
    ROW_SHIFTING_SLIVER = (
        "S:Mode$ CantBlockBy | ValidAttacker$ Creature.Sliver | "
        "ValidBlocker$ Creature.nonSliver | Description$ Slivers can't be "
        "blocked except by Slivers."
    )

    def test_valid_blocker_extracted(self):
        result = ForgeFeatureContext._parse_combat_trigger_filters(
            self.ROW_VAMPIRE_BLOCKER
        )
        assert "Creature.Vampire" in result

    def test_valid_blocker_card_self_skipped(self):
        # Card.Self sentinel must be excluded (mirrors ValidCard$ handling)
        result = ForgeFeatureContext._parse_combat_trigger_filters(
            self.ROW_WALL_OF_FROST
        )
        assert result == set()

    def test_plain_value_extracted(self):
        # Plain `Creature` (no subtype, no controller) must be extracted
        result = ForgeFeatureContext._parse_combat_trigger_filters(
            self.ROW_BLACK_RED_PUMP
        )
        assert result == {"Creature"}

    def test_both_present(self):
        # Strict equality — confirms the regex doesn't over-match other fields
        result = ForgeFeatureContext._parse_combat_trigger_filters(
            self.ROW_SHIFTING_SLIVER
        )
        assert result == {"Creature.Sliver", "Creature.nonSliver"}

    def test_non_combat_line_returns_empty(self):
        # T: line with only ValidCard$ (no ValidAttacker/Blocker)
        line = (
            "T:Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | "
            "ValidCard$ Creature.YouCtrl | Execute$ TrigPump | "
            "TriggerDescription$ Whenever a creature you control enters."
        )
        assert ForgeFeatureContext._parse_combat_trigger_filters(line) == set()

    def test_empty_and_none(self):
        assert ForgeFeatureContext._parse_combat_trigger_filters("") == set()
        assert ForgeFeatureContext._parse_combat_trigger_filters(None) == set()

    def test_valid_attacker_extracted(self):
        # Synthetic but minimal — ValidAttacker$ on a T: line
        line = (
            "T:Mode$ Attacks | ValidAttacker$ Creature.YouCtrl+Dragon | "
            "Execute$ TrigDraw | TriggerDescription$ Whenever a Dragon attacks, draw."
        )
        result = ForgeFeatureContext._parse_combat_trigger_filters(line)
        assert result == {"Creature.YouCtrl+Dragon"}

    def test_multi_type_filter_derives_all_coarse_types(self):
        """Regression: coarse-type derivation must iterate ALL comma-separated
        parts of a filter value, not just the first.

        Calls the SAME static helper that the production wiring uses
        (_derive_coarse_filter_types), so a future regression in the wiring
        block at forge_features.py:_process_forge_ability_row will be caught
        as long as it continues to call this helper.
        """
        # Multi-type filter as it would appear in a real ValidAttacker$ value
        result = ForgeFeatureContext._derive_coarse_filter_types("Creature,Artifact")
        assert result == {"creature", "artifact"}

    def test_derive_coarse_filter_types_skips_card_sentinel(self):
        """The 'Card' sentinel must NOT enter trigger_filters."""
        result = ForgeFeatureContext._derive_coarse_filter_types("Card")
        assert result == set()

    def test_derive_coarse_filter_types_preserves_subtype(self):
        """Subtypes after the first dot should be stripped — coarse type only."""
        result = ForgeFeatureContext._derive_coarse_filter_types(
            "Creature.YouCtrl+Dragon"
        )
        assert result == {"creature"}

    def test_derive_coarse_filter_types_handles_none_and_empty(self):
        assert ForgeFeatureContext._derive_coarse_filter_types(None) == set()
        assert ForgeFeatureContext._derive_coarse_filter_types("") == set()


class TestCostTypesExpansion:
    """Phase 1: 4 new cost categories (subcounter, exilegrave, taptype, return).
    Inputs are verbatim samples from forge_abilities.cost column.
    """

    # === Regression: the 5 existing categories must still fire ===

    def test_sacrifice_regression(self):
        # Real samples from DB
        assert "sacrifice" in ForgeFeatureContext._parse_cost_types("Sac<1/CARDNAME>")
        assert "sacrifice" in ForgeFeatureContext._parse_cost_types("2 Sac<1/Creature>")

    def test_tap_regression(self):
        assert "tap" in ForgeFeatureContext._parse_cost_types("T")
        assert "tap" in ForgeFeatureContext._parse_cost_types("1 T")
        assert "tap" in ForgeFeatureContext._parse_cost_types("T Sac<1/CARDNAME>")
        # Capital 'T' appears as a substring of 'PayLife<2>' but NOT as a
        # whitespace-separated token. Locks in the reason the rule uses
        # split() instead of plain substring match.
        assert "tap" not in ForgeFeatureContext._parse_cost_types("PayLife<2>")

    def test_discard_regression(self):
        assert "discard" in ForgeFeatureContext._parse_cost_types("1 R Discard<1/Card>")

    def test_exile_regression(self):
        assert "exile" in ForgeFeatureContext._parse_cost_types("2 Exile<1/CARDNAME>")

    def test_paylife_regression(self):
        assert "paylife" in ForgeFeatureContext._parse_cost_types("PayLife<2>")

    # === New categories ===

    def test_subcounter_p1p1(self):
        result = ForgeFeatureContext._parse_cost_types("SubCounter<1/P1P1>")
        assert "subcounter" in result

    def test_subcounter_with_tap(self):
        # Real combined form from DB
        result = ForgeFeatureContext._parse_cost_types("T SubCounter<1/CHARGE>")
        assert result >= {"tap", "subcounter"}

    def test_subcounter_loyalty_still_tagged(self):
        # Loyalty-minus planeswalker costs must also set subcounter — harmless
        # because no commander produces LOYALTY counters, so cost_feeds_cmdr
        # will never spuriously match. Controller-approved.
        assert "subcounter" in ForgeFeatureContext._parse_cost_types("SubCounter<2/LOYALTY>")

    def test_exilegrave_additive(self):
        # CRITICAL invariant: ExileFromGrave sets BOTH exile AND exilegrave.
        # This preserves the existing 'exile' count bit-for-bit so the
        # post-WIP baseline (0.5707) is not shifted by Phase 1.
        result = ForgeFeatureContext._parse_cost_types("5 R ExileFromGrave<1/CARDNAME>")
        assert result == {"exile", "exilegrave"}

    def test_exilegrave_with_creature_pool(self):
        result = ForgeFeatureContext._parse_cost_types("3 B G ExileFromGrave<1/Creature>")
        assert {"exile", "exilegrave"} <= result

    def test_taptype_convoke_style(self):
        result = ForgeFeatureContext._parse_cost_types("tapXType<1/Creature>")
        assert "taptype" in result

    def test_taptype_tribal(self):
        # Convoke/improvise-style tribal tap-cost
        result = ForgeFeatureContext._parse_cost_types("tapXType<2/Ape>")
        assert "taptype" in result

    def test_return_bounce_cost(self):
        # Return-to-hand-as-cost pattern (phoenix-like recursion)
        result = ForgeFeatureContext._parse_cost_types("2 Return<1/CARDNAME>")
        assert "return" in result

    def test_return_with_sacrifice(self):
        # Combined cost — Return and Sac both apply
        result = ForgeFeatureContext._parse_cost_types("Sac<1/Creature> Return<1/CARDNAME>")
        assert {"sacrifice", "return"} <= result

    def test_return_substring_anchored(self):
        # Unanchored 'Return' must NOT match other verbs that contain it.
        # We can't easily construct a realistic example, but verify the
        # anchoring by checking a plain word. This locks in the '<' anchor.
        result = ForgeFeatureContext._parse_cost_types("ReturnToHandNotACost")
        assert "return" not in result

    # === Edge cases ===

    def test_empty_string(self):
        assert ForgeFeatureContext._parse_cost_types("") == set()

    def test_combined_real_cost(self):
        # Full real-world combined cost: tap + sacrifice + subcounter
        result = ForgeFeatureContext._parse_cost_types("T Sac<1/CARDNAME> SubCounter<1/P1P1>")
        assert result == {"tap", "sacrifice", "subcounter"}
