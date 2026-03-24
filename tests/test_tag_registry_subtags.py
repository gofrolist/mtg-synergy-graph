"""Verify tag_registry.json has sub-tags and no parent tags."""
import json
import pytest

PARENT_TAGS = [
    "creature-pump", "creature-board", "creature-etb",
    "combat-events", "token-generation", "evasion-grant",
]

SUB_TAGS = {
    "pump-lord", "pump-anthem", "pump-combat", "pump-self",
    "board-tokens", "board-tribal", "board-go-wide", "board-generic",
    "etb-value", "etb-tokens", "etb-tribal",
    "combat-attack", "combat-damage", "combat-block",
    "tokens-creature", "tokens-artifact", "tokens-tribal",
    "evasion-flying", "evasion-unblockable", "evasion-menace",
}

@pytest.fixture
def registry():
    with open("tag_registry.json") as f:
        return json.load(f)

def test_no_parent_tags(registry):
    for tag in PARENT_TAGS:
        assert tag not in registry["tags"], f"Parent tag {tag} should be removed"

def test_all_subtags_present(registry):
    for tag in SUB_TAGS:
        assert tag in registry["tags"], f"Sub-tag {tag} missing"

def test_subtags_have_definitions(registry):
    for tag in SUB_TAGS:
        entry = registry["tags"][tag]
        assert entry.get("definition"), f"Sub-tag {tag} missing definition"
        assert entry.get("kind") in ("provides", "wants"), f"Sub-tag {tag} missing kind"
