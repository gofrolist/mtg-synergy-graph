#!/usr/bin/env python3
"""Train a two-tower synergy prediction model.

Takes commander embedding + card embedding → predicts synergy score 1-10.
Trained on LLM-generated scores, runs inference in <1ms per card.

Usage:
    python3 train_tower_model.py              # Train the model
    python3 train_tower_model.py --eval       # Evaluate existing model
    python3 train_tower_model.py --predict "Krenko, Mob Boss"  # Score all cards
    python3 train_tower_model.py --stats      # Show training data info
"""

import argparse
import json
import os
import sqlite3
import time

import numpy as np

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MODEL_PATH = os.path.join(DATA_DIR, "tower_model.npz")


def load_embeddings():
    """Load pre-computed card embeddings."""
    emb = np.load(os.path.join(DATA_DIR, "embeddings.npy"), mmap_mode='c')  # copy-on-write: avoids full read unless modified
    oid_list = json.load(open(os.path.join(DATA_DIR, "embeddings_index.json")))["oracle_ids"]
    oid_to_idx = {oid: i for i, oid in enumerate(oid_list)}
    # L2 normalize
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return emb / norms, oid_list, oid_to_idx


def load_structural_features():
    """Pre-load all data needed for structural features."""
    conn = sqlite3.connect(DB_PATH)

    provides = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM provides"):
        provides.setdefault(oid, set()).add(tag)
    wants = {}
    for oid, tag in conn.execute("SELECT oracle_id, tag FROM wants"):
        wants.setdefault(oid, set()).add(tag)
    strats = {}
    for oid, s in conn.execute("SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"):
        strats.setdefault(oid, set()).add(s)
    types = {}
    oracles = {}
    ranks = {}
    for row in conn.execute("SELECT oracle_id, type_line, oracle_text, edhrec_rank FROM cards"):
        types[row[0]] = row[1] or ""
        oracles[row[0]] = (row[2] or "").lower()
        ranks[row[0]] = row[3] or 50000

    # Mechanics scores (pre-computed)
    mech_scores = {}
    try:
        from mechanics_matcher import load_mechanics, compute_synergy
        all_mechs = load_mechanics(DB_PATH)
        mech_scores = {"_mechs": all_mechs, "_types": types}
    except Exception:
        pass

    # Load Spellbook combo partners for structural feature f[11]
    combo_partners = {}
    try:
        for row in conn.execute("SELECT card_oracle_ids FROM spellbook_combos").fetchall():
            oids = json.loads(row[0])
            for oid in oids:
                partners = set(oids) - {oid}
                combo_partners.setdefault(oid, set()).update(partners)
    except Exception:
        pass
    mech_scores["_combos"] = combo_partners

    conn.close()
    return provides, wants, strats, types, oracles, ranks, mech_scores


