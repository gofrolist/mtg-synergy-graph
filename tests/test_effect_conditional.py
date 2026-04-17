"""Tests for effect_conditional detection on trigger ports.

A trigger is 'effect-conditional' when its execute SVar chain contains
a runtime condition (ConditionCheckSVar, ConditionDefined, etc.) that
gates whether the payoff effect actually happens. Selvala fires her
draw trigger on any creature ETB but only draws if the new creature
has greater power than all others — the trigger matches broadly but
the effect rarely fires.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder
from mtg_synergy_graph.ports import extract_trigger_ports

FIXTURES = Path(__file__).parent / "fixtures"


class TestEffectConditionalExtraction:
    """Unit tests using synthetic parsed trigger dicts."""

    def test_unconditional_trigger(self):
        """Korvold-style: Sacrificed trigger with straightforward Execute."""
        parsed = {
            "Mode": "Sacrificed",
            "ValidCard": "Permanent.YouCtrl",
            "Execute": "TrigPutCounter",
        }
        svars = {
            "TrigPutCounter": "DB$ PutCounter | Defined$ Self | CounterType$ P1P1 | CounterNum$ 1",
        }
        ports = extract_trigger_ports("Korvold", parsed, svars)
        trigger = next(p for p in ports if p["port_type"] == "trigger")
        assert trigger.get("effect_conditional", False) is False

    def test_condition_check_svar(self):
        """Selvala-style: ETB trigger whose execute has ConditionCheckSVar."""
        parsed = {
            "Mode": "ChangesZone",
            "Origin": "Any",
            "Destination": "Battlefield",
            "ValidCard": "Creature.Other",
            "Execute": "TrigDraw",
        }
        svars = {
            "TrigDraw": (
                "DB$ Draw | Defined$ TriggeredCardController | NumCards$ 1 "
                "| ConditionCheckSVar$ Z | ConditionSVarCompare$ EQY"
            ),
            "Z": "Count$Valid Creature.triggerCard$CardPower",
            "Y": "Count$Valid Creature.YouCtrl$GreatestCardPower",
        }
        ports = extract_trigger_ports("Selvala", parsed, svars)
        trigger = next(p for p in ports if p["port_type"] == "trigger")
        assert trigger["effect_conditional"] is True

    def test_condition_present(self):
        """ConditionPresent$ also counts as a runtime gate."""
        parsed = {
            "Mode": "Sacrificed",
            "ValidCard": "Creature.YouCtrl",
            "Execute": "TrigCond",
        }
        svars = {
            "TrigCond": "DB$ GainLife | LifeAmount$ 1 | ConditionPresent$ Creature.YouCtrl",
        }
        ports = extract_trigger_ports("Hypothetical", parsed, svars)
        trigger = next(p for p in ports if p["port_type"] == "trigger")
        assert trigger["effect_conditional"] is True

    def test_check_svar_alone(self):
        """A standalone CheckSVar in the execute also counts."""
        parsed = {
            "Mode": "ChangesZone",
            "Origin": "Any",
            "Destination": "Battlefield",
            "ValidCard": "Creature.YouCtrl",
            "Execute": "TrigMeren",
        }
        svars = {
            "TrigMeren": (
                "DB$ ChangeZone | Origin$ Graveyard | Destination$ Battlefield "
                "| ValidTgts$ Creature.YouOwn | CheckSVar$ X | SVarCompare$ GEY"
            ),
            "X": "Count$YourCountersExperience",
            "Y": "ValidHand Creature.cmcLEX",
        }
        ports = extract_trigger_ports("Meren", parsed, svars)
        trigger = next(p for p in ports if p["port_type"] == "trigger")
        assert trigger["effect_conditional"] is True

    def test_chained_svar_condition(self):
        """Condition may sit one SVar-chain step deeper (on a SubAbility)."""
        parsed = {
            "Mode": "ChangesZone",
            "Origin": "Any",
            "Destination": "Battlefield",
            "ValidCard": "Creature.YouCtrl",
            "Execute": "TrigA",
        }
        svars = {
            "TrigA": "DB$ Draw | NumCards$ 1 | SubAbility$ DBB",
            "DBB": "DB$ GainLife | LifeAmount$ 2 | ConditionCheckSVar$ X | ConditionSVarCompare$ GE1",
            "X": "Count$YourLife",
        }
        ports = extract_trigger_ports("Hypothetical", parsed, svars)
        trigger = next(p for p in ports if p["port_type"] == "trigger")
        assert trigger["effect_conditional"] is True


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("effect_cond") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    yield conn
    conn.close()


class TestEffectConditionalInDB:
    """Integration: verify flag round-trips through the importer + schema."""

    def test_column_exists(self, populated_db):
        cols = populated_db.execute("PRAGMA table_info(card_ports)").fetchall()
        names = [c["name"] if hasattr(c, "keys") else c[1] for c in cols]
        assert "effect_conditional" in names


#: Skip the end-to-end scoring tests below when data/synergy.db is not
#: available — CI doesn't ship the Forge cardsfolder needed to build it.
#: The structural tests higher up this file don't depend on the DB and
#: continue to run in CI.
_requires_full_db = pytest.mark.skipif(
    not Path("data/synergy.db").exists(),
    reason="requires data/synergy.db (run scripts/import_cardsfolder.py)",
)


class TestEffectConditionalScoring:
    """Integration: trigger_effect matches from effect-conditional triggers
    get a ``:cond`` suffix on filter_group, which the IDF computer uses to
    halve the weight."""

    @_requires_full_db
    def test_trigger_effect_gets_cond_suffix(self):
        """Selvala's ETB trigger is effect_conditional; the matches
        produced via the trigger_effect rule should carry ':cond' in
        filter_group so the scorer can dampen them."""
        import sqlite3

        from mtg_synergy_graph.complement_rules import find_all_complements

        with sqlite3.connect("data/synergy.db") as conn:
            conn.row_factory = sqlite3.Row
            comps = find_all_complements(conn, ["Selvala, Heart of the Wilds"])
        trigger_effect_cond = [c for c in comps if c.rule_id == "trigger_effect" and ":cond" in (c.filter_group or "")]
        assert len(trigger_effect_cond) > 0, "expected Selvala's ETB-trigger x creature matches to carry ':cond' suffix"

    @_requires_full_db
    def test_korvold_no_cond_suffix(self):
        """Korvold's Sacrificed trigger is not effect_conditional; matches
        must NOT carry ':cond' suffix."""
        import sqlite3

        from mtg_synergy_graph.complement_rules import find_all_complements

        with sqlite3.connect("data/synergy.db") as conn:
            conn.row_factory = sqlite3.Row
            comps = find_all_complements(conn, ["Korvold, Fae-Cursed King"])
        cond_matches = [c for c in comps if c.rule_id == "trigger_effect" and ":cond" in (c.filter_group or "")]
        assert cond_matches == []

    def test_idf_halved_for_cond_group(self):
        """The IDF weight for a ':cond' filter_group must be 0.5× the
        weight computed for the same (rule_id, cmdr_ev, cand_ev) without
        the suffix."""
        from mtg_synergy_graph.complement_rules.core import PortComplement
        from mtg_synergy_graph.universal_scorer import _compute_idf_weights

        # Two candidates, same rule/event, one with :cond, one without.
        # Without :cond, single-candidate group gives IDF = 1/log2(2) = 1.0
        # With :cond, IDF = 1.0 * 0.5 = 0.5
        complements = [
            PortComplement(
                rule_id="trigger_effect",
                direction="synergy",
                candidate="A",
                cmdr_event="Sacrificed",
                cand_event="Sacrifice",
                filter_group="",
            ),
            PortComplement(
                rule_id="trigger_effect",
                direction="synergy",
                candidate="B",
                cmdr_event="Sacrificed",
                cand_event="Sacrifice",
                filter_group=":cond",
            ),
        ]
        weights = _compute_idf_weights(complements)
        plain_key = ("trigger_effect", "Sacrificed", "Sacrifice", "")
        cond_key = ("trigger_effect", "Sacrificed", "Sacrifice", ":cond")
        assert weights[plain_key] == pytest.approx(1.0)
        assert weights[cond_key] == pytest.approx(0.5)
