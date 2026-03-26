#!/usr/bin/env python3
"""Train a hybrid fusion model for EDHREC deck membership prediction.

Stage 1: Retrain two-tower neural net on EDHREC avg deck membership (binary).
Stage 2: LightGBM classifier on tower probability + 9 structural features.

Usage:
    python3 train_fusion_model.py                      # Full pipeline (tower + features + GBM)
    python3 train_fusion_model.py --tower-only         # Stage 1 only: binary tower
    python3 train_fusion_model.py --features-only      # Tower + feature matrix (no GBM)
    python3 train_fusion_model.py --feature-importance  # Print feature importance from saved GBM
"""

import argparse
import json
import os
import sqlite3
import time

import numpy as np

from train_tower_model import (
    load_embeddings,
    load_structural_features,
    compute_struct_features,
    forward,
    init_model,
)

import math

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TOWER_EDHREC_PATH = os.path.join(DATA_DIR, "tower_model_edhrec.npz")

FEATURE_NAMES = [
    "tower_prob", "causal_score", "forge_deck_overlap",
    "cmdr_tag_overlap", "strategy_keyword", "tribal_match",
    "edhrec_synergy", "edhrec_rank", "cmc", "is_creature",
]


def sigmoid(x):
    """Numerically stable sigmoid."""
    pos = x >= 0
    z = np.zeros_like(x)
    z[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    exp_x = np.exp(x[~pos])
    z[~pos] = exp_x / (1.0 + exp_x)
    return z


def roc_auc_score(y_true, y_score):
    """Compute ROC AUC without sklearn dependency."""
    # Sort by descending score
    order = np.argsort(-y_score)
    y_sorted = y_true[order]

    n_pos = y_sorted.sum()
    n_neg = len(y_sorted) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5

    # Accumulate true positives and compute AUC via trapezoidal rule
    tp = 0.0
    fp = 0.0
    auc = 0.0
    prev_fpr = 0.0
    prev_tpr = 0.0

    for i in range(len(y_sorted)):
        if y_sorted[i] == 1:
            tp += 1
        else:
            fp += 1
        fpr = fp / n_neg
        tpr = tp / n_pos
        auc += (fpr - prev_fpr) * (tpr + prev_tpr) / 2
        prev_fpr = fpr
        prev_tpr = tpr

    return auc


def load_edhrec_membership(conn):
    """Load positive pairs from edhrec_average_decks.

    Returns dict[cmdr_oracle_id -> set[card_oracle_ids]].
    """
    # Map card_name -> oracle_id via cards table
    name_to_oid = {}
    for row in conn.execute("SELECT name, oracle_id FROM cards"):
        name_to_oid[row[0]] = row[1]

    # Map commander_slug -> commander oracle_id
    # Slugs look like "krenko-mob-boss" - match against card names
    slug_to_oid = {}
    all_slugs = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT commander_slug FROM edhrec_average_decks"
        )
    )

    for slug in all_slugs:
        # Convert slug to LIKE pattern: "krenko-mob-boss" -> "%krenko%mob%boss%"
        parts = slug.split("-")
        pattern = "%" + "%".join(parts) + "%"
        row = conn.execute(
            "SELECT oracle_id FROM cards "
            "WHERE LOWER(REPLACE(REPLACE(name, '''', ''), ',', '')) LIKE ? "
            "AND type_line LIKE '%Legendary%' LIMIT 1",
            (pattern,),
        ).fetchone()
        if row:
            slug_to_oid[slug] = row[0]

    print(f"  Matched {len(slug_to_oid)}/{len(all_slugs)} commander slugs to oracle_ids")

    # Build positives: commander_oid -> set of card oracle_ids
    positives = {}
    unmatched_cards = 0
    for slug, cmdr_oid in slug_to_oid.items():
        card_oids = set()
        for row in conn.execute(
            "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?",
            (slug,),
        ):
            card_oid = name_to_oid.get(row[0])
            if card_oid and card_oid != cmdr_oid:
                card_oids.add(card_oid)
            elif not card_oid:
                unmatched_cards += 1
        if card_oids:
            positives[cmdr_oid] = card_oids

    total_pos = sum(len(v) for v in positives.values())
    print(f"  Positive pairs: {total_pos} across {len(positives)} commanders")
    if unmatched_cards > 0:
        print(f"  Unmatched card names: {unmatched_cards}")

    return positives


