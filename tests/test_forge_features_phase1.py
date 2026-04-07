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
