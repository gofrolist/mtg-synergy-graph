"""Tests for ``tests/_parity.py`` rule-level parity harness (issue #16).

These tests prove the harness correctly detects parity (and parity
violations) so a future migrator can rely on a green run BEFORE
deleting a Python helper. The synthetic helpers below mirror the
``cascade_tribal`` declarative rule on a tiny fixture; one is faithful,
two are intentionally broken to exercise the error path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from mtg_synergy_graph.complement_rules._interpreter_cache import clear_interpreter_cache
from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import clear_ports_cache
from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db
from tests._parity import assert_rule_parity


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    clear_ports_cache()
    clear_interpreter_cache()


def _seed_card(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, subtypes, cmc, "
        "color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, 'Creature', '', 4, 'G', 1000, 1)",
        (name,),
    )


def _seed_keyword_port(conn: sqlite3.Connection, card_name: str, keyword: str) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, 'keyword', ?, ?)",
        (card_name, keyword, f"K:{keyword}"),
    )


def _build_cascade_fixture(conn: sqlite3.Connection) -> None:
    """Synthetic fixture: 1 Cascade commander + 2 Cascade partners +
    1 commander-self + 1 unrelated card.

    The interpreter should emit ``cascade_tribal`` complements for the
    two non-self Cascade partners, and nothing for the unrelated card.
    """
    for name in ("Cmdr", "PartnerA", "PartnerB", "Unrelated"):
        _seed_card(conn, name)
        if name != "Unrelated":
            _seed_keyword_port(conn, name, "Cascade")
    seed_rules_db(conn)
    conn.commit()


def _faithful_cascade_helper(conn: sqlite3.Connection, commanders: Sequence[str]) -> list[PortComplement]:
    """Reference Python implementation of the declarative
    ``cascade_tribal`` rule. Returns the same PortComplement set the
    interpreter does on the same DB.

    Handles the multi-commander commander_set shape (a partner-pair
    commander or a Background combo passes two names). The gate fires
    if ANY commander in the set carries the Cascade keyword, and the
    candidate-side excludes every commander name.
    """
    cmdr_set = set(commanders)
    if not cmdr_set:
        return []
    placeholders = ",".join("?" * len(cmdr_set))
    cmdr_names = tuple(cmdr_set)
    # placeholders is a count-derived string of `?,?,?,…` — no user input
    # flows into the SQL fragment; the commander names are bound via
    # parameters. ruff S608's heuristic flags the f-string anyway.
    sql = (
        f"SELECT 1 FROM card_ports WHERE card_name IN ({placeholders}) "  # noqa: S608
        "AND port_type='keyword' AND event_class='Cascade' LIMIT 1"
    )
    cmdr_has_cascade = bool(conn.execute(sql, cmdr_names).fetchone())
    if not cmdr_has_cascade:
        return []
    partners = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type='keyword' AND event_class='Cascade'"
        ).fetchall()
        if row[0] not in cmdr_set
    ]
    return [
        PortComplement(
            rule_id="cascade_tribal",
            direction="synergy",
            candidate=p,
            cmdr_event="cascade_tribal",
            cand_event="same_keyword_partner",
        )
        for p in partners
    ]


def _missing_partner_helper(conn: sqlite3.Connection, commanders: Sequence[str]) -> list[PortComplement]:
    """Buggy helper: drops the first partner. Should fail parity."""
    return _faithful_cascade_helper(conn, commanders)[1:]


def _extra_phantom_helper(conn: sqlite3.Connection, commanders: Sequence[str]) -> list[PortComplement]:
    """Buggy helper: emits an extra PortComplement for a non-existent
    partner. Should fail parity."""
    return [
        *_faithful_cascade_helper(conn, commanders),
        PortComplement(
            rule_id="cascade_tribal",
            direction="synergy",
            candidate="Phantom",
            cmdr_event="cascade_tribal",
            cand_event="same_keyword_partner",
        ),
    ]


def test_parity_passes_when_helper_mirrors_interpreter(tmp_path: Path) -> None:
    """A faithful helper produces the same PortComplement set as the
    interpreter — ``assert_rule_parity`` must not raise."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _build_cascade_fixture(conn)
        assert_rule_parity(
            conn,
            ["Cmdr"],
            rule_id="cascade_tribal",
            py_helper=_faithful_cascade_helper,
        )
    finally:
        conn.close()


