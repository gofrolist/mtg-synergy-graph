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

from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext,
    CmdrFeatureContext,
    compute_card_features,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TOWER_EDHREC_PATH = os.path.join(DATA_DIR, "tower_model_edhrec.npz")

FEATURE_NAMES = [
    "tower_prob", "causal_score", "forge_deck_overlap",
    "tribal_match", "edhrec_synergy", "edhrec_rank", "cmc", "is_creature",
]

FORGE_FEATURE_NAMES = [
    "tower_forge",           # [0] forge tower (causal graph connectivity)
    "embedding_cosine",      # [1] card2vec embedding cosine(cmdr, card)
    "causal_cmdr_to_card",   # [2] commander → card edge strength
    "causal_card_to_cmdr",   # [3] card → commander edge strength
    "causal_bidirectional",  # [4] 1.0 if both directions have edges
    "causal_event_diversity", # [5] distinct event types connecting cmdr↔card
    "deck_edge_count",       # [6] deck cards with causal edges to this card
    "strategy_overlap",      # [7] shared strategies count
    "strategy_cosine",       # [8] strategy vector cosine similarity
    "oracle_similarity",     # [9] oracle text TF-IDF cosine similarity
    "phase_match",           # [10] cmdr and card trigger in same phase window
    "has_phase_trigger",     # [11] card has any phase-based trigger
    "tribal_match",          # [12] creature type match
    "type_creature",         # [13] card is a Creature
    "type_instant_sorcery",  # [14] card is Instant or Sorcery
    "type_artifact",         # [15] card is an Artifact
    "type_enchantment",      # [16] card is an Enchantment
    "type_land",             # [17] card is a Land
    "type_planeswalker",     # [18] card is a Planeswalker
    "cmc",                   # [19] mana cost
    "deck_exact_edge_ratio", # [20] fraction of deck edges with exact filter precision
    "cmdr_exact_edge",       # [21] 1.0 if any exact-precision edge to commander
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
    # Map card_name -> oracle_id via cards table (prefer non-token versions)
    name_to_oid = {}
    for row in conn.execute(
        "SELECT name, oracle_id, type_line FROM cards ORDER BY "
        "CASE WHEN type_line LIKE '%Token%' THEN 1 ELSE 0 END"
    ):
        if row[0] not in name_to_oid:
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

    # Filter out generic staples: cards in >30% of all EDHREC decks
    # These are auto-includes (Sol Ring, Arcane Signet, etc.) with no
    # commander-specific synergy — noise that teaches "popular = good"
    n_cmdrs_total = len(slug_to_oid)
    staple_threshold = n_cmdrs_total * 0.3
    card_deck_count = {}
    for row in conn.execute(
        "SELECT card_name, COUNT(DISTINCT commander_slug) FROM edhrec_average_decks GROUP BY card_name"
    ):
        card_deck_count[row[0]] = row[1]

    staple_oids = set()
    for card_name, count in card_deck_count.items():
        if count > staple_threshold:
            oid = name_to_oid.get(card_name)
            if oid:
                staple_oids.add(oid)

    n_before = sum(len(v) for v in positives.values())
    for cmdr_oid in positives:
        positives[cmdr_oid] -= staple_oids
    positives = {k: v for k, v in positives.items() if v}
    n_after = sum(len(v) for v in positives.values())

    total_pos = n_after
    print(f"  Positive pairs: {total_pos} across {len(positives)} commanders")
    print(f"  Filtered {n_before - n_after} staple pairs ({len(staple_oids)} cards in >30% of decks)")
    if unmatched_cards > 0:
        print(f"  Unmatched card names: {unmatched_cards}")

    return positives


def sample_negatives(positives_by_cmdr, all_card_oids, card_colors, cmdr_colors,
                     ratio=3, hard_ratio=0.5, card_strats=None, card_subtypes=None,
                     normed_emb=None, emb_oid_to_idx=None):
    """Sample negative pairs (cards NOT in a commander's avg deck).

    For each commander, samples ratio * |positives| negative cards:
    - hard_ratio fraction are "hard" negatives, split between:
      - strategy/subtype overlap (cards that look similar by mechanics)
      - embedding-ranked (highest cosine similarity to commander)
    - remainder are random color-legal cards

    Returns list of (cmdr_oid, card_oid, 0) tuples.
    """
    rng = np.random.RandomState(42)

    negatives = []

    for cmdr_oid, pos_cards in positives_by_cmdr.items():
        cmdr_ci = cmdr_colors.get(cmdr_oid, set())

        # Candidate pool: color-legal, not in deck
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

        n_hard = int(n_neg * hard_ratio) if hard_ratio > 0 else 0
        hard_chosen = set()

        if n_hard > 0:
            # Half of hard budget: strategy/subtype overlap
            n_strat_hard = n_hard // 2
            if card_strats or card_subtypes:
                cmdr_strats = card_strats.get(cmdr_oid, set()) if card_strats else set()
                cmdr_subs = card_subtypes.get(cmdr_oid, set()) if card_subtypes else set()

                hard_pool = []
                for oid in candidates:
                    shared_strat = bool(cmdr_strats & card_strats.get(oid, set())) if card_strats else False
                    shared_sub = bool(cmdr_subs & card_subtypes.get(oid, set())) if card_subtypes else False
                    if shared_strat or shared_sub:
                        hard_pool.append(oid)

                if hard_pool:
                    n_pick = min(n_strat_hard, len(hard_pool))
                    chosen_idx = rng.choice(len(hard_pool), size=n_pick, replace=False)
                    for idx in chosen_idx:
                        hard_chosen.add(hard_pool[idx])

            # Other half: embedding-ranked (highest cosine to commander)
            n_emb_hard = n_hard - len(hard_chosen)
            if n_emb_hard > 0 and normed_emb is not None and emb_oid_to_idx is not None:
                cmdr_idx = emb_oid_to_idx.get(cmdr_oid)
                if cmdr_idx is not None:
                    # Score remaining candidates by embedding similarity
                    emb_pool = [(oid, emb_oid_to_idx[oid]) for oid in candidates
                                if oid not in hard_chosen and oid in emb_oid_to_idx]
                    if emb_pool:
                        pool_oids = [o for o, _ in emb_pool]
                        pool_indices = [i for _, i in emb_pool]
                        cmdr_vec = normed_emb[cmdr_idx].astype(np.float32)
                        scores = normed_emb[pool_indices].astype(np.float32) @ cmdr_vec
                        # Take top-N most similar
                        n_pick = min(n_emb_hard, len(pool_oids))
                        top_idx = np.argpartition(-scores, n_pick)[:n_pick]
                        for idx in top_idx:
                            hard_chosen.add(pool_oids[idx])

            for oid in hard_chosen:
                negatives.append((cmdr_oid, oid, 0))

        # Fill remainder with random
        n_random = n_neg - len(hard_chosen)
        if n_random > 0:
            random_pool = [oid for oid in candidates if oid not in hard_chosen]
            if random_pool:
                n_random = min(n_random, len(random_pool))
                chosen_idx = rng.choice(len(random_pool), size=n_random, replace=False)
                for idx in chosen_idx:
                    negatives.append((cmdr_oid, random_pool[idx], 0))

    return negatives


def build_feature_matrix(pairs_by_cmdr, tower_model_path=TOWER_EDHREC_PATH, verbose=True):
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

    # 2. (removed — provides/wants tags no longer used, F3 zeroed out)

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

    # 5. Tower model data (load once)
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
    X = np.zeros((N, len(FEATURE_NAMES)), dtype=np.float32)
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
            if verbose:
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

        # --- (d) F3 cmdr_tag_overlap: removed (causal graph covers this) ---
        # --- (e) F4 strategy_keyword: removed (Forge tags cover this) ---

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

            # F3: tribal_match
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
            X[row_idx, 3] = tribal

            # F4: edhrec_synergy
            edh_syn = 0.0
            if cmdr_slug and card_name:
                edh_syn = edhrec_syn_map.get((cmdr_slug, card_name), 0.0)
            X[row_idx, 4] = edh_syn

            # F5: edhrec_rank (log10)
            X[row_idx, 5] = math.log10(max(meta.get("edhrec_rank", 50000), 1))

            # F6: cmc
            X[row_idx, 6] = float(meta.get("cmc", 0.0))

            # F7: is_creature
            X[row_idx, 7] = 1.0 if "Creature" in card_type_line else 0.0

            y[row_idx] = float(labels[j])
            cmdr_ids[row_idx] = cmdr_to_idx[cmdr_oid]
            row_idx += 1

    # Trim arrays to actual rows filled (in case any were skipped)
    X = X[:row_idx]
    y = y[:row_idx]
    cmdr_ids = cmdr_ids[:row_idx]

    conn.close()

    # Print summary statistics
    if verbose:
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


def build_forge_feature_matrix(pairs_by_cmdr, tower_model_path=None, verbose=True):
    """Build forge-only feature matrix (no EDHREC features).

    Uses forge tower (trained on causal graph) if available,
    falls back to EDHREC tower otherwise.

    Delegates per-card feature computation to the shared forge_features module
    (ForgeFeatureContext / CmdrFeatureContext / compute_card_features).
    """
    # Use forge tower (trained on causal graph connectivity)
    if tower_model_path is None:
        if os.path.exists(TOWER_FORGE_PATH):
            tower_model_path = TOWER_FORGE_PATH
        else:
            tower_model_path = TOWER_EDHREC_PATH  # fallback

    conn = sqlite3.connect(DB_PATH)

    # ── Card metadata (needed for type_line, cmc lookups) ─────────────
    card_meta = {}
    for row in conn.execute(
        "SELECT oracle_id, name, type_line, cmc FROM cards "
        "ORDER BY CASE WHEN type_line LIKE '%Token%' THEN 1 ELSE 0 END"
    ):
        oid, name, type_line, cmc = row
        card_meta[oid] = {
            "name": name,
            "type_line": type_line or "",
            "cmc": cmc or 0.0,
        }

    # ── Shared forge feature context (strategies, TF-IDF, phases) ─────
    normed_emb, oid_list, oid_to_idx = load_embeddings()
    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)

    if verbose:
        print(f"  Strategy vector: {ctx._n_strats} strategies")
        print(f"  Oracle TF-IDF: {ctx._n_vocab} vocab, {len(ctx._card_tokens)} cards with text")

    # ── Tower model (kept here for batch inference) ───────────────────
    sf_data = load_structural_features()
    provides_sf, wants_sf, strats_sf, types_sf, oracles_sf, ranks_sf, mech_sf = sf_data

    tower_model = None
    tower_means = None
    tower_stds = None
    if os.path.exists(tower_model_path):
        td = np.load(tower_model_path)
        tower_model = {k: td[k] for k in td.files if k not in ("struct_means", "struct_stds")}
        tower_means = td["struct_means"]
        tower_stds = td["struct_stds"]
        if verbose:
            print(f"  Tower loaded from {tower_model_path}")

    # ── Build pairs list ───────────────────────────────────────────────
    cmdr_oids_ordered = sorted(pairs_by_cmdr.keys())
    cmdr_to_idx = {oid: i for i, oid in enumerate(cmdr_oids_ordered)}

    all_rows = []
    for cmdr_oid, pairs in pairs_by_cmdr.items():
        for card_oid, label in pairs:
            all_rows.append((cmdr_oid, card_oid, label))

    N = len(all_rows)
    X = np.zeros((N, len(FORGE_FEATURE_NAMES)), dtype=np.float32)
    y = np.zeros(N, dtype=np.float32)
    cmdr_ids = np.zeros(N, dtype=np.int32)

    # Re-organise for commander-batch processing
    cmdr_pair_map = {}
    for cmdr_oid, card_oid, label in all_rows:
        cmdr_pair_map.setdefault(cmdr_oid, []).append((card_oid, label))

    n_cmdrs = len(cmdr_oids_ordered)
    row_idx = 0

    for ci, cmdr_oid in enumerate(cmdr_oids_ordered):
        if (ci + 1) % 50 == 0 or ci == 0:
            if verbose:
                print(f"  Building forge features for commander {ci+1}/{n_cmdrs}...", flush=True)

        pairs = cmdr_pair_map.get(cmdr_oid, [])
        if not pairs:
            continue

        card_oids = [p[0] for p in pairs]
        labels = [p[1] for p in pairs]
        n_pairs = len(pairs)

        # --- (a) Tower batch inference ---
        tower_probs = np.full(n_pairs, 0.5, dtype=np.float32)
        if tower_model is not None:
            cmdr_idx_emb = oid_to_idx.get(cmdr_oid)
            if cmdr_idx_emb is not None:
                batch_card_indices = []
                batch_positions = []
                for j, card_oid in enumerate(card_oids):
                    cidx = oid_to_idx.get(card_oid)
                    if cidx is not None:
                        batch_card_indices.append(cidx)
                        batch_positions.append(j)
                if batch_card_indices:
                    B = len(batch_card_indices)
                    X_cmdr_batch = np.tile(normed_emb[cmdr_idx_emb], (B, 1)).astype(np.float32)
                    X_card_batch = normed_emb[batch_card_indices].astype(np.float32)
                    sf_batch = np.zeros((B, 12), dtype=np.float32)
                    for k, pos in enumerate(batch_positions):
                        sf_batch[k] = compute_struct_features(
                            cmdr_oid, card_oids[pos],
                            provides_sf, wants_sf, strats_sf, types_sf, oracles_sf, ranks_sf, mech_sf)
                    sf_norm = (sf_batch - tower_means) / tower_stds
                    raw, _ = forward(tower_model, X_cmdr_batch, X_card_batch, sf_norm)
                    probs = sigmoid(raw)
                    for k, pos in enumerate(batch_positions):
                        tower_probs[pos] = probs[k]

        # --- (b) Per-commander context (causal edges, deck edges, precision) ---
        deck_oids_for_cmdr = {oid for oid, lbl in pairs if lbl == 1}
        cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids_for_cmdr)

        # --- (c) Commander subtypes for tribal ---
        cmdr_type_line = card_meta.get(cmdr_oid, {}).get("type_line", "")
        if "\u2014" in cmdr_type_line:
            try:
                cmdr_ctx.cmdr_subtypes = {
                    s.lower() for s in cmdr_type_line.split("\u2014")[1].strip().split()
                }
            except (IndexError, AttributeError):
                pass

        # --- Fill feature rows via shared compute_card_features ---
        for j in range(n_pairs):
            card_oid = card_oids[j]
            meta = card_meta.get(card_oid, {})
            card_type_line = meta.get("type_line", "")
            card_cmc = float(meta.get("cmc", 0.0))

            feats = compute_card_features(
                card_oid, card_type_line, card_cmc,
                float(tower_probs[j]), ctx, cmdr_ctx,
            )
            X[row_idx] = feats

            y[row_idx] = float(labels[j])
            cmdr_ids[row_idx] = cmdr_to_idx[cmdr_oid]
            row_idx += 1

    X = X[:row_idx]
    y = y[:row_idx]
    cmdr_ids = cmdr_ids[:row_idx]
    conn.close()

    if verbose:
        print(f"\nForge feature matrix: {X.shape}")
        print(f"  Positive labels: {int(y.sum())}")
        print(f"  Negative labels: {int(len(y) - y.sum())}")
        print(f"  Commanders: {len(cmdr_oids_ordered)}")
        print(f"\nPer-feature statistics:")
        for i, name in enumerate(FORGE_FEATURE_NAMES):
            col = X[:, i]
            print(f"  {name:>25s}: "
                  f"mean={col.mean():.4f}  std={col.std():.4f}  "
                  f"min={col.min():.4f}  max={col.max():.4f}  "
                  f"nonzero={np.count_nonzero(col)}/{len(col)}")

    return X, y, cmdr_ids


