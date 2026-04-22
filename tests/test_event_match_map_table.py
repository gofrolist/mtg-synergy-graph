"""JSON / DB / Python parity for event_match_map (Unit 3 of plan 003).

Three representations of the same data must stay in sync:

* JSON: ``data/event_match_seed.json`` — the committed source of
  truth.
* Python: ``graph_engine.EVENT_MATCH_MAP`` + ``COST_FEEDS_TRIGGER``
  — populated at module import from the JSON.
* SQLite: ``event_match_map`` + ``cost_feeds_trigger`` tables —
  seeded from the JSON by :func:`seed_event_match_map_db`.

These tests guard the equivalence so a rule migration that updates
one representation can't silently forget the others. ``match_event``
semantics are held bitwise-identical via an independent parity
pass — identity of ``bench.py audit --expect-identity`` on the
pinned fixture is the ultimate integration signal, but the unit
tests below catch drift earlier.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import COST_FEEDS_TRIGGER, EVENT_MATCH_MAP, match_event
from mtg_synergy_graph.port_graph.event_maps import (
    load_cost_feeds_trigger_from_db,
    load_cost_feeds_trigger_from_json,
    load_event_match_map_from_db,
    load_event_match_map_from_json,
    seed_event_match_map_db,
)


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "synergy.db")
    seed_event_match_map_db(conn)
    return conn


# ---------------------------------------------------------------------------
# JSON ↔ Python parity
# ---------------------------------------------------------------------------


def test_module_global_event_match_map_matches_json() -> None:
    """``graph_engine.EVENT_MATCH_MAP`` is the JSON-loaded dict, so
    key shape must match exactly.

    The values are function objects (callables per match_quality).
    Comparing identity isn't meaningful; structural equivalence on
    the (from, to) key set IS.
    """
    from_json = load_event_match_map_from_json()
    assert set(EVENT_MATCH_MAP) == set(from_json)
    for frm, inner in from_json.items():
        assert set(EVENT_MATCH_MAP[frm]) == set(inner)


def test_module_global_cost_feeds_trigger_matches_json() -> None:
    """``graph_engine.COST_FEEDS_TRIGGER`` is the JSON-loaded dict."""
    from_json = load_cost_feeds_trigger_from_json()
    assert from_json == COST_FEEDS_TRIGGER


# ---------------------------------------------------------------------------
# JSON ↔ DB parity
# ---------------------------------------------------------------------------


def test_seeded_db_round_trips_event_match_map(tmp_path: Path) -> None:
    """Seeding an empty DB from JSON, then reading it back,
    reconstructs the same (from, to) key shape as the JSON loader."""
    conn = _fresh_db(tmp_path)
    try:
        from_json = load_event_match_map_from_json()
        from_db = load_event_match_map_from_db(conn)
        assert set(from_db) == set(from_json)
        for frm, inner in from_json.items():
            assert set(from_db[frm]) == set(inner)
    finally:
        conn.close()


def test_seeded_db_round_trips_cost_feeds_trigger(tmp_path: Path) -> None:
    """cost_feeds_trigger round-trips key + set-of-triggers."""
    conn = _fresh_db(tmp_path)
    try:
        from_json = load_cost_feeds_trigger_from_json()
        from_db = load_cost_feeds_trigger_from_db(conn)
        assert from_db == from_json
    finally:
        conn.close()


def test_seed_is_idempotent(tmp_path: Path) -> None:
    """Re-seeding the same DB twice produces no duplicates — the
    ``INSERT OR REPLACE`` in :func:`seed_event_match_map_db` means
    importer re-runs on an existing DB are safe."""
    conn = _fresh_db(tmp_path)
    try:
        first = conn.execute("SELECT COUNT(*) FROM event_match_map").fetchone()[0]
        seed_event_match_map_db(conn)  # seed again
        second = conn.execute("SELECT COUNT(*) FROM event_match_map").fetchone()[0]
        assert first == second
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# match_event() semantic parity — the one-way gate
# ---------------------------------------------------------------------------


def test_load_from_db_raises_on_unknown_match_quality(tmp_path: Path) -> None:
    """Inserting a row with an unrecognised ``match_quality`` and then
    reading it back raises ValueError with the row's from/to context,
    not an unadorned KeyError."""
    conn = _fresh_db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO event_match_map (from_event, to_event, match_quality) VALUES (?, ?, ?)",
            ("FakeFrom", "FakeTo", "typo_quality"),
        )
        conn.commit()
        with pytest.raises(ValueError, match="typo_quality"):
            load_event_match_map_from_db(conn)
    finally:
        conn.close()


def test_load_seed_json_raises_on_corrupt_json(tmp_path: Path) -> None:
    """A truncated / invalid JSON file raises ValueError that includes
    the file path — not a bare json.JSONDecodeError — so operators
    know which file to fix."""
    from mtg_synergy_graph.port_graph.event_maps import _load_seed_json

    bad_file = tmp_path / "event_match_seed.json"
    bad_file.write_text("{corrupt json", encoding="utf-8")
    with pytest.raises(ValueError, match=str(bad_file)):
        _load_seed_json(bad_file)


@pytest.mark.parametrize(
    ("trig", "eff", "expected"),
    [
        # ChangesZone × Token: trigger destination decides.
        (
            {"event_class": "ChangesZone", "zone_destination": "Battlefield"},
            {"event_class": "Token"},
            True,
        ),
        (
            {"event_class": "ChangesZone", "zone_destination": "Graveyard"},
            {"event_class": "Token"},
            False,
        ),
        # SpellCast is "always" via the "*" catch-all.
        (
            {"event_class": "SpellCast"},
            {"event_class": "AnyEffect"},
            True,
        ),
        # Counter compat: matching counter_type.
        (
            {"event_class": "CounterAdded", "counter_type": "P1P1"},
            {"event_class": "PutCounter", "counter_type": "P1P1"},
            True,
        ),
        # Counter compat: non-matching counter_type.
        (
            {"event_class": "CounterAdded", "counter_type": "P1P1"},
            {"event_class": "PutCounter", "counter_type": "Charge"},
            False,
        ),
        # ChangesZoneAll is zone_compatible (matches with empty eff dest).
        (
            {"event_class": "ChangesZone", "zone_destination": "Graveyard"},
            {"event_class": "ChangeZoneAll", "zone_destination": ""},
            True,
        ),
        # Unknown from_event falls through to identity match.
        (
            {"event_class": "UnknownTrigger"},
            {"event_class": "UnknownTrigger"},
            True,
        ),
        (
            {"event_class": "UnknownTrigger"},
            {"event_class": "DifferentEvent"},
            False,
        ),
    ],
)
def test_match_event_behaviour_preserved(
    trig: dict[str, object],
    eff: dict[str, object],
    expected: bool,
) -> None:
    """``match_event`` produces the same boolean as the legacy
    implementation on canary (trigger, effect) pairs. These pairs
    are the observable behaviour of the event-map dispatch, sampled
    across each predicate kind so a mistake in the JSON or the
    dispatch table fails here."""
    assert match_event(trig, eff) is expected
