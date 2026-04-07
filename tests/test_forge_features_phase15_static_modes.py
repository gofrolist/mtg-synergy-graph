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

import sqlite3

import pytest

from mtg_synergy.recommend.forge_features import ForgeFeatureContext
from mtg_synergy_train.parse.forge_import import extract_ability_fields


# Phase 1.5 sub-project B: forge_abilities schema (21 cols, in DB column order)
_FORGE_ABILITIES_COLS = (
    "card_name TEXT NOT NULL, "
    "ability_index INTEGER NOT NULL, "
    "ability_type TEXT NOT NULL, "
    "verb TEXT, "
    "trigger_mode TEXT, "
    "trigger_filter TEXT, "
    "trigger_origin TEXT, "
    "trigger_destination TEXT, "
    "trigger_phase TEXT, "
    "trigger_zones TEXT, "
    "target TEXT, "
    "defined TEXT, "
    "amount TEXT, "
    "cost TEXT, "
    "keyword TEXT, "
    "token_script TEXT, "
    "counter_type TEXT, "
    "sub_ability TEXT, "
    "unless_cost TEXT, "
    "static_mode TEXT, "
    "raw_line TEXT NOT NULL, "
    "PRIMARY KEY (card_name, ability_index)"
)

_FORGE_ABILITIES_COLS_NO_STATIC_MODE = (
    "card_name TEXT NOT NULL, "
    "ability_index INTEGER NOT NULL, "
    "ability_type TEXT NOT NULL, "
    "verb TEXT, "
    "trigger_mode TEXT, "
    "trigger_filter TEXT, "
    "trigger_origin TEXT, "
    "trigger_destination TEXT, "
    "trigger_phase TEXT, "
    "trigger_zones TEXT, "
    "target TEXT, "
    "defined TEXT, "
    "amount TEXT, "
    "cost TEXT, "
    "keyword TEXT, "
    "token_script TEXT, "
    "counter_type TEXT, "
    "sub_ability TEXT, "
    "unless_cost TEXT, "
    "raw_line TEXT NOT NULL, "
    "PRIMARY KEY (card_name, ability_index)"
)


class _FakeCardProvider:
    """Minimal CardProvider stub returning preset type_lines."""

    def __init__(self, type_lines: dict):
        self._type_lines = type_lines

    def get_type_lines(self) -> dict:
        return dict(self._type_lines)

    def get_color_identities(self) -> dict:
        return {oid: frozenset() for oid in self._type_lines}

    def get_cmc(self) -> dict:
        return {oid: 0.0 for oid in self._type_lines}


