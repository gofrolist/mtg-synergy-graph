"""rules-table schema + JSON-predicate validator tests (plan 003 Unit 4).

Two layers:

1. Validator — exercises ``validate_gate_predicate`` with well-shaped
   and malformed trees so operators editing ``rules_seed.json`` get
   clear failures at seed time rather than silently wrong runtime
   scoring.
2. Table + loader — ``seed_rules_db`` writes the JSON into SQLite;
   ``load_rules_from_db`` reads it back. Idempotency and the
   ``active = 0`` filter are exercised via in-memory fixtures so the
   tests run in milliseconds.

Interpreter behaviour (Unit 5) is covered separately; this file
owns the shape of the rule rows and the validator contract only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.port_graph.rules_schema import (
    RuleRow,
    _default_seed_path,
    load_rules_from_db,
    seed_rules_db,
    validate_gate_predicate,
)

# ---------------------------------------------------------------------------
# validate_gate_predicate — happy paths
# ---------------------------------------------------------------------------


def test_validator_accepts_simple_has_port() -> None:
    """A bare ``has_port`` leaf with the required ``port_type`` field
    is valid — the minimum shape Unit 7's cascade_tribal migration
    uses."""
    validate_gate_predicate({"op": "has_port", "port_type": "keyword", "event_class": "Cascade"})


def test_validator_accepts_nested_and() -> None:
    """A two-leaf AND compiles. This is the shape Unit 8's
    candidate-side predicate uses (has_port AND not_in_commander_set)."""
    tree = {
        "op": "and",
        "args": [
            {"op": "has_port", "port_type": "keyword", "event_class": "Cascade"},
            {"op": "not_in_commander_set"},
        ],
    }
    validate_gate_predicate(tree)


def test_validator_accepts_or_and_not() -> None:
    """``or`` + ``not`` combinators compile with well-formed args."""
    tree = {
        "op": "or",
        "args": [
            {"op": "has_port", "port_type": "trigger", "event_class": "ChangesZone"},
            {"op": "not", "args": [{"op": "has_port", "port_type": "keyword"}]},
        ],
    }
    validate_gate_predicate(tree)


def test_validator_accepts_filter_tag_leaf() -> None:
    """``filter_tag`` leaf needs the ``tag`` field (e.g., YouCtrl)."""
    validate_gate_predicate({"op": "filter_tag", "tag": "YouCtrl"})


def test_validator_accepts_zone_eq_leaf() -> None:
    """``zone_eq`` leaf needs ``zone`` + ``value``."""
    validate_gate_predicate({"op": "zone_eq", "zone": "destination", "value": "Battlefield"})


def test_validator_accepts_not_in_commander_set() -> None:
    """``not_in_commander_set`` takes no arguments — it's a
    context-bound predicate resolved at interpret time."""
    validate_gate_predicate({"op": "not_in_commander_set"})


# ---------------------------------------------------------------------------
# validate_gate_predicate — failure paths
# ---------------------------------------------------------------------------


def test_validator_rejects_non_dict() -> None:
    """A predicate must be a JSON object, not a raw scalar."""
    with pytest.raises(ValueError, match="predicate must be an object"):
        validate_gate_predicate("has_port")


def test_validator_rejects_unknown_op() -> None:
    """An ``op`` not in GATE_OPS raises with the op name visible."""
    with pytest.raises(ValueError, match="unknown op 'badop'"):
        validate_gate_predicate({"op": "badop"})


def test_validator_rejects_has_port_without_port_type() -> None:
    """The ``has_port`` leaf requires ``port_type`` — an operator
    who forgot it gets a pointed error."""
    with pytest.raises(ValueError, match="missing required field 'port_type'"):
        validate_gate_predicate({"op": "has_port", "event_class": "Cascade"})


def test_validator_rejects_and_with_empty_args() -> None:
    """``and`` with no children is a bug — the operator probably
    meant ``always`` or a single leaf."""
    with pytest.raises(ValueError, match=r"\.args: 'and' requires a non-empty list"):
        validate_gate_predicate({"op": "and", "args": []})


def test_validator_rejects_not_with_wrong_arity() -> None:
    """``not`` takes exactly one child."""
    with pytest.raises(ValueError, match=r"'not' requires exactly one child"):
        validate_gate_predicate(
            {"op": "not", "args": [{"op": "has_port", "port_type": "x"}, {"op": "has_port", "port_type": "y"}]}
        )


def test_validator_rejects_nested_bad_op_with_path() -> None:
    """Errors point at the offending subtree via a dotted path."""
    tree = {
        "op": "and",
        "args": [
            {"op": "has_port", "port_type": "trigger"},
            {"op": "totally_fake_op"},
        ],
    }
    with pytest.raises(ValueError, match=r"\$\.args\[1\]: unknown op 'totally_fake_op'"):
        validate_gate_predicate(tree)


# ---------------------------------------------------------------------------
# seed_rules_db + load_rules_from_db
# ---------------------------------------------------------------------------


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "synergy.db")


def _write_seed(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "rules_seed.json"
    path.write_text(json.dumps({"rules": rows}), encoding="utf-8")
    return path


def test_seed_empty_rules_file_loads_zero_rows(tmp_path: Path) -> None:
    """The Unit 4 landing JSON has zero rules. Seeding is a no-op
    (no rows inserted, no errors)."""
    conn = _fresh_db(tmp_path)
    try:
        seed_path = _write_seed(tmp_path, [])
        inserted = seed_rules_db(conn, seed_path)
        assert inserted == 0
        assert load_rules_from_db(conn) == []
    finally:
        conn.close()


def test_seed_round_trips_one_rule(tmp_path: Path) -> None:
    """A single valid rule survives the seed → load round trip
    with all scalar fields preserved."""
    conn = _fresh_db(tmp_path)
    try:
        seed_path = _write_seed(
            tmp_path,
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
        inserted = seed_rules_db(conn, seed_path)
        assert inserted == 1
        rows = load_rules_from_db(conn)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, RuleRow)
        assert row.rule_id == "cascade_tribal"
        assert row.family == "tribal"
        assert row.cmdr_event == "cascade_tribal"
        assert row.cand_event == "same_keyword_partner"
        assert row.active == 1
        # JSON columns survive as strings that re-parse.
        assert json.loads(row.gate_predicate)["op"] == "has_port"
    finally:
        conn.close()


def test_seed_is_idempotent(tmp_path: Path) -> None:
    """Re-seeding with the same JSON produces the same row count.
    INSERT OR REPLACE means re-imports on a live DB don't double up."""
    conn = _fresh_db(tmp_path)
    try:
        seed_path = _write_seed(
            tmp_path,
            [
                {
                    "rule_id": "r1",
                    "family": "test",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "candidate_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "filter_group": "",
                    "cmdr_event": "r1",
                    "cand_event": "r1",
                    "active": 1,
                },
            ],
        )
        seed_rules_db(conn, seed_path)
        seed_rules_db(conn, seed_path)
        assert len(load_rules_from_db(conn)) == 1
    finally:
        conn.close()


