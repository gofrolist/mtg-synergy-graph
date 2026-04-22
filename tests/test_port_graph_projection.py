"""``port_nodes`` view tests (Unit 2 of plan 003).

Covers the projection behavior:

* Known ``(port_type, event_class)`` pairs map to the expected
  canonical ``node_kind``.
* Unknown pairs fall through to ``node_kind='UNKNOWN'``, with the
  raw shape preserved in ``subkind`` so the ``--unknowns`` reporter
  (Unit 6) can surface them.
* Empty / NULL fields in ``card_ports`` don't break the view.
* The view's columns pass ``card_ports`` values through verbatim so
  existing rule queries that read ``card_ports`` can switch to
  ``port_nodes`` without row-shape changes.

No scoring-path change — the view is observability-only until Unit 5
wires the interpreter to read from it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.port_graph.vocabulary import NODE_KINDS


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    """DB with the schema applied; view is created by ``open_db``."""
    return open_db(tmp_path / "synergy.db")


def _insert_port(conn: sqlite3.Connection, card_name: str, **fields: object) -> None:
    """Insert a minimal card + card_ports row. Missing columns use
    database defaults so each test only specifies what it needs."""
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, color_identity, legal_commander) "
        "VALUES (?, 'Creature', 'G', 1)",
        (card_name,),
    )
    cols = ["card_name", "port_type", "event_class"]
    vals: list[object] = [card_name, fields["port_type"], fields["event_class"]]
    for optional in ("valid_filter", "zone_origin", "zone_destination", "counter_type", "raw_line"):
        if optional in fields:
            cols.append(optional)
            vals.append(fields[optional])
    placeholders = ",".join("?" * len(cols))
    # Column names come from a local whitelist (see `optional` loop
    # above) — no user input flows into the SQL string.
    sql = f"INSERT INTO card_ports ({','.join(cols)}) VALUES ({placeholders})"  # noqa: S608
    conn.execute(sql, vals)
    conn.commit()


def _node_kind(conn: sqlite3.Connection, card_name: str) -> str:
    row = conn.execute(
        "SELECT node_kind FROM port_nodes WHERE card_name = ?",
        (card_name,),
    ).fetchone()
    assert row is not None
    return row["node_kind"]


# ---------------------------------------------------------------------------
# Known mappings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("port_type", "event_class", "zone_destination", "zone_origin", "expected"),
    [
        ("trigger", "ChangesZone", "Battlefield", None, "ETB"),
        ("trigger", "ChangesZone", "Graveyard", None, "DIES"),
        ("trigger", "ChangesZone", "Exile", "Battlefield", "LTB"),
        ("trigger", "ChangesZoneAll", None, None, "ZONECHANGE"),
        ("trigger", "SpellCast", None, None, "CAST"),
        ("trigger", "Attacks", None, None, "ATTACK"),
        ("trigger", "AttackerBlocked", None, None, "BLOCK"),
        ("trigger", "DamageDone", None, None, "DAMAGE"),
        ("trigger", "LifeGained", None, None, "LIFE_CHANGE"),
        ("trigger", "LifeLost", None, None, "LIFE_CHANGE"),
        ("trigger", "Sacrificed", None, None, "SACRIFICE"),
        ("trigger", "Discarded", None, None, "DISCARD"),
        ("trigger", "Drawn", None, None, "DRAW"),
        ("trigger", "Taps", None, None, "TAP"),
        ("trigger", "Untaps", None, None, "UNTAP"),
        ("trigger", "CounterAdded", None, None, "COUNTER_PLACED"),
        ("trigger", "TapsForMana", None, None, "PAYMANA"),
        ("effect", "Token", None, None, "ZONECHANGE"),
        ("effect", "Mana", None, None, "PAYMANA"),
        ("effect", "DealDamage", None, None, "DAMAGE"),
        ("effect", "GainLife", None, None, "LIFE_CHANGE"),
        ("effect", "Sacrifice", None, None, "SACRIFICE"),
        ("effect", "Draw", None, None, "DRAW"),
        ("effect", "PutCounter", None, None, "COUNTER_PLACED"),
        ("cost", "sacrifice", None, None, "SACRIFICE"),
        ("cost", "tap", None, None, "TAP"),
        ("cost", "pay_life", None, None, "LIFE_CHANGE"),
        ("cost", "exile_from_grave", None, None, "ZONECHANGE"),
        ("static", "Continuous", None, None, "STATIC_BUFF"),
        ("replacement", "ETBTapped", None, None, "STATIC_REPLACEMENT"),
        ("scales_with", "Party", None, None, "SCALES_WITH"),
        ("keyword", "Cascade", None, None, "STATIC_BUFF"),
        ("keyword", "Horsemanship", None, None, "STATIC_BUFF"),
    ],
)
def test_known_port_type_event_class_maps(
    tmp_path: Path,
    port_type: str,
    event_class: str,
    zone_destination: str | None,
    zone_origin: str | None,
    expected: str,
) -> None:
    """Each (port_type, event_class) combination in the CASE ladder
    projects to the canonical ``node_kind`` the rule engine expects.

    Parameterized so a new CASE branch added in Unit 7/8 needs one
    new row here — not a new test method.
    """
    conn = _fresh_db(tmp_path)
    try:
        fields: dict[str, object] = {"port_type": port_type, "event_class": event_class}
        if zone_destination is not None:
            fields["zone_destination"] = zone_destination
        if zone_origin is not None:
            fields["zone_origin"] = zone_origin
        _insert_port(conn, "card_under_test", **fields)
        assert _node_kind(conn, "card_under_test") == expected
        assert expected in NODE_KINDS
    finally:
        conn.close()


def test_every_node_kind_from_case_ladder_is_in_vocabulary(tmp_path: Path) -> None:
    """Every node_kind the view can emit must be a member of the
    canonical ``NODE_KINDS`` frozenset. Any CASE branch that emits
    an identifier not in the vocabulary is a bug.
    """
    conn = _fresh_db(tmp_path)
    try:
        # Cover one port per port_type so the view has rows across
        # all known branches.
        for i, (pt, ec) in enumerate(
            [
                ("trigger", "ChangesZone"),
                ("effect", "Token"),
                ("cost", "sacrifice"),
                ("static", "Continuous"),
                ("replacement", "ETBTapped"),
                ("scales_with", "Party"),
                ("keyword", "Cascade"),
            ]
        ):
            _insert_port(conn, f"card_{i}", port_type=pt, event_class=ec, zone_destination="Battlefield")
        kinds = {r["node_kind"] for r in conn.execute("SELECT DISTINCT node_kind FROM port_nodes")}
        assert kinds <= NODE_KINDS | {"UNKNOWN"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# UNKNOWN fallback + subkind
# ---------------------------------------------------------------------------


def test_novel_event_class_projects_to_unknown(tmp_path: Path) -> None:
    """A ``(port_type, event_class)`` pair not covered by any CASE
    branch falls through to ``node_kind='UNKNOWN'``. This is the
    contract the ``--unknowns`` reporter (Unit 6) keys on."""
    conn = _fresh_db(tmp_path)
    try:
        _insert_port(conn, "novel", port_type="trigger", event_class="NewMechanicFromFutureSet")
        assert _node_kind(conn, "novel") == "UNKNOWN"
    finally:
        conn.close()


def test_unknown_preserves_raw_shape_in_subkind(tmp_path: Path) -> None:
    """UNKNOWN rows carry ``subkind = port_type || '.' || event_class``
    so the human reviewing ``--unknowns`` output sees what came in.
    """
    conn = _fresh_db(tmp_path)
    try:
        _insert_port(conn, "x", port_type="trigger", event_class="Mystery")
        row = conn.execute("SELECT node_kind, subkind FROM port_nodes WHERE card_name = 'x'").fetchone()
        assert row["node_kind"] == "UNKNOWN"
        assert row["subkind"] == "trigger.Mystery"
    finally:
        conn.close()


def test_empty_event_class_projects_to_unknown(tmp_path: Path) -> None:
    """An empty-string event_class is ``'.'`` in subkind and UNKNOWN
    in node_kind — guards against malformed importer output."""
    conn = _fresh_db(tmp_path)
    try:
        _insert_port(conn, "empty", port_type="trigger", event_class="")
        row = conn.execute("SELECT node_kind, subkind FROM port_nodes WHERE card_name = 'empty'").fetchone()
        assert row["node_kind"] == "UNKNOWN"
        assert row["subkind"] == "trigger."
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_view_passes_through_raw_columns(tmp_path: Path) -> None:
    """Existing rule queries that SELECT from ``card_ports`` should
    be able to SELECT the same columns from ``port_nodes``. Verifies
    the pass-through column list is complete for the POC rules."""
    conn = _fresh_db(tmp_path)
    try:
        _insert_port(
            conn,
            "passthrough_card",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_origin="Any",
            zone_destination="Battlefield",
            counter_type="",
            raw_line="ETB: draw a card",
        )
        row = conn.execute(
            "SELECT card_name, port_type, event_class, valid_filter, "
            "zone_origin, zone_destination, raw_line FROM port_nodes "
            "WHERE card_name = 'passthrough_card'"
        ).fetchone()
        assert row["card_name"] == "passthrough_card"
        assert row["port_type"] == "trigger"
        assert row["event_class"] == "ChangesZone"
        assert row["valid_filter"] == "Creature.YouCtrl"
        assert row["zone_origin"] == "Any"
        assert row["zone_destination"] == "Battlefield"
        assert row["raw_line"] == "ETB: draw a card"
    finally:
        conn.close()


def test_view_row_count_equals_card_ports_row_count(tmp_path: Path) -> None:
    """The view is a 1:1 projection over ``card_ports`` — COUNT
    equality on a fresh DB with inserted ports. Any mismatch means
    the CASE ladder dropped or duplicated rows."""
    conn = _fresh_db(tmp_path)
    try:
        for i in range(5):
            _insert_port(conn, f"card{i}", port_type="trigger", event_class="Sacrificed")
        ports_count = conn.execute("SELECT COUNT(*) AS n FROM card_ports").fetchone()["n"]
        view_count = conn.execute("SELECT COUNT(*) AS n FROM port_nodes").fetchone()["n"]
        assert ports_count == view_count == 5
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integration — production DB characterization
# ---------------------------------------------------------------------------

_integration = pytest.mark.integration
_requires_full_db = pytest.mark.skipif(
    not Path("data/synergy.db").exists(),
    reason="requires data/synergy.db (run scripts/import_cardsfolder.py)",
)


@_integration
@_requires_full_db
def test_production_db_has_mapped_and_unknown_rows() -> None:
    """Characterization: on the real production DB, the view emits
    both mapped node_kinds AND some UNKNOWN rows. The UNKNOWN count
    is the signal Unit 6 reports; this test locks that the view
    actually produces classifications (sanity that the CASE ladder
    didn't regress to all-UNKNOWN)."""
    conn = open_db("data/synergy.db")
    try:
        kinds = {
            r["node_kind"]: r["n"]
            for r in conn.execute("SELECT node_kind, COUNT(*) AS n FROM port_nodes GROUP BY node_kind")
        }
        assert kinds, "port_nodes emitted zero rows — view broken"
        # At least one mapped kind fires on production data.
        mapped = set(kinds) - {"UNKNOWN"}
        assert mapped, f"every row projected to UNKNOWN: {kinds}"
        # Every mapped kind is in the vocabulary.
        assert mapped <= NODE_KINDS
    finally:
        conn.close()
