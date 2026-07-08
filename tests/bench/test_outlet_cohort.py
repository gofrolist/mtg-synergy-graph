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


# ---------------------------------------------------------------------------
# Outlet-payoff cohort fixture shape (plan 2026-07-07-002 Task 3)
# ---------------------------------------------------------------------------
# ``tests/fixtures/golden_set_outlet_payoff.json`` is built by
# ``scripts/bootstrap_outlet_payoff_fixture.py`` — a thin entry point over
# ``scripts/bootstrap_archetype_payoff_fixture.py``'s parameterized build/pin
# protocol (Task 3), pinning ``outlet_direction_death_payoff`` the same way
# ``golden_set_archetype_payoff.json`` pins ``archetype_payoff_cohort``. It is
# a SEPARATE fixture: ``outlet_direction_death_payoff`` is deliberately not a
# member of ``archetype_payoff_cohort``'s predicate union (see that
# function's docstring), so this fixture's existence never perturbs the
# existing one.

OUTLET_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden_set_outlet_payoff.json"


def test_outlet_fixture_loads_with_config_hash_and_cohort_members() -> None:
    """The committed fixture loads and carries both provenance fields."""
    from mtg_synergy_graph.bench.fixture import SCHEMA_VERSION, PinnedFixture

    fixture = PinnedFixture.load(OUTLET_FIXTURE)
    assert fixture.schema_version == SCHEMA_VERSION
    assert fixture.config_hash, "fixture has no config_hash — re-pin via bootstrap_outlet_payoff_fixture.py"
    assert fixture.cohort_members, "fixture has no cohort_members snapshot"
    assert fixture.entries, "fixture has no per-commander entries"
    # Every pinned entry corresponds to a snapshotted cohort member (build
    # protocol scores exactly the kept-commander list it snapshots).
    assert {e.commander for e in fixture.entries} <= set(fixture.cohort_members)


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_outlet_fixture_members_are_subset_of_live_predicate() -> None:
    """The pinned (EDHREC-filtered) snapshot never contains a non-cohort name.

    The fixture's ``cohort_members`` is the EDHREC High-Synergy-filtered
    subset of the raw predicate output (mirrors the archetype-payoff
    fixture's ``test_membership_matches_pinned_snapshot`` in
    ``tests/test_death_payoff.py``), so it must be a subset of — not
    necessarily equal to — the live, unfiltered ``outlet_direction_death_payoff``
    result.
    """
    import json

    from mtg_synergy_graph.db import open_db as _open_db

    pinned = set(json.loads(OUTLET_FIXTURE.read_text(encoding="utf-8"))["cohort_members"])
    conn = _open_db(str(LIVE_DB), create=False)
    try:
        live = outlet_direction_death_payoff(conn)
    finally:
        conn.close()

    missing = pinned - live
    assert not missing, f"Pinned cohort_members not in live predicate output: {sorted(missing)}"
