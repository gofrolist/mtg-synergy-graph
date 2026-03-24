"""Tests for tag reclassification pipeline."""
import json
import pytest


def test_build_reclassify_prompt_creature_pump():
    from reclassify_tags import build_reclassify_prompt
    cards = [
        {"name": "Goblin King", "oracle_text": "Other Goblins get +1/+1 and mountainwalk.", "type_line": "Creature — Goblin"},
    ]
    prompt = build_reclassify_prompt("creature-pump", cards)
    assert "pump-lord" in prompt
    assert "pump-anthem" in prompt
    assert "pump-combat" in prompt
    assert "pump-self" in prompt
    assert "Goblin King" in prompt


def test_build_reclassify_prompt_creature_board():
    from reclassify_tags import build_reclassify_prompt
    cards = [
        {"name": "Craterhoof Behemoth", "oracle_text": "When Craterhoof Behemoth enters, creatures you control gain trample and get +X/+X until end of turn, where X is the number of creatures you control.", "type_line": "Creature — Beast"},
    ]
    prompt = build_reclassify_prompt("creature-board", cards)
    assert "board-tokens" in prompt
    assert "board-tribal" in prompt
    assert "board-go-wide" in prompt
    assert "board-generic" in prompt


def test_parse_reclassify_results_valid():
    from reclassify_tags import parse_reclassify_results
    raw = json.dumps({"cards": [
        {"name": "Goblin King", "sub_tag": "pump-lord"},
        {"name": "Glorious Anthem", "sub_tag": "pump-anthem"},
    ]})
    results = parse_reclassify_results(raw, "creature-pump", 2)
    assert len(results) == 2
    assert results[0]["sub_tag"] == "pump-lord"
    assert results[1]["sub_tag"] == "pump-anthem"


def test_parse_reclassify_rejects_invalid_subtag():
    from reclassify_tags import parse_reclassify_results, SUBTAG_MAP
    raw = json.dumps({"cards": [
        {"name": "Goblin King", "sub_tag": "pump-lord"},
        {"name": "Bad Card", "sub_tag": "invalid-tag"},
    ]})
    results = parse_reclassify_results(raw, "creature-pump", 2)
    # Invalid sub_tag should be replaced with a valid fallback
    assert results[1]["sub_tag"] in SUBTAG_MAP["creature-pump"]


def test_valid_subtags_complete():
    """All parent tags have defined valid sub-tags."""
    from reclassify_tags import SUBTAG_MAP
    expected = {
        "creature-pump": {"pump-lord", "pump-anthem", "pump-combat", "pump-self"},
        "creature-board": {"board-tokens", "board-tribal", "board-go-wide", "board-generic"},
        "creature-etb": {"etb-value", "etb-tokens", "etb-tribal"},
        "combat-events": {"combat-attack", "combat-damage", "combat-block"},
        "token-generation": {"tokens-creature", "tokens-artifact", "tokens-tribal"},
        "evasion-grant": {"evasion-flying", "evasion-unblockable", "evasion-menace"},
    }
    for parent, exp_subs in expected.items():
        assert set(SUBTAG_MAP[parent].keys()) == exp_subs, f"Mismatch for {parent}"
