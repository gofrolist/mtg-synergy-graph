"""Shared fixtures for the mtg_synergy_graph test suite."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph import SynergyEngine, parse_card_file

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FULL_DB_PATH = Path(os.environ.get("MTG_SYNERGY_GRAPH_FULL_DB", "/tmp/synergy_full.db"))
_HAS_FULL_DB = FULL_DB_PATH.exists()


def _load(name: str) -> dict:
    return parse_card_file(FIXTURES_DIR / name)


@pytest.fixture(scope="session")
def cathars_crusade() -> dict:
    return _load("cathars_crusade.txt")


@pytest.fixture(scope="session")
def korvold() -> dict:
    return _load("korvold_fae_cursed_king.txt")


@pytest.fixture(scope="session")
def panharmonicon() -> dict:
    return _load("panharmonicon.txt")


@pytest.fixture(scope="session")
def rhystic_study() -> dict:
    return _load("rhystic_study.txt")


@pytest.fixture(scope="session")
def scute_swarm() -> dict:
    return _load("scute_swarm.txt")


@pytest.fixture(scope="session")
def full_engine():
    """Session-scoped SynergyEngine against the full DB.

    Shared across all test files so the per-commander score cache is
    populated once and reused — avoids re-running ``score_all_candidates``
    for the same commander in different test modules.
    """
    if not _HAS_FULL_DB:
        pytest.skip(f"full DB not found at {FULL_DB_PATH}")
    eng = SynergyEngine(FULL_DB_PATH)
    yield eng
    eng.close()


@pytest.fixture(scope="session")
def full_conn():
    """Session-scoped raw SQLite connection to the full DB."""
    if not _HAS_FULL_DB:
        pytest.skip(f"full DB not found at {FULL_DB_PATH}")
    conn = sqlite3.connect(FULL_DB_PATH)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