def compute_struct_features(cmdr_oid, card_oid, provides, wants, strats, types, oracles, ranks, mech_data):
    """Compute structural features for a commander-card pair."""
    f = np.zeros(12, dtype=np.float32)

    cp = provides.get(cmdr_oid, set())
    cw = wants.get(cmdr_oid, set())
    dp = provides.get(card_oid, set())
    dw = wants.get(card_oid, set())

    # Tag overlap
    f[0] = len(dp & cw)  # card provides → commander wants
    f[1] = len(dw & cp)  # card wants ← commander provides
    f[2] = min(f[0], f[1])  # bidirectional

    # Strategy overlap
    f[3] = len(strats.get(cmdr_oid, set()) & strats.get(card_oid, set()))

    # Type matching
    ct = types.get(cmdr_oid, "").lower()
    dt = types.get(card_oid, "").lower()
    cmdr_sub = set(ct.split(" — ")[1].split()) if " — " in ct else set()
    card_sub = set(dt.split(" — ")[1].split()) if " — " in dt else set()
    f[4] = len(cmdr_sub & card_sub)

    # Commander type in card oracle
    card_oracle = oracles.get(card_oid, "")
    f[5] = sum(1 for s in cmdr_sub if len(s) > 3 and s in card_oracle)

    # Card type in commander oracle
    cmdr_oracle = oracles.get(cmdr_oid, "")
    f[6] = sum(1 for s in card_sub if len(s) > 3 and s in cmdr_oracle)

    # EDHREC rank (card quality)
    f[7] = np.log10(max(ranks.get(card_oid, 50000), 1))

    # Mechanics score
    if "_mechs" in mech_data:
        all_mechs = mech_data["_mechs"]
        cm = all_mechs.get(cmdr_oid, [])
        dm = all_mechs.get(card_oid, [])
        if cm and dm:
            from mechanics_matcher import compute_synergy
            f[8] = compute_synergy(cm, dm, types.get(cmdr_oid, ""), types.get(card_oid, ""))

    # Card is creature
    f[9] = 1.0 if "creature" in dt else 0.0

    # Commander EDHREC rank
    f[10] = np.log10(max(ranks.get(cmdr_oid, 50000), 1))

    # Spellbook combo membership
    if "_combos" in mech_data:
        cmdr_combos = mech_data["_combos"].get(cmdr_oid, set())
        f[11] = 1.0 if card_oid in cmdr_combos else 0.0

    return f


# ── Model: Two-Tower with interaction MLP ───────────────────────────────────
# Architecture:
#   Commander embedding (768) → projection (768→128)
#   Card embedding (768) → projection (768→128)
#   Interaction: element-wise product (128) + structural features (12)
#   MLP: 140 → 64 → 32 → 1
#   Output: synergy score 1-10

def relu(x):
    return np.maximum(x, 0)

def init_model():
    """Initialize model weights."""
    np.random.seed(42)
    scale = lambda fan_in, fan_out: np.sqrt(2.0 / fan_in)
    return {
        # Projections
        "W_cmdr": np.random.randn(768, 128).astype(np.float32) * scale(768, 128),
        "b_cmdr": np.zeros(128, dtype=np.float32),
        "W_card": np.random.randn(768, 128).astype(np.float32) * scale(768, 128),
        "b_card": np.zeros(128, dtype=np.float32),
        # MLP head (128 interaction + 12 structural = 140)
        "W1": np.random.randn(140, 64).astype(np.float32) * scale(140, 64),
        "b1": np.zeros(64, dtype=np.float32),
        "W2": np.random.randn(64, 32).astype(np.float32) * scale(64, 32),
        "b2": np.zeros(32, dtype=np.float32),
        "W3": np.random.randn(32, 1).astype(np.float32) * scale(32, 1),
        "b3": np.float32(5.0),  # Initialize to mean score
    }


def forward(model, cmdr_emb, card_emb, struct_feat):
    """Forward pass. All inputs are batched (N, dim)."""
    # Project
    cmdr_proj = relu(cmdr_emb @ model["W_cmdr"] + model["b_cmdr"])  # (N, 128)
    card_proj = relu(card_emb @ model["W_card"] + model["b_card"])   # (N, 128)

    # Interaction: element-wise product captures "how features interact"
    interaction = cmdr_proj * card_proj  # (N, 128)

    # Concatenate with structural features
    combined = np.concatenate([interaction, struct_feat], axis=1)  # (N, 140)

    # MLP
    h1 = relu(combined @ model["W1"] + model["b1"])  # (N, 64)
    h2 = relu(h1 @ model["W2"] + model["b2"])        # (N, 32)
    out = (h2 @ model["W3"]).squeeze(-1) + model["b3"]  # (N,)

    return out, {"cmdr_proj": cmdr_proj, "card_proj": card_proj,
                 "interaction": interaction, "combined": combined,
                 "h1": h1, "h2": h2}


