"""Tests for mechanics_vectors auto-derived concept space and vector construction."""

import os
import sqlite3

import numpy as np
import pytest

from mtg_synergy.recommend.mechanics_vectors import (
    build_mechanics_vectors,
    _build_concept_vocabulary,
    _VERB_EVENT_CLASS,
    _TRIGGER_EVENT_CLASS,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")


def _needs_db():
    if not os.path.exists(DB_PATH):
        pytest.skip("tags.db not found")


# --- Unit tests (no DB needed) ---

def test_verb_trigger_event_class_alignment():
    """Key verb/trigger pairs must share the same event_class."""
    pairs = [
        ("GainLife", "LifeGained", "life_gain"),
        ("Draw", "Drawn", "draw"),
        ("DealDamage", "DamageDone", "damage"),
        ("Mill", "Milled", "mill"),
        ("Discard", "Discarded", "discard"),
        ("PutCounter", "CounterAdded", "counter_add"),
        ("Sacrifice", "Sacrificed", "sacrifice"),
        ("Tap", "Taps", "tap"),
        ("Untap", "Untaps", "untap"),
    ]
    for verb, trigger, expected_ec in pairs:
        assert _VERB_EVENT_CLASS[verb] == expected_ec, (
            f"Verb {verb} → {_VERB_EVENT_CLASS[verb]}, expected {expected_ec}")
        assert _TRIGGER_EVENT_CLASS[trigger] == expected_ec, (
            f"Trigger {trigger} → {_TRIGGER_EVENT_CLASS[trigger]}, expected {expected_ec}")


def test_auto_concepts_deterministic():
    """Calling _build_concept_vocabulary twice with same data produces same vocabulary."""
    abilities = [
        ("oid1", "Token", None, None, None, None, "g_1_1_goblin", None,
         "A:AB$ Token", None, None, None, None),
        ("oid2", "Mill", "ChangesZone", "Land.YouCtrl", None, None, None, None,
         "T:Mode$ ChangesZone | Destination$ Battlefield | ValidCard$ Land.YouCtrl",
         None, None, "Battlefield", None),
    ]
    vocab1, n1, cats1 = _build_concept_vocabulary(abilities)
    vocab2, n2, cats2 = _build_concept_vocabulary(abilities)
    assert vocab1 == vocab2, "Vocabulary not deterministic"
    assert n1 == n2
    assert cats1 == cats2


def test_concept_indices_unique_and_contiguous():
    """Auto-derived concept indices should be unique and contiguous."""
    abilities = [
        ("oid1", "Draw", None, None, None, None, None, None,
         "A:AB$ Draw", None, None, None, None),
        ("oid2", None, "Drawn", None, None, None, None, None,
         "T:Mode$ Drawn", None, None, None, None),
    ]
    vocab, n, _ = _build_concept_vocabulary(abilities)
    indices = list(vocab.values())
    assert len(indices) == len(set(indices)), "Duplicate concept indices"
    assert sorted(indices) == list(range(n)), "Non-contiguous indices"


# --- Integration tests (need DB) ---

def test_build_vectors_has_concepts_and_subtypes():
    """build_mechanics_vectors returns dim >= 50 (auto concepts + subtypes)."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        produces, consumes, dim, subtype_idx, category_dims = build_mechanics_vectors(conn)
        assert dim >= 50, f"Expected dim >= 50, got {dim}"
        assert len(produces) > 0
        assert len(consumes) > 0
        # All 8 categories present
        for cat in ["board", "resource", "disruption", "tempo",
                    "utility", "zones", "themes", "tribal"]:
            assert cat in category_dims, f"Missing category: {cat}"
    finally:
        conn.close()


def test_zone_change_produces_and_consumes():
    """Cards with zone_change abilities produce/consume zone-qualified tuples."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        produces, consumes, dim, _, category_dims = build_mechanics_vectors(conn)
        # Zone dims should have entries
        assert len(category_dims["zones"]) > 0, "No zone-category dimensions"
    finally:
        conn.close()


def test_vectors_l2_normalized():
    """All produces/consumes vectors should be L2-normalized (norm ~1.0)."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        produces, consumes, dim, _, _ = build_mechanics_vectors(conn)
        for oid, vec in list(produces.items())[:100]:
            norm = np.linalg.norm(vec)
            if norm > 0:
                assert abs(norm - 1.0) < 1e-5, (
                    f"Produces vector for {oid} not normalized: norm={norm}")
        for oid, vec in list(consumes.items())[:100]:
            norm = np.linalg.norm(vec)
            if norm > 0:
                assert abs(norm - 1.0) < 1e-5, (
                    f"Consumes vector for {oid} not normalized: norm={norm}")
    finally:
        conn.close()


def test_replacement_effect_consumes_event():
    """R: replacement effects should CONSUME the event they modify."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        bruvac_oid = "fake-bruvac-0000"
        self_mill_oid = "fake-selfmill-0000"
        abilities = [
            (bruvac_oid, None, None, None, None, None, None, None,
             "R:Event$ Mill | ValidPlayer$ Player.Opponent | ReplaceWith$ MillTwice",
             None, None, None, None),
            (self_mill_oid, "Mill", None, None, None, None, None, None,
             "A:AB$ Mill | Cost$ T | Defined$ You | NumCards$ 3",
             None, None, None, "You"),
        ]
        produces, consumes, dim, _, _ = build_mechanics_vectors(
            conn, preloaded_abilities=abilities)
        from mtg_synergy.recommend.mechanics_vectors import _concept_idx
        mill_tuple = ("mill", None, None, None)
        # Bruvac should CONSUME mill (replacement effect consumes the event it modifies)
        assert bruvac_oid in consumes
        if mill_tuple in _concept_idx:
            assert consumes[bruvac_oid][_concept_idx[mill_tuple]] > 0
        # Self-mill SHOULD produce mill
        assert self_mill_oid in produces
    finally:
        conn.close()


def test_self_targeting_replacement_produces():
    """Self-targeting replacement effects SHOULD produce concepts."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        faithmender_oid = "fake-faithmender-0000"
        abilities = [
            (faithmender_oid, None, None, None, None, None, None, None,
             "R:Event$ GainLife | ValidPlayer$ You | ReplaceWith$ GainDouble",
             None, None, None, None),
        ]
        produces, consumes, dim, _, _ = build_mechanics_vectors(
            conn, preloaded_abilities=abilities)
        assert faithmender_oid in produces, "Self-targeting replacement should produce"
    finally:
        conn.close()


def test_zone_change_with_origin_produces():
    """ChangeZone from Graveyard to Battlefield produces zone-qualified tuples."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        fake_oid = "00000000-0000-0000-0000-000000000001"
        abilities = [
            (fake_oid, "ChangeZone", None, None, None, None, None, None,
             "A:AB$ ChangeZone | Origin$ Graveyard | Destination$ Battlefield",
             None, "Graveyard", "Battlefield", None),
        ]
        produces, consumes, dim, _, _ = build_mechanics_vectors(
            conn, preloaded_abilities=abilities)
        assert fake_oid in produces
        # Should have nonzero values in zone-qualified dimensions
        vec = produces[fake_oid]
        assert np.any(vec > 0), "Should produce something"
    finally:
        conn.close()


def test_category_dims_cover_all_concepts():
    """All concept dimensions should be assigned to exactly one category."""
    _needs_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        _, _, dim, subtype_idx, category_dims = build_mechanics_vectors(conn)
        all_dims = set()
        for cat, dims in category_dims.items():
            for d in dims:
                assert d not in all_dims, (
                    f"Dimension {d} in multiple categories")
                all_dims.add(d)
        # All dims 0..dim-1 should be covered
        assert all_dims == set(range(dim)), (
            f"Missing dims: {set(range(dim)) - all_dims}")
    finally:
        conn.close()