def _build_test_db_with_static_mode(include_static_mode: bool = True,
                                    extra_rows: list | None = None) -> sqlite3.Connection:
    """Build an in-memory sqlite DB mirroring the 21-column forge_abilities schema.

    Inserts a single Panharmonicon S: row mapped to oracle_id "oid-pan".
    Optionally inserts extra rows (e.g., A: rows) for cross-pollution tests.
    Tables created: cards (minimal), forge_abilities, forge_name_map.
    """
    conn = sqlite3.connect(":memory:")
    cols = _FORGE_ABILITIES_COLS if include_static_mode else _FORGE_ABILITIES_COLS_NO_STATIC_MODE
    conn.execute(f"CREATE TABLE forge_abilities ({cols})")
    conn.execute(
        "CREATE TABLE forge_name_map (forge_name TEXT PRIMARY KEY, oracle_id TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE cards (oracle_id TEXT PRIMARY KEY, name TEXT, type_line TEXT, "
        "color_identity TEXT, cmc REAL)"
    )
    # Empty stub tables that ForgeFeatureContext.__init__ touches
    conn.execute(
        "CREATE TABLE forge_deck_tags (card_name TEXT NOT NULL, tag_type TEXT NOT NULL, "
        "tag TEXT NOT NULL, PRIMARY KEY (card_name, tag_type, tag))"
    )
    conn.execute(
        "CREATE TABLE edhrec_card_synergy (commander_slug TEXT NOT NULL, "
        "card_name TEXT NOT NULL, synergy REAL, inclusion INTEGER, "
        "num_decks INTEGER, section TEXT)"
    )
    conn.execute(
        "CREATE TABLE interaction_edges (source_id TEXT NOT NULL, target_id TEXT NOT NULL, "
        "edge_type TEXT NOT NULL, ability_a INTEGER NOT NULL, ability_b INTEGER NOT NULL, "
        "strength REAL NOT NULL, detail TEXT NOT NULL, filter_precision TEXT, event TEXT)"
    )

    conn.execute(
        "INSERT INTO forge_name_map (forge_name, oracle_id) VALUES (?, ?)",
        ("Panharmonicon", "oid-pan"),
    )
    conn.execute(
        "INSERT INTO cards (oracle_id, name, type_line, color_identity, cmc) VALUES (?, ?, ?, ?, ?)",
        ("oid-pan", "Panharmonicon", "Artifact", "", 4.0),
    )

    pan_raw = ("Mode$ Panharmonicon | ValidCard$ Permanent.YouCtrl | "
               "Description$ If a permanent entering causes a triggered ability of a "
               "permanent you control to trigger, that ability triggers an additional time.")
    if include_static_mode:
        conn.execute(
            "INSERT INTO forge_abilities (card_name, ability_index, ability_type, "
            "raw_line, static_mode) VALUES (?, ?, ?, ?, ?)",
            ("Panharmonicon", 0, "S", pan_raw, "Panharmonicon"),
        )
    else:
        conn.execute(
            "INSERT INTO forge_abilities (card_name, ability_index, ability_type, "
            "raw_line) VALUES (?, ?, ?, ?)",
            ("Panharmonicon", 0, "S", pan_raw),
        )

    for row in (extra_rows or []):
        conn.execute(
            "INSERT INTO forge_name_map (forge_name, oracle_id) VALUES (?, ?)",
            (row["card_name"], row["oracle_id"]),
        )
        conn.execute(
            "INSERT INTO cards (oracle_id, name, type_line, color_identity, cmc) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["oracle_id"], row["card_name"], row.get("type_line", "Artifact"),
             "", row.get("cmc", 0.0)),
        )
        if include_static_mode:
            conn.execute(
                "INSERT INTO forge_abilities (card_name, ability_index, ability_type, "
                "verb, raw_line, static_mode) VALUES (?, ?, ?, ?, ?, ?)",
                (row["card_name"], 0, row["ability_type"], row.get("verb"),
                 row["raw_line"], row.get("static_mode")),
            )
        else:
            conn.execute(
                "INSERT INTO forge_abilities (card_name, ability_index, ability_type, "
                "verb, raw_line) VALUES (?, ?, ?, ?, ?)",
                (row["card_name"], 0, row["ability_type"], row.get("verb"), row["raw_line"]),
            )

    conn.commit()
    return conn


def _make_ctx(conn: sqlite3.Connection) -> ForgeFeatureContext:
    """Build a ForgeFeatureContext bound to the in-memory test DB."""
    type_lines = {row[0]: row[1] for row in conn.execute("SELECT oracle_id, type_line FROM cards")}
    provider = _FakeCardProvider(type_lines)
    return ForgeFeatureContext(conn, card_provider=provider)