def sample_negatives(positives_by_cmdr, all_card_oids, card_colors, cmdr_colors, ratio=3):
    """Sample negative pairs (cards NOT in a commander's avg deck).

    For each commander, samples ratio * |positives| random cards that:
    - Are NOT in their avg deck
    - Have color identity subset of commander's color identity
    - Are not basic lands or tokens

    Returns list of (cmdr_oid, card_oid, 0) tuples.
    """
    rng = np.random.RandomState(42)

    # Pre-compute basic land oracle IDs to exclude
    basic_land_names = {"Plains", "Island", "Swamp", "Mountain", "Forest",
                        "Snow-Covered Plains", "Snow-Covered Island",
                        "Snow-Covered Swamp", "Snow-Covered Mountain",
                        "Snow-Covered Forest", "Wastes"}

    all_oids_set = set(all_card_oids)
    negatives = []

    for cmdr_oid, pos_cards in positives_by_cmdr.items():
        cmdr_ci = cmdr_colors.get(cmdr_oid, set())

        # Candidate pool: color-legal, not in deck, not basic land
        candidates = []
        for oid in all_card_oids:
            if oid in pos_cards or oid == cmdr_oid:
                continue
            card_ci = card_colors.get(oid, set())
            if not card_ci.issubset(cmdr_ci):
                continue
            candidates.append(oid)

        n_neg = min(len(candidates), ratio * len(pos_cards))
        if n_neg == 0:
            continue

        chosen = rng.choice(len(candidates), size=n_neg, replace=False)
        for idx in chosen:
            negatives.append((cmdr_oid, candidates[idx], 0))

    return negatives