def train():
    """Train the two-tower model."""
    print("Loading data...")
    normed_emb, oid_list, oid_to_idx = load_embeddings()
    provides, wants, strats, types, oracles, ranks, mech_data = load_structural_features()

    conn = sqlite3.connect(DB_PATH)
    # Train only on LLM-generated scores, not Spellbook boosts or manual fixes.
    # Those corrections are applied at runtime, not during training.
    pairs = conn.execute(
        "SELECT commander_oid, card_oid, score FROM synergy_scores "
        "WHERE model NOT LIKE 'auto%' AND model NOT LIKE 'spellbook%' AND model NOT LIKE 'manual%'"
    ).fetchall()

    # Normalize scores by provider to fix gemma3 scoring bias
    provider_means = {}
    for row in conn.execute(
        "SELECT model, AVG(score) FROM synergy_scores "
        "WHERE model NOT LIKE 'auto%' AND model NOT LIKE 'spellbook%' AND model NOT LIKE 'manual%' "
        "GROUP BY model"
    ).fetchall():
        provider_means[row[0]] = row[1]

    pair_providers = {}
    for row in conn.execute(
        "SELECT commander_oid, card_oid, model FROM synergy_scores "
        "WHERE model NOT LIKE 'auto%' AND model NOT LIKE 'spellbook%' AND model NOT LIKE 'manual%'"
    ).fetchall():
        pair_providers[(row[0], row[1])] = row[2]

    target_mean = provider_means.get("gpt-5.4-mini", 4.5)
    normalized_pairs = []
    for cmdr_oid, card_oid, score in pairs:
        provider = pair_providers.get((cmdr_oid, card_oid), "")
        provider_mean = provider_means.get(provider, target_mean)
        adjusted = score - (provider_mean - target_mean)
        adjusted = max(1.0, min(10.0, adjusted))
        normalized_pairs.append((cmdr_oid, card_oid, adjusted))
    pairs = normalized_pairs
    conn.close()

    print(f"Provider normalization: target_mean={target_mean:.2f}")
    print(f"Training pairs: {len(pairs)}")

    # Build arrays
    cmdr_embs = []
    card_embs = []
    struct_feats = []
    labels = []

    for cmdr_oid, card_oid, score in pairs:
        ci = oid_to_idx.get(cmdr_oid)
        di = oid_to_idx.get(card_oid)
        if ci is None or di is None:
            continue
        cmdr_embs.append(normed_emb[ci])
        card_embs.append(normed_emb[di])
        struct_feats.append(compute_struct_features(
            cmdr_oid, card_oid, provides, wants, strats, types, oracles, ranks, mech_data))
        labels.append(score)

    X_cmdr = np.array(cmdr_embs, dtype=np.float32)
    X_card = np.array(card_embs, dtype=np.float32)
    X_struct = np.array(struct_feats, dtype=np.float32)
    y = np.array(labels, dtype=np.float32)

    # Normalize structural features
    struct_means = X_struct.mean(axis=0)
    struct_stds = X_struct.std(axis=0)
    struct_stds[struct_stds == 0] = 1
    X_struct = (X_struct - struct_means) / struct_stds

    print(f"Data: {len(y)} pairs, cmdr={X_cmdr.shape}, card={X_card.shape}, struct={X_struct.shape}")

    # Split
    np.random.seed(42)
    idx = np.random.permutation(len(y))
    split = int(0.8 * len(y))
    train_idx, test_idx = idx[:split], idx[split:]

    # Initialize
    model = init_model()

    # Adam optimizer state
    adam_m = {k: np.zeros_like(v) for k, v in model.items()}
    adam_v = {k: np.zeros_like(v) for k, v in model.items()}

    lr = 0.001
    batch_size = 512
    epochs = 100
    best_corr = 0
    best_model = None

    print(f"Training for {epochs} epochs...")
    t0 = time.time()

    for epoch in range(epochs):
        # Shuffle training data
        perm = np.random.permutation(len(train_idx))
        epoch_loss = 0
        n_batches = 0

        for i in range(0, len(train_idx), batch_size):
            batch = train_idx[perm[i:i + batch_size]]

            # Forward
            pred, cache = forward(model, X_cmdr[batch], X_card[batch], X_struct[batch])
            pred_clipped = np.clip(pred, 1, 10)

            # Focal-style weighting: upweight rare high/low synergy scores
            sample_weights = np.ones(len(batch), dtype=np.float32)
            batch_labels = y[batch]
            sample_weights[batch_labels >= 8] = 3.0
            sample_weights[batch_labels >= 9] = 5.0
            sample_weights[batch_labels <= 2] = 2.0

            error = pred_clipped - batch_labels
            loss = np.mean(sample_weights * error ** 2)
            epoch_loss += loss
            n_batches += 1

            # Backward
            N = len(batch)
            grad_out = 2 * sample_weights * error / N
            mask = (pred >= 1) & (pred <= 10)
            grad_out = grad_out * mask.astype(np.float32)

            # W3, b3
            dW3 = cache["h2"].T @ grad_out[:, None]
            db3 = np.float32(grad_out.sum())

            # h2 → W2, b2
            dh2 = grad_out[:, None] * model["W3"].T
            dz2 = dh2 * (cache["h2"] > 0).astype(np.float32)
            dW2 = cache["h1"].T @ dz2
            db2 = dz2.sum(axis=0)

            # h1 → W1, b1
            dh1 = dz2 @ model["W2"].T
            dz1 = dh1 * (cache["h1"] > 0).astype(np.float32)
            dW1 = cache["combined"].T @ dz1
            db1 = dz1.sum(axis=0)

            # combined → interaction + struct (struct has no learnable params)
            d_combined = dz1 @ model["W1"].T
            d_interaction = d_combined[:, :128]

            # interaction = cmdr_proj * card_proj → element-wise product backward
            d_cmdr_proj = d_interaction * cache["card_proj"]
            d_card_proj = d_interaction * cache["cmdr_proj"]

            # Apply ReLU gradient
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
            test_pred, _ = forward(model, X_cmdr[test_idx], X_card[test_idx], X_struct[test_idx])
            test_pred = np.clip(test_pred, 1, 10)
            test_corr = np.corrcoef(y[test_idx], test_pred)[0, 1]
            test_mae = np.mean(np.abs(y[test_idx] - test_pred))
            high = y[test_idx] >= 7
            low = y[test_idx] <= 4
            sep = test_pred[high].mean() - test_pred[low].mean() if high.sum() > 0 and low.sum() > 0 else 0

            if test_corr > best_corr:
                best_corr = test_corr
                best_model = {k: v.copy() for k, v in model.items()}

            print(f"  Epoch {epoch+1:>3}: loss={epoch_loss/n_batches:.3f} "
                  f"corr={test_corr:.3f} MAE={test_mae:.2f} sep={sep:.1f}")

    elapsed = time.time() - t0
    print(f"\nTraining: {elapsed:.1f}s, best corr={best_corr:.3f}")

    # Save best model
    if best_model:
        np.savez(MODEL_PATH,
                 **best_model,
                 struct_means=struct_means,
                 struct_stds=struct_stds)
        print(f"Model saved to {MODEL_PATH}")

    # Final evaluation on best model
    model = best_model or model
    test_pred, _ = forward(model, X_cmdr[test_idx], X_card[test_idx], X_struct[test_idx])
    test_pred = np.clip(test_pred, 1, 10)
    test_corr = np.corrcoef(y[test_idx], test_pred)[0, 1]
    test_mae = np.mean(np.abs(y[test_idx] - test_pred))
    print(f"\nFinal: corr={test_corr:.3f} MAE={test_mae:.2f}")

    return model


