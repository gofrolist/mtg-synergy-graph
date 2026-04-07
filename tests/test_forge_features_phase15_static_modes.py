"""Phase 1.5 sub-project B — Static Mode$ extraction tests.

Covers:
1. forge_import.py extract_ability_fields S: branch (column extraction)
2. forge_features.py profile loading + auto-tag synthesis (next task)
3. mechanics_vectors.py synthetic event tuple (next task)

All test inputs are verbatim samples from data/tags.db forge_abilities
or data/forge/forge-gui/res/cardsfolder/ files. Do NOT invent S: line
strings — sample them from real corpus.
"""

from __future__ import annotations

from mtg_synergy_train.parse.forge_import import extract_ability_fields


class TestStaticModeColumnExtraction:
    """forge_import.py extract_ability_fields S: branch — column extraction."""

    def test_extracts_continuous_mode(self):
        # Verbatim format: S:Mode$ Continuous | Affected$ ... | AddPower$ ...
        line = "Mode$ Continuous | Affected$ Creature.YouCtrl | AddPower$ 1 | AddToughness$ 1 | Description$ Creatures you control get +1/+1."
        result = extract_ability_fields(line, "S", svars={})
        assert result["static_mode"] == "Continuous"

    def test_extracts_panharmonicon_mode(self):
        line = "Mode$ Panharmonicon | ValidCard$ Permanent.YouCtrl | Description$ If a permanent entering causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time."
        result = extract_ability_fields(line, "S", svars={})
        assert result["static_mode"] == "Panharmonicon"

    def test_extracts_reducecost_mode(self):
        line = "Mode$ ReduceCost | ValidCard$ Artifact.YouCtrl | Type$ Spell | Amount$ 1 | Description$ Artifact spells you cast cost {1} less to cast."
        result = extract_ability_fields(line, "S", svars={})
        assert result["static_mode"] == "ReduceCost"

    def test_non_s_lines_have_null_static_mode(self):
        # A: line — must NOT set static_mode
        a_line = "AB$ Tap | Cost$ T | Defined$ Self | SpellDescription$ Tap CARDNAME."
        a_result = extract_ability_fields(a_line, "A", svars={})
        assert a_result.get("static_mode") is None

        # T: line — must NOT set static_mode
        t_line = "Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | ValidCard$ Card.Self | Execute$ TrigPump"
        t_result = extract_ability_fields(t_line, "T", svars={})
        assert t_result.get("static_mode") is None

        # R: line — must NOT set static_mode
        r_line = "Event$ Moved | ValidCard$ Card.Self | Destination$ Graveyard | ReplaceWith$ Exile"
        r_result = extract_ability_fields(r_line, "R", svars={})
        assert r_result.get("static_mode") is None

    def test_s_line_no_mode_field_returns_null(self):
        # Defensive: S: line without Mode$ field — should return None, not crash
        line = "SP$ Effect | Description$ Malformed S: line for testing."
        result = extract_ability_fields(line, "S", svars={})
        assert result.get("static_mode") is None