def train_forge_gbm(X, y, cmdr_ids):
    """Train LightGBM on forge-only features with leave-commander-out CV.

    Saves to fusion_model_forge.lgb. Returns (model, cv_scores).
    """
    import lightgbm as lgb
    import joblib
    from sklearn.metrics import roc_auc_score as sklearn_auc

    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 127,
        "learning_rate": 0.02,
        "n_estimators": 1000,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_samples": 30,
        "verbose": -1,
    }

    splits = make_cv_splits(cmdr_ids, n_folds=5)
    fold_aucs = []
    for fold_i, (train_idx, test_idx) in enumerate(splits):
        model = lgb.LGBMClassifier(**params)
        model.fit(
            X[train_idx], y[train_idx],
            eval_set=[(X[test_idx], y[test_idx])],
            feature_name=FORGE_FEATURE_NAMES,
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        proba = model.predict_proba(X[test_idx])[:, 1]
        auc = sklearn_auc(y[test_idx], proba)
        fold_aucs.append(auc)
        print(f"  Fold {fold_i+1}: AUC={auc:.4f}")

    print(f"  Mean AUC: {np.mean(fold_aucs):.4f}")

    # Train final model on all data
    final_model = lgb.LGBMClassifier(**params)
    final_model.fit(X, y, feature_name=FORGE_FEATURE_NAMES)
    model_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    joblib.dump(final_model, model_path)
    print(f"  Forge model saved to {model_path}")

    return final_model, {"mean_auc": float(np.mean(fold_aucs)), "fold_aucs": fold_aucs}


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


TOWER_FORGE_PATH = os.path.join(DATA_DIR, "tower_model_forge.npz")


def load_forge_positives(conn, oid_to_idx, min_strength=0.3, max_per_cmdr=100):
    """Load positive pairs from causal graph edges (forge-native).

    Positive = card has a strong causal edge to/from a legendary creature.
    Caps at max_per_cmdr cards per commander (keeps strongest edges).
    Returns dict[cmdr_oid → set[card_oids]].
    """
    commanders = set()
    for row in conn.execute(
        "SELECT oracle_id FROM cards WHERE type_line LIKE '%Legendary%Creature%'"
    ):
        if row[0] in oid_to_idx:
            commanders.add(row[0])

    # Collect with strength for ranking
    raw = {}  # cmdr_oid → {card_oid: max_strength}
    for row in conn.execute(
        "SELECT source_id, target_id, strength FROM interaction_edges WHERE strength >= ?",
        (min_strength,)
    ):
        src, tgt, strength = row[0], row[1], row[2]
        if src in commanders and tgt in oid_to_idx and tgt != src:
            raw.setdefault(src, {})
            raw[src][tgt] = max(raw[src].get(tgt, 0), strength)
        if tgt in commanders and src in oid_to_idx and src != tgt:
            raw.setdefault(tgt, {})
            raw[tgt][src] = max(raw[tgt].get(src, 0), strength)

    # Keep top N strongest per commander
    positives = {}
    for cmdr, cards in raw.items():
        if len(cards) < 5:
            continue
        top = sorted(cards.items(), key=lambda x: -x[1])[:max_per_cmdr]
        positives[cmdr] = {oid for oid, _ in top}

    total = sum(len(v) for v in positives.values())
    print(f"  Forge positives: {total:,} pairs across {len(positives)} commanders "
          f"(strength >= {min_strength}, max {max_per_cmdr}/cmdr)")
    return positives


def train_tower_forge():
    """Train two-tower model on causal graph connectivity (forge-native).

    Same architecture as train_tower_binary() but uses forge causal edges
    instead of EDHREC deck membership as training signal.
    """
    print("=" * 60)
    print("FORGE TOWER: Training on causal graph connectivity")
    print("=" * 60)

    print("\nLoading embeddings...")
    normed_emb, oid_list, oid_to_idx = load_embeddings()

    print("Loading structural features...")
    provides, wants, strats, types, oracles, ranks, mech_data = load_structural_features()

    conn = sqlite3.connect(DB_PATH)

    card_colors = {}
    for row in conn.execute("SELECT oracle_id, color_identity FROM cards"):
        card_colors[row[0]] = set(json.loads(row[1] or "[]"))

    basic_land_names = {"Plains", "Island", "Swamp", "Mountain", "Forest",
                        "Snow-Covered Plains", "Snow-Covered Island",
                        "Snow-Covered Swamp", "Snow-Covered Mountain",
                        "Snow-Covered Forest", "Wastes"}
    basic_land_oids = set()
    for name in basic_land_names:
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        if row:
            basic_land_oids.add(row[0])

    all_card_oids = [oid for oid in oid_list
                     if oid not in basic_land_oids
                     and "Token" not in types.get(oid, "")]

    print(f"\nCard pool: {len(all_card_oids)} cards")

    # Load forge positives (causal graph edges)
    print("\nLoading causal graph positives...")
    positives_by_cmdr = load_forge_positives(conn, oid_to_idx)

    # Sample negatives (color-legal cards with no edge)
    print("\nSampling negatives (ratio=3)...")
    neg_pairs = sample_negatives(
        positives_by_cmdr, all_card_oids, card_colors, card_colors, ratio=3
    )
    print(f"  Negative pairs: {len(neg_pairs)}")

    # Build training data
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

    struct_means = X_struct.mean(axis=0)
    struct_stds = X_struct.std(axis=0)
    struct_stds[struct_stds == 0] = 1
    X_struct = (X_struct - struct_means) / struct_stds

    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    print(f"\nData: {len(y)} pairs ({n_pos} positive, {n_neg} negative)")
    print(f"  cmdr={X_cmdr.shape}, card={X_card.shape}, struct={X_struct.shape}")

    # Train/test split (stratified)
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

    model = init_model()
    model["b4"] = np.float32(0.0)

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
    best_auc_epoch = 0
    eps = 1e-7

    print(f"\nTraining for {epochs} epochs (batch={batch_size}, lr={lr})...")
    t0 = time.time()

    for epoch in range(epochs):
        perm = np.random.permutation(len(train_idx))
        epoch_loss = 0
        n_batches = 0

        for i in range(0, len(train_idx), batch_size):
            batch = train_idx[perm[i:i + batch_size]]

            raw, cache = forward(model, X_cmdr[batch], X_card[batch], X_struct[batch],
                                 dropout_rate=dropout_rate, training=True)
            pred = sigmoid(raw)
            N = len(batch)

            loss = -np.mean(y[batch] * np.log(pred + eps) + (1 - y[batch]) * np.log(1 - pred + eps))
            epoch_loss += loss
            n_batches += 1

            grad_out = (pred - y[batch]) / N

            # Backward pass (inline chain rule)
            dW4 = cache["h3"].T @ grad_out[:, None]
            db4 = np.float32(grad_out.sum())
            dh3 = grad_out[:, None] * model["W4"].T
            dz3 = dh3 * (cache["h3"] > 0).astype(np.float32)
            dW3 = cache["h2"].T @ dz3
            db3 = dz3.sum(axis=0)
            dh2 = dz3 @ model["W3"].T
            if cache["mask2"] is not None:
                dh2 = dh2 * cache["mask2"]
            dz2 = dh2 * (cache["h2"] > 0).astype(np.float32)
            dW2 = cache["h1"].T @ dz2
            db2 = dz2.sum(axis=0)
            dh1 = dz2 @ model["W2"].T
            if cache["mask1"] is not None:
                dh1 = dh1 * cache["mask1"]
            dz1 = dh1 * (cache["h1"] > 0).astype(np.float32)
            dW1 = cache["combined"].T @ dz1
            db1 = dz1.sum(axis=0)
            d_combined = dz1 @ model["W1"].T
            d_interaction = d_combined[:, :128]
            d_cmdr_proj = d_interaction * cache["card_proj"]
            d_card_proj = d_interaction * cache["cmdr_proj"]
            d_cmdr_proj = d_cmdr_proj * (cache["cmdr_proj"] > 0).astype(np.float32)
            d_card_proj = d_card_proj * (cache["card_proj"] > 0).astype(np.float32)
            dW_cmdr = X_cmdr[batch].T @ d_cmdr_proj
            db_cmdr = d_cmdr_proj.sum(axis=0)
            dW_card = X_card[batch].T @ d_card_proj
            db_card = d_card_proj.sum(axis=0)

            grads = {
                "W_cmdr": dW_cmdr, "b_cmdr": db_cmdr,
                "W_card": dW_card, "b_card": db_card,
                "W1": dW1, "b1": db1, "W2": dW2, "b2": db2,
                "W3": dW3, "b3": db3, "W4": dW4, "b4": db4,
            }
            beta1, beta2 = 0.9, 0.999
            t_step = epoch * (len(train_idx) // batch_size) + i // batch_size + 1
            for key in model:
                g = grads[key]
                adam_m[key] = beta1 * adam_m[key] + (1 - beta1) * g
                adam_v[key] = beta2 * adam_v[key] + (1 - beta2) * g ** 2
                m_hat = adam_m[key] / (1 - beta1 ** t_step)
                v_hat = adam_v[key] / (1 - beta2 ** t_step)
                model[key] -= lr * m_hat / (np.sqrt(v_hat) + eps)

        if (epoch + 1) % 10 == 0 or epoch == 0:
            test_raw, _ = forward(model, X_cmdr[test_idx], X_card[test_idx], X_struct[test_idx])
            test_prob = sigmoid(test_raw)
            auc = roc_auc_score(y[test_idx], test_prob)
            accuracy = np.mean((test_prob >= 0.5).astype(np.float32) == y[test_idx])
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
                best_epoch = epoch + 1
                print(f"    LR reduced to {lr}")

    elapsed = time.time() - t0
    print(f"\nTraining: {elapsed:.1f}s, best AUC={best_auc:.4f} (epoch {best_auc_epoch})")

    if best_model:
        np.savez(TOWER_FORGE_PATH, **best_model,
                 struct_means=struct_means, struct_stds=struct_stds)
        print(f"Forge tower saved to {TOWER_FORGE_PATH}")
    else:
        np.savez(TOWER_FORGE_PATH, **model,
                 struct_means=struct_means, struct_stds=struct_stds)

    return best_model or model


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

    # Load strategies + subtypes for hard negative sampling
    card_strats = {}
    for oid, s in conn.execute("SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"):
        card_strats.setdefault(oid, set()).add(s)

    card_subtypes = {}
    for row in conn.execute("SELECT oracle_id, type_line FROM cards WHERE type_line LIKE '%—%'"):
        oid, tl = row
        try:
            subs = {s.lower() for s in tl.split("—")[1].strip().split()}
            if subs:
                card_subtypes[oid] = subs
        except (IndexError, AttributeError):
            pass

    # Sample negatives: 50% hard (strategy/subtype overlap) + 50% random
    print("\nSampling negatives (ratio=3, 50% hard)...")
    neg_pairs = sample_negatives(
        positives_by_cmdr, all_card_oids, card_colors, card_colors, ratio=3,
        hard_ratio=0.5, card_strats=card_strats, card_subtypes=card_subtypes,
    )
    print(f"  Negative pairs: {len(neg_pairs)}")

    # Combine into pairs_by_cmdr: dict[cmdr_oid -> list[(card_oid, label)]]
    pairs_by_cmdr = {}
    for cmdr_oid, card_oids in positives_by_cmdr.items():
        pairs_by_cmdr[cmdr_oid] = [(oid, 1) for oid in card_oids]
    for cmdr_oid, card_oid, label in neg_pairs:
        pairs_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, label))

    return pairs_by_cmdr


