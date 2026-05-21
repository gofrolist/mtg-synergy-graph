"""Migration-parity tests for ``monarch_synergy``, ``toughness_synergy``,
``party_feeder`` (issue #14).

These three rules were migrated from Python helpers to declarative
``data/rules_seed.json`` rows in the 2026-04-24 batch. Bitwise identity
of scoring against the pinned golden fixture is covered by
``bench.py audit --expect-identity``; this file adds the focused
unit-level emission guards that fail fast on a JSON drift.

Mirrors the pattern used in ``test_rules_migration_peer_tribal.py`` —
synthetic ports seed a commander and a partner candidate, then
``find_all_complements`` is asserted to emit exactly one row tagged
with the rule_id.

``etb_tapped_stax_feeder`` (the fourth rule in the batch) is guarded
by its own NULL-invariant test at
``tests/test_etb_tapped_stax_null_filter_invariant.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.complement_rules import find_all_complements
from mtg_synergy_graph.complement_rules._interpreter_cache import clear_interpreter_cache
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import clear_ports_cache
from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db


@pytest.fixture(autouse=True)
def _reset_ports_cache() -> None:
    clear_ports_cache()
    clear_interpreter_cache()


def _seed_card(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, subtypes, cmc, "
        "color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, 'Creature', '', 4, 'G', 1000, 1)",
        (name,),
    )


def _seed_port(
    conn: sqlite3.Connection,
    card_name: str,
    *,
    port_type: str,
    event_class: str,
    raw_line: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, ?, ?, ?)",
        (card_name, port_type, event_class, raw_line or f"{port_type}:{event_class}"),
    )


# ---------------------------------------------------------------------------
# monarch_synergy — political family
# ---------------------------------------------------------------------------


def test_monarch_synergy_emits_for_monarch_partner(tmp_path: Path) -> None:
    """Commander with BecomeMonarch + candidate with BecomeMonarch →
    one ``monarch_synergy`` complement tagged
    ``cand_event='monarch_or_pillowfort'``."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Partner")
        _seed_port(conn, "Cmdr", port_type="effect", event_class="BecomeMonarch")
        _seed_port(conn, "Partner", port_type="effect", event_class="BecomeMonarch")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "monarch_synergy"]
        assert len(matches) == 1
        assert matches[0].candidate == "Partner"
        assert matches[0].cmdr_event == "BecomeMonarch"
        assert matches[0].cand_event == "monarch_or_pillowfort"
    finally:
        conn.close()


def test_monarch_synergy_emits_for_pillowfort_partner(tmp_path: Path) -> None:
    """The candidate disjunct allows ``static + CantAttackUnless``
    (pillowfort) as a valid partner for a Monarch commander."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Pillow")
        _seed_port(conn, "Cmdr", port_type="effect", event_class="BecomeMonarch")
        _seed_port(conn, "Pillow", port_type="static", event_class="CantAttackUnless")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "monarch_synergy"]
        assert len(matches) == 1
        assert matches[0].candidate == "Pillow"
    finally:
        conn.close()


def test_monarch_synergy_excludes_commander_self(tmp_path: Path) -> None:
    """``not_in_commander_set`` prevents the commander from emitting
    a complement to itself."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_port(conn, "Cmdr", port_type="effect", event_class="BecomeMonarch")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "monarch_synergy"]
        assert matches == []
    finally:
        conn.close()


def test_monarch_synergy_does_not_fire_without_gate(tmp_path: Path) -> None:
    """A commander lacking the BecomeMonarch port emits no
    ``monarch_synergy`` complements regardless of candidate shapes."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Partner")
        # Commander has a different port; candidate has the right shape.
        _seed_port(conn, "Cmdr", port_type="effect", event_class="Token")
        _seed_port(conn, "Partner", port_type="effect", event_class="BecomeMonarch")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "monarch_synergy"]
        assert matches == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# toughness_synergy — scaling family
# ---------------------------------------------------------------------------


def test_toughness_synergy_emits_for_defender_partner(tmp_path: Path) -> None:
    """Commander scaling with CardToughness + candidate with Defender
    keyword → one ``toughness_synergy`` complement tagged
    ``cand_event='Defender'``."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Wall")
        _seed_port(
            conn,
            "Cmdr",
            port_type="scales_with",
            event_class="CardToughness",
        )
        _seed_port(conn, "Wall", port_type="keyword", event_class="Defender")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "toughness_synergy"]
        assert len(matches) == 1
        assert matches[0].candidate == "Wall"
        assert matches[0].cmdr_event == "toughness_scaling"
        assert matches[0].cand_event == "Defender"
    finally:
        conn.close()


def test_toughness_synergy_does_not_fire_without_gate(tmp_path: Path) -> None:
    """Commander lacking ``scales_with CardToughness`` emits no
    ``toughness_synergy`` complement even if the candidate has
    Defender."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Wall")
        _seed_port(conn, "Cmdr", port_type="keyword", event_class="Flying")
        _seed_port(conn, "Wall", port_type="keyword", event_class="Defender")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "toughness_synergy"]
        assert matches == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# party_feeder — scaling family
# ---------------------------------------------------------------------------


def test_party_feeder_emits_for_party_peer(tmp_path: Path) -> None:
    """Commander with ``scales_with Party`` + candidate with the same
    port → one ``party_feeder`` complement tagged
    ``cand_event='party_peer'``."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Peer")
        _seed_port(conn, "Cmdr", port_type="scales_with", event_class="Party")
        _seed_port(conn, "Peer", port_type="scales_with", event_class="Party")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "party_feeder"]
        assert len(matches) == 1
        assert matches[0].candidate == "Peer"
        assert matches[0].cmdr_event == "party_axis"
        assert matches[0].cand_event == "party_peer"
    finally:
        conn.close()


def test_party_feeder_excludes_commander_self(tmp_path: Path) -> None:
    """A second commander in the set carrying the party port is
    excluded by ``not_in_commander_set``."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "CmdrA")
        _seed_card(conn, "CmdrB")
        _seed_port(conn, "CmdrA", port_type="scales_with", event_class="Party")
        _seed_port(conn, "CmdrB", port_type="scales_with", event_class="Party")
        seed_rules_db(conn)
        conn.commit()

        # Both cards in commander_set → neither can be the candidate.
        comps = find_all_complements(conn, ["CmdrA", "CmdrB"])
        matches = [c for c in comps if c.rule_id == "party_feeder"]
        assert matches == []
    finally:
        conn.close()


def test_party_feeder_does_not_fire_without_gate(tmp_path: Path) -> None:
    """Commander lacking ``scales_with Party`` emits no
    ``party_feeder`` complement even when candidates have it."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Cmdr")
        _seed_card(conn, "Partner")
        _seed_port(conn, "Cmdr", port_type="scales_with", event_class="CardToughness")
        _seed_port(conn, "Partner", port_type="scales_with", event_class="Party")
        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["Cmdr"])
        matches = [c for c in comps if c.rule_id == "party_feeder"]
        assert matches == []
    finally:
        conn.close()
