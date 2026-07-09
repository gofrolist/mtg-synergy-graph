"""Tests for the team_anthem cohort predicate (coverage instrument, Unit 4).

Reads the real ``data/synergy.db`` (mirrors the live-anchor pattern in
tests/bench/test_outlet_cohort.py) — skipped when that DB is absent (CI).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import team_anthem
from mtg_synergy_graph.bench.coverage_report import _COHORT_DISPATCH
from mtg_synergy_graph.db import open_db

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_team_anthem_includes_known_members():
    conn = open_db(str(LIVE_DB), create=False)
    try:
        members = team_anthem(conn)
    finally:
        conn.close()
    assert "Avacyn, Angel of Hope" in members
    assert "Iroas, God of Victory" in members


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_team_anthem_excludes_symmetric_anthem():
    conn = open_db(str(LIVE_DB), create=False)
    try:
        # Ascendant Evincar is symmetric (Creature.Black+Other, no YouCtrl).
        assert "Ascendant Evincar" not in team_anthem(conn)
    finally:
        conn.close()


def test_team_anthem_registered_in_dispatch():
    assert _COHORT_DISPATCH.get("team_anthem") is team_anthem