def _load_forge_pairs_for_features(conn, oid_to_idx):
    """Load causal graph positives + sample negatives. Zero EDHREC dependency.

    Returns dict[cmdr_oid -> list[(card_oid, label)]].
    """
    # Load color identities
    card_colors = {}
    for row in conn.execute("SELECT oracle_id, color_identity FROM cards"):
        card_colors[row[0]] = set(json.loads(row[1] or "[]"))

    # Build card pool (exclude basic lands and tokens)
    basic_land_names = {"Plains", "Island", "Swamp", "Mountain", "Forest",
                        "Snow-Covered Plains", "Snow-Covered Island",
                        "Snow-Covered Swamp", "Snow-Covered Mountain",
                        "Snow-Covered Forest", "Wastes"}
    basic_land_oids = set()
    for name in basic_land_names:
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        if row:
            basic_land_oids.add(row[0])

    type_lines = {}
    for row in conn.execute("SELECT oracle_id, type_line FROM cards"):
        type_lines[row[0]] = row[1] or ""

    all_card_oids = []
    for oid in oid_to_idx:
        if oid in basic_land_oids:
            continue
        tl = type_lines.get(oid, "")
        if "Token" in tl or "token" in tl:
            continue
        all_card_oids.append(oid)

    # Load positives from causal graph (not EDHREC)
    print("\nLoading causal graph positives...")
    positives_by_cmdr = load_forge_positives(conn, oid_to_idx)

    total_pos = sum(len(v) for v in positives_by_cmdr.values())
    print(f"  Positive pairs with embeddings: {total_pos} across {len(positives_by_cmdr)} commanders")

    # Sample negatives (same 3:1 ratio, color-legal)
    print("\nSampling negatives (ratio=3)...")
    neg_pairs = sample_negatives(
        positives_by_cmdr, all_card_oids, card_colors, card_colors, ratio=3
    )
    print(f"  Negative pairs: {len(neg_pairs)}")

    # Combine into pairs_by_cmdr
    pairs_by_cmdr = {}
    for cmdr_oid, card_oids in positives_by_cmdr.items():
        pairs_by_cmdr[cmdr_oid] = [(oid, 1) for oid in card_oids]
    for cmdr_oid, card_oid, label in neg_pairs:
        pairs_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, label))

    return pairs_by_cmdr


