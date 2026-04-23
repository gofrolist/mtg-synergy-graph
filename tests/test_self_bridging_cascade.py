"""Tests for ``_find_self_bridging_cascade`` — the SQL-backed wrapper
around the depth-2 pathway walker.

The walker itself is exhaustively tested in ``test_pathway_walker.py``;
these tests exercise the Stage-1 SQL / Stage-2 bulk-load / Stage-3
Python pipeline end-to-end with an in-memory SQLite DB.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from mtg_synergy_graph.complement_rules.pathway import _find_self_bridging_cascade

PortRow = dict[str, Any]


# ---------------------------------------------------------------------------
# In-memory DB helpers
# ---------------------------------------------------------------------------


def _insert_card(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, types, subtypes, supertypes, "
        "keywords, color_identity, cmc, edhrec_rank, oracle_id, legal_commander) "
        "VALUES (?, '', '', '', '', '', '', 0, NULL, NULL, 1)",
        (name,),
    )


def _insert_port(
    conn: sqlite3.Connection,
    card_name: str,
    *,
    port_type: str,
    event_class: str,
    valid_filter: str = "",
    zone_origin: str = "",
    zone_destination: str = "",
    counter_type: str = "",
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, "
        "zone_origin, zone_destination, counter_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, counter_type),
    )


def _cmdr_port(
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
    zone_origin: str = "",
    zone_destination: str = "",
    counter_type: str = "",
) -> PortRow:
    """Build a commander PortRow dict (in-memory, no DB row needed)."""
    return {
        "port_type": port_type,
        "event_class": event_class,
        "valid_filter": valid_filter,
        "zone_origin": zone_origin,
        "zone_destination": zone_destination,
        "counter_type": counter_type,
    }


# ---------------------------------------------------------------------------
# Happy path: real Korvold + Bloodghast shape
# ---------------------------------------------------------------------------


def test_sacrifice_cascade_shape_fires(rules_db: sqlite3.Connection) -> None:
    """Korvold-style commander (Sacrificed trigger + Sacrifice effect)
    paired with a Gravecrawler-style candidate (sacrifice cost +
    Sacrificed trigger + self-return from graveyard) emits exactly
    one self_bridging_cascade complement.

    This is the canonical port-level cascade shape: the candidate's
    sacrifice cost feeds the commander's Sacrificed trigger, AND the
    candidate's own Sacrificed trigger resonates with the commander's
    Sacrifice effect. The internal edge is cost_feeds between the
    candidate's cost and trigger ports.
    """
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed", valid_filter="Permanent"),
        _cmdr_port("effect", "Sacrifice", valid_filter="Permanent.Other"),
    ]
    _insert_card(rules_db, "Gravecrawler")
    # Sacrifice cost (matches commander trigger via cost_feeds).
    _insert_port(rules_db, "Gravecrawler", port_type="cost", event_class="sacrifice")
    # Sacrificed trigger (matches commander effect via event_match:
    # Sacrificed -> Sacrifice is a canonical entry in EVENT_MATCH_MAP).
    _insert_port(rules_db, "Gravecrawler", port_type="trigger", event_class="Sacrificed")

    result = _find_self_bridging_cascade(rules_db, cmdr_ports, {"Korvold"})

    assert len(result) == 1
    comp = result[0]
    assert comp.rule_id == "self_bridging_cascade"
    assert comp.candidate == "Gravecrawler"
    assert comp.cand_event == "self_bridging"
    assert comp.filter_group == "depth_2"
    assert comp.direction == "synergy"


def test_cmdr_event_label_is_deterministic_and_sorted(rules_db: sqlite3.Connection) -> None:
    """The ``cmdr_event`` label is the two matched commander events
    joined with '+' in lexicographic order. Two runs of the helper
    must return byte-identical labels."""
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
    ]
    # Candidate with sacrifice cost + Sacrificed trigger (cost_feeds).
    _insert_card(rules_db, "SacFodder")
    _insert_port(rules_db, "SacFodder", port_type="cost", event_class="sacrifice")
    _insert_port(rules_db, "SacFodder", port_type="trigger", event_class="Sacrificed")

    result_a = _find_self_bridging_cascade(rules_db, cmdr_ports, set())
    result_b = _find_self_bridging_cascade(rules_db, cmdr_ports, set())
    assert len(result_a) == 1 == len(result_b)
    assert result_a[0].cmdr_event == result_b[0].cmdr_event
    # Lexicographic sort guarantees 'Sacrifice' (effect) before 'Sacrificed' (trigger).
    assert result_a[0].cmdr_event == "Sacrifice+Sacrificed"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_commander_with_fewer_than_two_ports_returns_empty(rules_db: sqlite3.Connection) -> None:
    cmdr_ports = [_cmdr_port("trigger", "Sacrificed")]
    _insert_card(rules_db, "Bloodghast")
    _insert_port(rules_db, "Bloodghast", port_type="trigger", event_class="ChangesZone")
    _insert_port(rules_db, "Bloodghast", port_type="effect", event_class="ChangeZone")
    assert _find_self_bridging_cascade(rules_db, cmdr_ports, set()) == []


def test_candidate_with_only_one_matching_port_returns_empty(rules_db: sqlite3.Connection) -> None:
    """A candidate with a single matching port cannot form |M| >= 2.
    Also covers the Stage-1 ``HAVING COUNT(DISTINCT ...) >= 2`` gate."""
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
    ]
    _insert_card(rules_db, "VanillaSacFodder")
    _insert_port(rules_db, "VanillaSacFodder", port_type="trigger", event_class="Sacrificed")
    # Second port intentionally uses an event_class NOT in the
    # relevant set -- the Stage-1 HAVING should filter this card out.
    _insert_port(rules_db, "VanillaSacFodder", port_type="effect", event_class="Manifest")
    assert _find_self_bridging_cascade(rules_db, cmdr_ports, set()) == []


def test_candidate_with_multiple_internal_edges_fires_exactly_once(
    rules_db: sqlite3.Connection,
) -> None:
    """A 3-port candidate where multiple pairs would bridge should
    still emit exactly one PortComplement (walker returns first match;
    dedup ``seen`` set is belt-and-suspenders)."""
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
        _cmdr_port("trigger", "ChangesZone"),
    ]
    _insert_card(rules_db, "TriplePort")
    _insert_port(rules_db, "TriplePort", port_type="cost", event_class="sacrifice")
    _insert_port(rules_db, "TriplePort", port_type="trigger", event_class="Sacrificed")
    _insert_port(
        rules_db,
        "TriplePort",
        port_type="effect",
        event_class="ChangeZone",
        zone_destination="Battlefield",
    )
    result = _find_self_bridging_cascade(rules_db, cmdr_ports, set())
    assert len(result) == 1
    assert result[0].candidate == "TriplePort"


def test_commander_itself_is_excluded_from_results(rules_db: sqlite3.Connection) -> None:
    """A candidate in ``cmdr_set`` must not appear in results even if
    it has the required port pattern (that would be a card matching
    itself)."""
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
    ]
    _insert_card(rules_db, "Korvold, Fae-Cursed King")
    _insert_port(rules_db, "Korvold, Fae-Cursed King", port_type="cost", event_class="sacrifice")
    _insert_port(rules_db, "Korvold, Fae-Cursed King", port_type="trigger", event_class="Sacrificed")
    result = _find_self_bridging_cascade(rules_db, cmdr_ports, {"Korvold, Fae-Cursed King"})
    assert result == []


def test_empty_db_returns_empty(rules_db: sqlite3.Connection) -> None:
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
    ]
    assert _find_self_bridging_cascade(rules_db, cmdr_ports, set()) == []


def test_empty_cmdr_ports_returns_empty(rules_db: sqlite3.Connection) -> None:
    assert _find_self_bridging_cascade(rules_db, [], set()) == []


# ---------------------------------------------------------------------------
# Dedup / key uniqueness
# ---------------------------------------------------------------------------


def test_multiple_candidates_each_emit_one_complement(rules_db: sqlite3.Connection) -> None:
    """Three Bloodghast-shaped candidates should yield three
    complements with identical ``(rule_id, cand_event, filter_group)``
    but distinct ``candidate`` names."""
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
    ]
    for name in ("SacFodderA", "SacFodderB", "SacFodderC"):
        _insert_card(rules_db, name)
        _insert_port(rules_db, name, port_type="cost", event_class="sacrifice")
        _insert_port(rules_db, name, port_type="trigger", event_class="Sacrificed")

    result = _find_self_bridging_cascade(rules_db, cmdr_ports, set())
    assert len(result) == 3
    names = {c.candidate for c in result}
    assert names == {"SacFodderA", "SacFodderB", "SacFodderC"}
    # All share the same rule_id / cand_event / filter_group tuple.
    assert {c.rule_id for c in result} == {"self_bridging_cascade"}
    assert {c.cand_event for c in result} == {"self_bridging"}
    assert {c.filter_group for c in result} == {"depth_2"}


def test_dedup_key_is_stable_across_invocations(rules_db: sqlite3.Connection) -> None:
    """Dedup-key uniqueness: running the helper twice on the same DB
    yields the same 4-tuple dedup keys. Guards against the IDF
    bucketing (FR2) from spurious duplication."""
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
    ]
    _insert_card(rules_db, "SacFodder")
    _insert_port(rules_db, "SacFodder", port_type="cost", event_class="sacrifice")
    _insert_port(rules_db, "SacFodder", port_type="trigger", event_class="Sacrificed")

    run1 = _find_self_bridging_cascade(rules_db, cmdr_ports, set())
    run2 = _find_self_bridging_cascade(rules_db, cmdr_ports, set())

    def _key(c: object) -> tuple[str, str, str, str]:
        return (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)  # type: ignore[attr-defined]

    assert {_key(c) for c in run1} == {_key(c) for c in run2}


# ---------------------------------------------------------------------------
# SQLite concat-distinct correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage1_event_classes",
    [
        ["ChangesZone", "ChangeZone"],  # both in EVENT_MATCH_MAP
        ["sacrifice", "Sacrificed"],  # both via COST_FEEDS_TRIGGER
    ],
)
def test_stage1_count_distinct_concat_trick(
    rules_db: sqlite3.Connection,
    stage1_event_classes: list[str],
) -> None:
    """Stage-1 uses ``COUNT(DISTINCT event_class) >= 2`` (SQLite-clean
    single-column distinct). Verify candidates with exactly 2 distinct
    event_classes pass through."""
    cmdr_ports = [
        _cmdr_port("trigger", "Sacrificed"),
        _cmdr_port("effect", "Sacrifice"),
        _cmdr_port("trigger", "ChangesZone"),
        _cmdr_port("effect", "ChangeZone"),
    ]
    _insert_card(rules_db, "Candidate")
    # Both ports share the same (port_type, valid_filter) but distinct
    # event_class -- exercises the DISTINCT on event_class.
    _insert_port(
        rules_db,
        "Candidate",
        port_type="trigger" if stage1_event_classes[0] != "sacrifice" else "cost",
        event_class=stage1_event_classes[0],
        zone_destination="Battlefield",
    )
    _insert_port(
        rules_db,
        "Candidate",
        port_type="effect" if stage1_event_classes[1] != "Sacrificed" else "trigger",
        event_class=stage1_event_classes[1],
        zone_destination="Battlefield",
    )
    result = _find_self_bridging_cascade(rules_db, cmdr_ports, set())
    assert len(result) == 1
    assert result[0].candidate == "Candidate"
