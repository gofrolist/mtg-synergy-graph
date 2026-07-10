import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import aristocrats
from mtg_synergy_graph.bench.coverage_report import _COHORT_DISPATCH
from mtg_synergy_graph.complement_rules.aristocrats import _commander_is_aristocrats

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"
_LLC = "c.legal_commander=1 AND c.supertypes LIKE '%Legendary%' AND c.card_types LIKE '%Creature%'"


def test_dispatch_registration():
    assert _COHORT_DISPATCH.get("aristocrats") is aristocrats


@pytest.mark.skipif(not LIVE_DB.exists(), reason="requires built data/synergy.db")
def test_aristocrats_cohort_membership():
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    members = aristocrats(conn)
    # Sacrifice-outlet and death-trigger commanders are IN.
    assert "Yawgmoth, Thran Physician" in members
    assert "Meren of Clan Nel Toth" in members
    # NOTE: "Teysa Karlov" (per the original brief) does NOT qualify -- her
    # ability is a static Panharmonicon-style trigger-doubler
    # (port_type='static', event_class='Panharmonicon'), not a
    # trigger/ChangesZone port or a sacrifice cost, so the mechanical gate
    # correctly excludes her (verified against the shipped
    # complement_rules.aristocrats._commander_is_aristocrats, which encodes
    # the identical condition). "Teysa, Opulent Oligarch" has a genuine
    # trigger/ChangesZone Battlefield->Graveyard port and is used here instead.
    assert "Teysa, Opulent Oligarch" in members
    # A commander with no sac-outlet / death trigger is OUT.
    assert "Azusa, Lost but Seeking" not in members
    # God-Eternal Bontu has a ChangesZone trigger whose zone_destination is a
    # comma-list ('Graveyard,...'); the SQL must match it via substring, not
    # exact-equality.
    assert "God-Eternal Bontu" in members
    # Gorbag's sacrifice cost references lowercase 'that creature' (no capital-C
    # 'Creature' type); the case-sensitive gate correctly excludes him.
    assert "Gorbag of Minas Morgul" not in members


@pytest.mark.skipif(not LIVE_DB.exists(), reason="requires built data/synergy.db")
def test_aristocrats_cohort_matches_python_gate_exactly():
    # Drift guard (PR review, Task 4): the SQL cohort predicate MUST be
    # set-equal to the shipped Python gate _commander_is_aristocrats over the
    # live DB, or the pinned fixture + noise band no longer mirror the rule's
    # actual firing. This is why the SQL uses instr(col,'x')>0 (case-sensitive
    # substring == Python `in`) rather than `= 'x'` / `LIKE '%x%'`.
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    sql_set = aristocrats(conn)
    ports_by: dict[str, list[dict]] = {}
    cols = "card_name, port_type, event_class, cost_subtype, cost_target, zone_origin, zone_destination"
    for row in conn.execute(f"SELECT {cols} FROM card_ports"):  # noqa: S608 — static column list
        ports_by.setdefault(row["card_name"], []).append(dict(row))
    names = [r["name"] for r in conn.execute(f"SELECT DISTINCT c.name FROM cards c WHERE {_LLC}")]  # noqa: S608 — module constant
    py_set = {n for n in names if _commander_is_aristocrats(ports_by.get(n, []))}
    assert sql_set == py_set, (
        f"cohort drift: SQL-only={sorted(sql_set - py_set)[:5]}, Python-only={sorted(py_set - sql_set)[:5]}"
    )
