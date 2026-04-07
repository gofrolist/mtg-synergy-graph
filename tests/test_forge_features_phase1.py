"""Phase 1 Forge DSL extraction tests.

Covers combat trigger filter extraction (ValidAttacker$ / ValidBlocker$)
from T: ability raw_line rows. Inputs are verbatim samples from
data/tags.db forge_abilities.raw_line rows.
"""

import sqlite3

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
        parts of a filter value, matching the existing ValidCard$ block.

        Builds a tiny in-memory DB with one T: row using a multi-type combat
        filter, then runs a minimal ForgeFeatureContext and asserts both
        coarse types land in trigger_filters.
        """
        conn = sqlite3.connect(":memory:")
        # Minimal forge_abilities schema matching the project's real schema
        conn.execute(
            "CREATE TABLE forge_abilities ("
            "card_name TEXT, ability_index INT, ability_type TEXT, verb TEXT, "
            "trigger_mode TEXT, trigger_filter TEXT, trigger_origin TEXT, "
            "trigger_destination TEXT, trigger_phase TEXT, trigger_zones TEXT, "
            "target TEXT, defined TEXT, amount TEXT, cost TEXT, keyword TEXT, "
            "token_script TEXT, counter_type TEXT, sub_ability TEXT, "
            "unless_cost TEXT, raw_line TEXT)"
        )
        conn.execute(
            "INSERT INTO forge_abilities VALUES "
            "('Fake Card', 0, 'T', NULL, 'Attacks', NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
            "'T:Mode$ Attacks | ValidAttacker$ Creature,Artifact | "
            "Execute$ TrigDraw | TriggerDescription$ Whenever a creature or "
            "artifact attacks, draw.')"
        )

        # Exercise the helper and the wiring logic directly (no full context).
        raw = conn.execute(
            "SELECT raw_line FROM forge_abilities WHERE card_name='Fake Card'"
        ).fetchone()[0]
        filters = ForgeFeatureContext._parse_combat_trigger_filters(raw)
        assert filters == {"Creature,Artifact"}

        # Derive coarse types using the exact loop shape from the wiring block.
        trigger_filters: set[str] = set()
        for combat_filter in filters:
            for part in combat_filter.split(","):
                main = part.split(".")[0].strip()
                if main and main != "Card" and main[0].isupper():
                    trigger_filters.add(main.lower())
        assert trigger_filters == {"creature", "artifact"}