class TestStaticModeProfileLoading:
    """forge_features.py — Phase 1.5 sub-project B static_mode wiring."""

    def test_static_mode_populates_profile_set(self):
        conn = _build_test_db_with_static_mode()
        ctx = _make_ctx(conn)
        profile = ctx._forge_profiles["oid-pan"]
        assert "Panharmonicon" in profile["static_modes"]

    def test_static_mode_compaction_to_frozenset(self):
        conn = _build_test_db_with_static_mode()
        ctx = _make_ctx(conn)
        profile = ctx._forge_profiles["oid-pan"]
        assert isinstance(profile["static_modes"], frozenset)

    def test_schema_check_raises_on_missing_static_mode_column(self):
        conn = _build_test_db_with_static_mode(include_static_mode=False)
        type_lines = {row[0]: row[1] for row in conn.execute("SELECT oracle_id, type_line FROM cards")}
        provider = _FakeCardProvider(type_lines)
        with pytest.raises(RuntimeError, match=r"static_mode.*Phase 1\.5"):
            ForgeFeatureContext(conn, card_provider=provider)

    def test_schema_check_raises_on_missing_forge_abilities_table(self):
        """Distinct error message when forge_abilities table doesn't exist at all
        (e.g., a fresh DB before any import). The 'missing column' message would
        mislead the developer into running --import expecting the existing table
        to be re-populated, when actually the table needs to be created from scratch."""
        conn = sqlite3.connect(":memory:")
        # Create the OTHER tables ForgeFeatureContext touches, but NOT forge_abilities
        conn.execute(
            "CREATE TABLE cards (oracle_id TEXT PRIMARY KEY, name TEXT, type_line TEXT, "
            "color_identity TEXT, cmc REAL)"
        )
        conn.execute(
            "CREATE TABLE forge_name_map (forge_name TEXT PRIMARY KEY, oracle_id TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE forge_deck_tags (card_name TEXT NOT NULL, tag_type TEXT NOT NULL, "
            "tag TEXT NOT NULL, PRIMARY KEY (card_name, tag_type, tag))"
        )
        conn.execute(
            "CREATE TABLE edhrec_card_synergy (commander_slug TEXT NOT NULL, "
            "card_name TEXT NOT NULL, synergy REAL, inclusion INTEGER, "
            "num_decks INTEGER, section TEXT)"
        )
        conn.execute(
            "CREATE TABLE interaction_edges (source_id TEXT NOT NULL, target_id TEXT NOT NULL, "
            "edge_type TEXT NOT NULL, ability_a INTEGER NOT NULL, ability_b INTEGER NOT NULL, "
            "strength REAL NOT NULL, detail TEXT NOT NULL, filter_precision TEXT, event TEXT)"
        )
        conn.commit()
        provider = _FakeCardProvider({})
        with pytest.raises(RuntimeError, match=r"forge_abilities table not found"):
            ForgeFeatureContext(conn, card_provider=provider)

    def test_non_s_rows_dont_pollute_static_modes(self):
        # Add an A: row for a separate card; static_modes must remain empty for it,
        # while verbs must contain "Tap".
        extra = [{
            "card_name": "TapCard",
            "oracle_id": "oid-tap",
            "ability_type": "A",
            "verb": "Tap",
            "raw_line": "AB$ Tap | Cost$ T | Defined$ Self | SpellDescription$ Tap CARDNAME.",
            "static_mode": None,
        }]
        conn = _build_test_db_with_static_mode(extra_rows=extra)
        ctx = _make_ctx(conn)

        pan = ctx._forge_profiles["oid-pan"]
        tap = ctx._forge_profiles["oid-tap"]
        assert "Panharmonicon" in pan["static_modes"]
        assert "Tap" not in pan["static_modes"]
        assert tap["static_modes"] == frozenset()
        assert "Tap" in tap["verbs"]


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

        # T: line — must NOT set static_mode. Mode$ on a T: line is the
        # trigger condition (e.g., ChangesZone), and lands in trigger_mode.
        # Cross-column assertion proves the T: and S: branches are isolated:
        # if a future change broke prefix dispatch and T: rows fell into the
        # S: branch, static_mode would absorb "ChangesZone" and trigger_mode
        # would be None — both halves of the assertion would fail.
        t_line = "Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | ValidCard$ Card.Self | Execute$ TrigPump"
        t_result = extract_ability_fields(t_line, "T", svars={})
        assert t_result.get("static_mode") is None
        assert t_result.get("trigger_mode") == "ChangesZone"

        # R: line — must NOT set static_mode
        r_line = "Event$ Moved | ValidCard$ Card.Self | Destination$ Graveyard | ReplaceWith$ Exile"
        r_result = extract_ability_fields(r_line, "R", svars={})
        assert r_result.get("static_mode") is None

    def test_s_line_no_mode_field_returns_null(self):
        # Defensive: S: line with SP$ but no Mode$ field. SP$ was previously
        # stored as verb for S: rows (the verb-pollution bug). Verify it is
        # now ignored for both verb and static_mode — neither field is
        # populated when only SP$ is present.
        line = "SP$ Effect | Description$ Malformed S: line for testing."
        result = extract_ability_fields(line, "S", svars={})
        assert result.get("static_mode") is None
        assert result.get("verb") is None
