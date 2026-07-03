"""Tests for the archetype-payoff cohort predicate module (plan 2026-07-03-001 Unit 1).

The cohort module is an eval-harness instrument: a pure read-side partition of
commanders with a subtype-keyed death payoff. DBs are built via ``open_db`` on
``tmp_path`` paths only — never project-relative literals (CLAUDE.md
Conventions; a conftest autouse fixture fails the run if a new ``*.db`` appears
under the repo root or ``data/``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import cohorts
from mtg_synergy_graph.bench.cohorts import (
    archetype_payoff_cohort,
    subtype_death_payoff,
)
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
        (name, supertypes, card_types, "", 3, "B", 1000, legal),
    )


def _insert_port(
    conn: sqlite3.Connection,
    card_name: str,
    *,
    port_type: str,
    event_class: str,
    valid_filter: str | None = None,
    zone_origin: str | None = None,
    zone_destination: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, "
        "zone_origin, zone_destination) VALUES (?, ?, ?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination),
    )
    return int(cur.lastrowid)


def _seed_token_subtype_vocab(conn: sqlite3.Connection, subtypes: list[str]) -> None:
    """Register token-producible subtypes in the global vocab.

    The vocab is ``DISTINCT attr_value WHERE attr_kind='token_subtype'`` across
    all ports; we attach the subtypes to one dummy producer port so they exist.
    """
    _insert_commander(conn, "Token Producer")
    pid = _insert_port(conn, "Token Producer", port_type="effect", event_class="Token")
    for subtype in subtypes:
        conn.execute(
            "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', ?)",
            (pid, subtype),
        )


@pytest.fixture()
def cohort_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "synergy.db")
    # Token-producible subtypes present in the world.
    _seed_token_subtype_vocab(conn, ["Saproling", "Zombie", "Treasure"])

    # Slimefoot: Saproling death trigger (Sacrificed) — IN cohort.
    _insert_commander(conn, "Slimefoot, the Stowaway")
    _insert_port(
        conn,
        "Slimefoot, the Stowaway",
        port_type="trigger",
        event_class="Sacrificed",
        valid_filter="Saproling.YouCtrl",
    )

    # Wilhelt: Zombie death trigger via ChangesZone Battlefield->Graveyard — IN.
    _insert_commander(conn, "Wilhelt, the Rotcleaver")
    _insert_port(
        conn,
        "Wilhelt, the Rotcleaver",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Zombie.YouCtrl+Other",
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )

    # Plain goodstuff commander: an ETB trigger, no subtype death — OUT.
    _insert_commander(conn, "Goodstuff General")
    _insert_port(
        conn,
        "Goodstuff General",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Creature.Self",
        zone_origin="Any",
        zone_destination="Battlefield",
    )

    # Subtype trigger but NON-death event (Attacks) — OUT (death-event gate).
    _insert_commander(conn, "Attacker Lord")
    _insert_port(
        conn,
        "Attacker Lord",
        port_type="trigger",
        event_class="Attacks",
        valid_filter="Zombie.YouCtrl",
    )

    # Death trigger but subtype NOT in token vocab (Wall) — OUT (vocab gate).
    _insert_commander(conn, "Wall Whisperer")
    _insert_port(
        conn,
        "Wall Whisperer",
        port_type="trigger",
        event_class="Sacrificed",
        valid_filter="Wall.YouCtrl",
    )

    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# subtype_death_payoff
# ---------------------------------------------------------------------------


def test_sacrificed_subtype_trigger_is_in_cohort(cohort_db: sqlite3.Connection) -> None:
    """Happy path: a Saproling Sacrificed trigger puts Slimefoot in the cohort."""
    cohort = subtype_death_payoff(cohort_db)
    assert "Slimefoot, the Stowaway" in cohort


def test_changeszone_to_graveyard_subtype_trigger_is_in_cohort(
    cohort_db: sqlite3.Connection,
) -> None:
    """Happy path: Battlefield->Graveyard subtype trigger counts as death."""
    cohort = subtype_death_payoff(cohort_db)
    assert "Wilhelt, the Rotcleaver" in cohort


def test_goodstuff_commander_excluded(cohort_db: sqlite3.Connection) -> None:
    """A commander with no subtype-death trigger is excluded."""
    cohort = subtype_death_payoff(cohort_db)
    assert "Goodstuff General" not in cohort


def test_non_death_event_excluded(cohort_db: sqlite3.Connection) -> None:
    """Edge: a subtype trigger on a non-death event (Attacks) is excluded."""
    cohort = subtype_death_payoff(cohort_db)
    assert "Attacker Lord" not in cohort


def test_non_token_subtype_excluded(cohort_db: sqlite3.Connection) -> None:
    """Edge: a death trigger on a non-token-producible subtype is excluded."""
    cohort = subtype_death_payoff(cohort_db)
    assert "Wall Whisperer" not in cohort


def test_non_legal_commander_excluded(tmp_path: Path) -> None:
    """Edge: an illegal-commander card with a subtype death trigger is excluded."""
    conn = open_db(tmp_path / "synergy.db")
    _seed_token_subtype_vocab(conn, ["Saproling"])
    _insert_commander(conn, "Banned Fungus", legal=0)
    _insert_port(
        conn,
        "Banned Fungus",
        port_type="trigger",
        event_class="Sacrificed",
        valid_filter="Saproling.YouCtrl",
    )
    conn.commit()
    try:
        assert "Banned Fungus" not in subtype_death_payoff(conn)
    finally:
        conn.close()


def test_returns_set_of_str(cohort_db: sqlite3.Connection) -> None:
    result = subtype_death_payoff(cohort_db)
    assert isinstance(result, set)
    assert all(isinstance(name, str) for name in result)


# ---------------------------------------------------------------------------
# archetype_payoff_cohort (union)
# ---------------------------------------------------------------------------


def test_union_equals_single_seeded_predicate(cohort_db: sqlite3.Connection) -> None:
    """With one seeded predicate, the union equals that predicate's output."""
    assert archetype_payoff_cohort(cohort_db) == subtype_death_payoff(cohort_db)


def test_union_reflects_added_predicate(cohort_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a second predicate to the tuple changes the union as expected."""

    def dummy_predicate(_conn: sqlite3.Connection) -> set[str]:
        return {"Synthetic Extra Commander"}

    monkeypatch.setattr(
        cohorts,
        "_COHORT_PREDICATES",
        (*cohorts._COHORT_PREDICATES, dummy_predicate),
    )
    cohort = archetype_payoff_cohort(cohort_db)
    assert "Synthetic Extra Commander" in cohort
    assert "Slimefoot, the Stowaway" in cohort
