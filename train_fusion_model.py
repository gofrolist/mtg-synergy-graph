#!/usr/bin/env python3
"""Train a hybrid fusion model for EDHREC deck membership prediction.

Stage 1: Retrain two-tower neural net on EDHREC avg deck membership (binary).
Stage 2 (future): LightGBM on tower probability + structural features.

Usage:
    python3 train_fusion_model.py --tower-only    # Stage 1: binary tower
    python3 train_fusion_model.py                  # Full pipeline (tower + GBM)
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

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TOWER_EDHREC_PATH = os.path.join(DATA_DIR, "tower_model_edhrec.npz")


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


def main():
    parser = argparse.ArgumentParser(description="Train hybrid fusion model")
    parser.add_argument(
        "--tower-only",
        action="store_true",
        help="Train only Stage 1 (binary tower on EDHREC membership)",
    )
    args = parser.parse_args()

    if args.tower_only:
        train_tower_binary()
    else:
        # Full pipeline: tower + feature matrix + GBM
        # For now, only tower is implemented
        print("Full fusion pipeline not yet implemented. Use --tower-only for Stage 1.")
        train_tower_binary()


if __name__ == "__main__":
    main()