def build_feature_matrix(pairs_by_cmdr, tower_model_path=TOWER_EDHREC_PATH):
    """Build 10-feature matrix for all (commander, card) pairs.

    Args:
        pairs_by_cmdr: dict[cmdr_oid -> list[(card_oid, label)]] from
            load_edhrec_membership + sample_negatives
        tower_model_path: path to trained tower model (.npz)

    Returns:
        X: np.array(N, 10) feature matrix
        y: np.array(N,) labels
        cmdr_ids: np.array(N,) commander index per pair (for CV splits)
    """
    conn = sqlite3.connect(DB_PATH)

    # ── Bulk-load all DB data at startup ──────────────────────────────────

    # 1. Card metadata (name, oracle_id, type_line, cmc, edhrec_rank, oracle_text)
    card_meta = {}          # oid -> {name, type_line, cmc, edhrec_rank, oracle_text}
    name_to_oid = {}        # name -> oid
    for row in conn.execute(
        "SELECT oracle_id, name, type_line, cmc, edhrec_rank, oracle_text FROM cards"
    ):
        oid, name, type_line, cmc, edhrec_rank, oracle_text = row
        card_meta[oid] = {
            "name": name,
            "type_line": type_line or "",
            "cmc": cmc or 0.0,
            "edhrec_rank": edhrec_rank or 50000,
            "oracle_text": (oracle_text or "").lower(),
        }
        name_to_oid[name] = oid

    # 2. Provides / wants tags (bulk)
    provides_map = {}       # oid -> set[tag]
    wants_map = {}          # oid -> set[tag]
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM provides"):
        provides_map.setdefault(oid, set()).add(tag)
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM wants"):
        wants_map.setdefault(oid, set()).add(tag)

    # 3. Forge deck tags (bulk)
    forge_has = {}          # oid -> set[tag]
    forge_hints = {}        # oid -> set[tag]
    try:
        for card_name, tag_type, tag in conn.execute(
            "SELECT card_name, tag_type, tag FROM forge_deck_tags"
        ):
            oid = name_to_oid.get(card_name)
            if not oid:
                continue
            if tag_type == "has":
                forge_has.setdefault(oid, set()).add(tag)
            elif tag_type == "hints":
                forge_hints.setdefault(oid, set()).add(tag)
    except Exception:
        pass

    # 4. EDHREC synergy (slug, card_name) -> synergy score
    #    Also build slug -> cmdr_oid mapping
    slug_to_oid = {}
    all_slugs = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT commander_slug FROM edhrec_average_decks"
        )
    )
    for slug in all_slugs:
        parts = slug.split("-")
        pattern = "%" + "%".join(parts) + "%"
        row = conn.execute(
            "SELECT oracle_id FROM cards "
            "WHERE LOWER(REPLACE(REPLACE(name, '''', ''), ',', '')) LIKE ? "
            "AND type_line LIKE '%Legendary%' LIMIT 1",
            (pattern,),
        ).fetchone()
        if row:
            slug_to_oid[slug] = row[0]
    oid_to_slug = {v: k for k, v in slug_to_oid.items()}

    edhrec_syn_map = {}     # (slug, card_name) -> synergy
    try:
        for slug, card_name, syn in conn.execute(
            "SELECT commander_slug, card_name, synergy FROM edhrec_card_synergy"
        ):
            if syn is not None:
                edhrec_syn_map[(slug, card_name)] = syn
    except Exception:
        pass

    # 5. Commander profiles (strategies)
    from mtg_synergy.recommend.commander_profile import load_profile
    from mtg_synergy.recommend.scoring import STRATEGY_KEYWORDS

    # 6. Tower model data (load once)
    print("  Loading tower model for inference...")
    normed_emb, oid_list, oid_to_idx = load_embeddings()
    sf_data = load_structural_features()
    provides_sf, wants_sf, strats_sf, types_sf, oracles_sf, ranks_sf, mech_sf = sf_data

    tower_model = None
    tower_means = None
    tower_stds = None
    if os.path.exists(tower_model_path):
        td = np.load(tower_model_path)
        tower_model = {
            k: td[k] for k in td.files
            if k not in ("struct_means", "struct_stds")
        }
        tower_means = td["struct_means"]
        tower_stds = td["struct_stds"]
        print(f"  Tower model loaded from {tower_model_path}", flush=True)
    else:
        print(f"  WARNING: Tower model not found at {tower_model_path}, tower_prob=0.5")

    # ── Build pairs list with commander index ─────────────────────────────

    cmdr_oids_ordered = sorted(pairs_by_cmdr.keys())
    cmdr_to_idx = {oid: i for i, oid in enumerate(cmdr_oids_ordered)}

    all_rows = []       # list of (cmdr_oid, card_oid, label)
    for cmdr_oid, pairs in pairs_by_cmdr.items():
        for card_oid, label in pairs:
            all_rows.append((cmdr_oid, card_oid, label))

    N = len(all_rows)
    X = np.zeros((N, 10), dtype=np.float32)
    y = np.zeros(N, dtype=np.float32)
    cmdr_ids = np.zeros(N, dtype=np.int32)

    # ── Process commander-by-commander ────────────────────────────────────

    n_cmdrs = len(cmdr_oids_ordered)
    row_idx = 0      # tracks position in X/y for sequential fill

    # Re-organise for commander-batch processing
    cmdr_pair_map = {}  # cmdr_oid -> [(card_oid, label)]
    for cmdr_oid, card_oid, label in all_rows:
        cmdr_pair_map.setdefault(cmdr_oid, []).append((card_oid, label))

    for ci, cmdr_oid in enumerate(cmdr_oids_ordered):
        if (ci + 1) % 50 == 0 or ci == 0:
            print(f"  Building features for commander {ci+1}/{n_cmdrs}...", flush=True)

        pairs = cmdr_pair_map.get(cmdr_oid, [])
        if not pairs:
            continue

        card_oids = [p[0] for p in pairs]
        labels = [p[1] for p in pairs]
        n_pairs = len(pairs)

        # --- (a) Tower batch inference ---
        tower_probs = np.full(n_pairs, 0.5, dtype=np.float32)
        if tower_model is not None:
            cmdr_idx = oid_to_idx.get(cmdr_oid)
            if cmdr_idx is not None:
                # Gather card indices that exist in embeddings
                batch_card_indices = []
                batch_positions = []
                for j, card_oid in enumerate(card_oids):
                    cidx = oid_to_idx.get(card_oid)
                    if cidx is not None:
                        batch_card_indices.append(cidx)
                        batch_positions.append(j)

                if batch_card_indices:
                    B = len(batch_card_indices)
                    X_cmdr_batch = np.tile(
                        normed_emb[cmdr_idx], (B, 1)
                    ).astype(np.float32)
                    X_card_batch = normed_emb[batch_card_indices].astype(np.float32)

                    # Compute structural features for batch
                    sf_batch = np.zeros((B, 12), dtype=np.float32)
                    for k, pos in enumerate(batch_positions):
                        sf_batch[k] = compute_struct_features(
                            cmdr_oid, card_oids[pos],
                            provides_sf, wants_sf, strats_sf,
                            types_sf, oracles_sf, ranks_sf, mech_sf,
                        )
                    # Normalize
                    sf_norm = (sf_batch - tower_means) / tower_stds

                    raw, _ = forward(tower_model, X_cmdr_batch, X_card_batch, sf_norm)
                    probs = sigmoid(raw)
                    for k, pos in enumerate(batch_positions):
                        tower_probs[pos] = probs[k]

        # --- (b) Causal score (lightweight bulk query) ---
        causal_scores = np.zeros(n_pairs, dtype=np.float32)
        card_oid_set = set(card_oids)
        try:
            # Commander -> card edges (outgoing)
            cmdr_out = {}
            for row in conn.execute(
                "SELECT target_id, SUM(strength) FROM interaction_edges "
                "WHERE source_id = ? GROUP BY target_id",
                (cmdr_oid,),
            ):
                if row[0] in card_oid_set:
                    cmdr_out[row[0]] = row[1]
            # Card -> commander edges (incoming)
            cmdr_in = {}
            for row in conn.execute(
                "SELECT source_id, SUM(strength) FROM interaction_edges "
                "WHERE target_id = ? GROUP BY source_id",
                (cmdr_oid,),
            ):
                if row[0] in card_oid_set:
                    cmdr_in[row[0]] = row[1]
            for j, card_oid in enumerate(card_oids):
                out_str = cmdr_out.get(card_oid, 0.0)
                in_str = cmdr_in.get(card_oid, 0.0)
                score = out_str + in_str
                # Bidirectional bonus
                if out_str > 0 and in_str > 0:
                    score *= 1.5
                causal_scores[j] = max(min(score, 10.0), -5.0)
        except Exception:
            pass  # causal_scores stays 0

        # --- (c) Forge deck overlap ---
        cmdr_forge_has = forge_has.get(cmdr_oid, set())
        cmdr_forge_hints = forge_hints.get(cmdr_oid, set())

        # --- (d) Commander tag overlap ---
        cmdr_provides = provides_map.get(cmdr_oid, set())
        cmdr_wants = wants_map.get(cmdr_oid, set())

        # --- (e) Strategy keyword hits ---
        profile = None
        strategy_keywords = []
        try:
            profile = load_profile(conn, cmdr_oid)
            if profile and profile.strategies:
                for strat in profile.strategies:
                    strategy_keywords.extend(STRATEGY_KEYWORDS.get(strat, []))
        except Exception:
            pass

        # --- (f) Tribal match: commander subtypes ---
        cmdr_type_line = card_meta.get(cmdr_oid, {}).get("type_line", "")
        cmdr_subtypes = set()
        if "\u2014" in cmdr_type_line:
            try:
                cmdr_subtypes = {
                    s.lower() for s in cmdr_type_line.split("\u2014")[1].strip().split()
                }
            except (IndexError, AttributeError):
                pass

        # --- (g) EDHREC synergy: get slug for this commander ---
        cmdr_slug = oid_to_slug.get(cmdr_oid, "")

        # --- Fill feature rows for all pairs of this commander ---
        for j in range(n_pairs):
            card_oid = card_oids[j]
            meta = card_meta.get(card_oid, {})
            card_name = meta.get("name", "")
            card_type_line = meta.get("type_line", "")
            card_oracle = meta.get("oracle_text", "")

            # F0: tower_prob
            X[row_idx, 0] = tower_probs[j]

            # F1: causal_score
            X[row_idx, 1] = causal_scores[j]

            # F2: forge_deck_overlap
            card_has = forge_has.get(card_oid, set())
            card_hints = forge_hints.get(card_oid, set())
            X[row_idx, 2] = float(
                len(card_has & cmdr_forge_hints) + len(card_hints & cmdr_forge_has)
            )

            # F3: cmdr_tag_overlap
            card_prov = provides_map.get(card_oid, set())
            card_want = wants_map.get(card_oid, set())
            X[row_idx, 3] = float(
                len(card_prov & cmdr_wants) + len(card_want & cmdr_provides)
            )

            # F4: strategy_keyword
            kw_hits = 0
            if strategy_keywords and card_oracle:
                kw_hits = sum(1 for kw in strategy_keywords if kw in card_oracle)
            X[row_idx, 4] = float(kw_hits)

            # F5: tribal_match
            tribal = 0.0
            if cmdr_subtypes and "creature" in card_type_line.lower():
                card_subtypes_raw = set()
                if "\u2014" in card_type_line:
                    try:
                        card_subtypes_raw = {
                            s.lower()
                            for s in card_type_line.split("\u2014")[1].strip().split()
                        }
                    except (IndexError, AttributeError):
                        pass
                if cmdr_subtypes & card_subtypes_raw:
                    tribal = 1.0
            X[row_idx, 5] = tribal

            # F6: edhrec_synergy
            edh_syn = 0.0
            if cmdr_slug and card_name:
                edh_syn = edhrec_syn_map.get((cmdr_slug, card_name), 0.0)
            X[row_idx, 6] = edh_syn

            # F7: edhrec_rank (log10)
            X[row_idx, 7] = math.log10(max(meta.get("edhrec_rank", 50000), 1))

            # F8: cmc
            X[row_idx, 8] = float(meta.get("cmc", 0.0))

            # F9: is_creature
            X[row_idx, 9] = 1.0 if "Creature" in card_type_line else 0.0

            y[row_idx] = float(labels[j])
            cmdr_ids[row_idx] = cmdr_to_idx[cmdr_oid]
            row_idx += 1

    # Trim arrays to actual rows filled (in case any were skipped)
    X = X[:row_idx]
    y = y[:row_idx]
    cmdr_ids = cmdr_ids[:row_idx]

    conn.close()

    # Print summary statistics
    print(f"\nFeature matrix: {X.shape}")
    print(f"  Positive labels: {int(y.sum())}")
    print(f"  Negative labels: {int(len(y) - y.sum())}")
    print(f"  Commanders: {len(cmdr_oids_ordered)}")
    print(f"\nPer-feature statistics:")
    for i, name in enumerate(FEATURE_NAMES):
        col = X[:, i]
        print(f"  {name:>20s}: "
              f"mean={col.mean():.4f}  std={col.std():.4f}  "
              f"min={col.min():.4f}  max={col.max():.4f}  "
              f"nonzero={np.count_nonzero(col)}/{len(col)}")

    return X, y, cmdr_ids


