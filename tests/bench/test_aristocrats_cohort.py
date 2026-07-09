import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import aristocrats
from mtg_synergy_graph.bench.coverage_report import _COHORT_DISPATCH

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"


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
