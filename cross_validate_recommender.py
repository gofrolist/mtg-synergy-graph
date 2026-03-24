#!/usr/bin/env python3
"""Leave-one-out cross-validation for the recommendation model.

For each test deck, trains a model WITHOUT that commander's data,
then measures ranking quality on the held-out commander's examples.

Two modes:
  1. LOO-CV using training data (fast, measures ranking quality)
  2. Full recommendation CV (slower, measures EDHREC overlap)

Usage:
    python3 cross_validate_recommender.py                # fast LOO-CV
    python3 cross_validate_recommender.py --ablation      # feature ablation
    python3 cross_validate_recommender.py --full          # full EDHREC overlap test
"""

import json
import math
import os
import random
import re
import sys

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TRAINING_PATH = os.path.join(DATA_DIR, "recommender_training.json")
CACHE_DIR = os.path.join(DATA_DIR, "edhrec_commanders")

sys.path.insert(0, os.path.dirname(__file__))
from train_recommender import _train_gbt, _predict_tree


def load_edhrec_high_synergy(slug):
    """Load EDHREC high-synergy cards for a commander."""
    cache_path = os.path.join(CACHE_DIR, f"{slug}.json")
    if not os.path.exists(cache_path):
        return []
    with open(cache_path) as f:
        data = json.load(f)
    cards = []
    cardlists = data.get("container", {}).get("json_dict", {}).get("cardlists", [])
    for cl in cardlists:
        header = cl.get("header", "")
        for cv in cl.get("cardviews", []):
            if cv.get("synergy", 0) > 0.05:
                cards.append({"name": cv["name"], "synergy": cv["synergy"],
                              "section": header})
    return cards


def quick_train(data, feature_names, zero_edhrec=False):
    """Fast model training. Returns model dict."""
    if zero_edhrec:
        data = [dict(r) for r in data]
        for r in data:
            r["edhrec_direct_synergy"] = 0.0

    means = {f: 0.0 for f in feature_names}
    for row in data:
        for f in feature_names:
            means[f] += row.get(f, 0)
    for f in feature_names:
        means[f] /= max(len(data), 1)

    stds = {f: 0.0 for f in feature_names}
    for row in data:
        for f in feature_names:
            stds[f] += (row.get(f, 0) - means[f]) ** 2
    for f in feature_names:
        stds[f] = max((stds[f] / max(len(data), 1)) ** 0.5, 0.001)

    # Subsample for training speed (CV doesn't need full data)
    if len(data) > 20000:
        data = random.sample(data, 20000)

    weights = {f: 0.0 for f in feature_names}
    bias = 0.0
    for epoch in range(25):
        for row in data:
            x = {f: (row.get(f, 0) - means[f]) / stds[f] for f in feature_names}
            y = row["label"]
            z = sum(weights[f] * x[f] for f in feature_names) + bias
            z = max(-20, min(20, z))
            pred = 1.0 / (1.0 + math.exp(-z))
            error = pred - y
            for f in feature_names:
                weights[f] -= 0.1 * error * x[f]
            bias -= 0.1 * error

    # BPR fine-tune
    by_cmdr = {}
    for row in data:
        cmdr = row["commander"]
        by_cmdr.setdefault(cmdr, {"pos": [], "neg": []})
        if row["label"] == 1:
            by_cmdr[cmdr]["pos"].append(row)
        else:
            by_cmdr[cmdr]["neg"].append(row)

    for epoch in range(10):
        for cmdr, groups in by_cmdr.items():
            if not groups["pos"] or not groups["neg"]:
                continue
            for pos in groups["pos"]:
                neg = random.choice(groups["neg"])
                x_pos = {f: (pos.get(f, 0) - means[f]) / stds[f] for f in feature_names}
                x_neg = {f: (neg.get(f, 0) - means[f]) / stds[f] for f in feature_names}
                z_pos = sum(weights[f] * x_pos[f] for f in feature_names) + bias
                z_neg = sum(weights[f] * x_neg[f] for f in feature_names) + bias
                diff = max(-20, min(20, z_pos - z_neg))
                sigma = 1.0 / (1.0 + math.exp(-diff))
                error = sigma - 1.0
                for f in feature_names:
                    weights[f] -= 0.01 * error * (x_pos[f] - x_neg[f])

    # GBT (lighter for CV speed)
    sub = random.sample(data, min(5000, len(data)))
    trees = _train_gbt(sub, feature_names, means, stds, n_trees=20, max_depth=2, lr=0.1)

    return {"weights": weights, "bias": bias, "means": means, "stds": stds,
            "feature_names": feature_names, "trees": trees}


