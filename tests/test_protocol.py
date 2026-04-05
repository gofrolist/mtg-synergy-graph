"""Tests for CardProvider protocol and SqliteCardProvider implementation."""
import json
import sqlite3

import pytest

from mtg_synergy.protocol import SqliteCardProvider


@pytest.fixture
def provider_db(tmp_path):
    """Create a minimal SQLite DB with cards for SqliteCardProvider testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE cards (
            oracle_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type_line TEXT,
            mana_cost TEXT,
            cmc REAL,
            color_identity TEXT,
            legal_commander INTEGER DEFAULT 1,
            edhrec_rank INTEGER
        );
    """)
    cards = [
        ("oid-krenko", "Krenko, Mob Boss", "Legendary Creature — Goblin Warrior",
         "{2}{R}{R}", 4.0, '["R"]', 1),
        ("oid-bolt", "Lightning Bolt", "Instant",
         "{R}", 1.0, '["R"]', 1),
        ("oid-sol", "Sol Ring", "Artifact",
         "{1}", 1.0, '[]', 1),
        ("oid-kozilek", "Kozilek, the Great Distortion",
         "Legendary Creature — Eldrazi", "{8}{C}{C}", 10.0, '["C"]', 1),
        ("oid-token", "Goblin Token", "Token Creature — Goblin",
         "", 0.0, '["R"]', 0),
        ("oid-swords", "Swords to Plowshares", "Instant",
         "{W}", 1.0, '["W"]', 1),
    ]
    conn.executemany(
        "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, NULL)", cards
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def provider(provider_db):
    return SqliteCardProvider(provider_db)


class TestGetTypeLines:
    def test_returns_all_cards_with_type_lines(self, provider):
        result = provider.get_type_lines()
        assert "oid-krenko" in result
        assert result["oid-krenko"] == "Legendary Creature — Goblin Warrior"

    def test_excludes_null_type_lines(self, provider, provider_db):
        provider_db.execute(
            "INSERT INTO cards (oracle_id, name, type_line) "
            "VALUES ('oid-null', 'Null Card', NULL)"
        )
        result = provider.get_type_lines()
        assert "oid-null" not in result


class TestGetNameToOid:
    def test_returns_lowercase_mapping(self, provider):
        result = provider.get_name_to_oid()
        assert result["krenko, mob boss"] == "oid-krenko"
        assert result["lightning bolt"] == "oid-bolt"

    def test_case_insensitive_keys(self, provider):
        result = provider.get_name_to_oid()
        assert "Krenko, Mob Boss" not in result
        assert "krenko, mob boss" in result


class TestGetColorLegal:
    def test_filters_by_color_subset(self, provider):
        result = provider.get_color_legal({"R"})
        names = {c["name"] for c in result}
        assert "Krenko, Mob Boss" in names
        assert "Lightning Bolt" in names
        assert "Sol Ring" in names  # empty CI is subset of any set
        assert "Swords to Plowshares" not in names  # W not in {R}

    def test_excludes_tokens(self, provider):
        result = provider.get_color_legal({"R"})
        names = {c["name"] for c in result}
        assert "Goblin Token" not in names

    def test_excludes_specified_oids(self, provider):
        result = provider.get_color_legal({"R"}, exclude_oids={"oid-krenko"})
        names = {c["name"] for c in result}
        assert "Krenko, Mob Boss" not in names
        assert "Lightning Bolt" in names

    def test_colorless_identity(self, provider):
        result = provider.get_color_legal({"C"})
        names = {c["name"] for c in result}
        assert "Sol Ring" in names  # empty CI <= {"C"}
        assert "Kozilek, the Great Distortion" in names  # {"C"} <= {"C"}

    def test_returns_color_identity_as_set(self, provider):
        result = provider.get_color_legal({"R"})
        krenko = next(c for c in result if c["name"] == "Krenko, Mob Boss")
        assert krenko["color_identity"] == {"R"}
        sol = next(c for c in result if c["name"] == "Sol Ring")
        assert sol["color_identity"] == set()


class TestGetCommanders:
    def test_finds_commander_case_insensitive(self, provider):
        result = provider.get_commanders(["krenko, mob boss"])
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "Krenko, Mob Boss"
        assert result[0]["oracle_id"] == "oid-krenko"
        assert result[0]["color_identity"] == {"R"}

    def test_returns_none_for_missing_commander(self, provider):
        result = provider.get_commanders(["Nonexistent Commander"])
        assert result is None

    def test_multiple_commanders(self, provider, provider_db):
        # Add a second legendary creature
        provider_db.execute(
            "INSERT INTO cards (oracle_id, name, type_line, color_identity, legal_commander) "
            "VALUES ('oid-partner', 'Partner Commander', "
            "'Legendary Creature — Human', '[\"W\"]', 1)"
        )
        result = provider.get_commanders(
            ["Krenko, Mob Boss", "Partner Commander"]
        )
        assert result is not None
        assert len(result) == 2
