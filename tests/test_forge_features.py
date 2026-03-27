"""Tests for ForgeFeatureContext forge ability profiles."""

import os
import sqlite3

import numpy as np
import pytest

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")


def _make_ctx():
    """Build a minimal ForgeFeatureContext from the real DB."""
    if not os.path.exists(DB_PATH):
        pytest.skip("tags.db not found")
    from mtg_synergy.recommend.forge_features import ForgeFeatureContext

    conn = sqlite3.connect(DB_PATH)
    # Build minimal oid_to_idx from cards table
    oid_to_idx = {}
    for i, (oid,) in enumerate(
        conn.execute("SELECT DISTINCT oracle_id FROM cards")
    ):
        oid_to_idx[oid] = i
    n = len(oid_to_idx)
    # Dummy normed embeddings (not needed for profile tests)
    normed_emb = np.zeros((n, 8), dtype=np.float16)
    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx, preload_edges=False)
    return ctx, conn


def test_forge_profiles_loaded():
    """_forge_profiles dict exists, is non-empty, and entries have correct structure."""
    ctx, conn = _make_ctx()
    try:
        assert hasattr(ctx, "_forge_profiles")
        assert isinstance(ctx._forge_profiles, dict)
        assert len(ctx._forge_profiles) > 1000, (
            f"Expected >1000 profiles, got {len(ctx._forge_profiles)}"
        )
        # Check structure of an arbitrary entry
        sample_oid = next(iter(ctx._forge_profiles))
        profile = ctx._forge_profiles[sample_oid]
        expected_keys = {
            "verbs", "triggers", "keywords", "counter_types",
            "targets", "ability_types", "trigger_filters",
        }
        assert set(profile.keys()) == expected_keys
        # All values should be sets
        for key in expected_keys:
            assert isinstance(profile[key], set), f"{key} should be a set"
    finally:
        conn.close()


def test_forge_profile_krenko():
    """Krenko Mob Boss should have Token verb in its profile."""
    ctx, conn = _make_ctx()
    try:
        # Krenko, Mob Boss oracle_id
        krenko_oid = "68418069-f615-40ef-ae0d-764192acae00"
        assert krenko_oid in ctx._forge_profiles, (
            "Krenko Mob Boss not found in forge profiles"
        )
        profile = ctx._forge_profiles[krenko_oid]
        assert "Token" in profile["verbs"], (
            f"Expected 'Token' in Krenko verbs, got {profile['verbs']}"
        )
        # Krenko should have an activated ability type
        assert "A" in profile["ability_types"], (
            f"Expected 'A' (activated) in Krenko ability_types, got {profile['ability_types']}"
        )
    finally:
        conn.close()
