"""Tests for the toughness_payoff cohort predicate (coverage instrument).

Hermetic: builds a tmp_path synergy.db via open_db and hand-inserts commanders
and ports (same pattern as tests/bench/test_cohorts.py). Never touches the real
data/synergy.db and never uses a project-relative DB literal (CLAUDE.md
Conventions; a conftest autouse fixture fails the run if a new *.db appears
under the repo root or data/).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import toughness_payoff
from mtg_synergy_graph.db import open_db


def _insert_commander(
    conn: sqlite3.Connection,
    name: str,
    *,
    legal: int = 1,
    supertypes: str = "Legendary",
    card_types: str = "Legendary Creature",
) -> None:
    conn.execute(
        "INSERT INTO cards (name, supertypes, card_types, subtypes, cmc, "
        "color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, supertypes, card_types, "", 3, "G", 1000, legal),
    )


def _insert_toughness_port(
    conn: sqlite3.Connection,
    card_name: str,
    *,
    event_class: str = "CardToughness",
    scaling_expression: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, "
        "scaling_expression) VALUES (?, 'scales_with', ?, ?)",
        (card_name, event_class, scaling_expression),
    )


@pytest.fixture()
def cohort_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "synergy.db")

    # (a) scales_with CardToughness via event_class -> selected.
    _insert_commander(conn, "Phenax-like")
    _insert_toughness_port(conn, "Phenax-like", event_class="CardToughness")

    # (b) CardToughness via scaling_expression only -> selected.
    _insert_commander(conn, "Tanazir-like")
    _insert_toughness_port(
        conn,
        "Tanazir-like",
        event_class="Other",
        scaling_expression="Count$CardToughness",
    )

    # (c) explicit combat commander, a legal legendary creature -> selected.
    _insert_commander(conn, "Doran, the Siege Tower")

    # (d) explicit-set name that is NOT a legendary creature -> filtered out.
    _insert_commander(conn, "High Alert", supertypes="", card_types="Enchantment")

    # (e) toughness only in a buff/P-T raw_line, no CardToughness port ->
    #     must NOT be selected (predicate keys on port shape, not raw_line).
    _insert_commander(conn, "Buff Noise Legend")
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) VALUES (?, 'effect', 'Pump', ?)",
        ("Buff Noise Legend", "Target creature gets +0/+3 Toughness"),
    )

    # (f) CardToughness port but NOT legal_commander -> filtered out.
    _insert_commander(conn, "Illegal Toughness Legend", legal=0)
    _insert_toughness_port(conn, "Illegal Toughness Legend")

    # (g) CardToughness port but NOT a creature -> filtered out.
    _insert_commander(
        conn,
        "Toughness Enchantment",
        supertypes="Legendary",
        card_types="Legendary Enchantment",
    )
    _insert_toughness_port(conn, "Toughness Enchantment")

    conn.commit()
    yield conn
    conn.close()


def test_selects_cardtoughness_port_commanders(cohort_db):
    cohort = toughness_payoff(cohort_db)
    assert "Phenax-like" in cohort  # event_class = 'CardToughness'
    assert "Tanazir-like" in cohort  # scaling_expression LIKE '%CardToughness%'


def test_includes_explicit_combat_commander(cohort_db):
    assert "Doran, the Siege Tower" in toughness_payoff(cohort_db)


def test_excludes_non_creature_from_explicit_set(cohort_db):
    # High Alert is in _TOUGHNESS_COMBAT_COMMANDERS but is not a legendary
    # creature -> the join must drop it.
    assert "High Alert" not in toughness_payoff(cohort_db)


def test_excludes_raw_line_toughness_noise(cohort_db):
    # Keys on port shape, never raw_line LIKE '%Toughness%'.
    assert "Buff Noise Legend" not in toughness_payoff(cohort_db)


def test_excludes_illegal_and_non_creature_toughness_ports(cohort_db):
    cohort = toughness_payoff(cohort_db)
    assert "Illegal Toughness Legend" not in cohort  # legal_commander = 0
    assert "Toughness Enchantment" not in cohort  # not a creature


def test_all_selected_are_legal_legendary_creatures(cohort_db):
    cohort = toughness_payoff(cohort_db)
    assert cohort  # non-empty
    placeholders = ",".join("?" * len(cohort))
    rows = cohort_db.execute(
        f"SELECT name FROM cards WHERE name IN ({placeholders}) "  # noqa: S608
        "AND legal_commander = 1 AND supertypes LIKE '%Legendary%' "
        "AND card_types LIKE '%Creature%'",
        tuple(cohort),
    ).fetchall()
    assert len(rows) == len(cohort)
