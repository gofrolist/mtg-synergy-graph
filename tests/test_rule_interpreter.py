"""RuleInterpreter tests (Unit 5 of plan 003).

Drives the interpreter against synthetic in-memory DBs. Covers
predicate compilation for each GATE_OPS leaf + combinator, the
commander-set parameter expansion, and the identity-preservation
guarantee that an empty rules table produces zero complements.

End-to-end bitwise identity via real production scoring is held
by ``bench.py audit --expect-identity`` on the pinned fixture —
not re-asserted here. Unit 7 (cascade_tribal migration) exercises
the integration path with real EDHREC commanders.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.port_graph.interpreter import (
    RuleInterpreter,
    _compile_candidate_predicate,
    _compile_gate_predicate,
)
from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "synergy.db")


def _seed_rules(tmp_path: Path, conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Write ``rows`` to a temp seed JSON and load into conn."""
    path = tmp_path / "rules_seed.json"
    path.write_text(json.dumps({"rules": rows}), encoding="utf-8")
    seed_rules_db(conn, path)


def _seed_port(
    conn: sqlite3.Connection,
    card_name: str,
    *,
    port_type: str,
    event_class: str,
    valid_filter: str | None = None,
    zone_destination: str | None = None,
    counter_type: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, color_identity, legal_commander) "
        "VALUES (?, 'Creature', 'G', 1)",
        (card_name,),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_destination, counter_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter, zone_destination, counter_type),
    )


# ---------------------------------------------------------------------------
# _compile_candidate_predicate — SQL emission
# ---------------------------------------------------------------------------


def test_candidate_has_port_with_event_class_compiles() -> None:
    """``has_port`` with both fields produces a 2-param WHERE fragment."""
    c = _compile_candidate_predicate({"op": "has_port", "port_type": "keyword", "event_class": "Cascade"})
    assert c.sql == "port_type = ? AND event_class = ?"
    assert c.static_params == ("keyword", "Cascade")
    assert c.needs_commander_set is False


def test_candidate_has_port_without_event_class_compiles() -> None:
    """``has_port`` with just ``port_type`` compiles to a single-param
    fragment — useful for broad type matches."""
    c = _compile_candidate_predicate({"op": "has_port", "port_type": "trigger"})
    assert c.sql == "port_type = ?"
    assert c.static_params == ("trigger",)