def predict_commander(commander_name: str):
    """Score all cards for a commander using the trained model."""
    if not os.path.exists(MODEL_PATH):
        print("No trained model. Run training first.")
        return

    print("Loading model...")
    data = np.load(MODEL_PATH)
    model = {k: data[k] for k in data.files if k not in ("struct_means", "struct_stds")}
    struct_means = data["struct_means"]
    struct_stds = data["struct_stds"]

    normed_emb, oid_list, oid_to_idx = load_embeddings()
    provides, wants, strats, types, oracles, ranks, mech_data = load_structural_features()

    conn = sqlite3.connect(DB_PATH)
    cmdr = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (commander_name,)).fetchone()
    if not cmdr:
        print(f"Commander not found: {commander_name}")
        return
    cmdr_oid = cmdr[0]
    cmdr_idx = oid_to_idx.get(cmdr_oid)
    if cmdr_idx is None:
        print("Commander has no embedding")
        return

    cmdr_emb = normed_emb[cmdr_idx]

    # Score ALL cards
    t0 = time.time()
    all_cards = conn.execute(
        "SELECT oracle_id, name, color_identity FROM cards WHERE legal_commander = 1"
    ).fetchall()

    # Get commander color identity
    ci_row = conn.execute("SELECT color_identity FROM cards WHERE oracle_id = ?", (cmdr_oid,)).fetchone()
    color_identity = set(json.loads(ci_row[0] or "[]")) if ci_row else set()

    # Batch all cards
    card_embs = []
    struct_feats = []
    card_names = []
    card_oids = []

    for oid, name, card_ci_str in all_cards:
        card_ci = set(json.loads(card_ci_str or "[]"))
        if not card_ci.issubset(color_identity):
            continue
        if oid == cmdr_oid:
            continue
        card_idx = oid_to_idx.get(oid)
        if card_idx is None:
            continue

        card_embs.append(normed_emb[card_idx])
        sf = compute_struct_features(cmdr_oid, oid, provides, wants, strats, types, oracles, ranks, mech_data)
        struct_feats.append((sf - struct_means) / struct_stds)
        card_names.append(name)
        card_oids.append(oid)

    X_card = np.array(card_embs, dtype=np.float32)
    X_struct = np.array(struct_feats, dtype=np.float32)
    X_cmdr = np.tile(cmdr_emb, (len(X_card), 1))

    # Forward pass — ALL cards at once
    scores, _ = forward(model, X_cmdr, X_card, X_struct)
    scores = np.clip(scores, 1, 10)

    elapsed = time.time() - t0
    print(f"Scored {len(scores)} cards in {elapsed:.2f}s ({elapsed/len(scores)*1e6:.0f}µs/card)")

    # Top 30
    top_idx = np.argsort(scores)[::-1][:30]
    print(f"\nTop 30 for {commander_name}:")
    for i in top_idx:
        edh = conn.execute("SELECT synergy FROM edhrec_card_synergy WHERE commander_slug = ? AND card_name = ?",
                           (commander_name.lower().replace(" ", "-").replace(",", "").replace("'", ""),
                            card_names[i])).fetchone()
        edh_str = f"EDHREC={edh[0]:.2f}" if edh else ""
        print(f"  {scores[i]:.1f}/10  {card_names[i]:<40} {edh_str}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Train two-tower synergy model")
    parser.add_argument("--predict", help="Score all cards for a commander")
    parser.add_argument("--eval", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.stats:
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM synergy_scores WHERE model NOT LIKE 'auto%'").fetchone()[0]
        cmdrs = conn.execute("SELECT COUNT(DISTINCT commander_oid) FROM synergy_scores WHERE model NOT LIKE 'auto%'").fetchone()[0]
        print(f"Training data: {total} pairs from {cmdrs} commanders")
        print(f"Model exists: {os.path.exists(MODEL_PATH)}")
        conn.close()
    elif args.predict:
        predict_commander(args.predict)
    else:
        train()


if __name__ == "__main__":
    main()
