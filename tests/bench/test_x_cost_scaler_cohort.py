"""Tests for the x_cost_scaler cohort predicate (coverage instrument, Unit 4).

Reads the real ``data/synergy.db`` (mirrors the live-anchor pattern in
tests/bench/test_attack_reward_cohort.py) — skipped when that DB is absent (CI).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import x_cost_scaler
from mtg_synergy_graph.bench.coverage_report import _COHORT_DISPATCH
from mtg_synergy_graph.db import open_db

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_x_cost_scaler_includes_known_members():
    conn = open_db(str(LIVE_DB), create=False)
    try:
        members = x_cost_scaler(conn)
    finally:
        conn.close()
    assert "Zaxara, the Exemplary" in members  # canonical X-spells commander
    assert "Gadwick, the Wizened" in members


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_x_cost_scaler_excludes_non_xpaid():
    conn = open_db(str(LIVE_DB), create=False)
    try:
        # Krenko is a Goblin token commander with no X-cost ability.
        assert "Krenko, Mob Boss" not in x_cost_scaler(conn)
    finally:
        conn.close()


def test_x_cost_scaler_registered_in_dispatch():
    assert _COHORT_DISPATCH.get("x_cost_scaler") is x_cost_scaler