def test_candidate_and_combines_fragments() -> None:
    """``and`` combines children with ``AND`` and concatenates params
    in tree order."""
    c = _compile_candidate_predicate(
        {
            "op": "and",
            "args": [
                {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                {"op": "not_in_commander_set"},
            ],
        }
    )
    assert "AND" in c.sql
    assert c.static_params == ("keyword", "Cascade")
    assert c.needs_commander_set is True


def test_candidate_or_combines_fragments() -> None:
    c = _compile_candidate_predicate(
        {
            "op": "or",
            "args": [
                {"op": "has_port", "port_type": "trigger"},
                {"op": "has_port", "port_type": "replacement"},
            ],
        }
    )
    assert c.sql == "(port_type = ?) OR (port_type = ?)"
    assert c.static_params == ("trigger", "replacement")


def test_candidate_not_wraps_child() -> None:
    c = _compile_candidate_predicate({"op": "not", "args": [{"op": "has_port", "port_type": "keyword"}]})
    assert c.sql == "NOT (port_type = ?)"
    assert c.static_params == ("keyword",)


def test_candidate_filter_tag_uses_like() -> None:
    c = _compile_candidate_predicate({"op": "filter_tag", "tag": "YouCtrl"})
    assert c.sql == "valid_filter LIKE ?"
    assert c.static_params == ("%YouCtrl%",)


def test_candidate_zone_eq_destination() -> None:
    c = _compile_candidate_predicate({"op": "zone_eq", "zone": "destination", "value": "Battlefield"})
    assert c.sql == "zone_destination = ?"
    assert c.static_params == ("Battlefield",)


def test_candidate_zone_eq_unknown_zone_raises() -> None:
    with pytest.raises(ValueError, match=r"unknown zone"):
        _compile_candidate_predicate({"op": "zone_eq", "zone": "graveyard", "value": "anything"})


def test_candidate_counter_type_compiles() -> None:
    c = _compile_candidate_predicate({"op": "counter_type", "type": "P1P1"})
    assert c.sql == "counter_type = ?"
    assert c.static_params == ("P1P1",)


def test_candidate_not_in_commander_set_placeholder() -> None:
    """``not_in_commander_set`` emits a placeholder that gets
    substituted per call — so the compiled SQL contains a sentinel
    string rather than the final clause."""
    c = _compile_candidate_predicate({"op": "not_in_commander_set"})
    assert "__CMDR_SET_PLACEHOLDER__" in c.sql
    assert c.needs_commander_set is True


# ---------------------------------------------------------------------------
# _compile_gate_predicate — Python-side evaluation
# ---------------------------------------------------------------------------


def test_gate_has_port_matches_port_type_only() -> None:
    gate = _compile_gate_predicate({"op": "has_port", "port_type": "keyword"})
    assert gate({"port_type": "keyword", "event_class": "anything"}) is True
    assert gate({"port_type": "trigger"}) is False


def test_gate_has_port_with_event_class() -> None:
    gate = _compile_gate_predicate({"op": "has_port", "port_type": "keyword", "event_class": "Cascade"})
    assert gate({"port_type": "keyword", "event_class": "Cascade"}) is True
    assert gate({"port_type": "keyword", "event_class": "Horsemanship"}) is False


def test_gate_and_short_circuits() -> None:
    gate = _compile_gate_predicate(
        {
            "op": "and",
            "args": [
                {"op": "has_port", "port_type": "trigger"},
                {"op": "filter_tag", "tag": "YouCtrl"},
            ],
        }
    )
    assert gate({"port_type": "trigger", "valid_filter": "Creature.YouCtrl"}) is True
    assert gate({"port_type": "trigger", "valid_filter": "Creature.OppCtrl"}) is False


def test_gate_or_fires_on_either() -> None:
    gate = _compile_gate_predicate(
        {
            "op": "or",
            "args": [
                {"op": "has_port", "port_type": "trigger"},
                {"op": "has_port", "port_type": "replacement"},
            ],
        }
    )
    assert gate({"port_type": "trigger"}) is True
    assert gate({"port_type": "replacement"}) is True
    assert gate({"port_type": "effect"}) is False


def test_gate_not_in_commander_set_rejected_at_compile() -> None:
    """``not_in_commander_set`` in a gate predicate is meaningless
    (gates run against commander ports) — raise at compile so a
    mis-wired JSON fails loudly."""
    with pytest.raises(ValueError, match="candidate-only"):
        _compile_gate_predicate({"op": "not_in_commander_set"})


# ---------------------------------------------------------------------------
# RuleInterpreter — end-to-end on synthetic DB
# ---------------------------------------------------------------------------


def test_interpreter_empty_db_owns_no_rule_ids(tmp_path: Path) -> None:
    """Empty rules table → empty rule_ids. The Unit-5 landing state:
    interpreter is loaded but owns nothing, so the registry's
    DECLARATIVE_RULE_IDS short-circuit keeps identity."""
    conn = _fresh_db(tmp_path)
    try:
        interp = RuleInterpreter(conn)
        assert interp.rule_ids == frozenset()
        assert interp.find_complements(conn, [], set()) == []
    finally:
        conn.close()


def test_interpreter_emits_cascade_tribal_shape(tmp_path: Path) -> None:
    """End-to-end sanity: a cascade-style rule row emits one
    PortComplement per matching candidate, with the right
    cmdr_event / cand_event / filter_group / direction.
    """
    conn = _fresh_db(tmp_path)
    try:
        _seed_rules(
            tmp_path,
            conn,
            [
                {
                    "rule_id": "cascade_tribal",
                    "family": "tribal",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "candidate_port_predicate": {
                        "op": "and",
                        "args": [
                            {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                            {"op": "not_in_commander_set"},
                        ],
                    },
                    "filter_group": "",
                    "cmdr_event": "cascade_tribal",
                    "cand_event": "same_keyword_partner",
                    "active": 1,
                },
            ],
        )
        # Commander has Cascade; 3 other cards have Cascade; 1 does not.
        _seed_port(conn, "MaelstromWanderer", port_type="keyword", event_class="Cascade")
        _seed_port(conn, "Apex", port_type="keyword", event_class="Cascade")
        _seed_port(conn, "Shardless", port_type="keyword", event_class="Cascade")
        _seed_port(conn, "Imoti", port_type="keyword", event_class="Cascade")
        _seed_port(conn, "UnrelatedCreature", port_type="trigger", event_class="SpellCast")

        interp = RuleInterpreter(conn)
        cmdr_ports = [{"port_type": "keyword", "event_class": "Cascade"}]
        results = interp.find_complements(conn, cmdr_ports, {"MaelstromWanderer"})

        # 3 distinct candidate matches (commander itself excluded).
        assert len(results) == 3
        cands = {c.candidate for c in results}
        assert cands == {"Apex", "Shardless", "Imoti"}
        # Each PortComplement carries the row's declared events.
        for c in results:
            assert isinstance(c, PortComplement)
            assert c.rule_id == "cascade_tribal"
            assert c.direction == "synergy"
            assert c.cmdr_event == "cascade_tribal"
            assert c.cand_event == "same_keyword_partner"
            assert c.filter_group == ""
    finally:
        conn.close()


def test_interpreter_skips_rule_when_gate_fails(tmp_path: Path) -> None:
    """A commander without the gate's port_type gets zero
    complements, even if candidates in the DB match the candidate
    predicate. Short-circuit: no SQL call."""
    conn = _fresh_db(tmp_path)
    try:
        _seed_rules(
            tmp_path,
            conn,
            [
                {
                    "rule_id": "cascade_tribal",
                    "family": "tribal",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "candidate_port_predicate": {
                        "op": "and",
                        "args": [
                            {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                            {"op": "not_in_commander_set"},
                        ],
                    },
                    "filter_group": "",
                    "cmdr_event": "cascade_tribal",
                    "cand_event": "same_keyword_partner",
                    "active": 1,
                },
            ],
        )
        # Candidates exist but commander has no Cascade port.
        _seed_port(conn, "Apex", port_type="keyword", event_class="Cascade")
        _seed_port(conn, "NonCascadeCommander", port_type="trigger", event_class="SpellCast")

        interp = RuleInterpreter(conn)
        cmdr_ports = [{"port_type": "trigger", "event_class": "SpellCast"}]
        results = interp.find_complements(conn, cmdr_ports, {"NonCascadeCommander"})
        assert results == []
    finally:
        conn.close()


def test_interpreter_excludes_commander_from_candidates(tmp_path: Path) -> None:
    """A commander that matches its own candidate predicate is NOT
    emitted as its own partner. Guards against Maelstrom Wanderer
    showing up as a suggested pick for Maelstrom Wanderer."""
    conn = _fresh_db(tmp_path)
    try:
        _seed_rules(
            tmp_path,
            conn,
            [
                {
                    "rule_id": "cascade_tribal",
                    "family": "tribal",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "candidate_port_predicate": {
                        "op": "and",
                        "args": [
                            {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                            {"op": "not_in_commander_set"},
                        ],
                    },
                    "filter_group": "",
                    "cmdr_event": "cascade_tribal",
                    "cand_event": "same_keyword_partner",
                    "active": 1,
                },
            ],
        )
        _seed_port(conn, "MaelstromWanderer", port_type="keyword", event_class="Cascade")

        interp = RuleInterpreter(conn)
        cmdr_ports = [{"port_type": "keyword", "event_class": "Cascade"}]
        results = interp.find_complements(conn, cmdr_ports, {"MaelstromWanderer"})
        # Only self — excluded — so empty.
        assert results == []
    finally:
        conn.close()


def test_interpreter_deactivated_rule_not_loaded(tmp_path: Path) -> None:
    """``active = 0`` rows do not appear in the interpreter's
    rule_ids and cannot fire. Enables operators to A/B a rule by
    flipping the flag rather than deleting the row."""
    conn = _fresh_db(tmp_path)
    try:
        _seed_rules(
            tmp_path,
            conn,
            [
                {
                    "rule_id": "off_rule",
                    "family": "test",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "candidate_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "filter_group": "",
                    "cmdr_event": "x",
                    "cand_event": "y",
                    "active": 0,
                },
            ],
        )
        interp = RuleInterpreter(conn)
        assert interp.rule_ids == frozenset()
    finally:
        conn.close()


def test_interpreter_multiple_rules_fire_independently(tmp_path: Path) -> None:
    """Two rules in the same table both emit complements on a
    commander that matches both gates."""
    conn = _fresh_db(tmp_path)
    try:
        _seed_rules(
            tmp_path,
            conn,
            [
                {
                    "rule_id": "cascade_tribal",
                    "family": "tribal",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "candidate_port_predicate": {
                        "op": "and",
                        "args": [
                            {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                            {"op": "not_in_commander_set"},
                        ],
                    },
                    "filter_group": "",
                    "cmdr_event": "cascade_tribal",
                    "cand_event": "same_keyword_partner",
                    "active": 1,
                },
                {
                    "rule_id": "horsemanship_tribal",
                    "family": "tribal",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Horsemanship"},
                    "commander_port_predicate": {
                        "op": "has_port",
                        "port_type": "keyword",
                        "event_class": "Horsemanship",
                    },
                    "candidate_port_predicate": {
                        "op": "and",
                        "args": [
                            {"op": "has_port", "port_type": "keyword", "event_class": "Horsemanship"},
                            {"op": "not_in_commander_set"},
                        ],
                    },
                    "filter_group": "",
                    "cmdr_event": "horsemanship_tribal",
                    "cand_event": "same_keyword_partner",
                    "active": 1,
                },
            ],
        )
        _seed_port(conn, "DualKeywordCommander", port_type="keyword", event_class="Cascade")
        _seed_port(conn, "DualKeywordCommander", port_type="keyword", event_class="Horsemanship")
        _seed_port(conn, "CascadePartner", port_type="keyword", event_class="Cascade")
        _seed_port(conn, "HorsemanshipPartner", port_type="keyword", event_class="Horsemanship")

        interp = RuleInterpreter(conn)
        assert interp.rule_ids == {"cascade_tribal", "horsemanship_tribal"}

        cmdr_ports = [
            {"port_type": "keyword", "event_class": "Cascade"},
            {"port_type": "keyword", "event_class": "Horsemanship"},
        ]
        results = interp.find_complements(conn, cmdr_ports, {"DualKeywordCommander"})
        by_rule = {(c.rule_id, c.candidate) for c in results}
        assert ("cascade_tribal", "CascadePartner") in by_rule
        assert ("horsemanship_tribal", "HorsemanshipPartner") in by_rule
        assert len(results) == 2
    finally:
        conn.close()


def test_gate_zone_eq_rejects_invalid_zone() -> None:
    """``zone_eq`` with an unknown zone value raises ValueError at
    compile time — mis-wired seed JSON fails loudly rather than
    silently matching the wrong column."""
    with pytest.raises(ValueError, match=r"unknown zone"):
        _compile_gate_predicate({"op": "zone_eq", "zone": "unknown", "value": "x"})


def test_candidate_tribe_compiles_to_like() -> None:
    """``tribe`` leaf emits a LIKE fragment against ``valid_filter``."""
    c = _compile_candidate_predicate({"op": "tribe", "name": "Elf"})
    assert c.sql == "valid_filter LIKE ?"
    assert c.static_params == ("%Elf%",)
    assert c.needs_commander_set is False


def test_candidate_color_compiles_to_like() -> None:
    """``color`` leaf emits a LIKE fragment against ``valid_filter``."""
    c = _compile_candidate_predicate({"op": "color", "color": "G"})
    assert c.sql == "valid_filter LIKE ?"
    assert c.static_params == ("%G%",)
    assert c.needs_commander_set is False


def test_gate_tribe_matches_substring_in_filter() -> None:
    """Gate callable for ``tribe`` returns True when the tribe name
    appears anywhere in the port's ``valid_filter`` string."""
    gate = _compile_gate_predicate({"op": "tribe", "name": "Elf"})
    assert gate({"valid_filter": "Creature.Elf.YouCtrl"}) is True
    assert gate({"valid_filter": "Creature.Wizard.YouCtrl"}) is False
    assert gate({"valid_filter": None}) is False


def test_candidate_zone_eq_origin() -> None:
    """``zone_eq`` with ``zone='origin'`` compiles to ``zone_origin = ?``."""
    c = _compile_candidate_predicate({"op": "zone_eq", "zone": "origin", "value": "Hand"})
    assert c.sql == "zone_origin = ?"
    assert c.static_params == ("Hand",)


def test_interpreter_empty_commander_set_still_queries(tmp_path: Path) -> None:
    """An empty ``cmdr_set`` (edge case — shouldn't happen in
    production but defended at the boundary) falls back to a
    ``1 = 1`` filter. No commander to exclude → all matches emit."""
    conn = _fresh_db(tmp_path)
    try:
        _seed_rules(
            tmp_path,
            conn,
            [
                {
                    "rule_id": "cascade_tribal",
                    "family": "tribal",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                    "candidate_port_predicate": {
                        "op": "and",
                        "args": [
                            {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
                            {"op": "not_in_commander_set"},
                        ],
                    },
                    "filter_group": "",
                    "cmdr_event": "cascade_tribal",
                    "cand_event": "same_keyword_partner",
                    "active": 1,
                },
            ],
        )
        _seed_port(conn, "Apex", port_type="keyword", event_class="Cascade")

        interp = RuleInterpreter(conn)
        cmdr_ports = [{"port_type": "keyword", "event_class": "Cascade"}]
        results = interp.find_complements(conn, cmdr_ports, set())
        # With cmdr_set empty, Apex is the one match and is emitted.
        assert len(results) == 1
        assert results[0].candidate == "Apex"
    finally:
        conn.close()