def test_parity_fails_when_helper_misses_partner(tmp_path: Path) -> None:
    """A helper that drops a partner the interpreter finds must fail
    with a message naming the missing row."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _build_cascade_fixture(conn)
        with pytest.raises(AssertionError) as exc_info:
            assert_rule_parity(
                conn,
                ["Cmdr"],
                rule_id="cascade_tribal",
                py_helper=_missing_partner_helper,
            )
        msg = str(exc_info.value)
        assert "cascade_tribal" in msg
        # The interpreter has rows the helper omitted.
        assert "In interpreter but not in Python helper" in msg
        # And the missing row's name must appear in the error.
        assert "PartnerA" in msg or "PartnerB" in msg
    finally:
        conn.close()


def test_parity_fails_when_helper_emits_phantom_row(tmp_path: Path) -> None:
    """A helper that emits a partner the interpreter does not find
    must fail with a message naming the phantom row."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _build_cascade_fixture(conn)
        with pytest.raises(AssertionError) as exc_info:
            assert_rule_parity(
                conn,
                ["Cmdr"],
                rule_id="cascade_tribal",
                py_helper=_extra_phantom_helper,
            )
        msg = str(exc_info.value)
        assert "In Python helper but not in interpreter" in msg
        assert "Phantom" in msg
    finally:
        conn.close()


def test_parity_passes_with_multi_commander_partner_pair(tmp_path: Path) -> None:
    """A partner-pair commander_set (two cards, both carrying Cascade)
    must produce parity: the demo helper's IN-clause expands to cover
    both names, and the interpreter likewise excludes both from
    candidate matches."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        for name in ("CmdrA", "CmdrB", "PartnerA", "PartnerB"):
            _seed_card(conn, name)
            _seed_keyword_port(conn, name, "Cascade")
        seed_rules_db(conn)
        conn.commit()

        assert_rule_parity(
            conn,
            ["CmdrA", "CmdrB"],
            rule_id="cascade_tribal",
            py_helper=_faithful_cascade_helper,
        )
    finally:
        conn.close()


def _duplicating_cascade_helper(conn: sqlite3.Connection, commanders: Sequence[str]) -> list[PortComplement]:
    """Buggy helper: emits every PortComplement twice. The interpreter
    dedupes; the harness must catch this as a multiplicity mismatch
    (set comparison would silently pass)."""
    base = _faithful_cascade_helper(conn, commanders)
    return [*base, *base]


def test_parity_fails_when_helper_emits_duplicates_interpreter_does_not(
    tmp_path: Path,
) -> None:
    """Multiset guard: helper emits each row twice, interpreter once.
    Set comparison would silently pass (the unique sets are identical);
    multiset (Counter) comparison must reject. The raise itself is
    the proof — under the old frozenset-based implementation this
    test would not raise.
    """
    conn = open_db(tmp_path / "synergy.db")
    try:
        _build_cascade_fixture(conn)
        with pytest.raises(AssertionError) as exc_info:
            assert_rule_parity(
                conn,
                ["Cmdr"],
                rule_id="cascade_tribal",
                py_helper=_duplicating_cascade_helper,
            )
        msg = str(exc_info.value)
        # Side: Python helper has 2 extra rows (one extra copy of each
        # partner). The header line reports the total extra count.
        assert "In Python helper but not in interpreter (2)" in msg
        # And the rows themselves are listed by partner name.
        assert "PartnerA" in msg
        assert "PartnerB" in msg
    finally:
        conn.close()


def _double_duplicating_cascade_helper(conn: sqlite3.Connection, commanders: Sequence[str]) -> list[PortComplement]:
    """Buggy helper: emits each row three times so the per-row delta
    is (x2). Used to exercise the multiplicity-marker formatter."""
    base = _faithful_cascade_helper(conn, commanders)
    return [*base, *base, *base]


def test_parity_error_message_includes_multiplicity_marker_when_delta_gt_one(
    tmp_path: Path,
) -> None:
    """When a single PortComplement diverges by more than one copy,
    the error message renders the ``(xN)`` multiplicity marker so the
    reader can distinguish 'extra row' from 'extra copies of an
    existing row'."""
    conn = open_db(tmp_path / "synergy.db")
    try:
        _build_cascade_fixture(conn)
        with pytest.raises(AssertionError) as exc_info:
            assert_rule_parity(
                conn,
                ["Cmdr"],
                rule_id="cascade_tribal",
                py_helper=_double_duplicating_cascade_helper,
            )
        msg = str(exc_info.value)
        # Per partner the helper has 3 copies, interpreter has 1; diff = 2.
        assert "(x2)" in msg, f"expected multiplicity marker (x2) in: {msg!r}"
    finally:
        conn.close()
