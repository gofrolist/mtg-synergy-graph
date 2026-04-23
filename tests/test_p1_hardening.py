"""P1 structural hardening batch (post-review #2).

Tests for the six P1 items:

1. ``_interpreter_cache`` reuses the same :class:`RuleInterpreter` for
   the same connection and ``clear_interpreter_cache`` evicts the entry.
2. ``DECLARATIVE_RULE_IDS`` equals the set of ``active=1`` rule_ids in
   ``data/rules_seed.json`` — no hand-maintained literal drift.
3. ``graph_engine`` can be imported (and ``EVENT_MATCH_MAP`` accessed)
   even when the seed file is temporarily absent, thanks to lazy
   initialisation — the seed is read only on first access of the
   lazy attributes.
4-5. ``validate_gate_predicate`` / ``validate_candidate_predicate``
   reject ``not_in_commander_set`` in gate context and reject multiple
   ``not_in_commander_set`` leaves / ``not_in_commander_set`` under
   ``not`` in candidate context.
6. ``open_db`` creates the ``port_nodes`` view exactly once and the
   view projects correctly.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Item #1 — interpreter cache
# ---------------------------------------------------------------------------


def test_interpreter_cache_returns_same_instance_for_same_conn() -> None:
    from mtg_synergy_graph.complement_rules._interpreter_cache import (
        clear_interpreter_cache,
        get_interpreter,
    )
    from mtg_synergy_graph.db import open_db

    clear_interpreter_cache()
    conn = open_db(":memory:")
    try:
        a = get_interpreter(conn)
        b = get_interpreter(conn)
        assert a is b, "cached interpreter must be reused for the same connection"
    finally:
        conn.close()
        clear_interpreter_cache()


def test_interpreter_cache_clear_evicts() -> None:
    from mtg_synergy_graph.complement_rules._interpreter_cache import (
        _interpreter_cache,
        clear_interpreter_cache,
        get_interpreter,
    )
    from mtg_synergy_graph.db import open_db

    clear_interpreter_cache()
    conn = open_db(":memory:")
    try:
        _ = get_interpreter(conn)
        assert _interpreter_cache, "cache must be populated after first access"
        clear_interpreter_cache()
        assert not _interpreter_cache, "clear_interpreter_cache must evict all entries"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Item #2 — DECLARATIVE_RULE_IDS derived from rules_seed.json
# ---------------------------------------------------------------------------


def test_declarative_rule_ids_match_seed_file() -> None:
    """The frozenset must equal the set of ``active=1`` rule_ids in
    ``data/rules_seed.json``. Catches a seed edit that forgot to bump
    the literal (the whole reason the literal was removed)."""
    from mtg_synergy_graph.complement_rules.registry import DECLARATIVE_RULE_IDS
    from mtg_synergy_graph.port_graph._paths import default_seed_path

    seed_path = default_seed_path("rules_seed.json")
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    expected = {row["rule_id"] for row in data.get("rules", []) if int(row.get("active", 1)) == 1}
    assert frozenset(expected) == DECLARATIVE_RULE_IDS


# ---------------------------------------------------------------------------
# Item #3 — graph_engine import doesn't require the seed file
# ---------------------------------------------------------------------------


def test_graph_engine_import_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-executing the :mod:`graph_engine` module body must NOT call
    the JSON loaders — the cache fill is deferred to first attribute
    access. Simulated by patching the loaders to raise and re-running
    the module via ``importlib.reload``.

    We restore the original module afterwards (``sys.modules`` keyed
    by name; reload mutates the existing object in place) so the rest
    of the test session is unaffected.
    """
    import importlib

    from mtg_synergy_graph import graph_engine as ge
    from mtg_synergy_graph.port_graph import event_maps as em

    def _raise(*_a: object, **_kw: object) -> None:
        raise FileNotFoundError("simulated missing seed")

    monkeypatch.setattr(em, "load_event_match_map_from_json", _raise)
    monkeypatch.setattr(em, "load_cost_feeds_trigger_from_json", _raise)

    # Clear the lazy cache so attribute access WOULD call the loader
    # if the module body did. Then reload; the reload must succeed
    # WITHOUT touching the patched loaders.
    ge._reset_event_cache_for_tests()
    importlib.reload(ge)
    # Reload succeeded without FileNotFoundError. Restore a populated
    # cache so subsequent tests observing the module see real data.
    monkeypatch.undo()
    ge._reset_event_cache_for_tests()
    _ = ge.EVENT_MATCH_MAP  # re-populate cache from real seed


