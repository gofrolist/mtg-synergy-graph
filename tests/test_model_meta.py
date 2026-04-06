"""Tests for model metadata save/load/registry."""

import json
import os
import tempfile

import pytest

from mtg_synergy.model_meta import (
    _file_md5,
    append_to_registry,
    build_meta,
    load_model_meta,
    save_model_meta,
)


@pytest.fixture
def tmp_model(tmp_path):
    """Create a fake model file and return its path."""
    model_path = str(tmp_path / "test_model.lgb")
    with open(model_path, "wb") as f:
        f.write(b"fake model content for testing")
    return model_path


@pytest.fixture
def sample_meta(tmp_model):
    """Return a minimal valid metadata dict."""
    return {
        "version": "2026-04-05T14:23:01",
        "git_commit": "abc1234",
        "git_dirty": False,
        "mean_ndcg30": 0.5940,
        "fold_ndcgs": [0.591, 0.598, 0.593],
        "n_folds": 3,
        "fold_best_iters": [620, 685, 650],
        "final_rounds": 717,
        "hyperparameters": {"num_leaves": 767, "learning_rate": 0.025},
        "n_samples": 2100000,
        "n_commanders": 2724,
        "grade_distribution": {"0": 1400000, "1": 141000},
        "n_features": 93,
        "feature_names": ["f1", "f2"],
        "feature_importance": [{"name": "f1", "splits": 100, "pct": 60.0}],
        "training_time_s": 127.3,
        "model_size_bytes": 35000000,
        "model_md5": _file_md5(tmp_model),
        "quick_mode": False,
        "tune_mode": False,
    }


def test_save_and_load_round_trip(tmp_model, sample_meta):
    meta_path = save_model_meta(sample_meta, tmp_model)
    assert meta_path == tmp_model + ".meta.json"
    assert os.path.exists(meta_path)

    loaded = load_model_meta(tmp_model)
    assert loaded is not None
    assert loaded["version"] == "2026-04-05T14:23:01"
    assert loaded["mean_ndcg30"] == pytest.approx(0.5940)
    assert loaded["n_features"] == 93
    assert loaded["model_md5"] == sample_meta["model_md5"]


def test_load_missing_returns_none(tmp_path):
    missing = str(tmp_path / "nonexistent.lgb")
    assert load_model_meta(missing) is None


def test_save_writes_valid_json(tmp_model, sample_meta):
    meta_path = save_model_meta(sample_meta, tmp_model)
    with open(meta_path) as f:
        parsed = json.load(f)
    assert isinstance(parsed, dict)
    assert set(parsed.keys()) == set(sample_meta.keys())


def test_append_to_registry(tmp_path, sample_meta):
    registry = str(tmp_path / "registry.jsonl")

    append_to_registry(sample_meta, registry)
    append_to_registry({**sample_meta, "version": "v2"}, registry)

    with open(registry) as f:
        lines = f.readlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["version"] == "2026-04-05T14:23:01"
    assert second["version"] == "v2"


def test_file_md5_deterministic(tmp_model):
    h1 = _file_md5(tmp_model)
    h2 = _file_md5(tmp_model)
    assert h1 == h2
    assert len(h1) == 32  # hex MD5 is 32 chars


def test_build_meta_populates_all_fields(tmp_model):
    scores = {
        "mean_ndcg30": 0.59,
        "fold_ndcgs": [0.59],
        "fold_best_iters": [600],
        "final_rounds": 660,
        "n_folds": 1,
        "hyperparameters": {"num_leaves": 767},
        "training_time_s": 100.0,
        "quick_mode": True,
        "tune_mode": False,
    }
    meta = build_meta(
        scores=scores,
        model_path=tmp_model,
        n_samples=1000,
        n_commanders=50,
        grade_distribution={"0": 500, "5": 500},
        n_features=10,
        feature_names=["f" + str(i) for i in range(10)],
        feature_importance=[{"name": "f0", "splits": 50, "pct": 100.0}],
    )

    assert meta["mean_ndcg30"] == pytest.approx(0.59)
    assert meta["model_md5"] == _file_md5(tmp_model)
    assert meta["model_size_bytes"] > 0
    assert meta["quick_mode"] is True
    assert "version" in meta
    assert "git_commit" in meta