def test_seed_rejects_invalid_predicate_without_writing(tmp_path: Path) -> None:
    """A bad predicate in the seed raises BEFORE any row is written.
    Guards against partial-load states where half the rules got
    through and the scorer sees a truncated rule set."""
    conn = _fresh_db(tmp_path)
    try:
        seed_path = _write_seed(
            tmp_path,
            [
                {
                    "rule_id": "good_one",
                    "family": "test",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "candidate_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "filter_group": "",
                    "cmdr_event": "x",
                    "cand_event": "y",
                    "active": 1,
                },
                {
                    "rule_id": "bad_one",
                    "family": "test",
                    "gate_predicate": {"op": "totally_fake"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "x"},
                    "candidate_port_predicate": {"op": "has_port", "port_type": "x"},
                    "filter_group": "",
                    "cmdr_event": "x",
                    "cand_event": "y",
                    "active": 1,
                },
            ],
        )
        with pytest.raises(ValueError, match="unknown op 'totally_fake'"):
            seed_rules_db(conn, seed_path)
        # DB unchanged — no rows inserted.
        assert load_rules_from_db(conn) == []
    finally:
        conn.close()


def test_load_filters_inactive_rules(tmp_path: Path) -> None:
    """``active = 0`` rows are present in the DB but hidden from the
    interpreter via ``load_rules_from_db``. Lets operators A/B a
    migration without physically deleting the row."""
    conn = _fresh_db(tmp_path)
    try:
        seed_path = _write_seed(
            tmp_path,
            [
                {
                    "rule_id": "on_rule",
                    "family": "test",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "candidate_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "filter_group": "",
                    "cmdr_event": "x",
                    "cand_event": "y",
                    "active": 1,
                },
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
        seed_rules_db(conn, seed_path)
        rows = load_rules_from_db(conn)
        assert [r.rule_id for r in rows] == ["on_rule"]
    finally:
        conn.close()


def test_seed_rejects_missing_predicate_column(tmp_path: Path) -> None:
    """A seed row missing ``commander_port_predicate`` raises ValueError
    before any DB write; the error message names the missing column."""
    conn = _fresh_db(tmp_path)
    try:
        seed_path = _write_seed(
            tmp_path,
            [
                {
                    "rule_id": "incomplete_rule",
                    "family": "test",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword"},
                    # commander_port_predicate deliberately omitted
                    "candidate_port_predicate": {"op": "has_port", "port_type": "keyword"},
                    "filter_group": "",
                    "cmdr_event": "x",
                    "cand_event": "y",
                    "active": 1,
                },
            ],
        )
        with pytest.raises(ValueError, match="commander_port_predicate"):
            seed_rules_db(conn, seed_path)
        assert load_rules_from_db(conn) == []
    finally:
        conn.close()


def test_tribal_family_rejects_mismatched_event_class(tmp_path: Path) -> None:
    """A tribal-family row where the gate event_class differs from the
    candidate event_class raises ValueError with rule_id context."""
    conn = _fresh_db(tmp_path)
    try:
        seed_path = _write_seed(
            tmp_path,
            [
                {
                    "rule_id": "mismatched_tribal",
                    "family": "tribal",
                    "gate_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "A"},
                    "commander_port_predicate": {"op": "has_port", "port_type": "keyword", "event_class": "A"},
                    "candidate_port_predicate": {
                        "op": "and",
                        "args": [
                            {"op": "has_port", "port_type": "keyword", "event_class": "B"},
                            {"op": "not_in_commander_set"},
                        ],
                    },
                    "filter_group": "",
                    "cmdr_event": "x",
                    "cand_event": "y",
                    "active": 1,
                },
            ],
        )
        with pytest.raises(ValueError, match="mismatched_tribal"):
            seed_rules_db(conn, seed_path)
        assert load_rules_from_db(conn) == []
    finally:
        conn.close()


def test_default_seed_path_resolves_to_repo_json() -> None:
    """The auto-discover path resolves to the committed
    ``data/rules_seed.json`` at repo root when invoked inside the
    repo. Guards against future moves of the seed file."""
    p = _default_seed_path()
    assert p.name == "rules_seed.json"
    assert p.parent.name == "data"
