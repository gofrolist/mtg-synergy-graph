"""Tests for mechanics_vectors zone-aware concepts and vector construction."""

import os
import sqlite3

import numpy as np
import pytest

from mtg_synergy.recommend.mechanics_vectors import (
    GAME_CONCEPTS,
    N_CONCEPTS,
    _concept_idx,
    build_mechanics_vectors,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")


def _needs_db():
    if not os.path.exists(DB_PATH):
        pytest.skip("tags.db not found")


# --- Unit tests (no DB needed) ---

def test_zone_concepts_exist():
    """All 5 zone-aware concepts are present in GAME_CONCEPTS."""
    zone_concepts = [
        "enters_from_graveyard",
        "enters_from_exile",
        "enters_from_hand",
        "goes_to_graveyard",
        "goes_to_exile",
    ]
    for concept in zone_concepts:
        assert concept in GAME_CONCEPTS, f"Missing zone concept: {concept}"
        assert concept in _concept_idx, f"Missing from _concept_idx: {concept}"


def test_n_concepts_is_32():
    """N_CONCEPTS should be 32 (27 original + 5 zone-aware)."""
    assert N_CONCEPTS == 32, f"Expected N_CONCEPTS=32, got {N_CONCEPTS}"


def test_concept_indices_unique():
    """All concept indices should be unique and contiguous."""
    indices = list(_concept_idx.values())
    assert len(indices) == len(set(indices)), "Duplicate concept indices"
    assert sorted(indices) == list(range(N_CONCEPTS)), "Non-contiguous indices"


def test_zone_concepts_after_artifact_available():
    """Zone concepts should appear after artifact_available in GAME_CONCEPTS."""
    artifact_idx = GAME_CONCEPTS.index("artifact_available")
    zone_start = GAME_CONCEPTS.index("enters_from_graveyard")
    assert zone_start == artifact_idx + 1, (
        f"Zone concepts should start right after artifact_available "
        f"(idx {artifact_idx}), but start at {zone_start}"
    )


# --- Integration tests (need DB) ---

def test_build_vectors_dimension_increased():
    """build_mechanics_vectors returns dim >= 32 (concepts + subtypes)."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        produces, consumes, dim, subtype_idx = build_mechanics_vectors(conn)
        assert dim >= 32, f"Expected dim >= 32, got {dim}"
        assert dim == N_CONCEPTS + len(subtype_idx), (
            f"dim={dim} != N_CONCEPTS({N_CONCEPTS}) + subtypes({len(subtype_idx)})"
        )
    finally:
        conn.close()


def test_graveyard_producers_exist():
    """Some cards should produce goes_to_graveyard or enters_from_graveyard."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        produces, consumes, dim, _ = build_mechanics_vectors(conn)
        grave_prod_idx = _concept_idx["goes_to_graveyard"]
        grave_enter_idx = _concept_idx["enters_from_graveyard"]
        grave_producers = sum(
            1 for v in produces.values()
            if v[grave_prod_idx] > 0 or v[grave_enter_idx] > 0
        )
        assert grave_producers > 0, "No cards produce graveyard zone concepts"
    finally:
        conn.close()


def test_graveyard_consumers_exist():
    """Some cards should consume enters_from_graveyard."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        produces, consumes, dim, _ = build_mechanics_vectors(conn)
        grave_idx = _concept_idx["enters_from_graveyard"]
        grave_consumers = sum(1 for v in consumes.values() if v[grave_idx] > 0)
        assert grave_consumers > 0, "No cards consume enters_from_graveyard"
    finally:
        conn.close()


def test_vectors_l2_normalized():
    """All produces/consumes vectors should be L2-normalized (norm ~1.0)."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        produces, consumes, dim, _ = build_mechanics_vectors(conn)
        # Non-zero vectors should be L2-normalized; zero vectors are allowed
        for oid, vec in list(produces.items())[:100]:
            norm = np.linalg.norm(vec)
            if norm > 0:
                assert abs(norm - 1.0) < 1e-5, (
                    f"Produces vector for {oid} not normalized: norm={norm}"
                )
        for oid, vec in list(consumes.items())[:100]:
            norm = np.linalg.norm(vec)
            if norm > 0:
                assert abs(norm - 1.0) < 1e-5, (
                    f"Consumes vector for {oid} not normalized: norm={norm}"
                )
    finally:
        conn.close()


def test_preloaded_abilities_with_zone_fields():
    """build_mechanics_vectors handles preloaded tuples with zone fields."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        # Create a minimal preloaded abilities list with zone fields
        fake_oid = "00000000-0000-0000-0000-000000000001"
        abilities = [
            # A ChangeZone from Graveyard to Battlefield (reanimation effect)
            (fake_oid, "ChangeZone", None, None, None, None, None, None,
             None, None, "Graveyard", "Battlefield"),
            # A trigger on ChangesZone with origin Graveyard (responds to reanimation)
            (fake_oid + "x", None, "ChangesZone", None, None, None, None, None,
             None, None, "Graveyard", None),
        ]
        produces, consumes, dim, _ = build_mechanics_vectors(
            conn, preloaded_abilities=abilities
        )
        # The ChangeZone verb should produce enters_from_graveyard
        assert fake_oid in produces
        assert produces[fake_oid][_concept_idx["enters_from_graveyard"]] > 0
        # The trigger should consume enters_from_graveyard
        assert fake_oid + "x" in consumes
        assert consumes[fake_oid + "x"][_concept_idx["enters_from_graveyard"]] > 0
    finally:
        conn.close()
