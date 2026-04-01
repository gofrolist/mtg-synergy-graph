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


def test_n_concepts_is_36():
    """N_CONCEPTS should be 36 (27 original + 5 zone-aware + 4 theme)."""
    assert N_CONCEPTS == 36, f"Expected N_CONCEPTS=36, got {N_CONCEPTS}"


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


def test_replacement_opponent_mill_excluded():
    """Opponent-only replacement effects (like Bruvac) should NOT produce card_milled."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        bruvac_oid = "fake-bruvac-0000"
        self_mill_oid = "fake-selfmill-0000"
        abilities = [
            # Bruvac: R: replacement, verb=None, Event$ in raw_line, no defined
            (bruvac_oid, None, None, None, None, None, None, None,
             "R:Event$ Mill | ValidPlayer$ Player.Opponent | ReplaceWith$ MillTwice",
             None, None, None, None),
            # A normal self-mill card: verb=Mill, defined=You
            (self_mill_oid, "Mill", None, None, None, None, None, None,
             "A:AB$ Mill | Cost$ T | Defined$ You | NumCards$ 3",
             None, None, None, "You"),
        ]
        produces, consumes, dim, _ = build_mechanics_vectors(
            conn, preloaded_abilities=abilities
        )
        mill_idx = _concept_idx["card_milled"]
        # Bruvac should NOT produce card_milled (opponent-only replacement)
        if bruvac_oid in produces:
            assert produces[bruvac_oid][mill_idx] == 0.0, (
                "Opponent-only mill replacement should not produce card_milled"
            )
        # Normal self-mill card SHOULD produce card_milled
        assert self_mill_oid in produces
        assert produces[self_mill_oid][mill_idx] > 0, (
            "Normal Mill verb should produce card_milled"
        )
    finally:
        conn.close()


def test_trigger_opponent_mill_excluded():
    """T: abilities that only mill opponents (defined=Player.Opponent) should NOT produce card_milled."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        manic_oid = "fake-manicscribe-0000"
        mindcrank_oid = "fake-mindcrank-0000"
        hedron_oid = "fake-hedroncrab-0000"
        abilities = [
            # Manic Scribe ETB: verb=Mill, defined=Player.Opponent
            (manic_oid, "Mill", "ChangesZone", None, None, None, None, None,
             "T:Mode$ ChangesZone | Execute$ TrigMill1",
             None, None, None, "Player.Opponent"),
            # Mindcrank: verb=Mill, defined=TriggeredPlayer, ValidPlayer$Opponent
            (mindcrank_oid, "Mill", "LifeLost", None, None, None, None, None,
             "T:Mode$ LifeLost | ValidPlayer$ Opponent | Execute$ TrigMill",
             None, None, None, "TriggeredPlayer"),
            # Hedron Crab: verb=Mill, target=Player (any player, can self-mill)
            (hedron_oid, "Mill", "ChangesZone", None, None, None, None, None,
             "T:Mode$ ChangesZone | Execute$ TrigMill",
             None, None, None, None),
        ]
        produces, consumes, dim, _ = build_mechanics_vectors(
            conn, preloaded_abilities=abilities
        )
        mill_idx = _concept_idx["card_milled"]
        # Manic Scribe: opponent-only → no card_milled
        if manic_oid in produces:
            assert produces[manic_oid][mill_idx] == 0.0, (
                "Opponent-only Mill (defined=Player.Opponent) should not produce card_milled"
            )
        # Mindcrank: TriggeredPlayer + ValidPlayer$Opponent → no card_milled
        if mindcrank_oid in produces:
            assert produces[mindcrank_oid][mill_idx] == 0.0, (
                "Opponent-only Mill (TriggeredPlayer+ValidPlayer$Opponent) should not produce card_milled"
            )
        # Hedron Crab: any-player targeting → SHOULD produce card_milled
        assert hedron_oid in produces
        assert produces[hedron_oid][mill_idx] > 0, (
            "Any-player Mill (no opponent restriction) should produce card_milled"
        )
    finally:
        conn.close()


def test_replacement_self_life_gain_produces():
    """Self-targeting replacement effects (like Rhox Faithmender) SHOULD produce concepts."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        faithmender_oid = "fake-faithmender-0000"
        abilities = [
            # Rhox Faithmender: R: replacement, verb=None, Event$ in raw_line
            (faithmender_oid, None, None, None, None, None, None, None,
             "R:Event$ GainLife | ValidPlayer$ You | ReplaceWith$ GainDouble",
             None, None, None, None),
        ]
        produces, consumes, dim, _ = build_mechanics_vectors(
            conn, preloaded_abilities=abilities
        )
        life_idx = _concept_idx["life_gained"]
        # Self-targeting life gain replacement SHOULD produce life_gained
        assert faithmender_oid in produces
        assert produces[faithmender_oid][life_idx] > 0, (
            "Self-targeting life gain replacement should produce life_gained"
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