def test_graph_engine_lazy_load_caches_after_first_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated access of the lazy attributes must re-use the cached
    value — the seed is parsed at most once per process."""
    from mtg_synergy_graph import graph_engine as ge
    from mtg_synergy_graph.port_graph import event_maps as em

    call_count = {"event": 0, "cost": 0}
    real_event = em.load_event_match_map_from_json
    real_cost = em.load_cost_feeds_trigger_from_json

    def counting_event(*a: object, **kw: object) -> object:
        call_count["event"] += 1
        return real_event(*a, **kw)

    def counting_cost(*a: object, **kw: object) -> object:
        call_count["cost"] += 1
        return real_cost(*a, **kw)

    monkeypatch.setattr(em, "load_event_match_map_from_json", counting_event)
    monkeypatch.setattr(em, "load_cost_feeds_trigger_from_json", counting_cost)
    ge._reset_event_cache_for_tests()

    _ = ge.EVENT_MATCH_MAP
    _ = ge.EVENT_MATCH_MAP
    _ = ge.COST_FEEDS_TRIGGER
    _ = ge.COST_FEEDS_TRIGGER

    assert call_count["event"] == 1, "EVENT_MATCH_MAP should load exactly once"
    assert call_count["cost"] == 1, "COST_FEEDS_TRIGGER should load exactly once"
    # Restore the real cache so other tests are not affected.
    ge._reset_event_cache_for_tests()


# ---------------------------------------------------------------------------
# Items #4 & #5 — validator hardening
# ---------------------------------------------------------------------------


def test_validator_rejects_not_in_commander_set_in_gate() -> None:
    from mtg_synergy_graph.port_graph.rules_schema import validate_gate_predicate

    bad = {"op": "not_in_commander_set"}
    with pytest.raises(ValueError, match="not_in_commander_set"):
        validate_gate_predicate(bad, path="$", context="gate")


def test_validator_rejects_not_in_commander_set_nested_in_gate() -> None:
    from mtg_synergy_graph.port_graph.rules_schema import validate_gate_predicate

    bad = {
        "op": "and",
        "args": [
            {"op": "has_port", "port_type": "trigger"},
            {"op": "not_in_commander_set"},
        ],
    }
    with pytest.raises(ValueError, match="not_in_commander_set"):
        validate_gate_predicate(bad, path="$", context="gate")


def test_validator_accepts_not_in_commander_set_in_candidate() -> None:
    """Candidate-context validation must allow a single, non-negated
    ``not_in_commander_set`` leaf."""
    from mtg_synergy_graph.port_graph.rules_schema import validate_gate_predicate

    good = {
        "op": "and",
        "args": [
            {"op": "has_port", "port_type": "trigger"},
            {"op": "not_in_commander_set"},
        ],
    }
    validate_gate_predicate(good, path="$", context="candidate")  # does not raise


def test_validator_rejects_double_not_in_commander_set_in_candidate() -> None:
    from mtg_synergy_graph.port_graph.rules_schema import validate_gate_predicate

    bad = {
        "op": "and",
        "args": [
            {"op": "not_in_commander_set"},
            {"op": "not_in_commander_set"},
        ],
    }
    with pytest.raises(ValueError, match="at most once"):
        validate_gate_predicate(bad, path="$", context="candidate")


def test_validator_rejects_not_wrapping_not_in_commander_set() -> None:
    from mtg_synergy_graph.port_graph.rules_schema import validate_gate_predicate

    bad = {
        "op": "not",
        "args": [{"op": "not_in_commander_set"}],
    }
    with pytest.raises(ValueError, match="not_in_commander_set"):
        validate_gate_predicate(bad, path="$", context="candidate")


# ---------------------------------------------------------------------------
# Item #6 — port_nodes view created by open_db
# ---------------------------------------------------------------------------


def test_open_db_creates_port_nodes_view() -> None:
    """Opening a fresh DB must leave the ``port_nodes`` view queryable,
    with node_kind projection working for a known (port_type,
    event_class) pair."""
    from mtg_synergy_graph.db import open_db

    conn = open_db(":memory:")
    try:
        # View exists.
        row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'view' AND name = 'port_nodes'").fetchone()
        assert row is not None, "port_nodes view must be created by open_db"

        # Insert a test card + ETB trigger port and verify projection.
        conn.execute("INSERT INTO cards (name) VALUES (?)", ("_test_card",))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, zone_destination) VALUES (?, ?, ?, ?)",
            ("_test_card", "trigger", "ChangesZone", "Battlefield"),
        )
        node_kind = conn.execute(
            "SELECT node_kind FROM port_nodes WHERE card_name = ?",
            ("_test_card",),
        ).fetchone()["node_kind"]
        assert node_kind == "ETB"
    finally:
        conn.close()


def test_schema_sql_does_not_contain_inline_port_nodes_view() -> None:
    """The inline DDL in ``schema.sql`` was a drift risk — Item #6
    removes it. Guard against reintroduction."""
    from importlib import resources

    schema_sql = resources.files("mtg_synergy_graph").joinpath("schema.sql").read_text(encoding="utf-8")
    # The schema file must not define port_nodes inline anymore.
    # Note: comment lines mentioning "port_nodes" are fine.
    #
    # Look for the CREATE VIEW statement specifically.
    assert "CREATE VIEW IF NOT EXISTS port_nodes" not in schema_sql, (
        "port_nodes view must be created by port_graph.projection.create_port_nodes_view, not inlined in schema.sql"
    )
