"""Tests for the forge fusion model."""

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


def test_leave_commander_out_split():
    """CV splits should separate commanders, not individual pairs."""
    import numpy as np
    cmdr_ids = np.repeat(range(5), 10)
    from train_fusion_model import make_cv_splits
    splits = make_cv_splits(cmdr_ids, n_folds=5)
    assert len(splits) == 5
    for train_idx, test_idx in splits:
        train_cmdrs = set(cmdr_ids[train_idx])
        test_cmdrs = set(cmdr_ids[test_idx])
        assert train_cmdrs.isdisjoint(test_cmdrs)
    # All indices should be covered across all test folds
    all_test = set()
    for _, test_idx in splits:
        all_test.update(test_idx)
    assert all_test == set(range(len(cmdr_ids)))


def test_forge_feature_names():
    """Forge feature list should have exactly 71 named features."""
    from train_fusion_model import FORGE_FEATURE_NAMES
    assert len(FORGE_FEATURE_NAMES) == 71
    assert "causal_cmdr_to_card" in FORGE_FEATURE_NAMES
    assert "ability_density" in FORGE_FEATURE_NAMES
    assert "tower_prob" not in FORGE_FEATURE_NAMES
