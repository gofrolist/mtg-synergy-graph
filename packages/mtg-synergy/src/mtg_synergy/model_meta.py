"""Model metadata: save/load sidecar JSON and append to training registry."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any


def _git_info() -> tuple[str, bool]:
    """Return (short_sha, is_dirty). Falls back to ("unknown", False)."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        )
        return sha, dirty
    except Exception:
        return "unknown", False


def _file_md5(path: str) -> str:
    """Return hex MD5 digest of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save_model_meta(meta: dict[str, Any], model_path: str) -> str:
    """Write *meta* as JSON next to *model_path* (``<model>.meta.json``).

    Returns the path of the written file.
    """
    meta_path = model_path + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, default=str)
    return meta_path


def load_model_meta(model_path: str) -> dict[str, Any] | None:
    """Read the sidecar ``<model_path>.meta.json``. Returns *None* if missing."""
    meta_path = model_path + ".meta.json"
    if not os.path.exists(meta_path):
        return None
    with open(meta_path) as f:
        return json.load(f)


def append_to_registry(meta: dict[str, Any], registry_path: str) -> None:
    """Append one JSON line to the JSONL training registry."""
    with open(registry_path, "a") as f:
        f.write(json.dumps(meta, default=str) + "\n")


def build_meta(
    *,
    scores: dict[str, Any],
    model_path: str,
    n_samples: int,
    n_commanders: int,
    grade_distribution: dict[str, int],
    n_features: int,
    feature_names: list[str],
    feature_importance: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a complete metadata dict from training outputs."""
    git_commit, git_dirty = _git_info()
    return {
        "version": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "mean_ndcg30": scores["mean_ndcg30"],
        "fold_ndcgs": scores["fold_ndcgs"],
        "n_folds": scores["n_folds"],
        "fold_best_iters": scores["fold_best_iters"],
        "final_rounds": scores["final_rounds"],
        "hyperparameters": scores["hyperparameters"],
        "n_samples": n_samples,
        "n_commanders": n_commanders,
        "grade_distribution": grade_distribution,
        "n_features": n_features,
        "feature_names": feature_names,
        "feature_importance": feature_importance,
        "training_time_s": scores["training_time_s"],
        "model_size_bytes": os.path.getsize(model_path),
        "model_md5": _file_md5(model_path),
        "quick_mode": scores.get("quick_mode", False),
        "tune_mode": scores.get("tune_mode", False),
    }