def make_cv_splits(cmdr_ids, n_folds=5, seed=42):
    """Leave-commander-group-out CV splits.

    Ensures no commander appears in both train and test within the same fold.
    Returns list of (train_idx, test_idx) tuples.
    """
    unique_cmdrs = np.unique(cmdr_ids)
    rng = np.random.RandomState(seed)
    rng.shuffle(unique_cmdrs)
    fold_size = len(unique_cmdrs) // n_folds
    splits = []
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else len(unique_cmdrs)
        test_cmdrs = set(unique_cmdrs[start:end])
        test_idx = np.array([j for j, c in enumerate(cmdr_ids) if c in test_cmdrs])
        train_idx = np.array([j for j, c in enumerate(cmdr_ids) if c not in test_cmdrs])
        splits.append((train_idx, test_idx))
    return splits


def train_gbm(X, y, cmdr_ids):
    """Train LightGBM with leave-commander-out CV.

    Returns (model, cv_scores) where cv_scores is a dict with mean_auc and fold_aucs.
    """
    import lightgbm as lgb
    import joblib
    from sklearn.metrics import roc_auc_score as sklearn_auc

    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 63,
        "learning_rate": 0.05,
        "n_estimators": 500,
        "verbose": -1,
    }

    splits = make_cv_splits(cmdr_ids, n_folds=5)
    fold_aucs = []
    for fold_i, (train_idx, test_idx) in enumerate(splits):
        model = lgb.LGBMClassifier(**params)
        model.fit(
            X[train_idx], y[train_idx],
            eval_set=[(X[test_idx], y[test_idx])],
            feature_name=FEATURE_NAMES,
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        proba = model.predict_proba(X[test_idx])[:, 1]
        auc = sklearn_auc(y[test_idx], proba)
        fold_aucs.append(auc)
        print(f"  Fold {fold_i+1}: AUC={auc:.4f}")

    print(f"  Mean AUC: {np.mean(fold_aucs):.4f}")

    # Train final model on all data
    final_model = lgb.LGBMClassifier(**params)
    final_model.fit(X, y, feature_name=FEATURE_NAMES)
    model_path = os.path.join(DATA_DIR, "fusion_model.lgb")
    joblib.dump(final_model, model_path)
    print(f"  Model saved to {model_path}")

    return final_model, {"mean_auc": float(np.mean(fold_aucs)), "fold_aucs": fold_aucs}


def train_tower_binary():
    """Train two-tower model on EDHREC membership (binary classification)."""
    print("=" * 60)
    print("Stage 1: Training tower on EDHREC binary membership")
    print("=" * 60)

    print("\nLoading embeddings...")
    normed_emb, oid_list, oid_to_idx = load_embeddings()

    print("Loading structural features...")
    provides, wants, strats, types, oracles, ranks, mech_data = load_structural_features()

    conn = sqlite3.connect(DB_PATH)

    # Load color identities for all cards
    card_colors = {}
    for row in conn.execute("SELECT oracle_id, color_identity FROM cards"):
        card_colors[row[0]] = set(json.loads(row[1] or "[]"))

    # Load commander color identities (same dict, commanders are cards)
    cmdr_colors = card_colors

    # Get basic land oracle IDs to exclude from negative sampling
    basic_land_names = {"Plains", "Island", "Swamp", "Mountain", "Forest",
                        "Snow-Covered Plains", "Snow-Covered Island",
                        "Snow-Covered Swamp", "Snow-Covered Mountain",
                        "Snow-Covered Forest", "Wastes"}
    basic_land_oids = set()
    for name in basic_land_names:
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        if row:
            basic_land_oids.add(row[0])

    # Filter: only cards with embeddings and that are legal in commander
    all_card_oids = []
    for oid in oid_list:
        if oid in basic_land_oids:
            continue
        tl = types.get(oid, "")
        if "Token" in tl or "token" in tl:
            continue
        all_card_oids.append(oid)

    print(f"\nCard pool: {len(all_card_oids)} cards (with embeddings, excl. basics/tokens)")

    # Load positives
    print("\nLoading EDHREC membership data...")
    positives_by_cmdr = load_edhrec_membership(conn)

    # Filter to commanders that have embeddings
    positives_by_cmdr = {
        k: v for k, v in positives_by_cmdr.items()
        if k in oid_to_idx
    }
    # Filter card oids to those with embeddings
    for cmdr_oid in list(positives_by_cmdr.keys()):
        positives_by_cmdr[cmdr_oid] = {
            oid for oid in positives_by_cmdr[cmdr_oid]
            if oid in oid_to_idx
        }
        if not positives_by_cmdr[cmdr_oid]:
            del positives_by_cmdr[cmdr_oid]

    total_pos = sum(len(v) for v in positives_by_cmdr.values())
    print(f"  Positive pairs with embeddings: {total_pos} across {len(positives_by_cmdr)} commanders")

    # Sample negatives
    print("\nSampling negatives (ratio=3)...")
    neg_pairs = sample_negatives(
        positives_by_cmdr, all_card_oids, card_colors, cmdr_colors, ratio=3
    )
    print(f"  Negative pairs: {len(neg_pairs)}")

    # Build training data: positives + negatives
    print("\nBuilding feature arrays...")
    all_pairs = []
    for cmdr_oid, card_oids in positives_by_cmdr.items():
        for card_oid in card_oids:
            all_pairs.append((cmdr_oid, card_oid, 1))
    all_pairs.extend(neg_pairs)

    cmdr_embs = []
    card_embs = []
    struct_feats = []
    labels = []
    skipped = 0

    for cmdr_oid, card_oid, label in all_pairs:
        ci = oid_to_idx.get(cmdr_oid)
        di = oid_to_idx.get(card_oid)
        if ci is None or di is None:
            skipped += 1
            continue
        cmdr_embs.append(normed_emb[ci])
        card_embs.append(normed_emb[di])
        struct_feats.append(compute_struct_features(
            cmdr_oid, card_oid, provides, wants, strats, types, oracles, ranks, mech_data
        ))
        labels.append(label)

    conn.close()

    X_cmdr = np.array(cmdr_embs, dtype=np.float32)
    X_card = np.array(card_embs, dtype=np.float32)
    X_struct = np.array(struct_feats, dtype=np.float32)
    y = np.array(labels, dtype=np.float32)

    if skipped > 0:
        print(f"  Skipped {skipped} pairs (missing embeddings)")

    # Normalize structural features
    struct_means = X_struct.mean(axis=0)
    struct_stds = X_struct.std(axis=0)
    struct_stds[struct_stds == 0] = 1
    X_struct = (X_struct - struct_means) / struct_stds

    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    print(f"\nData: {len(y)} pairs ({n_pos} positive, {n_neg} negative)")
    print(f"  cmdr={X_cmdr.shape}, card={X_card.shape}, struct={X_struct.shape}")

    # Split train/test (stratified by label)
    np.random.seed(42)
    pos_idx = np.where(y == 1)[0]
    neg_idx = np.where(y == 0)[0]
    np.random.shuffle(pos_idx)
    np.random.shuffle(neg_idx)

    pos_split = int(0.8 * len(pos_idx))
    neg_split = int(0.8 * len(neg_idx))
    train_idx = np.concatenate([pos_idx[:pos_split], neg_idx[:neg_split]])
    test_idx = np.concatenate([pos_idx[pos_split:], neg_idx[neg_split:]])
    np.random.shuffle(train_idx)
    np.random.shuffle(test_idx)

    print(f"  Train: {len(train_idx)} ({int(y[train_idx].sum())} pos)")
    print(f"  Test: {len(test_idx)} ({int(y[test_idx].sum())} pos)")

    # Initialize model
    model = init_model()
    # Override output bias for binary classification (logit 0 = 50% probability)
    model["b4"] = np.float32(0.0)

    # Adam optimizer state
    adam_m = {k: np.zeros_like(v) for k, v in model.items()}
    adam_v = {k: np.zeros_like(v) for k, v in model.items()}

    lr = 0.001
    batch_size = 512
    epochs = 150
    dropout_rate = 0.1
    patience = 20
    best_auc = 0.0
    best_model = None
    best_epoch = 0
    best_auc_epoch = 0  # Track actual epoch of best AUC for display
    eps = 1e-7

    print(f"\nTraining for {epochs} epochs (batch={batch_size}, lr={lr}, patience={patience})...")
    t0 = time.time()

    for epoch in range(epochs):
        # Shuffle training data
        perm = np.random.permutation(len(train_idx))
        epoch_loss = 0
        n_batches = 0

        for i in range(0, len(train_idx), batch_size):
            batch = train_idx[perm[i:i + batch_size]]

            # Forward pass (raw logits, no clipping)
            raw, cache = forward(
                model, X_cmdr[batch], X_card[batch], X_struct[batch],
                dropout_rate=dropout_rate, training=True,
            )

            batch_labels = y[batch]
            N = len(batch)

            # BCE loss: -mean(y*log(sig) + (1-y)*log(1-sig))
            prob = sigmoid(raw)
            loss = -np.mean(
                batch_labels * np.log(prob + eps)
                + (1 - batch_labels) * np.log(1 - prob + eps)
            )
            epoch_loss += loss
            n_batches += 1

            # BCE gradient w.r.t. raw logit: (sigmoid(raw) - label) / N
            grad_out = (prob - batch_labels) / N

            # --- Backward pass (same chain rule as existing, different initial gradient) ---

            # W4, b4
            dW4 = cache["h3"].T @ grad_out[:, None]
            db4 = np.float32(grad_out.sum())

            # h3 -> W3, b3
            dh3 = grad_out[:, None] * model["W4"].T
            dz3 = dh3 * (cache["h3"] > 0).astype(np.float32)
            dW3 = cache["h2"].T @ dz3
            db3 = dz3.sum(axis=0)

            # h2 -> W2, b2 (with dropout mask)
            dh2 = dz3 @ model["W3"].T
            if cache["mask2"] is not None:
                dh2 = dh2 * cache["mask2"]
            dz2 = dh2 * (cache["h2"] > 0).astype(np.float32)
            dW2 = cache["h1"].T @ dz2
            db2 = dz2.sum(axis=0)

            # h1 -> W1, b1 (with dropout mask)
            dh1 = dz2 @ model["W2"].T
            if cache["mask1"] is not None:
                dh1 = dh1 * cache["mask1"]
            dz1 = dh1 * (cache["h1"] > 0).astype(np.float32)
            dW1 = cache["combined"].T @ dz1
            db1 = dz1.sum(axis=0)

            # combined -> interaction + struct
            d_combined = dz1 @ model["W1"].T
            d_interaction = d_combined[:, :128]

            # interaction = cmdr_proj * card_proj
            d_cmdr_proj = d_interaction * cache["card_proj"]
            d_card_proj = d_interaction * cache["cmdr_proj"]

            # ReLU gradient on projections
            d_cmdr_proj = d_cmdr_proj * (cache["cmdr_proj"] > 0).astype(np.float32)
            d_card_proj = d_card_proj * (cache["card_proj"] > 0).astype(np.float32)

            # Projection gradients
            dW_cmdr = X_cmdr[batch].T @ d_cmdr_proj
            db_cmdr = d_cmdr_proj.sum(axis=0)
            dW_card = X_card[batch].T @ d_card_proj
            db_card = d_card_proj.sum(axis=0)

            # Adam update
            grads = {
                "W_cmdr": dW_cmdr, "b_cmdr": db_cmdr,
                "W_card": dW_card, "b_card": db_card,
                "W1": dW1, "b1": db1,
                "W2": dW2, "b2": db2,
                "W3": dW3, "b3": db3,
                "W4": dW4, "b4": db4,
            }
            t_step = epoch * (len(train_idx) // batch_size) + i // batch_size + 1
            for key in model:
                g = grads[key]
                adam_m[key] = 0.9 * adam_m[key] + 0.1 * g
                adam_v[key] = 0.999 * adam_v[key] + 0.001 * (g ** 2)
                m_hat = adam_m[key] / (1 - 0.9 ** t_step)
                v_hat = adam_v[key] / (1 - 0.999 ** t_step)
                model[key] = model[key] - lr * m_hat / (np.sqrt(v_hat) + 1e-8)

        # Evaluate every 10 epochs
        if (epoch + 1) % 10 == 0:
            test_raw, _ = forward(model, X_cmdr[test_idx], X_card[test_idx], X_struct[test_idx])
            test_prob = sigmoid(test_raw)

            # AUC
            auc = roc_auc_score(y[test_idx], test_prob)

            # Accuracy at 0.5 threshold
            test_pred_label = (test_prob >= 0.5).astype(np.float32)
            accuracy = np.mean(test_pred_label == y[test_idx])

            # BCE on test
            test_bce = -np.mean(
                y[test_idx] * np.log(test_prob + eps)
                + (1 - y[test_idx]) * np.log(1 - test_prob + eps)
            )

            print(f"  Epoch {epoch+1:>3}: train_bce={epoch_loss/n_batches:.4f} "
                  f"test_bce={test_bce:.4f} AUC={auc:.4f} acc={accuracy:.3f}")

            if auc > best_auc:
                best_auc = auc
                best_model = {k: v.copy() for k, v in model.items()}
                best_epoch = epoch + 1
                best_auc_epoch = epoch + 1
            elif (epoch + 1) - best_epoch >= patience and lr > 0.0001:
                lr *= 0.5
                best_epoch = epoch + 1  # Reset patience after LR reduction
                print(f"    LR reduced to {lr}")

    elapsed = time.time() - t0
    print(f"\nTraining: {elapsed:.1f}s, best AUC={best_auc:.4f} (epoch {best_auc_epoch})")

    # Save best model
    if best_model:
        np.savez(
            TOWER_EDHREC_PATH,
            **best_model,
            struct_means=struct_means,
            struct_stds=struct_stds,
        )
        print(f"Model saved to {TOWER_EDHREC_PATH}")
    else:
        print("WARNING: No best model found, saving final model")
        np.savez(
            TOWER_EDHREC_PATH,
            **model,
            struct_means=struct_means,
            struct_stds=struct_stds,
        )

    # Final evaluation on best model
    final_model = best_model or model
    test_raw, _ = forward(final_model, X_cmdr[test_idx], X_card[test_idx], X_struct[test_idx])
    test_prob = sigmoid(test_raw)
    final_auc = roc_auc_score(y[test_idx], test_prob)
    final_acc = np.mean((test_prob >= 0.5).astype(np.float32) == y[test_idx])

    # Precision/recall at 0.5 threshold
    tp = np.sum((test_prob >= 0.5) & (y[test_idx] == 1))
    fp = np.sum((test_prob >= 0.5) & (y[test_idx] == 0))
    fn = np.sum((test_prob < 0.5) & (y[test_idx] == 1))
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0

    print(f"\nFinal test metrics:")
    print(f"  AUC:       {final_auc:.4f}")
    print(f"  Accuracy:  {final_acc:.3f}")
    print(f"  Precision: {precision:.3f}")
    print(f"  Recall:    {recall:.3f}")

    return final_model


def _load_pairs_for_features(conn, oid_to_idx):
    """Load EDHREC membership + sample negatives, return pairs_by_cmdr for build_feature_matrix.

    Returns dict[cmdr_oid -> list[(card_oid, label)]].
    """
    # Load color identities
    card_colors = {}
    for row in conn.execute("SELECT oracle_id, color_identity FROM cards"):
        card_colors[row[0]] = set(json.loads(row[1] or "[]"))

    # Get basic land oracle IDs to exclude
    basic_land_names = {"Plains", "Island", "Swamp", "Mountain", "Forest",
                        "Snow-Covered Plains", "Snow-Covered Island",
                        "Snow-Covered Swamp", "Snow-Covered Mountain",
                        "Snow-Covered Forest", "Wastes"}
    basic_land_oids = set()
    for name in basic_land_names:
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        if row:
            basic_land_oids.add(row[0])

    # Pre-load type lines for token filtering
    type_lines = {}
    for row in conn.execute("SELECT oracle_id, type_line FROM cards"):
        type_lines[row[0]] = row[1] or ""

    # Build card pool
    all_card_oids = []
    for oid in oid_to_idx:
        if oid in basic_land_oids:
            continue
        tl = type_lines.get(oid, "")
        if "Token" in tl or "token" in tl:
            continue
        all_card_oids.append(oid)

    # Load positives
    print("\nLoading EDHREC membership data...")
    positives_by_cmdr = load_edhrec_membership(conn)

    # Filter to commanders with embeddings
    positives_by_cmdr = {
        k: v for k, v in positives_by_cmdr.items() if k in oid_to_idx
    }
    for cmdr_oid in list(positives_by_cmdr.keys()):
        positives_by_cmdr[cmdr_oid] = {
            oid for oid in positives_by_cmdr[cmdr_oid] if oid in oid_to_idx
        }
        if not positives_by_cmdr[cmdr_oid]:
            del positives_by_cmdr[cmdr_oid]

    total_pos = sum(len(v) for v in positives_by_cmdr.values())
    print(f"  Positive pairs with embeddings: {total_pos} across {len(positives_by_cmdr)} commanders")

    # Sample negatives
    print("\nSampling negatives (ratio=3)...")
    neg_pairs = sample_negatives(
        positives_by_cmdr, all_card_oids, card_colors, card_colors, ratio=3
    )
    print(f"  Negative pairs: {len(neg_pairs)}")

    # Combine into pairs_by_cmdr: dict[cmdr_oid -> list[(card_oid, label)]]
    pairs_by_cmdr = {}
    for cmdr_oid, card_oids in positives_by_cmdr.items():
        pairs_by_cmdr[cmdr_oid] = [(oid, 1) for oid in card_oids]
    for cmdr_oid, card_oid, label in neg_pairs:
        pairs_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, label))

    return pairs_by_cmdr


