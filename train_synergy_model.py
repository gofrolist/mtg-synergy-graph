#!/usr/bin/env python3
"""Train a synergy prediction model from LLM-generated scores.

Phase 1: score_synergies.py generates (commander, card) → score labels via LLM
Phase 2: This script trains a model that predicts synergy from card features alone

The trained model can then score ANY commander × card pair without API calls.

Usage:
    # Check training data availability
    python3 train_synergy_model.py --stats

    # Train the model
    python3 train_synergy_model.py

    # Evaluate on held-out data
    python3 train_synergy_model.py --eval

    # Predict for a specific commander + card
    python3 train_synergy_model.py --predict "Krenko, Mob Boss" "Impact Tremors"
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time

import numpy as np

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MODEL_PATH = os.path.join(DATA_DIR, "synergy_model.json")


# ── Feature computation ─────────────────────────────────────────────────────
# Features that DON'T depend on EDHREC data

def compute_features(commander: dict, candidate: dict) -> dict:
    """Compute EDHREC-independent features for a commander-candidate pair."""
    cmdr_provides = set(commander.get("provides", []))
    cmdr_wants = set(commander.get("wants", []))
    cmdr_oracle = (commander.get("oracle_text") or "").lower()
    cmdr_keywords = {k.lower() for k in (commander.get("keywords") or [])}
    cmdr_type_line = (commander.get("type_line") or "").lower()
    cmdr_oid = commander.get("oracle_id", "")

    card_provides = set(candidate.get("provides", []))
    card_wants = set(candidate.get("wants", []))
    card_oracle = (candidate.get("oracle_text") or "").lower()
    card_keywords = {k.lower() for k in (candidate.get("keywords") or [])}
    card_type_line = (candidate.get("type_line") or "").lower()
    card_oid = candidate.get("oracle_id", "")

    # Strip reminder text from oracle
    card_oracle_clean = re.sub(r'\([^)]*\)', '', card_oracle)

    # 1. Direct tag connections
    provides_to_wants = len(card_provides & cmdr_wants)
    wants_from_provides = len(card_wants & cmdr_provides)
    bidirectional = min(provides_to_wants, wants_from_provides)

    # 2. Bridge connections (best per want, not accumulated)
    from synergy_graph import SEMANTIC_BRIDGES
    bridge_score = 0.0
    for want in cmdr_wants:
        best = 0.0
        for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
            if w_tag == want and p_tag in card_provides and weight > best:
                best = weight
        bridge_score += best
    for want in card_wants:
        best = 0.0
        for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
            if w_tag == want and p_tag in cmdr_provides and weight > best:
                best = weight
        bridge_score += best

    # 3. Oracle text concept overlap (strip reminder text)
    _concepts = [
        "human", "goblin", "elf", "zombie", "vampire", "dragon", "angel",
        "demon", "sliver", "artifact", "enchantment", "equipment", "aura",
        "instant", "sorcery", "token", "counter", "poison",
        "mill", "draw", "discard", "sacrifice", "exile", "return",
        "graveyard", "library", "damage", "life", "mana",
        "untap", "tap", "equip", "proliferate", "attack", "combat",
        "enters", "dies", "cast", "creature", "noncreature",
    ]
    cmdr_concepts = set()
    for word in _concepts:
        if re.search(r'\b' + word + r's?\b', cmdr_oracle):
            cmdr_concepts.add(word)

    oracle_overlap = 0
    for concept in cmdr_concepts:
        if re.search(r'\b' + concept + r's?\b', card_oracle_clean):
            oracle_overlap += 1

    # 4. Rare concept overlap (specific mechanics, not generic)
    rare_concepts = {"mill", "graveyard", "poison", "infect", "proliferate",
                     "equipment", "equip", "aura", "enchant", "sacrifice",
                     "exile", "counter", "token", "planeswalker",
                     "draw", "discard", "untap"}
    rare_overlap = 0
    for concept in rare_concepts:
        if re.search(r'\b' + concept + r's?\b', cmdr_oracle) and \
           re.search(r'\b' + concept + r's?\b', card_oracle_clean):
            rare_overlap += 1

    # 5. Keyword overlap
    keyword_overlap = len(card_keywords & cmdr_keywords)

    # 6. Shared creature types
    cmdr_subtypes = set()
    if " — " in cmdr_type_line:
        cmdr_subtypes = {s.strip().lower() for s in cmdr_type_line.split(" — ")[1].split()}
    card_subtypes = set()
    if " — " in card_type_line:
        card_subtypes = {s.strip().lower() for s in card_type_line.split(" — ")[1].split()}
    shared_types = len(cmdr_subtypes & card_subtypes)

    # 7. Commander type mentioned in card oracle
    cmdr_type_in_oracle = 0
    for subtype in cmdr_subtypes:
        if len(subtype) > 3 and re.search(r'\b' + re.escape(subtype) + r's?\b', card_oracle_clean):
            cmdr_type_in_oracle += 1

    # 8. Card type mentioned in commander oracle
    card_type_in_cmdr_oracle = 0
    for subtype in card_subtypes:
        if len(subtype) > 3 and re.search(r'\b' + re.escape(subtype) + r's?\b', cmdr_oracle):
            card_type_in_cmdr_oracle += 1

    # 9. CMC difference
    cmdr_cmc = commander.get("cmc", 0) or 0
    card_cmc = candidate.get("cmc", 0) or 0
    cmc_diff = abs(cmdr_cmc - card_cmc)

    # 10. Card rarity
    rarity_map = {"mythic": 4, "rare": 3, "uncommon": 2, "common": 1}
    card_rarity = rarity_map.get(candidate.get("rarity", ""), 1)

    # 11. EDHREC rank (general card popularity, not commander-specific)
    card_rank = candidate.get("edhrec_rank") or 50000
    rank_log = math.log10(max(card_rank, 1))

    # 12. Total tag count (card complexity proxy)
    total_tags = len(card_provides) + len(card_wants)

    # 13. Interaction features
    bridge_x_oracle = bridge_score * oracle_overlap
    provides_x_bridge = provides_to_wants * bridge_score

    # 14. Oracle text similarity (Jaccard)
    _stops = {'a', 'an', 'the', 'of', 'to', 'and', 'or', 'in', 'on', 'at', 'is',
              'it', 'that', 'this', 'for', 'you', 'your', 'its', 'may', 'if', 'with',
              'from', 'by', 'be', 'as', 'are', 'was', 'has', 'have', 'had', 'not',
              'but', 'all', 'each', 'any', 'one', 'two', 'three', 'can', 'do'}
    cmdr_words = set(cmdr_oracle.split()) - _stops if cmdr_oracle else set()
    card_words = set(card_oracle_clean.split()) - _stops if card_oracle_clean else set()
    text_similarity = len(cmdr_words & card_words) / max(len(cmdr_words | card_words), 1)

    # 15. Color identity overlap
    cmdr_ci = set(json.loads(commander.get("color_identity", "[]"))
                  if isinstance(commander.get("color_identity"), str) else
                  commander.get("color_identity", []))
    card_ci = set(json.loads(candidate.get("color_identity", "[]"))
                  if isinstance(candidate.get("color_identity"), str) else
                  candidate.get("color_identity", []))
    color_overlap = len(cmdr_ci & card_ci) / max(len(cmdr_ci), 1) if cmdr_ci else 0

    # 16. Spellbook combo potential (commander + card in same combo)
    spellbook_combos = 0
    if hasattr(compute_features, '_spellbook_cache'):
        cmdr_combos = compute_features._spellbook_cache.get(cmdr_oid, set())
        card_combos = compute_features._spellbook_cache.get(card_oid, set())
        spellbook_combos = len(cmdr_combos & card_combos)

    # 17. Spellbook feature overlap
    spellbook_feature_match = 0
    if hasattr(compute_features, '_spellbook_features'):
        cmdr_feats = compute_features._spellbook_features.get(cmdr_oid, set())
        card_feats = compute_features._spellbook_features.get(card_oid, set())
        spellbook_feature_match = len(cmdr_feats & card_feats)

    # 18. Ability trigger-effect match
    ability_match = 0
    if hasattr(compute_features, '_ability_cache'):
        cmdr_triggers, cmdr_effects = set(), set()
        card_triggers, card_effects = set(), set()
        for tt, et in compute_features._ability_cache.get(cmdr_oid, []):
            if tt:
                tags = json.loads(tt) if isinstance(tt, str) else tt
                cmdr_triggers.update(tags if isinstance(tags, (list, set)) else [])
            if et:
                tags = json.loads(et) if isinstance(et, str) else et
                cmdr_effects.update(tags if isinstance(tags, (list, set)) else [])
        for tt, et in compute_features._ability_cache.get(card_oid, []):
            if tt:
                tags = json.loads(tt) if isinstance(tt, str) else tt
                card_triggers.update(tags if isinstance(tags, (list, set)) else [])
            if et:
                tags = json.loads(et) if isinstance(et, str) else et
                card_effects.update(tags if isinstance(tags, (list, set)) else [])
        ability_match = len(cmdr_effects & card_triggers) + len(card_effects & cmdr_triggers)

    # 19. Strategy alignment
    strategy_match = 0
    if hasattr(compute_features, '_strategy_cache'):
        cmdr_strats = compute_features._strategy_cache.get(cmdr_oid, set())
        card_strats = compute_features._strategy_cache.get(card_oid, set())
        strategy_match = len(cmdr_strats & card_strats)

    # 20. Non-keyword ability count (card quality proxy)
    ability_count = 0
    if hasattr(compute_features, '_ability_cache'):
        abilities = compute_features._ability_cache.get(card_oid, [])
        # Count non-keyword abilities
        ability_count = len(abilities)

    # 21. Embedding similarity (oracle text semantic similarity)
    embedding_sim = 0.0
    if hasattr(compute_features, '_embedding_cache'):
        cmdr_emb = compute_features._embedding_cache.get(cmdr_oid)
        card_emb = compute_features._embedding_cache.get(card_oid)
        if cmdr_emb is not None and card_emb is not None:
            embedding_sim = float(np.dot(cmdr_emb, card_emb))

    return {
        "provides_to_wants": provides_to_wants,
        "wants_from_provides": wants_from_provides,
        "bidirectional": bidirectional,
        "bridge_score": round(bridge_score, 3),
        "oracle_overlap": oracle_overlap,
        "rare_overlap": rare_overlap,
        "keyword_overlap": keyword_overlap,
        "shared_types": shared_types,
        "cmdr_type_in_oracle": cmdr_type_in_oracle,
        "card_type_in_cmdr_oracle": card_type_in_cmdr_oracle,
        "cmc_diff": cmc_diff,
        "card_rarity": card_rarity,
        "rank_log": round(rank_log, 2),
        "total_tags": total_tags,
        "bridge_x_oracle": round(bridge_score * oracle_overlap, 3),
        "provides_x_bridge": round(provides_to_wants * bridge_score, 3),
        "text_similarity": round(text_similarity, 4),
        "color_overlap": round(color_overlap, 3),
        "spellbook_combos": min(spellbook_combos, 20),
        "spellbook_feature_match": min(spellbook_feature_match, 15),
        "ability_match": ability_match,
        "strategy_match": strategy_match,
        "ability_count": min(ability_count, 10),
        "embedding_sim": round(embedding_sim, 4),
    }


FEATURE_NAMES = list(compute_features.__code__.co_consts)  # Will be set properly below
# Actual names determined by the dict keys in compute_features return
FEATURE_NAMES = [
    "provides_to_wants", "wants_from_provides", "bidirectional",
    "bridge_score", "oracle_overlap", "rare_overlap",
    "keyword_overlap", "shared_types", "cmdr_type_in_oracle",
    "card_type_in_cmdr_oracle", "cmc_diff", "card_rarity",
    "rank_log", "total_tags", "bridge_x_oracle", "provides_x_bridge",
    "text_similarity", "color_overlap", "spellbook_combos",
    "spellbook_feature_match", "ability_match", "strategy_match",
    "ability_count", "embedding_sim",
]


# ── Cache initialization ────────────────────────────────────────────────────

def init_caches():
    """Load all caches needed for feature computation."""
    conn = sqlite3.connect(DB_PATH)

    # Spellbook cache
    spellbook_cache = {}
    for combo_id, oid in conn.execute("SELECT combo_id, oracle_id FROM spellbook_combo_cards"):
        spellbook_cache.setdefault(oid, set()).add(combo_id)
    compute_features._spellbook_cache = spellbook_cache

    # Ability cache
    ability_cache = {}
    for oid, tt, et in conn.execute("SELECT oracle_id, trigger_tags, effect_tags FROM abilities"):
        ability_cache.setdefault(oid, []).append((tt, et))
    compute_features._ability_cache = ability_cache

    # Strategy cache
    strategy_cache = {}
    for oid, strat in conn.execute("SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"):
        strategy_cache.setdefault(oid, set()).add(strat)
    compute_features._strategy_cache = strategy_cache

    # Spellbook feature cache
    spellbook_features = {}
    try:
        feat_names = {fid: fname for fid, fname in conn.execute("SELECT id, name FROM spellbook_features")}
        combo_features = {}
        for combo_id, fid in conn.execute("SELECT combo_id, feature_id FROM spellbook_combo_features"):
            combo_features.setdefault(combo_id, set()).add(feat_names.get(fid, ""))
        for oid, combos in spellbook_cache.items():
            for combo_id in combos:
                if combo_id in combo_features:
                    spellbook_features.setdefault(oid, set()).update(combo_features[combo_id])
    except Exception:
        pass
    compute_features._spellbook_features = spellbook_features

    # Embedding cache (oracle text embeddings)
    embedding_cache = {}
    emb_path = os.path.join(DATA_DIR, "embeddings.npy")
    idx_path = os.path.join(DATA_DIR, "embeddings_index.json")
    if os.path.exists(emb_path) and os.path.exists(idx_path):
        emb = np.load(emb_path)
        oid_list = json.load(open(idx_path))["oracle_ids"]
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1
        normed = emb / norms
        for i, oid in enumerate(oid_list):
            embedding_cache[oid] = normed[i]
    compute_features._embedding_cache = embedding_cache

    conn.close()
    print(f"  Caches: {len(spellbook_cache)} spellbook, {len(ability_cache)} abilities, "
          f"{len(strategy_cache)} strategies, {len(embedding_cache)} embeddings")


# ── Training data from LLM scores ──────────────────────────────────────────

def build_training_data() -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build training data from synergy_scores table (LLM-generated labels)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Check synergy_scores exist
    has_table = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='synergy_scores'"
    ).fetchone()[0]
    if not has_table:
        print("ERROR: No synergy_scores table. Run score_synergies.py first.")
        sys.exit(1)

    total_scores = conn.execute("SELECT COUNT(*) FROM synergy_scores").fetchone()[0]
    commanders = conn.execute(
        "SELECT DISTINCT commander_oid FROM synergy_scores"
    ).fetchall()
    print(f"Training data: {total_scores} scores from {len(commanders)} commanders")

    if total_scores < 100:
        print("ERROR: Need at least 100 synergy scores. Run score_synergies.py for more commanders.")
        sys.exit(1)

    # Load all card data
    cards_by_oid = {}
    for row in conn.execute("SELECT * FROM cards"):
        card = dict(row)
        card["provides"] = [r[0] for r in conn.execute(
            "SELECT tag FROM provides WHERE oracle_id = ?", (card["oracle_id"],))]
        card["wants"] = [r[0] for r in conn.execute(
            "SELECT tag FROM wants WHERE oracle_id = ?", (card["oracle_id"],))]
        try:
            card["keywords"] = json.loads(card["keywords"]) if card["keywords"] else []
        except Exception:
            card["keywords"] = []
        cards_by_oid[card["oracle_id"]] = card

    # Build feature matrix
    features = []
    labels = []
    pairs = []

    for cmdr_oid_row in commanders:
        cmdr_oid = cmdr_oid_row[0]
        cmdr = cards_by_oid.get(cmdr_oid)
        if not cmdr:
            continue

        scores = conn.execute(
            "SELECT card_oid, score FROM synergy_scores WHERE commander_oid = ?",
            (cmdr_oid,)
        ).fetchall()

        for card_oid, score in scores:
            card = cards_by_oid.get(card_oid)
            if not card:
                continue

            feats = compute_features(cmdr, card)
            features.append([feats[f] for f in FEATURE_NAMES])
            labels.append(score)
            pairs.append(f"{cmdr['name']} + {card['name']}")

    conn.close()

    X = np.array(features, dtype=np.float64)
    y = np.array(labels, dtype=np.float64)
    print(f"Feature matrix: {X.shape}, Labels: {y.shape}")
    print(f"Label distribution: min={y.min():.0f} max={y.max():.0f} mean={y.mean():.1f} std={y.std():.1f}")

    return X, y, pairs


# ── Model: Gradient Boosted Trees ───────────────────────────────────────────
# Simple GBT implementation (no sklearn dependency)

def _build_tree(X, residuals, max_depth=4, min_samples=5):
    """Build a single regression tree on residuals."""
    n_samples, n_features = X.shape

    def _split(indices, depth):
        if len(indices) < min_samples or depth >= max_depth:
            return {"value": float(np.mean(residuals[indices]))}

        best_feature = -1
        best_threshold = 0.0
        best_loss = float("inf")
        best_left = best_right = None

        parent_mean = np.mean(residuals[indices])
        parent_loss = np.sum((residuals[indices] - parent_mean) ** 2)

        for f in range(n_features):
            values = X[indices, f]
            # Try percentile-based thresholds for speed
            thresholds = np.unique(np.percentile(values, [20, 40, 50, 60, 80]))
            for t in thresholds:
                left = indices[values <= t]
                right = indices[values > t]
                if len(left) < min_samples or len(right) < min_samples:
                    continue
                left_mean = np.mean(residuals[left])
                right_mean = np.mean(residuals[right])
                loss = np.sum((residuals[left] - left_mean) ** 2) + \
                       np.sum((residuals[right] - right_mean) ** 2)
                if loss < best_loss:
                    best_loss = loss
                    best_feature = f
                    best_threshold = float(t)
                    best_left = left
                    best_right = right

        if best_feature == -1 or best_loss >= parent_loss * 0.99:
            return {"value": float(np.mean(residuals[indices]))}

        return {
            "feature": best_feature,
            "threshold": best_threshold,
            "left": _split(best_left, depth + 1),
            "right": _split(best_right, depth + 1),
        }

    return _split(np.arange(n_samples), 0)


def _predict_tree(tree, x):
    """Predict a single sample with a tree."""
    if "value" in tree:
        return tree["value"]
    if x[tree["feature"]] <= tree["threshold"]:
        return _predict_tree(tree["left"], x)
    else:
        return _predict_tree(tree["right"], x)


def train_gbt(X, y, n_trees=50, learning_rate=0.1, max_depth=4):
    """Train gradient boosted regression trees."""
    n_samples = len(y)
    init_pred = float(np.mean(y))
    predictions = np.full(n_samples, init_pred)
    trees = []

    for i in range(n_trees):
        residuals = y - predictions
        tree = _build_tree(X, residuals, max_depth=max_depth)
        tree_preds = np.array([_predict_tree(tree, X[j]) for j in range(n_samples)])
        predictions += learning_rate * tree_preds
        trees.append(tree)

        if (i + 1) % 10 == 0:
            mse = np.mean((y - predictions) ** 2)
            print(f"    Tree {i+1}/{n_trees}: MSE={mse:.4f}")

    return {
        "init": init_pred,
        "trees": trees,
        "learning_rate": learning_rate,
    }


def predict_gbt(model, x):
    """Predict with trained GBT model."""
    pred = model["init"]
    for tree in model["trees"]:
        pred += model["learning_rate"] * _predict_tree(tree, x)
    return pred


# ── Training pipeline ───────────────────────────────────────────────────────

def train():
    """Full training pipeline."""
    print("Initializing caches...")
    init_caches()

    print("\nBuilding training data from LLM scores...")
    X, y, pairs = build_training_data()

    # Normalize features
    means = np.mean(X, axis=0)
    stds = np.std(X, axis=0)
    stds[stds == 0] = 1.0
    X_norm = (X - means) / stds

    # Train/test split (80/20, stratified by commander)
    np.random.seed(42)
    n = len(y)
    indices = np.random.permutation(n)
    split = int(0.8 * n)
    train_idx, test_idx = indices[:split], indices[split:]

    X_train, y_train = X_norm[train_idx], y[train_idx]
    X_test, y_test = X_norm[test_idx], y[test_idx]

    print(f"\nTrain: {len(train_idx)}, Test: {len(test_idx)}")

    # Train GBT
    print("\nTraining GBT...")
    gbt = train_gbt(X_train, y_train, n_trees=50, learning_rate=0.1, max_depth=4)

    # Evaluate
    test_preds = np.array([predict_gbt(gbt, X_test[i]) for i in range(len(X_test))])
    mse = np.mean((y_test - test_preds) ** 2)
    mae = np.mean(np.abs(y_test - test_preds))
    corr = np.corrcoef(y_test, test_preds)[0, 1] if len(y_test) > 1 else 0

    # Classification accuracy: does it rank high-synergy (≥7) vs low (≤4) correctly?
    high_mask = y_test >= 7
    low_mask = y_test <= 4
    if high_mask.sum() > 0 and low_mask.sum() > 0:
        high_pred_mean = test_preds[high_mask].mean()
        low_pred_mean = test_preds[low_mask].mean()
        separation = high_pred_mean - low_pred_mean
    else:
        separation = 0

    print(f"\n{'='*50}")
    print(f"EVALUATION RESULTS")
    print(f"{'='*50}")
    print(f"  MSE:  {mse:.3f}")
    print(f"  MAE:  {mae:.3f}")
    print(f"  Corr: {corr:.3f}")
    print(f"  High(≥7) vs Low(≤4) separation: {separation:.2f}")
    print(f"  (Perfect separation ≈ 3-5, random ≈ 0)")

    # Feature importance (by permutation)
    print(f"\nFeature importance (permutation):")
    importances = []
    base_mse = mse
    for f in range(X_test.shape[1]):
        X_perm = X_test.copy()
        np.random.shuffle(X_perm[:, f])
        perm_preds = np.array([predict_gbt(gbt, X_perm[i]) for i in range(len(X_test))])
        perm_mse = np.mean((y_test - perm_preds) ** 2)
        importances.append((FEATURE_NAMES[f], perm_mse - base_mse))

    importances.sort(key=lambda x: -x[1])
    for name, imp in importances[:15]:
        bar = "█" * int(imp / max(importances[0][1], 0.001) * 20)
        print(f"  {name:<25} {imp:>8.4f} {bar}")

    # Save model
    model = {
        "gbt": gbt,
        "means": means.tolist(),
        "stds": stds.tolist(),
        "feature_names": FEATURE_NAMES,
        "metrics": {
            "mse": round(mse, 4),
            "mae": round(mae, 4),
            "correlation": round(corr, 4),
            "separation": round(separation, 3),
            "train_size": len(train_idx),
            "test_size": len(test_idx),
        },
        "model_type": "gbt_synergy",
    }

    with open(MODEL_PATH, "w") as f:
        json.dump(model, f, indent=2)
    print(f"\nModel saved to {MODEL_PATH}")

    return model


# ── Prediction ──────────────────────────────────────────────────────────────

def load_model() -> dict:
    """Load trained model."""
    if not os.path.exists(MODEL_PATH):
        print(f"ERROR: No model at {MODEL_PATH}. Run training first.")
        sys.exit(1)
    with open(MODEL_PATH) as f:
        return json.load(f)


def predict(model: dict, commander: dict, candidate: dict) -> float:
    """Predict synergy score for a commander-candidate pair. Returns 1-10."""
    feats = compute_features(commander, candidate)
    x = np.array([feats[f] for f in model["feature_names"]], dtype=np.float64)

    # Normalize
    means = np.array(model["means"])
    stds = np.array(model["stds"])
    x_norm = (x - means) / stds

    score = predict_gbt(model["gbt"], x_norm)
    return max(1.0, min(10.0, score))


def predict_pair(commander_name: str, card_name: str):
    """Predict and display synergy for a specific pair."""
    init_caches()
    model = load_model()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    def load_card(name):
        row = conn.execute("SELECT * FROM cards WHERE name = ?", (name,)).fetchone()
        if not row:
            print(f"ERROR: Card not found: {name}")
            sys.exit(1)
        card = dict(row)
        card["provides"] = [r[0] for r in conn.execute(
            "SELECT tag FROM provides WHERE oracle_id = ?", (card["oracle_id"],))]
        card["wants"] = [r[0] for r in conn.execute(
            "SELECT tag FROM wants WHERE oracle_id = ?", (card["oracle_id"],))]
        try:
            card["keywords"] = json.loads(card["keywords"]) if card["keywords"] else []
        except Exception:
            card["keywords"] = []
        return card

    cmdr = load_card(commander_name)
    card = load_card(card_name)
    conn.close()

    score = predict(model, cmdr, card)
    feats = compute_features(cmdr, card)

    print(f"\n{commander_name} + {card_name}")
    print(f"  Predicted synergy: {score:.1f}/10")
    print(f"\n  Key features:")
    for f in FEATURE_NAMES:
        v = feats[f]
        if v > 0:
            print(f"    {f}: {v}")


def show_stats():
    """Show training data availability."""
    conn = sqlite3.connect(DB_PATH)

    has_table = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='synergy_scores'"
    ).fetchone()[0]
    if not has_table:
        print("No synergy_scores table yet. Run score_synergies.py first.")
        conn.close()
        return

    total = conn.execute("SELECT COUNT(*) FROM synergy_scores").fetchone()[0]
    print(f"Total LLM synergy scores: {total}")

    print("\nPer commander:")
    for row in conn.execute("""
        SELECT c.name, COUNT(*) as cnt,
               ROUND(AVG(ss.score), 1) as avg_score
        FROM synergy_scores ss
        JOIN cards c ON c.oracle_id = ss.commander_oid
        GROUP BY ss.commander_oid
        ORDER BY cnt DESC
    """).fetchall():
        print(f"  {row[0]:<40} {row[1]:>5} cards  avg={row[2]}")

    has_model = os.path.exists(MODEL_PATH)
    print(f"\nModel: {'EXISTS' if has_model else 'NOT TRAINED'}")
    if has_model:
        with open(MODEL_PATH) as f:
            m = json.load(f)
        metrics = m.get("metrics", {})
        print(f"  MSE: {metrics.get('mse', '?')}")
        print(f"  Correlation: {metrics.get('correlation', '?')}")
        print(f"  Separation: {metrics.get('separation', '?')}")

    conn.close()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Train synergy prediction model from LLM scores")
    parser.add_argument("--stats", action="store_true", help="Show training data stats")
    parser.add_argument("--eval", action="store_true", help="Evaluate existing model")
    parser.add_argument("--predict", nargs=2, metavar=("COMMANDER", "CARD"),
                        help="Predict synergy for a specific pair")
    args = parser.parse_args()

    if args.stats:
        show_stats()
    elif args.predict:
        predict_pair(args.predict[0], args.predict[1])
    else:
        train()


if __name__ == "__main__":
    main()
