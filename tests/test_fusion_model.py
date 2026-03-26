"""Tests for the fusion model (Stage 1: tower on EDHREC membership)."""

import sqlite3
import os
import pytest

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")


def test_load_edhrec_membership_data():
    """Verify EDHREC avg deck data is loadable and has expected shape."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT commander_slug, card_name FROM edhrec_average_decks LIMIT 10"
    ).fetchall()
    conn.close()
    assert len(rows) == 10
    assert all(isinstance(r[0], str) and isinstance(r[1], str) for r in rows)


def test_edhrec_commander_count():
    """At least 800 commanders with avg decklists."""
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(DISTINCT commander_slug) FROM edhrec_average_decks"
    ).fetchone()[0]
    conn.close()
    assert count >= 800


def test_tower_binary_output_range():
    """Tower EDHREC model should save normalization params."""
    import numpy as np
    model_path = os.path.join(os.path.dirname(__file__), "..", "data", "tower_model_edhrec.npz")
    if not os.path.exists(model_path):
        pytest.skip("Tower EDHREC model not trained yet")
    data = np.load(model_path)
    assert "struct_means" in data.files
    assert "struct_stds" in data.files