def main():
    parser = argparse.ArgumentParser(description="Train hybrid fusion model")
    parser.add_argument(
        "--tower-only",
        action="store_true",
        help="Train only Stage 1 (binary tower on EDHREC membership)",
    )
    parser.add_argument(
        "--features-only",
        action="store_true",
        help="Build and inspect the 10-feature matrix without training GBM",
    )
    parser.add_argument(
        "--feature-importance",
        action="store_true",
        help="Print feature importance from trained GBM model",
    )
    args = parser.parse_args()

    if args.feature_importance:
        import joblib
        model_path = os.path.join(DATA_DIR, "fusion_model.lgb")
        if not os.path.exists(model_path):
            print(f"ERROR: No saved model at {model_path}. Run full training first.")
            return
        model = joblib.load(model_path)
        print("Feature importance (split count):")
        for name, imp in sorted(
            zip(FEATURE_NAMES, model.feature_importances_), key=lambda x: -x[1]
        ):
            print(f"  {name:25s} {imp:6d}")
        return

    if args.features_only:
        print("=" * 60)
        print("Building 10-feature matrix for Stage 2 (LightGBM)")
        print("=" * 60)

        # Load embeddings for oid_to_idx
        print("\nLoading embeddings...")
        _, _, oid_to_idx = load_embeddings()

        conn = sqlite3.connect(DB_PATH)
        pairs_by_cmdr = _load_pairs_for_features(conn, oid_to_idx)
        conn.close()

        X, y, cmdr_ids = build_feature_matrix(pairs_by_cmdr)
        print(f"\nDone. Feature matrix shape: {X.shape}")
        print(f"Labels shape: {y.shape} (pos={int(y.sum())}, neg={int(len(y)-y.sum())})")
        print(f"Commander IDs shape: {cmdr_ids.shape} (unique={len(np.unique(cmdr_ids))})")
    elif args.tower_only:
        train_tower_binary()
    else:
        # Full pipeline: tower + feature matrix + GBM
        # Stage 1: Tower (skip if model already exists)
        if not os.path.exists(TOWER_EDHREC_PATH):
            train_tower_binary()
        else:
            print(f"Tower model already exists at {TOWER_EDHREC_PATH}, skipping Stage 1.")

        # Stage 2: Feature matrix + LightGBM
        print("\n" + "=" * 60)
        print("Stage 2: LightGBM on 10-feature matrix")
        print("=" * 60)

        print("\nLoading embeddings...")
        _, _, oid_to_idx = load_embeddings()

        conn = sqlite3.connect(DB_PATH)
        pairs_by_cmdr = _load_pairs_for_features(conn, oid_to_idx)
        conn.close()

        X, y, cmdr_ids = build_feature_matrix(pairs_by_cmdr)

        print(f"\nTraining LightGBM with leave-commander-out CV...")
        model, cv_scores = train_gbm(X, y, cmdr_ids)
        print(f"\nDone. Mean CV AUC: {cv_scores['mean_auc']:.4f}")


if __name__ == "__main__":
    main()