def holdout_evaluation(seed=42, drop_features=None):
    """Train on 80% of commanders, evaluate Recall@K on held-out 20%.

    This gives the TRUE generalization performance, avoiding the trap of
    evaluating on training commanders.

    Args:
        drop_features: list of feature names to zero out (e.g. ["edhrec_synergy"])
    """
    import lightgbm as lgb
    import joblib
    from sklearn.metrics import roc_auc_score as sklearn_auc

    drop_indices = []
    if drop_features:
        for f in drop_features:
            if f in FEATURE_NAMES:
                drop_indices.append(FEATURE_NAMES.index(f))
            else:
                print(f"WARNING: Unknown feature '{f}', ignoring")

    title = "HELD-OUT EVALUATION: Train 80% / Test 20% commanders"
    if drop_features:
        title += f" (dropped: {', '.join(drop_features)})"
    print("=" * 60)
    print(title)
    print("=" * 60)

    # Load embeddings
    print("\nLoading embeddings...")
    _, _, oid_to_idx = load_embeddings()

    conn = sqlite3.connect(DB_PATH)

    # Load pairs
    pairs_by_cmdr = _load_pairs_for_features(conn, oid_to_idx)

    # Split commanders 80/20
    rng = np.random.RandomState(seed)
    all_cmdrs = list(pairs_by_cmdr.keys())
    rng.shuffle(all_cmdrs)
    split_idx = int(len(all_cmdrs) * 0.8)
    train_cmdrs = set(all_cmdrs[:split_idx])
    test_cmdrs = set(all_cmdrs[split_idx:])
    print(f"\nCommander split: {len(train_cmdrs)} train / {len(test_cmdrs)} test")

    # Build feature matrix for train commanders only
    train_pairs = {c: pairs_by_cmdr[c] for c in train_cmdrs}
    print("\nBuilding feature matrix for TRAIN commanders...")
    X_train, y_train, cmdr_ids_train = build_feature_matrix(train_pairs)

    # Zero out dropped features
    if drop_indices:
        for idx in drop_indices:
            X_train[:, idx] = 0.0
        print(f"  Zeroed out features: {[FEATURE_NAMES[i] for i in drop_indices]}")

    # Train GBM on train split
    print(f"\nTraining LightGBM on {len(train_cmdrs)} commanders...")
    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 63,
        "learning_rate": 0.05,
        "n_estimators": 500,
        "verbose": -1,
    }
    model = lgb.LGBMClassifier(**params)
    model.fit(X_train, y_train, feature_name=FEATURE_NAMES)
    train_auc = sklearn_auc(y_train, model.predict_proba(X_train)[:, 1])
    print(f"  Train AUC: {train_auc:.4f}")

    # Build feature matrix for test commanders
    test_pairs = {c: pairs_by_cmdr[c] for c in test_cmdrs}
    print(f"\nBuilding feature matrix for TEST commanders...")
    X_test, y_test, cmdr_ids_test = build_feature_matrix(test_pairs)

    # Zero out dropped features in test set too
    if drop_indices:
        for idx in drop_indices:
            X_test[:, idx] = 0.0

    test_auc = sklearn_auc(y_test, model.predict_proba(X_test)[:, 1])
    print(f"  Test AUC: {test_auc:.4f}")

    # Now compute Recall@K on test commanders
    # For each test commander: rank ALL their candidate cards by GBM probability,
    # then check how many of the EDHREC avg deck cards appear in our top K
    print("\n" + "=" * 60)
    print("RECALL@K ON HELD-OUT COMMANDERS")
    print("=" * 60)

    # Load EDHREC avg decks for test commanders
    # We need commander slug for each test commander OID
    cmdr_oid_to_slug = {}
    for row in conn.execute(
        "SELECT DISTINCT commander_slug FROM edhrec_average_decks"
    ):
        slug = row[0]
        # Map slug -> commander name -> oracle_id
        name_row = conn.execute(
            "SELECT oracle_id, name FROM cards WHERE REPLACE(LOWER(name), ' ', '-') = ? "
            "OR REPLACE(REPLACE(LOWER(name), ',', ''), ' ', '-') = ?",
            (slug, slug)
        ).fetchone()
        if name_row and name_row[0] in test_cmdrs:
            cmdr_oid_to_slug[name_row[0]] = slug

    # Also try loading the slug mapping used in load_edhrec_membership
    # The membership loader mapped slug → cmdr_oid, so let's reverse it
    positives = load_edhrec_membership(conn)
    slug_lookup = {}
    for row in conn.execute("SELECT DISTINCT commander_slug FROM edhrec_average_decks"):
        slug = row[0]
        # Find which cmdr_oid has cards matching this slug's deck
        deck_cards = set(r[0] for r in conn.execute(
            "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?", (slug,)))
        for cmdr_oid in test_cmdrs:
            if cmdr_oid in positives:
                # Check overlap between this commander's positive cards and the slug's deck
                cmdr_card_names = set()
                for card_oid in positives[cmdr_oid]:
                    name_row = conn.execute(
                        "SELECT name FROM cards WHERE oracle_id = ?", (card_oid,)).fetchone()
                    if name_row:
                        cmdr_card_names.add(name_row[0])
                overlap = len(cmdr_card_names & deck_cards)
                if overlap > 10:  # Strong match
                    slug_lookup[cmdr_oid] = slug
                    break

    print(f"  Mapped {len(slug_lookup)} test commanders to EDHREC slugs")

    # For each test commander, get ALL their pairs (positive + negative),
    # score with GBM, rank, and compute Recall@K
    k_values = [30, 50, 100]
    recalls_avg = {k: [] for k in k_values}
    recalls_syn = {k: [] for k in k_values}

    test_cmdr_list = sorted(test_cmdrs & set(slug_lookup.keys()))

    for cmdr_oid in test_cmdr_list:
        slug = slug_lookup[cmdr_oid]

        # Get avg deck card names
        avg_deck = set(r[0] for r in conn.execute(
            "SELECT card_name FROM edhrec_average_decks WHERE commander_slug = ?", (slug,)))
        if len(avg_deck) < 20:
            continue

        # Get high-synergy cards
        syn_top = set(r[0] for r in conn.execute(
            "SELECT card_name FROM edhrec_card_synergy WHERE commander_slug = ? AND synergy >= 0.2",
            (slug,)))

        # Get this commander's test pairs
        pairs = pairs_by_cmdr.get(cmdr_oid, [])
        if not pairs:
            continue

        # Build feature matrix for just this commander
        single_pairs = {cmdr_oid: pairs}
        X_cmdr, y_cmdr, _ = build_feature_matrix(single_pairs, verbose=False)
        if len(X_cmdr) == 0:
            continue

        # Score with GBM
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            probs = model.predict_proba(X_cmdr)[:, 1]

        # Map card_oid back to card_name for recall computation
        card_names_ordered = []
        for card_oid, label in pairs:
            name_row = conn.execute(
                "SELECT name FROM cards WHERE oracle_id = ?", (card_oid,)).fetchone()
            card_names_ordered.append(name_row[0] if name_row else "")

        # Rank by probability
        ranked_indices = np.argsort(-probs)
        ranked_names = [card_names_ordered[i] for i in ranked_indices if card_names_ordered[i]]

        # Compute Recall@K against avg deck
        for k in k_values:
            our_top_k = set(ranked_names[:k])
            recall = len(our_top_k & avg_deck) / len(avg_deck) if avg_deck else 0
            recalls_avg[k].append(recall)

        # Compute Recall@K against high synergy
        if len(syn_top) >= 5:
            for k in k_values:
                our_top_k = set(ranked_names[:k])
                recall = len(our_top_k & syn_top) / len(syn_top) if syn_top else 0
                recalls_syn[k].append(recall)

    print(f"\n  [Average Deck Recall — {len(recalls_avg[100])} held-out commanders]")
    for k in k_values:
        if recalls_avg[k]:
            avg = sum(recalls_avg[k]) / len(recalls_avg[k])
            print(f"    Recall@{k}: {avg:.1%}")

    print(f"\n  [High Synergy Recall (>=0.2) — {len(recalls_syn[100])} held-out commanders]")
    for k in k_values:
        if recalls_syn[k]:
            avg = sum(recalls_syn[k]) / len(recalls_syn[k])
            print(f"    Recall@{k}: {avg:.1%}")

    conn.close()


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
    parser.add_argument(
        "--holdout-eval",
        action="store_true",
        help="Train on 80%% of commanders, evaluate Recall@K on held-out 20%%",
    )
    parser.add_argument(
        "--drop-feature",
        action="append",
        default=[],
        help="Zero out a feature during holdout eval (e.g. --drop-feature edhrec_synergy)",
    )
    parser.add_argument(
        "--forge-only",
        action="store_true",
        help="Train forge-only GBM on causal labels (uses cached features if available)",
    )
    parser.add_argument(
        "--rebuild-features",
        action="store_true",
        help="Force rebuild feature matrix (ignore cache) when used with --forge-only",
    )
    parser.add_argument(
        "--forge-tower",
        action="store_true",
        help="Train tower model on causal graph connectivity (forge-native, no EDHREC)",
    )
    args = parser.parse_args()

    if args.holdout_eval:
        holdout_evaluation(drop_features=args.drop_feature if args.drop_feature else None)
        return

    if args.forge_tower:
        train_tower_forge()
        return

    if args.forge_only:
        print("=" * 60)
        print("FORGE-ONLY MODEL — EDHREC labels, forge features")
        print("=" * 60)

        # Check for cached feature matrix (skip 3+ min rebuild)
        cache_path = os.path.join(DATA_DIR, "forge_features_cache.npz")
        if os.path.exists(cache_path) and not args.rebuild_features:
            print(f"\nLoading cached feature matrix from {cache_path}")
            cached = np.load(cache_path)
            X_forge = cached["X"]
            y_forge = cached["y"]
            cmdr_ids_forge = cached["cmdr_ids"]
            print(f"  Matrix: {X_forge.shape}, positives: {int(y_forge.sum())}, "
                  f"negatives: {int(len(y_forge) - y_forge.sum())}")
        else:
            print("\nLoading embeddings...")
            _, _, oid_to_idx = load_embeddings()

            conn = sqlite3.connect(DB_PATH)
            pairs_by_cmdr = _load_pairs_for_features(conn, oid_to_idx)
            conn.close()

            print("\n--- Building FORGE-ONLY feature matrix ---")
            X_forge, y_forge, cmdr_ids_forge = build_forge_feature_matrix(pairs_by_cmdr)

            # Cache for fast reruns
            np.savez(cache_path, X=X_forge, y=y_forge, cmdr_ids=cmdr_ids_forge)
            print(f"  Feature matrix cached to {cache_path}")

        # Train forge GBM only (no baseline)
        print("\n" + "=" * 60)
        print("Training FORGE-ONLY model (20 features)")
        print("=" * 60)
        _, forge_scores = train_forge_gbm(X_forge, y_forge, cmdr_ids_forge)

        forge_auc = forge_scores["mean_auc"]
        print(f"\n  Forge-only AUC: {forge_auc:.4f}")

        # Print forge model feature importance
        import joblib
        forge_model = joblib.load(os.path.join(DATA_DIR, "fusion_model_forge.lgb"))
        print(f"\n  Forge model feature importance:")
        total_imp = sum(forge_model.feature_importances_)
        for name, imp in sorted(
            zip(FORGE_FEATURE_NAMES, forge_model.feature_importances_), key=lambda x: -x[1]
        ):
            print(f"    {name:25s} {imp:6d} ({imp/total_imp*100:5.1f}%)")
        return

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
