#!/usr/bin/env python3
"""Sweep training hyperparameters to maximize NDCG@30.

Two-phase sweep:
  Phase 1: grade boundaries + negative ratio (requires feature rebuild per config)
  Phase 2: sample weights + label_gain (retrain only, reuses features)

Uses single-fold (quick) evaluation for speed. Best config is retrained
with full 3-fold CV at the end.

Usage:
    python3 scripts/sweep_hyperparams.py              # Full sweep (~2-3 hrs)
    python3 scripts/sweep_hyperparams.py --phase 1    # Grade boundaries + neg ratio only
    python3 scripts/sweep_hyperparams.py --phase 2    # Weights + label_gain only (uses current cache)
    python3 scripts/sweep_hyperparams.py --dry-run     # Show configs without running
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from itertools import product

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mtg_synergy.config import DATA_DIR, DB_PATH

# Import internals from train_fusion_model
import train_fusion_model as tm


# ── Phase 1 configs: grade boundaries + negative ratio ──────────────
# These require rebuilding the feature cache each time.

GRADE_BOUNDARY_CONFIGS = [
    # (grade5_min, grade4_min, grade3_min, grade2_min)
    (0.40, 0.20, 0.05, 0.0),   # current
    (0.35, 0.15, 0.05, 0.0),   # lower thresholds → more grade 4/5
    (0.50, 0.25, 0.10, 0.0),   # higher thresholds → fewer grade 4/5
    (0.40, 0.20, 0.10, 0.0),   # raise grade 3 floor
    (0.30, 0.15, 0.05, 0.0),   # much lower → many more top grades
]

NEGATIVE_RATIOS = [2, 3, 4]  # current = 3


# ── Phase 2 configs: sample weights + label_gain ────────────────────
# These only require retraining (reuse cached features).

SAMPLE_WEIGHT_CONFIGS = [
    # [grade0, grade1, grade2, grade3, grade4, grade5]
    [1.0, 1.0, 1.0, 1.0, 2.0, 3.0],   # current
    [1.0, 1.0, 1.0, 1.0, 3.0, 5.0],   # sharper
    [1.0, 1.0, 1.0, 1.0, 1.5, 2.0],   # flatter
    [1.0, 1.0, 1.0, 1.0, 2.0, 5.0],   # heavy grade 5
    [1.0, 1.0, 1.0, 1.0, 3.0, 3.0],   # equal 4/5
]

LABEL_GAIN_CONFIGS = [
    [0, 1, 3, 6, 15, 30],    # current
    [0, 1, 3, 6, 10, 20],    # less steep
    [0, 1, 3, 6, 20, 40],    # steeper
    [0, 1, 2, 4, 10, 30],    # compress low grades
    [0, 1, 3, 8, 20, 30],    # boost mid grades
]


def _rebuild_features(grade_boundaries, neg_ratio):
    """Rebuild feature matrix with given grade boundaries and negative ratio."""
    # Temporarily override module-level constants
    old_grade = tm._GRADE_BOUNDARIES
    old_ratio = tm._NEGATIVE_RATIO
    tm._GRADE_BOUNDARIES = grade_boundaries
    tm._NEGATIVE_RATIO = neg_ratio

    try:
        conn = sqlite3.connect(DB_PATH)
        pairs_by_cmdr = tm._load_pairs_for_features(conn)
        conn.close()
        X, y, cmdr_ids = tm.build_forge_feature_matrix(pairs_by_cmdr)
        return X, y, cmdr_ids
    finally:
        tm._GRADE_BOUNDARIES = old_grade
        tm._NEGATIVE_RATIO = old_ratio


def _train_quick(X, y, cmdr_ids, sample_weights, label_gain):
    """Train single-fold and return NDCG@30."""
    import lightgbm as lgb

    sort_order = np.argsort(cmdr_ids)
    X = X[sort_order]
    y = y[sort_order]
    cmdr_ids = cmdr_ids[sort_order]

    w_arr = np.array(sample_weights, dtype=np.float32)
    w = w_arr[y.astype(np.intp)]

    splits = tm.make_cv_splits(cmdr_ids, n_folds=3)
    train_idx, test_idx = splits[0]  # single fold

    X_train, X_test = X[train_idx], X[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]
    w_train = w[train_idx]
    cmdr_train = cmdr_ids[train_idx]
    cmdr_test = cmdr_ids[test_idx]

    # Build group arrays from the sliced commander IDs directly
    train_groups = tm._build_group_array(cmdr_train, np.arange(len(cmdr_train)))
    test_groups = tm._build_group_array(cmdr_test, np.arange(len(cmdr_test)))

    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [10, 30],
        "subsample": 0.8,
        "min_data_in_bin": 5,
        "verbose": -1,
        "label_gain": label_gain,
        "num_leaves": 767,
        "learning_rate": 0.025,
        "min_child_samples": 40,
        "bagging_freq": 5,
        "colsample_bytree": 0.6,
        "feature_fraction_bynode": 0.9,
        "n_estimators": 1000,
    }

    dtrain = lgb.Dataset(X_train, label=y_train, weight=w_train, group=train_groups)
    dtest = lgb.Dataset(X_test, label=y_test, group=test_groups, reference=dtrain)

    callbacks = [lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)]
    model = lgb.train(params, dtrain, valid_sets=[dtest], callbacks=callbacks)

    preds = model.predict(X_test)
    ndcg = tm._compute_ndcg30(preds, y_test, test_groups, label_gain)
    return ndcg, model.current_iteration(), model


def run_phase1(dry_run=False):
    """Sweep grade boundaries × negative ratios."""
    configs = list(product(GRADE_BOUNDARY_CONFIGS, NEGATIVE_RATIOS))
    print(f"\n{'='*60}")
    print(f"PHASE 1: Grade boundaries × negative ratio ({len(configs)} configs)")
    print(f"{'='*60}")

    if dry_run:
        for i, (gb, nr) in enumerate(configs):
            print(f"  [{i+1:2d}] grades={gb}, neg_ratio={nr}")
        return []

    # Use current sample weights and label_gain as defaults
    default_weights = [1.0, 1.0, 1.0, 1.0, 2.0, 3.0]
    default_label_gain = [0, 1, 3, 6, 15, 30]

    results = []
    best_ndcg = 0
    best_config = None

    for i, (gb, nr) in enumerate(configs):
        t0 = time.time()
        tag = f"grades={gb}, neg_ratio={nr}"
        print(f"\n[{i+1}/{len(configs)}] {tag}")

        X, y, cmdr_ids = _rebuild_features(gb, nr)
        ndcg, rounds, _ = _train_quick(X, y, cmdr_ids, default_weights, default_label_gain)
        elapsed = time.time() - t0

        result = {
            "phase": 1,
            "grade_boundaries": gb,
            "neg_ratio": nr,
            "ndcg30": ndcg,
            "rounds": rounds,
            "n_samples": len(y),
            "elapsed_s": round(elapsed, 1),
        }
        results.append(result)

        marker = " *** BEST ***" if ndcg > best_ndcg else ""
        print(f"  NDCG@30={ndcg:.4f} ({rounds} rounds, {elapsed:.0f}s){marker}")

        if ndcg > best_ndcg:
            best_ndcg = ndcg
            best_config = result
            # Save this feature matrix for Phase 2
            cache_path = os.path.join(DATA_DIR, "sweep_best_features.npz")
            np.savez(cache_path, X=X, y=y, cmdr_ids=cmdr_ids)

    print(f"\n{'─'*60}")
    print(f"Phase 1 best: NDCG@30={best_ndcg:.4f}")
    print(f"  Config: grades={best_config['grade_boundaries']}, "
          f"neg_ratio={best_config['neg_ratio']}")
    return results


def run_phase2(dry_run=False, feature_cache=None):
    """Sweep sample weights × label_gain (reuses features)."""
    configs = list(product(SAMPLE_WEIGHT_CONFIGS, LABEL_GAIN_CONFIGS))
    print(f"\n{'='*60}")
    print(f"PHASE 2: Sample weights × label_gain ({len(configs)} configs)")
    print(f"{'='*60}")

    if dry_run:
        for i, (sw, lg) in enumerate(configs):
            print(f"  [{i+1:2d}] weights={sw}, label_gain={lg}")
        return []

    # Load features: prefer Phase 1 best, fall back to current cache
    if feature_cache is not None:
        X, y, cmdr_ids = feature_cache
    else:
        sweep_cache = os.path.join(DATA_DIR, "sweep_best_features.npz")
        main_cache = os.path.join(DATA_DIR, "forge_features_cache.npz")
        cache_path = sweep_cache if os.path.exists(sweep_cache) else main_cache

        if not os.path.exists(cache_path):
            print("ERROR: No feature cache found. Run Phase 1 first or "
                  "train_fusion_model.py to build cache.")
            return []

        print(f"  Loading features from {cache_path}")
        cached = np.load(cache_path, allow_pickle=False)
        X, y, cmdr_ids = cached["X"], cached["y"], cached["cmdr_ids"]

    print(f"  Matrix: {X.shape[0]:,} samples, {X.shape[1]} features")

    results = []
    best_ndcg = 0
    best_config = None

    for i, (sw, lg) in enumerate(configs):
        t0 = time.time()
        tag = f"weights={sw}, label_gain={lg}"
        print(f"\n[{i+1}/{len(configs)}] {tag}")

        ndcg, rounds, _ = _train_quick(X, y, cmdr_ids, sw, lg)
        elapsed = time.time() - t0

        result = {
            "phase": 2,
            "sample_weights": sw,
            "label_gain": lg,
            "ndcg30": ndcg,
            "rounds": rounds,
            "elapsed_s": round(elapsed, 1),
        }
        results.append(result)

        marker = " *** BEST ***" if ndcg > best_ndcg else ""
        print(f"  NDCG@30={ndcg:.4f} ({rounds} rounds, {elapsed:.0f}s){marker}")

        if ndcg > best_ndcg:
            best_ndcg = ndcg
            best_config = result

    print(f"\n{'─'*60}")
    print(f"Phase 2 best: NDCG@30={best_ndcg:.4f}")
    print(f"  Config: weights={best_config['sample_weights']}, "
          f"label_gain={best_config['label_gain']}")
    return results


def _validate_top_configs(all_results, top_n=5, validate_cmdrs=50):
    """Run full pipeline validation on top N configs by NDCG.

    Trains a full model for each config, saves it to disk, runs the
    recommendation pipeline, and checks for suspicious outputs.
    This catches regressions that NDCG alone misses (penalty bugs,
    wrong-type recommendations, etc.).
    """
    import shutil

    sorted_results = sorted(all_results, key=lambda r: r["ndcg30"], reverse=True)
    candidates = sorted_results[:top_n]

    model_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    backup_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb.sweep_backup")

    # Back up current model
    if os.path.exists(model_path):
        shutil.copy2(model_path, backup_path)

    print(f"\n{'='*60}")
    print(f"PIPELINE VALIDATION — top {len(candidates)} configs")
    print(f"{'='*60}")

    try:
        for i, r in enumerate(candidates):
            print(f"\n[{i+1}/{len(candidates)}] NDCG@30={r['ndcg30']:.4f}")

            # Load features for this config
            if r["phase"] == 1:
                print(f"  Rebuilding features: grades={r['grade_boundaries']}, "
                      f"neg_ratio={r['neg_ratio']}")
                X, y, cmdr_ids = _rebuild_features(
                    tuple(r["grade_boundaries"]), r["neg_ratio"])
                sw = [1.0, 1.0, 1.0, 1.0, 2.0, 3.0]
                lg = [0, 1, 3, 6, 15, 30]
            else:
                # Use best Phase 1 features or current cache
                sweep_cache = os.path.join(DATA_DIR, "sweep_best_features.npz")
                main_cache = os.path.join(DATA_DIR, "forge_features_cache.npz")
                cache_path = sweep_cache if os.path.exists(sweep_cache) else main_cache
                cached = np.load(cache_path, allow_pickle=False)
                X, y, cmdr_ids = cached["X"], cached["y"], cached["cmdr_ids"]
                sw = r["sample_weights"]
                lg = r["label_gain"]
                print(f"  weights={sw}, label_gain={lg}")

            # Train and save model
            _, _, model = _train_quick(X, y, cmdr_ids, sw, lg)
            model.save_model(model_path)

            # Run pipeline validation
            n_issues = tm._run_pipeline_validation(validate_cmdrs)
            r["pipeline_issues"] = n_issues
            r["pipeline_validated"] = True

            status = "PASS" if n_issues == 0 else f"FAIL ({n_issues} issues)"
            print(f"  Pipeline: {status}")

    finally:
        # Restore original model
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, model_path)
            os.remove(backup_path)
            print(f"\n  Original model restored.")


def main():
    parser = argparse.ArgumentParser(description="Sweep training hyperparameters")
    parser.add_argument("--phase", type=int, choices=[1, 2],
                        help="Run only Phase 1 or Phase 2 (default: both)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show configs without running")
    parser.add_argument("--skip-validate", action="store_true",
                        help="Skip pipeline validation of top configs")
    parser.add_argument("--validate-top", type=int, default=5,
                        help="Number of top configs to validate (default: 5)")
    args = parser.parse_args()

    all_results = []
    t_start = time.time()

    if args.phase in (None, 1):
        all_results.extend(run_phase1(dry_run=args.dry_run))

    if args.phase in (None, 2):
        all_results.extend(run_phase2(dry_run=args.dry_run))

    if args.dry_run or not all_results:
        return

    # ── Pipeline validation on top configs ──
    if not args.skip_validate:
        _validate_top_configs(all_results, top_n=args.validate_top)

    # ── Summary ──
    total_time = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"SWEEP COMPLETE — {len(all_results)} configs in {total_time/60:.1f} min")
    print(f"{'='*60}")

    # Sort all results by NDCG
    all_results.sort(key=lambda r: r["ndcg30"], reverse=True)

    print(f"\nTop 10 configs:")
    header_pipe = "  Pipeline" if any(r.get("pipeline_validated") for r in all_results) else ""
    print(f"{'Rank':>4}  {'NDCG@30':>8}  {'Rounds':>6}{header_pipe}  Config")
    print(f"{'─'*4}  {'─'*8}  {'─'*6}  {'─'*8 if header_pipe else ''}{'─'*50}")
    for i, r in enumerate(all_results[:10]):
        if r["phase"] == 1:
            cfg = f"grades={r['grade_boundaries']}, neg_ratio={r['neg_ratio']}"
        else:
            cfg = f"weights={r['sample_weights']}, gain={r['label_gain']}"
        pipe = ""
        if r.get("pipeline_validated"):
            n = r["pipeline_issues"]
            pipe = f"  {'PASS':>8}" if n == 0 else f"  FAIL({n:>2})"
        print(f"{i+1:4d}  {r['ndcg30']:8.4f}  {r['rounds']:6d}{pipe}  {cfg}")

    # Save full results
    results_path = os.path.join(DATA_DIR, "sweep_results.json")
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nFull results saved to {results_path}")

    # Find best config that passed pipeline validation
    validated = [r for r in all_results if r.get("pipeline_issues", 0) == 0]
    best = validated[0] if validated else all_results[0]

    print(f"\n{'='*60}")
    print(f"RECOMMENDED CHANGES")
    print(f"{'='*60}")
    if best.get("pipeline_issues", 0) > 0:
        print(f"  WARNING: Best config has {best['pipeline_issues']} pipeline issues!")
        print(f"  Investigate before applying.")
    if best["phase"] == 1:
        print(f"  _GRADE_BOUNDARIES = {best['grade_boundaries']}")
        print(f"  _NEGATIVE_RATIO = {best['neg_ratio']}")
        print(f"  (then run Phase 2 to optimize weights/label_gain)")
    else:
        print(f"  _grade_weight_arr = np.array({best['sample_weights']})")
        print(f"  label_gain = {best['label_gain']}")
    print(f"\n  Expected NDCG@30: {best['ndcg30']:.4f} (single-fold)")
    print(f"  Retrain with full 3-fold CV to confirm.")


if __name__ == "__main__":
    main()