def score_row(model, row, zero_edhrec=False):
    """Score a single data row with a model."""
    feat_names = model["feature_names"]
    features = {f: row.get(f, 0) for f in feat_names}
    if zero_edhrec:
        features["edhrec_direct_synergy"] = 0.0

    x = {f: (features[f] - model["means"][f]) / model["stds"][f] for f in feat_names}
    for d in ["rank_log", "cmc_diff"]:
        if d in x:
            x[d] = max(-0.5, min(0.5, x[d]))

    z_lin = sum(model["weights"][f] * x[f] for f in feat_names) + model["bias"]
    z_lin = max(-20, min(20, z_lin))
    lin_score = 1.0 / (1.0 + math.exp(-z_lin))

    trees_data = model.get("trees")
    if trees_data and "trees" in trees_data:
        z_gbt = trees_data["init"]
        for tree in trees_data["trees"]:
            z_gbt += _predict_tree(tree, x) * trees_data["lr"]
        z_gbt = max(-10, min(10, z_gbt))
        gbt_score = 1.0 / (1.0 + math.exp(-z_gbt))
        if abs(gbt_score - lin_score) < 0.1:
            return (gbt_score + lin_score) / 2
        return lin_score
    return lin_score


def run_loo_cv():
    """Leave-one-out CV: for each commander, train without it, score its examples."""
    with open(TRAINING_PATH) as f:
        all_data = json.load(f)

    feature_names = [k for k in all_data[0].keys()
                     if k not in ("label", "synergy", "commander", "candidate")]

    by_cmdr = {}
    for row in all_data:
        by_cmdr.setdefault(row["commander"], []).append(row)

    print("=" * 70)
    print("LEAVE-ONE-OUT CROSS-VALIDATION")
    print("=" * 70)

    # Mode A: With EDHREC features (current model)
    print("\n--- MODE A: With EDHREC features (current production model) ---")
    mode_a_aucs = []
    mode_a_recall = []

    # Mode B: Without edhrec_direct_synergy (independent mode)
    print("--- MODE B: Without edhrec_direct_synergy (independent mode) ---\n")
    mode_b_aucs = []
    mode_b_recall = []

    cmdrs = sorted(by_cmdr.keys())
    n = len(cmdrs)

    # Sample 50 commanders for speed (or all if < 50)
    random.seed(42)
    test_cmdrs = random.sample(cmdrs, min(30, n))
    print(f"Testing {len(test_cmdrs)} commanders (sampled from {n})\n")

    for i, cmdr in enumerate(test_cmdrs):
        held_out = by_cmdr[cmdr]
        train_data = [r for c, rows in by_cmdr.items() if c != cmdr for r in rows]

        if len(held_out) < 5 or not any(r["label"] == 1 for r in held_out):
            continue

        pos = [r for r in held_out if r["label"] == 1]
        neg = [r for r in held_out if r["label"] == 0]

        # Mode A: Train with all features
        random.seed(42)
        model_a = quick_train(train_data, feature_names, zero_edhrec=False)

        # Mode B: Train without edhrec_direct_synergy
        random.seed(42)
        model_b = quick_train(train_data, feature_names, zero_edhrec=True)

        # Score held-out data
        for mode, model, aucs, recalls, zero in [
            ("A", model_a, mode_a_aucs, mode_a_recall, False),
            ("B", model_b, mode_b_aucs, mode_b_recall, True),
        ]:
            pos_scores = [(r["candidate"], score_row(model, r, zero_edhrec=zero)) for r in pos]
            neg_scores = [(r["candidate"], score_row(model, r, zero_edhrec=zero)) for r in neg]

            # AUC: fraction of (pos, neg) pairs where pos > neg
            correct = sum(1 for _, ps in pos_scores for _, ns in neg_scores if ps > ns)
            total = len(pos_scores) * len(neg_scores)
            auc = correct / max(total, 1)
            aucs.append(auc)

            # Recall@30: what fraction of positives are in top 30 overall?
            all_scored = pos_scores + neg_scores
            all_scored.sort(key=lambda x: -x[1])
            top_30_names = {name for name, _ in all_scored[:30]}
            pos_names = {name for name, _ in pos_scores}
            recall = len(top_30_names & pos_names) / max(len(pos_names), 1)
            recalls.append(recall)

        if (i + 1) % 10 == 0:
            avg_a = sum(mode_a_aucs) / len(mode_a_aucs)
            avg_b = sum(mode_b_aucs) / len(mode_b_aucs)
            print(f"  [{i+1}/{len(test_cmdrs)}] AUC: A={avg_a:.3f} B={avg_b:.3f}")

    # Summary
    print(f"\n{'=' * 70}")
    print("RESULTS")
    print(f"{'=' * 70}")

    avg_auc_a = sum(mode_a_aucs) / max(len(mode_a_aucs), 1)
    avg_auc_b = sum(mode_b_aucs) / max(len(mode_b_aucs), 1)
    avg_recall_a = sum(mode_a_recall) / max(len(mode_a_recall), 1)
    avg_recall_b = sum(mode_b_recall) / max(len(mode_b_recall), 1)

    print(f"\n  Mode A (with EDHREC features):")
    print(f"    Average AUC:       {avg_auc_a:.3f}")
    print(f"    Average Recall@30: {avg_recall_a:.1%}")

    print(f"\n  Mode B (independent, no edhrec_direct_synergy):")
    print(f"    Average AUC:       {avg_auc_b:.3f}")
    print(f"    Average Recall@30: {avg_recall_b:.1%}")

    drop_auc = avg_auc_a - avg_auc_b
    drop_recall = avg_recall_a - avg_recall_b
    print(f"\n  Drop when removing EDHREC direct synergy:")
    print(f"    AUC drop:       {drop_auc:+.3f}")
    print(f"    Recall@30 drop: {drop_recall:+.1%}")

    # Distribution of AUCs
    print(f"\n  Mode B AUC distribution:")
    bins = [0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
    for lo, hi in zip(bins[:-1], bins[1:]):
        count = sum(1 for a in mode_b_aucs if lo <= a < hi)
        bar = "█" * count
        print(f"    [{lo:.1f}-{hi:.1f}): {count:3d} {bar}")


def run_ablation():
    """Feature ablation study."""
    with open(TRAINING_PATH) as f:
        all_data = json.load(f)

    feature_names = [k for k in all_data[0].keys()
                     if k not in ("label", "synergy", "commander", "candidate")]

    feature_groups = {
        "EDHREC direct synergy": ["edhrec_direct_synergy"],
        "EDHREC co-occurrence": ["edhrec_cooccur"],
        "Card2Vec": ["card2vec_sim"],
        "Spellbook": ["spellbook_combos", "total_spellbook", "spellbook_feature_match"],
        "Precon co-occur": ["precon_cooccur"],
        "Tag matching": ["provides_to_wants", "wants_from_provides", "bridge_score",
                         "bridge_x_oracle", "provides_x_bridge", "bidirectional"],
        "Oracle text": ["oracle_overlap", "rare_overlap", "text_similarity",
                        "cmdr_type_in_oracle", "card_type_in_cmdr_oracle"],
        "Card metadata": ["keyword_overlap", "shared_types", "type_match",
                          "cmc_diff", "rank_log", "total_tags", "color_overlap",
                          "card_rarity", "card_saltiness"],
        "Strategy/theme": ["strategy_match", "theme_overlap", "ability_match"],
    }

    # Split by commander for proper CV
    by_cmdr = {}
    for row in all_data:
        by_cmdr.setdefault(row["commander"], []).append(row)
    cmdrs = sorted(by_cmdr.keys())
    random.seed(42)
    random.shuffle(cmdrs)
    split = int(len(cmdrs) * 0.8)
    train_cmdrs = set(cmdrs[:split])
    test_cmdrs = set(cmdrs[split:])

    train_data = [r for r in all_data if r["commander"] in train_cmdrs]
    test_data = [r for r in all_data if r["commander"] in test_cmdrs]

    def compute_auc(train, test, feat_names, zero_edhrec=False):
        random.seed(42)
        model = quick_train(train, feat_names, zero_edhrec=zero_edhrec)
        pos_scores = []
        neg_scores = []
        for r in test:
            s = score_row(model, r, zero_edhrec=zero_edhrec)
            if r["label"] == 1:
                pos_scores.append(s)
            else:
                neg_scores.append(s)
        correct = sum(1 for ps in pos_scores for ns in neg_scores if ps > ns)
        total = len(pos_scores) * len(neg_scores)
        return correct / max(total, 1)

    print("=" * 70)
    print("FEATURE ABLATION STUDY (commander-level train/test split)")
    print("=" * 70)

    # Baseline with all features
    baseline = compute_auc(train_data, test_data, feature_names)
    print(f"\n  Baseline AUC (all features): {baseline:.3f}")

    # Baseline without EDHREC direct (independent mode)
    independent_features = [f for f in feature_names if f != "edhrec_direct_synergy"]
    baseline_indep = compute_auc(train_data, test_data, independent_features, zero_edhrec=True)
    print(f"  Independent baseline (no edhrec_direct): {baseline_indep:.3f}")
    print()

    # Ablate each group from the independent baseline
    print("  Ablation from independent baseline:")
    results = {}
    for group_name, group_features in sorted(feature_groups.items()):
        if group_name == "EDHREC direct synergy":
            continue  # Already removed in independent baseline
        remaining = [f for f in independent_features if f not in group_features]
        auc = compute_auc(train_data, test_data, remaining, zero_edhrec=True)
        drop = baseline_indep - auc
        results[group_name] = {"auc": auc, "drop": drop}

    for group in sorted(results, key=lambda x: -results[x]["drop"]):
        r = results[group]
        direction = f"-{r['drop']:.3f}" if r['drop'] > 0 else f"+{-r['drop']:.3f}"
        bar = "█" * max(0, int(r['drop'] * 500))
        print(f"    Without {group:<22} AUC={r['auc']:.3f} ({direction}) {bar}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ablation", action="store_true")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    if args.ablation:
        run_ablation()
    else:
        run_loo_cv()
