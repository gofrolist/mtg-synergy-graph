"""Shared fixtures for the mtg_synergy_graph test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import parse_card_file

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
