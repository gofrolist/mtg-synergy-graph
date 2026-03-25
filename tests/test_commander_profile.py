"""Tests for commander archetype inference."""
import sqlite3
import json
import pytest
from mtg_synergy.recommend.commander_profile import (
    CommanderProfile, infer_profile, ensure_profile_schema
)


def test_profile_from_oracle_text_tokens():
    """Krenko's text mentions creating tokens -> detected as 'tokens' strategy."""
    profile = infer_profile(
        oracle_text="{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        type_line="Legendary Creature \u2014 Goblin Warrior",
        parsed_events_produced={"creature_enters"},
        parsed_events_consumed=set(),
        parsed_effects={"create"},
    )
    assert isinstance(profile, CommanderProfile)
    assert "tokens" in profile.strategies or "go-wide" in profile.strategies
    assert profile.tribal_type == "Goblin"


def test_profile_from_oracle_text_aristocrats():
    """Syr Konrad triggers on creature dying -> aristocrats."""
    profile = infer_profile(
        oracle_text="Whenever a creature dies, or a creature card is put into a graveyard from anywhere other than the battlefield, or a creature card leaves your graveyard, Syr Konrad, the Grim deals 1 damage to each opponent.",
        type_line="Legendary Creature \u2014 Human Knight",
        parsed_events_produced=set(),
        parsed_events_consumed={"dies"},
        parsed_effects={"deal_damage"},
    )
    assert "aristocrats" in profile.strategies


def test_profile_tribal_from_type_line():
    """Type line with creature subtypes -> tribal detection."""
    profile = infer_profile(
        oracle_text="Some ability text.",
        type_line="Legendary Creature \u2014 Elf Druid",
        parsed_events_produced=set(),
        parsed_events_consumed=set(),
        parsed_effects=set(),
    )
    assert profile.tribal_type in ("Elf", "Druid")


def test_profile_no_false_positives():
    """Generic commander text shouldn't match every strategy."""
    profile = infer_profile(
        oracle_text="Flying, vigilance",
        type_line="Legendary Creature \u2014 Angel",
        parsed_events_produced=set(),
        parsed_events_consumed=set(),
        parsed_effects=set(),
    )
    assert len(profile.strategies) <= 2


def test_profile_db_roundtrip(tmp_db):
    """Profile can be stored and retrieved from DB."""
    conn = sqlite3.connect(tmp_db)
    ensure_profile_schema(conn)
    profile = CommanderProfile(
        strategies={"tokens", "tribal-goblin"},
        tribal_type="Goblin",
        key_events_produced={"creature_enters"},
        key_events_consumed=set(),
        key_effects={"create"},
    )
    from mtg_synergy.recommend.commander_profile import save_profile, load_profile
    save_profile(conn, "krenko-001", profile)
    loaded = load_profile(conn, "krenko-001")
    assert loaded is not None
    assert loaded.strategies == {"tokens", "tribal-goblin"}
    assert loaded.tribal_type == "Goblin"
    conn.close()
