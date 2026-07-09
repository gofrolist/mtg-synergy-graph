"""Tests for the attack_reward cohort predicate (coverage instrument, Unit 4).

Reads the real ``data/synergy.db`` (mirrors the live-anchor pattern in
tests/bench/test_team_anthem_cohort.py) — skipped when that DB is absent (CI).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.bench.cohorts import attack_reward
from mtg_synergy_graph.bench.coverage_report import _COHORT_DISPATCH
from mtg_synergy_graph.db import open_db

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "synergy.db"


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_attack_reward_includes_known_members():
    conn = open_db(str(LIVE_DB), create=False)
    try:
        members = attack_reward(conn)
    finally:
        conn.close()
    assert "Agrus Kos, Wojek Veteran" in members  # self-attack + team PumpAll
    assert "Aloy, Savior of Meridian" in members  # AttackersDeclared / AttackingPlayer=You


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_attack_reward_excludes_exalted():
    conn = open_db(str(LIVE_DB), create=False)
    try:
        # Rafiq is Exalted — rewards attacking alone, not a wide board.
        assert "Rafiq of the Many" not in attack_reward(conn)
    finally:
        conn.close()


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live synergy.db not present")
def test_attack_reward_excludes_tribal():
    conn = open_db(str(LIVE_DB), create=False)
    try:
        # Najeela is a Warrior-tribal commander — routed to the tribal rules.
        assert "Najeela, the Blade-Blossom" not in attack_reward(conn)
    finally:
        conn.close()


def test_attack_reward_registered_in_dispatch():
    assert _COHORT_DISPATCH.get("attack_reward") is attack_reward
