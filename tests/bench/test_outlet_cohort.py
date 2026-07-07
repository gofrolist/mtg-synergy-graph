"""Tests for the outlet-direction death-payoff cohort predicate
(plan 2026-07-07-002 Task 2).

Mirrors ``tests/bench/test_cohorts.py``'s DB-fixture pattern: real ``open_db``
schema on a ``tmp_path`` path (never a repo-relative literal — CLAUDE.md
Conventions; a conftest autouse fixture fails the run if a new ``*.db``
appears under the repo root or ``data/``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import (
    outlet_direction_death_payoff,
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
    _insert_commander(conn, "Token Producer")
    pid = _insert_port(conn, "Token Producer", port_type="effect", event_class="Token")
    for subtype in subtypes:
        conn.execute(
            "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', ?)",
            (pid, subtype),
        )


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "synergy.db")
    _seed_token_subtype_vocab(conn, ["Saproling", "Zombie"])
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# outlet_direction_death_payoff
# ---------------------------------------------------------------------------


def test_changeszone_battlefield_to_graveyard_non_self_is_in_cohort(db: sqlite3.Connection) -> None:
    """Happy path: Meren-shaped commander (ChangesZone death, non-self filter)."""
    _insert_commander(db, "Meren-Shaped Commander")
    _insert_port(
        db,
        "Meren-Shaped Commander",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Creature.Other+YouCtrl",
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )
    db.commit()
    assert "Meren-Shaped Commander" in outlet_direction_death_payoff(db)


def test_etb_shaped_trigger_excluded(db: sqlite3.Connection) -> None:
    """A ChangesZone trigger reaching the battlefield (ETB) is not a death event."""
    _insert_commander(db, "ETB Commander")
    _insert_port(
        db,
        "ETB Commander",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Creature.Other+YouCtrl",
        zone_origin="Any",
        zone_destination="Battlefield",
    )
    db.commit()
    assert "ETB Commander" not in outlet_direction_death_payoff(db)


def test_self_only_filter_excluded(db: sqlite3.Connection) -> None:
    """A Card.Self-only death trigger cannot be fed by any other card."""
    _insert_commander(db, "Self Only Commander")
    _insert_port(
        db,
        "Self Only Commander",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Card.Self",
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )
    db.commit()
    assert "Self Only Commander" not in outlet_direction_death_payoff(db)


def test_explicit_sacrificed_trigger_excluded(db: sqlite3.Connection) -> None:
    """A commander with an explicit Sacrificed trigger is served by cost_feeds_trigger."""
    _insert_commander(db, "Sac Trigger Commander")
    _insert_port(
        db,
        "Sac Trigger Commander",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Creature.Other+YouCtrl",
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )
    _insert_port(
        db,
        "Sac Trigger Commander",
        port_type="trigger",
        event_class="Sacrificed",
        valid_filter="Creature.YouCtrl",
    )
    db.commit()
    assert "Sac Trigger Commander" not in outlet_direction_death_payoff(db)


def test_subtype_cohort_member_excluded(db: sqlite3.Connection) -> None:
    """A commander already claimed by subtype_death_payoff is not double-counted."""
    _insert_commander(db, "Subtype Cohort Commander")
    _insert_port(
        db,
        "Subtype Cohort Commander",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Zombie.Other+YouCtrl",
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )
    db.commit()
    assert "Subtype Cohort Commander" in subtype_death_payoff(db)
    assert "Subtype Cohort Commander" not in outlet_direction_death_payoff(db)


def test_non_legendary_excluded(db: sqlite3.Connection) -> None:
    """The SQL-level legal-legendary-creature gate mirrors subtype_death_payoff."""
    _insert_commander(db, "Nonlegendary Beater", supertypes="", card_types="Creature")
    _insert_port(
        db,
        "Nonlegendary Beater",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Creature.Other+YouCtrl",
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )
    db.commit()
    assert "Nonlegendary Beater" not in outlet_direction_death_payoff(db)


def test_returns_set_of_str(db: sqlite3.Connection) -> None:
    _insert_commander(db, "Meren-Shaped Commander")
    _insert_port(
        db,
        "Meren-Shaped Commander",
        port_type="trigger",
        event_class="ChangesZone",
        valid_filter="Creature.Other+YouCtrl",
        zone_origin="Battlefield",
        zone_destination="Graveyard",
    )
    db.commit()
    result = outlet_direction_death_payoff(db)
    assert isinstance(result, set)
    assert all(isinstance(name, str) for name in result)


# ---------------------------------------------------------------------------
# Live anchors (Step 3) — skipif on data/synergy.db
# ---------------------------------------------------------------------------

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"

_ANCHORS = (
    "Judith, the Scourge Diva",
    "Marchesa, the Black Rose",
    "Meren of Clan Nel Toth",
    "Titania, Protector of Argoth",
    "The Gitrog Monster",
)


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
class TestLiveAnchors:
    def test_anchors_and_cohort_size(self) -> None:
        from mtg_synergy_graph.db import open_db as _open_db

        conn = _open_db(str(LIVE_DB), create=False)
        try:
            cohort = outlet_direction_death_payoff(conn)
        finally:
            conn.close()

        missing = [name for name in _ANCHORS if name not in cohort]
        assert not missing, f"Anchor(s) missing from outlet_direction_death_payoff: {missing}"

        assert "Slimefoot, the Stowaway" not in cohort
        assert "Wilhelt, the Rotcleaver" not in cohort

        assert 120 <= len(cohort) <= 150, f"Cohort size {len(cohort)} outside tolerance [120, 150]"
