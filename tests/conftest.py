"""Shared fixtures for the mtg_synergy_graph test suite."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# scripts/ is not a package but test files import gap_report / forge_oracle /
# scaffold_rule from it for in-process testing. Centralize the sys.path mutation
# here so individual test modules don't each carry their own copy (plan 002
# code-review finding KP-007).
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from mtg_synergy_graph import parse_card_file  # noqa: E402 — after sys.path mutation

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# In-memory SQLite schema for complement-rule tests
# ---------------------------------------------------------------------------
#
# Historically each complement-rule test file defined its own SCHEMA
# string + ``_port`` / ``_add_port`` helpers. Copies drifted
# independently: schema additions (e.g., ``counter_type``,
# ``branch_kind``, ``effect_conditional``) had to land in every file.
# The helpers below consolidate one superset schema matching the
# production ``card_ports`` / ``cards`` columns used by any existing
# ``_find_*`` rule query. New tests should prefer ``rules_db`` +
# ``add_port``; older files keep their local copies until touched for
# other reasons.


_RULES_SCHEMA = """\
CREATE TABLE cards (
    name TEXT PRIMARY KEY,
    card_types TEXT,
    types TEXT,
    subtypes TEXT,
    supertypes TEXT,
    keywords TEXT,
    color_identity TEXT,
    cmc INTEGER,
    edhrec_rank INTEGER,
    oracle_id TEXT,
    legal_commander INTEGER DEFAULT 1
);
CREATE TABLE card_ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name TEXT NOT NULL,
    port_type TEXT NOT NULL,
    event_class TEXT NOT NULL,
    valid_filter TEXT,
    raw_line TEXT,
    zone_origin TEXT,
    zone_destination TEXT,
    counter_type TEXT,
    affected_scope TEXT,
    branch_kind TEXT,
    effect_conditional INTEGER DEFAULT 0,
    replacement_event TEXT,
    replacement_result TEXT,
    execute_ref TEXT
);
"""


@pytest.fixture()
def rules_db():
    """In-memory SQLite connection with the complement-rule schema.

    Schema is the superset of columns touched by any existing
    ``_find_*`` helper. Tests that only need a subset of columns can
    still use this fixture — unused columns default to NULL.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_RULES_SCHEMA)
    try:
        yield c
    finally:
        c.close()


def _load(name: str) -> dict:
    return parse_card_file(FIXTURES_DIR / name)


@pytest.fixture(scope="session")
def bloodghast() -> dict:
    return _load("bloodghast.txt")


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
