"""Tribal / replacement-stack batch migration (plan 003 Unit 8).

Locks the shape of the 15 migrated rules (13 keyword-tribals + 2
replacement-stacks) and the basic emission invariants. End-to-end
bitwise identity against the pinned golden fixture is held by
``bench.py audit --expect-identity`` at CI time — these tests are
the focused unit-level guards that fail fast if the JSON drifts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.complement_rules import find_all_complements
from mtg_synergy_graph.complement_rules._interpreter_cache import clear_interpreter_cache
from mtg_synergy_graph.complement_rules.registry import DECLARATIVE_RULE_IDS, RULE_GATES
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import clear_ports_cache


@pytest.fixture(autouse=True)
def _reset_ports_cache() -> None:
    """``graph_engine._ports_cache`` and the
    ``complement_rules._interpreter_cache`` are both keyed on
    ``id(conn)``; CPython reuses ids after a connection is GC'd, so
    successive parametrized tests can see a stale cached entry from a
    previous test's commander / rules table. Always clear before each
    case."""
    clear_ports_cache()
    clear_interpreter_cache()


#: Keyword-tribal rules migrated from ``generated/`` in Unit 8 plus
#: new declarative keyword-tribals added later. Each tuple is
#: ``(rule_id, keyword)`` — the keyword value seeds a port row and
#: gates the rule.
_KEYWORD_TRIBAL_RULES: tuple[tuple[str, str], ...] = (
    ("changeling_tribal", "Changeling"),
    ("choose_tribal", "Choose"),
    ("doctor_s_tribal", "Doctor's"),
    ("etbreplacement_copy_dbcopy_optional_tribal", "ETBReplacement:Copy:DBCopy:Optional"),
    ("etbreplacement_other_choosect_tribal", "ETBReplacement:Other:ChooseCT"),
    ("firebending_2_tribal", "Firebending:2"),
    ("landwalk_island_tribal", "Landwalk:Island"),
    ("melee_tribal", "Melee"),
    ("mentor_tribal", "Mentor"),
    ("more_tribal", "More"),
    ("prowess_tribal", "Prowess"),
    ("start_tribal", "Start"),
    ("training_tribal", "Training"),
    ("ward_2_tribal", "Ward:2"),
)

#: Replacement-stack rules — different shape: three-tuple
#: ``(rule_id, event_class, replacement_result)``.
_REPL_STACK_RULES: tuple[tuple[str, str, str], ...] = (
    ("repl_damagedone_counters_stack", "DamageDone", "Counters"),
    ("repl_moved_exile_stack", "Moved", "Exile"),
)


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


def test_all_migrated_rules_in_declarative_set() -> None:
    """Every migrated rule_id appears in DECLARATIVE_RULE_IDS."""
    expected = {rid for rid, _ in _KEYWORD_TRIBAL_RULES} | {rid for rid, _, _ in _REPL_STACK_RULES}
    assert expected <= DECLARATIVE_RULE_IDS


def test_no_migrated_rule_in_python_rule_gates() -> None:
    """None of the 15 migrated rule_ids appear in RULE_GATES —
    their Python helpers were deleted so no registration survives."""
    gate_ids = {g.rule_id for g in RULE_GATES}
    migrated = {rid for rid, _ in _KEYWORD_TRIBAL_RULES} | {rid for rid, _, _ in _REPL_STACK_RULES}
    assert migrated.isdisjoint(gate_ids)


def test_declarative_set_size_matches_seed() -> None:
    """Every rule_id in DECLARATIVE_RULE_IDS corresponds to a row in
    data/rules_seed.json. Catches a seed edit that forgot to update the
    set constant (or vice versa)."""
    import json

    seed_path = Path(__file__).resolve().parent.parent / "data" / "rules_seed.json"
    seed = json.loads(seed_path.read_text())
    seed_ids = {r["rule_id"] for r in seed["rules"]}
    assert seed_ids == DECLARATIVE_RULE_IDS
    # Sentinel check: every historical migration target is still declarative
    # so a bad revert gets caught.
    assert "cascade_tribal" in DECLARATIVE_RULE_IDS
    assert "monarch_synergy" in DECLARATIVE_RULE_IDS
    assert "toughness_synergy" in DECLARATIVE_RULE_IDS
    assert "party_feeder" in DECLARATIVE_RULE_IDS
    assert "etb_tapped_stax_feeder" in DECLARATIVE_RULE_IDS


# ---------------------------------------------------------------------------
# End-to-end emission on synthetic fixtures
# ---------------------------------------------------------------------------


def _seed_card(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, 'Creature', '', 4, 'G', 1000, 1)",
        (name,),
    )


def _seed_keyword_port(conn: sqlite3.Connection, name: str, keyword: str) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, 'keyword', ?, ?)",
        (name, keyword, f"K:{keyword}"),
    )


def _seed_replacement_port(conn: sqlite3.Connection, name: str, event: str, result: str) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, replacement_result, raw_line) "
        "VALUES (?, 'replacement', ?, ?, ?)",
        (name, event, result, f"R${event}:{result}"),
    )


@pytest.mark.parametrize(("rule_id", "keyword"), _KEYWORD_TRIBAL_RULES)
def test_keyword_tribal_rule_emits_partner(tmp_path: Path, rule_id: str, keyword: str) -> None:
    """For each migrated keyword-tribal rule: a commander carrying
    the keyword port and a candidate carrying the same keyword
    produces exactly one complement for ``rule_id``. Parameterized
    so every migrated rule gets its own test case."""
    conn = open_db(tmp_path / f"synergy_{rule_id}.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Partner")
        _seed_keyword_port(conn, "Cmdr", keyword)
        _seed_keyword_port(conn, "Partner", keyword)

        from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db

        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == rule_id]
        assert len(matches) == 1
        assert matches[0].candidate == "Partner"
        assert matches[0].cmdr_event == rule_id
        assert matches[0].cand_event == "same_keyword_partner"
    finally:
        conn.close()


@pytest.mark.parametrize(("rule_id", "event", "result"), _REPL_STACK_RULES)
def test_replacement_stack_rule_emits_partner(tmp_path: Path, rule_id: str, event: str, result: str) -> None:
    """For each migrated replacement-stack rule: a commander
    carrying ``(replacement, event, result)`` and a candidate with
    the same shape produces exactly one complement. Validates the
    three-column ``has_port`` extension (``port_type`` +
    ``event_class`` + ``replacement_result``)."""
    conn = open_db(tmp_path / f"synergy_{rule_id}.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Partner")
        _seed_replacement_port(conn, "Cmdr", event, result)
        _seed_replacement_port(conn, "Partner", event, result)

        from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db

        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == rule_id]
        assert len(matches) == 1
        assert matches[0].candidate == "Partner"
        assert matches[0].cand_event == "same_shape_replacement"
    finally:
        conn.close()


def test_wrong_keyword_does_not_fire_rule(tmp_path: Path) -> None:
    """A commander with a different keyword than any rule's gate
    produces zero complements from those rules."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_keyword_port(conn, "Cmdr", "NonexistentKeyword")

        from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db

        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        declarative_matches = [c for c in comps if c.rule_id in DECLARATIVE_RULE_IDS]
        assert declarative_matches == []
    finally:
        conn.close()
