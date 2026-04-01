#!/usr/bin/env python3
"""Train LightGBM LambdaRank model for card recommendation.

Trained on EDHREC labels with 93 Forge-native features.

Usage:
    python3 train_fusion_model.py                          # Train forge GBM (default)
    python3 train_fusion_model.py --rebuild-features       # Rebuild feature cache + train
"""

import argparse
import json
import os
import sqlite3
import time

import numpy as np

from mtg_synergy.config import ALLOWED_SLUG_TABLES
from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext,
    CmdrFeatureContext,
    compute_batch_features,
)

from mtg_synergy.config import DATA_DIR, DB_PATH

# ── Training constants ────────────────────────────────────────────────
_NEGATIVE_RATIO = 2
_STAPLE_THRESHOLD = 0.30
_GRADE_BOUNDARIES = (0.30, 0.15, 0.05, 0.0)
_FINAL_ROUND_MULTIPLIER = 1.1

FORGE_FEATURE_NAMES = [
    # ── Causal graph features ──
    "causal_cmdr_to_card",          # F0
    "causal_card_to_cmdr",          # F1
    "deck_edge_count",              # F2
    # ── Strategy features ──
    "strategy_cosine",              # F3
    "forge_ability_cosine",         # F4
    # ── Phase / trigger features ──
    "phase_match",                  # F5
    "has_phase_trigger",            # F6
    # ── Tribal / CMC features ──
    "tribal_match",                 # F7
    "cmc",                          # F8
    # ── Edge precision features ──
    "deck_exact_edge_ratio",        # F9
    "causal_composite",             # F10
    "card_hub_score",               # F11
    "deck_exact_count",             # F12
    # ── Forge type / mechanics features ──
    "forge_type_synergy",
    "cmdr_forge_type_match",
    "forge_ability_depth",
    "forge_anti_tribal",
    "forge_verb_alignment",
    "counter_type_match",
    "ability_type_ratio_T",
    "ability_type_ratio_A",
    "zone_alignment",
    "target_alignment",
    "forge_keyword_synergy",
    "activated_ability_count",
    "is_permanent_effect",
    "is_temporary_effect",
    "duration_match",
    "combat_damage_flag",
    "effect_zone_match",
    "scales_with_board",
    "is_secondary_trigger",
    "gain_control",
    "granted_keyword_count",
    "condition_count",
    # ── Forge deck-building AI tag features ──
    "deck_hints_to_has",
    "deck_has_to_hints",
    "deck_needs_to_has",
    "deck_has_overlap",
    "deck_hints_overlap",
    # ── Scaling / variable features ──
    "damage_scales",
    "draw_scales",
    "life_scales",
    "produces_mana",
    "granted_ability_match",
    "token_amount_variable",
    # ── Ability / token complexity features ──
    "total_ability_count",
    "triggered_ability_count",
    "token_power_toughness",
    "token_keyword_count",
    "zone_graveyard_interact",
    "ability_density",
    # ── Needs / dependency features ──
    "cmdr_needs_to_card_has",
    "card_needs_satisfied",
    "needs_rarity",
    # ── Counter / anthem distinction features ──
    "put_counter_ratio",
    "cmdr_counter_x_put_counter",
    "cmdr_p1p1_card_no_counters",
    # ── Functional fingerprint features ──
    "func_produces_amplifies",
    "func_requires_produces",
    "func_card_requires_cmdr",
    "func_full_cosine",
    # ── 2-hop graph features ──
    "cmdr_2hop_count",
    "cmdr_2hop_ratio",
    # ── Card quality / noise suppression ──
    "forge_ability_richness",
    "card_strategy_count",
    "deck_tag_count",
    "edhrec_deck_pct",
    # ── Tribal depth features ──
    "tribal_lord_for_cmdr",
    "tribal_member_of_cmdr",
    "tribal_synergy_depth",
    # ── General commander demand features ──
    "verb_demand_match",
    "type_demand_match",
    # ── Per-category mechanics sub-products (produce/consume) ──
    "mech_board_fwd",               # creature/permanent events
    "mech_board_rev",
    "mech_resource_fwd",            # counters/draw/life/damage
    "mech_resource_rev",
    "mech_disruption_fwd",          # discard/mill/target
    "mech_disruption_rev",
    "mech_tempo_fwd",               # spell_cast/attacks/blocks
    "mech_tempo_rev",
    "mech_utility_fwd",             # tap/untap/pump/mana/phase
    "mech_utility_rev",
    "mech_zones_fwd",               # graveyard/exile/hand
    "mech_zones_rev",
    "mech_themes_fwd",              # equipment/defender/etb
    "mech_themes_rev",
    "mech_tribal_fwd",              # 80 subtypes
    "mech_tribal_rev",
    # ── New field features ──
    "affected_scope_ratio",             # F89: fraction of effects targeting self
    "pump_magnitude",                   # F90: max pump power (0-15)
    "pump_is_variable",                 # F91: pump uses X/Y variable
    "type_change_tribal_match",         # F92: ChangeType$ matches commander tribal
]


def _resolve_slugs_to_oids(conn, table="edhrec_average_decks"):
    """Resolve EDHREC commander slugs to oracle_ids via Python string matching.

    Builds a normalized name→oid lookup once, then matches all slugs in-memory
    instead of doing N separate SQL LIKE queries. ~100x faster for ~3000 slugs.

    Returns (slug_to_oid, name_to_oid) dicts.
    """
    if table not in ALLOWED_SLUG_TABLES:
        raise ValueError(f"table must be one of {ALLOWED_SLUG_TABLES}, got {table!r}")

    # Map card_name -> oracle_id (prefer non-token versions)
    name_to_oid = {}
    for row in conn.execute(
        "SELECT name, oracle_id, type_line FROM cards ORDER BY "
        "CASE WHEN type_line LIKE '%Token%' THEN 1 ELSE 0 END"
    ):
        if row[0] not in name_to_oid:
            name_to_oid[row[0]] = row[1]

    # Build normalized name → oracle_id for legendary cards only
    # Normalized: lowercase, remove apostrophes and commas (matches old SQL REPLACE behavior)
    norm_legendaries = {}  # normalized_name → oracle_id
    for row in conn.execute(
        "SELECT name, oracle_id FROM cards WHERE type_line LIKE '%Legendary%'"
    ):
        norm = row[0].lower().replace("'", "").replace(",", "")
        if norm not in norm_legendaries:
            norm_legendaries[norm] = row[1]

    # Resolve slugs in Python (no SQL LIKE queries)
    all_slugs = set(
        r[0] for r in conn.execute(
            f"SELECT DISTINCT commander_slug FROM {table}"
        )
    )

    slug_to_oid = {}
    for slug in all_slugs:
        parts = slug.split("-")
        slug_name = " ".join(parts)

        # O(1) exact match: "krenko-mob-boss" → "krenko mob boss"
        oid = norm_legendaries.get(slug_name)
        if oid is not None:
            slug_to_oid[slug] = oid
            continue

        # Fallback: ordered substring match (matches old SQL LIKE '%a%b%c%' behavior)
        for norm_name, oid in norm_legendaries.items():
            pos = 0
            matched = True
            for p in parts:
                idx = norm_name.find(p, pos)
                if idx < 0:
                    matched = False
                    break
                pos = idx + len(p)
            if matched:
                slug_to_oid[slug] = oid
                break

    return slug_to_oid, name_to_oid


def sample_negatives(positives_by_cmdr, all_card_oids, card_colors, cmdr_colors,
                     ratio=_NEGATIVE_RATIO, card_strats=None, card_subtypes=None,
                     card_has_tags=None):
    """Sample negative pairs (cards NOT in a commander's EDHREC page).

    For each commander, samples ratio * |positives| negative cards in 3 tiers:
    - 1/3 strategy/subtype overlap (same tribe/archetype, wrong card)
    - 1/3 tag overlap (same has-tags as commander, e.g., Ability$Counters)
    - 1/3 random color-legal cards

    Returns list of (cmdr_oid, card_oid, 0) tuples.
    """
    rng = np.random.RandomState(42)

    # Pre-group cards by color identity signature for O(1) lookup per commander.
    ci_to_cards = {}
    for oid in all_card_oids:
        ci_key = frozenset(card_colors.get(oid, set()))
        ci_to_cards.setdefault(ci_key, []).append(oid)

    # Pre-compute: for each possible commander CI, which CI keys are legal subsets
    ci_key_subsets = {}
    all_ci_keys = list(ci_to_cards.keys())
    unique_cmdr_cis = set()
    for cmdr_oid in positives_by_cmdr:
        unique_cmdr_cis.add(frozenset(cmdr_colors.get(cmdr_oid, set())))
    for cmdr_ci_key in unique_cmdr_cis:
        ci_key_subsets[cmdr_ci_key] = [k for k in all_ci_keys if k <= cmdr_ci_key]

    # Reverse index: tag → set of oids that have it (for tag-overlap negatives)
    tag_to_oids = {}
    if card_has_tags:
        for oid, tags in card_has_tags.items():
            for tag in tags:
                tag_to_oids.setdefault(tag, set()).add(oid)

    negatives = []

    for cmdr_oid, pos_cards in positives_by_cmdr.items():
        cmdr_ci = frozenset(cmdr_colors.get(cmdr_oid, set()))

        # Candidate pool: color-legal, not in deck
        exclude = pos_cards | {cmdr_oid} if isinstance(pos_cards, set) else set(pos_cards) | {cmdr_oid}
        candidates = []
        cand_set = set()
        for ci_key in ci_key_subsets.get(cmdr_ci, []):
            for oid in ci_to_cards[ci_key]:
                if oid not in exclude:
                    candidates.append(oid)
                    cand_set.add(oid)

        n_neg = min(len(candidates), ratio * len(pos_cards))
        if n_neg == 0:
            continue

        n_per_tier = n_neg // 3
        all_chosen = set()

        # Tier 1: strategy/subtype overlap (same tribe but wrong card)
        if n_per_tier > 0 and (card_strats or card_subtypes):
            cmdr_strats = card_strats.get(cmdr_oid, set()) if card_strats else set()
            cmdr_subs = card_subtypes.get(cmdr_oid, set()) if card_subtypes else set()
            hard_pool = [oid for oid in candidates
                         if (card_strats and bool(cmdr_strats & card_strats.get(oid, set()))) or
                            (card_subtypes and bool(cmdr_subs & card_subtypes.get(oid, set())))]
            if hard_pool:
                n_pick = min(n_per_tier, len(hard_pool))
                for idx in rng.choice(len(hard_pool), size=n_pick, replace=False):
                    all_chosen.add(hard_pool[idx])

        # Tier 2: tag overlap (shares has-tags with commander, e.g., Ability$Counters)
        if n_per_tier > 0 and card_has_tags:
            cmdr_tags = card_has_tags.get(cmdr_oid, set())
            if cmdr_tags:
                tag_pool_set = set()
                for tag in cmdr_tags:
                    tag_pool_set.update(tag_to_oids.get(tag, set()))
                tag_pool = [oid for oid in tag_pool_set
                            if oid in cand_set and oid not in all_chosen]
                if tag_pool:
                    n_pick = min(n_per_tier, len(tag_pool))
                    for idx in rng.choice(len(tag_pool), size=n_pick, replace=False):
                        all_chosen.add(tag_pool[idx])

        for oid in all_chosen:
            negatives.append((cmdr_oid, oid, 0))

        # Tier 3: random (fill remainder)
        n_random = n_neg - len(all_chosen)
        if n_random > 0:
            random_pool = [oid for oid in candidates if oid not in all_chosen]
            if random_pool:
                n_random = min(n_random, len(random_pool))
                for idx in rng.choice(len(random_pool), size=n_random, replace=False):
                    negatives.append((cmdr_oid, random_pool[idx], 0))

    return negatives


# Module-level globals for fork-based workers (inherited, not serialized)
_shared_ctx = None
_shared_card_meta = None


def _compute_chunk_shared(args):
    """Worker function using shared ForgeFeatureContext (fork-inherited).

    Uses module-level _shared_ctx instead of creating a new one per worker.
    Eliminates ~20s edge index loading per worker.
    """
    cmdr_chunk, pairs_by_cmdr, cmdr_to_idx, n_features = args
    ctx = _shared_ctx
    card_meta = _shared_card_meta

    # Pre-allocate arrays for this chunk
    n_pairs = sum(len(pairs_by_cmdr[c]) for c in cmdr_chunk)
    X = np.zeros((n_pairs, n_features), dtype=np.float32)
    y = np.zeros(n_pairs, dtype=np.float32)
    cmdr_ids = np.zeros(n_pairs, dtype=np.int32)
    row_idx = 0

    for cmdr_oid in cmdr_chunk:
        pairs = pairs_by_cmdr.get(cmdr_oid, [])
        if not pairs:
            continue

        deck_oids_for_cmdr = {oid for oid, lbl in pairs if lbl > 0}
        cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids_for_cmdr)

        # Batch compute features for all cards of this commander
        card_oids = [oid for oid, _ in pairs]
        labels = np.array([float(lbl) for _, lbl in pairs], dtype=np.float32)
        card_cmcs = np.array([float(card_meta.get(oid, {}).get("cmc", 0.0))
                              for oid in card_oids], dtype=np.float32)

        batch_X = compute_batch_features(card_oids, card_cmcs, ctx, cmdr_ctx)

        n = len(card_oids)
        cmdr_idx = cmdr_to_idx[cmdr_oid]
        X[row_idx:row_idx + n] = batch_X
        y[row_idx:row_idx + n] = labels
        cmdr_ids[row_idx:row_idx + n] = cmdr_idx
        row_idx += n

    return X[:row_idx], y[:row_idx], cmdr_ids[:row_idx]


def _init_shared_pool(ctx, card_meta):
    """Pool initializer: set module-level globals for fork-inherited context."""
    global _shared_ctx, _shared_card_meta
    _shared_ctx = ctx
    _shared_card_meta = card_meta


def build_forge_feature_matrix(pairs_by_cmdr, verbose=True):
    """Build 93-feature matrix from causal graph, strategies, and Forge ability data.

    Loads ForgeFeatureContext once in the parent process, then shares it
    with fork-based worker processes to avoid redundant edge index loading.
    """
    import multiprocessing as mp

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

    # ── Single shared forge feature context ──────────────────────────
    ctx = ForgeFeatureContext(conn, preload_edges=True, preload_strength=True)
    conn.close()
    ctx.conn = None  # prevent accidental DB access in workers

    # Fill CMC array for batch lookups
    for oid, meta in card_meta.items():
        i = ctx.oid_to_idx.get(oid)
        if i is not None:
            ctx._arr_cmc[i] = float(meta["cmc"])

    if verbose:
        print(f"  Strategy vector: {ctx._n_strats} strategies")
        print(f"  Forge ability vectors: {ctx._n_abilities} vocab, {len(ctx._ability_vectors)} cards with vectors")

    # ── Prepare for parallel workers ─────────────────────────────────
    cmdr_oids_ordered = sorted(pairs_by_cmdr.keys())
    cmdr_to_idx = {oid: i for i, oid in enumerate(cmdr_oids_ordered)}

    n_workers = min(mp.cpu_count(), len(cmdr_oids_ordered), 8)
    chunk_size = (len(cmdr_oids_ordered) + n_workers - 1) // n_workers
    chunks = [cmdr_oids_ordered[i:i + chunk_size]
              for i in range(0, len(cmdr_oids_ordered), chunk_size)]

    if verbose:
        print(f"  Parallel feature build: {n_workers} workers, "
              f"{len(cmdr_oids_ordered)} commanders", flush=True)

    worker_args = [
        (chunk, pairs_by_cmdr, cmdr_to_idx, len(FORGE_FEATURE_NAMES))
        for chunk in chunks
    ]

    t0 = time.time()
    # Use fork context so workers inherit the shared ctx (no serialization)
    fork_ctx = mp.get_context("fork")
    with fork_ctx.Pool(n_workers, initializer=_init_shared_pool,
                       initargs=(ctx, card_meta)) as pool:
        results = pool.map(_compute_chunk_shared, worker_args)

    # ── Merge results from all workers ───────────────────────────────
    X = np.concatenate([r[0] for r in results], axis=0)
    y = np.concatenate([r[1] for r in results], axis=0)
    cmdr_ids = np.concatenate([r[2] for r in results], axis=0)

    if verbose:
        print(f"  Feature build time: {time.time() - t0:.1f}s")
        print(f"\nForge feature matrix: {X.shape}")
        n_pos = int((y > 0).sum())
        print(f"  Positive (grade>0): {n_pos}, Negative (grade=0): {int(len(y) - n_pos)}")
        if y.max() > 1:
            for g in range(int(y.max()), 0, -1):
                print(f"    Grade {g}: {int((y == g).sum())}")

        print(f"  Commanders: {len(cmdr_oids_ordered)}")
        print("\nPer-feature statistics:")
        for i, name in enumerate(FORGE_FEATURE_NAMES):
            col = X[:, i]
            print(f"  {name:>25s}: "
                  f"mean={col.mean():.4f}  std={col.std():.4f}  "
                  f"min={col.min():.4f}  max={col.max():.4f}  "
                  f"nonzero={np.count_nonzero(col)}/{len(col)}")

    return X, y, cmdr_ids


def _build_group_array(cmdr_ids, idx_array):
    """Build LambdaRank group array from commander IDs for a subset of indices."""
    sub_cmdrs = cmdr_ids[idx_array]
    if len(sub_cmdrs) == 0:
        return []
    # Vectorized: find where commander ID changes, compute run lengths
    breaks = np.where(np.diff(sub_cmdrs) != 0)[0] + 1
    return np.diff(np.concatenate(([0], breaks, [len(sub_cmdrs)]))).tolist()


def _compute_ndcg30(preds, labels, groups, label_gain):
    """Vectorized NDCG@30 computation per commander group."""
    label_gain = np.asarray(label_gain, dtype=np.float64)
    # Pre-compute discount factors for positions 0..29
    discounts = 1.0 / np.log2(np.arange(2, 32, dtype=np.float64))  # 30 positions

    ndcg_scores = []
    start = 0
    for g in groups:
        end = start + g
        k = min(30, g)
        pred_slice = preds[start:end]
        label_slice = labels[start:end]

        # DCG: top-k by predicted score
        if k < g:
            top_k = np.argpartition(-pred_slice, k)[:k]
            top_k = top_k[np.argsort(-pred_slice[top_k])]
        else:
            top_k = np.argsort(-pred_slice)[:k]
        gains = label_gain[label_slice[top_k].astype(np.intp)]
        dcg = np.dot(gains, discounts[:k])

        # IDCG: top-k by true label
        ideal_gains = np.sort(label_gain[label_slice.astype(np.intp)])[::-1][:k]
        idcg = np.dot(ideal_gains, discounts[:k])

        ndcg_scores.append(dcg / idcg if idcg > 0 else 0.0)
        start = end

    return np.mean(ndcg_scores) if ndcg_scores else 0.0


def _train_one_fold(args):
    """Train a single CV fold. Used by both serial and parallel paths."""
    import lightgbm as lgb

    fold_i, train_idx, test_idx, X, y, w, cmdr_ids, params, feature_names = args

    # Ensure data within each fold is sorted by commander
    train_sort = np.argsort(cmdr_ids[train_idx])
    test_sort = np.argsort(cmdr_ids[test_idx])
    ti = train_idx[train_sort]
    vi = test_idx[test_sort]

    train_group = _build_group_array(cmdr_ids, ti)
    test_group = _build_group_array(cmdr_ids, vi)

    # Limit threads per fold to avoid contention when running parallel folds
    fold_params = {**params}
    if "num_threads" not in fold_params:
        import multiprocessing as mp
        fold_params["num_threads"] = max(1, mp.cpu_count() // 3)

    train_data = lgb.Dataset(X[ti], label=y[ti], weight=w[ti], group=train_group,
                             feature_name=feature_names, free_raw_data=False)
    eval_data = lgb.Dataset(X[vi], label=y[vi], weight=w[vi], group=test_group,
                            reference=train_data, feature_name=feature_names,
                            free_raw_data=False)

    booster = lgb.train(
        fold_params, train_data,
        num_boost_round=1000,
        valid_sets=[eval_data],
        callbacks=[lgb.early_stopping(40, verbose=False),
                   lgb.log_evaluation(0)],
    )

    # Vectorized NDCG@30 computation
    preds = booster.predict(X[vi])
    avg_ndcg = _compute_ndcg30(preds, y[vi], test_group, params["label_gain"])

    best_iter = booster.best_iteration or booster.current_iteration()
    return fold_i, avg_ndcg, best_iter


def _run_cv_folds(splits, X, y, w, cmdr_ids, params, feature_names, quick):
    """Run CV folds in parallel (or serial for single fold)."""
    from concurrent.futures import ProcessPoolExecutor

    fold_args = [
        (fold_i, train_idx, test_idx, X, y, w, cmdr_ids, params, feature_names)
        for fold_i, (train_idx, test_idx) in enumerate(splits)
    ]

    if quick or len(splits) == 1:
        # Serial for single fold
        results = [_train_one_fold(fold_args[0])]
    else:
        # Parallel CV folds
        print(f"  Training {len(splits)} folds in parallel...", flush=True)
        with ProcessPoolExecutor(max_workers=len(splits)) as executor:
            results = list(executor.map(_train_one_fold, fold_args))

    # Sort by fold index and report
    results.sort(key=lambda x: x[0])
    fold_ndcgs = []
    fold_best_iters = []
    for fold_i, avg_ndcg, best_iter in results:
        fold_ndcgs.append(avg_ndcg)
        fold_best_iters.append(best_iter)
        print(f"  Fold {fold_i+1}: NDCG@30={avg_ndcg:.4f} "
              f"({best_iter} rounds)")

    return fold_ndcgs, fold_best_iters


def train_forge_gbm(X, y, cmdr_ids, tune=False, quick=False):
    """Train LightGBM LambdaRank on forge features with graded relevance.

    Uses synergy-based relevance grades (0-5) instead of binary labels.
    LambdaRank optimizes NDCG, teaching the model to rank high-synergy
    cards above low-synergy ones.

    quick=True runs single-fold validation only (faster iteration).
    Saves to fusion_model_forge.lgb. Returns (model, cv_scores).
    """
    import lightgbm as lgb

    # Sort data by commander ID (required for LambdaRank groups)
    sort_order = np.argsort(cmdr_ids)
    X = X[sort_order]
    y = y[sort_order]
    cmdr_ids = cmdr_ids[sort_order]

    is_graded = y.max() > 1  # Check if we have graded labels
    if not is_graded:
        print("  WARNING: Binary labels detected, using classification instead of ranking")

    # Per-grade sample weights: upweight rare high-synergy examples
    _grade_weight_arr = np.array([1.0, 1.0, 1.0, 1.0, 2.0, 3.0], dtype=np.float32)
    w = _grade_weight_arr[y.astype(np.intp)]

    print(f"  Sample weights: grade 4→{_grade_weight_arr[4]}x, grade 5→{_grade_weight_arr[5]}x")

    splits = make_cv_splits(cmdr_ids, n_folds=3)  # always build 3 folds
    if quick:
        splits = splits[:1]  # use only fold 0 for quick validation

    # Hyperparameter search: use known-best by default, search only with --tune
    _hp_default = {
        "num_leaves": 767,
        "learning_rate": 0.025,
        "min_child_samples": 40,
        "bagging_freq": 5,
        "colsample_bytree": 0.6,
        "feature_fraction_bynode": 0.9,
    }
    _hp_configs = [
        # Vary tree structure
        {"num_leaves": 511, "learning_rate": 0.03, "min_child_samples": 40,
         "bagging_freq": 5, "colsample_bytree": 0.6, "feature_fraction_bynode": 0.9},
        {"num_leaves": 1023, "learning_rate": 0.02, "min_child_samples": 40,
         "bagging_freq": 5, "colsample_bytree": 0.6, "feature_fraction_bynode": 0.9},
        # Vary regularization
        {"num_leaves": 767, "learning_rate": 0.025, "min_child_samples": 80,
         "bagging_freq": 5, "colsample_bytree": 0.6, "feature_fraction_bynode": 0.8},
        {"num_leaves": 767, "learning_rate": 0.025, "min_child_samples": 40,
         "bagging_freq": 5, "colsample_bytree": 0.7, "feature_fraction_bynode": 0.8},
        # Vary learning rate
        {"num_leaves": 767, "learning_rate": 0.02, "min_child_samples": 40,
         "bagging_freq": 5, "colsample_bytree": 0.6, "feature_fraction_bynode": 0.9},
        # Default (included in search for fair comparison)
        {"num_leaves": 767, "learning_rate": 0.025, "min_child_samples": 40,
         "bagging_freq": 5, "colsample_bytree": 0.6, "feature_fraction_bynode": 0.9},
    ]

    base_params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [10, 30],
        "subsample": 0.8,
        "min_data_in_bin": 5,
        "verbose": -1,
        "label_gain": [0, 1, 3, 8, 20, 30],  # 6 grades: neg, anti-syn, low, moderate, top, high-syn
    }

    if tune:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        print(f"  Hyperparameter search ({len(_hp_configs)} configs, parallel)...")

        # Pre-compute fold data once (shared across all HP configs)
        train_idx, test_idx = splits[0]
        train_sort = np.argsort(cmdr_ids[train_idx])
        test_sort = np.argsort(cmdr_ids[test_idx])
        ti = train_idx[train_sort]
        vi = test_idx[test_sort]

        # Run HP configs in parallel (2-3 at a time, each with limited threads)
        n_hp_workers = min(3, len(_hp_configs))
        threads_per = max(1, mp.cpu_count() // n_hp_workers)

        hp_fold_args = []
        for hp_i, hp in enumerate(_hp_configs):
            hp_params = {**base_params, **hp, "num_threads": threads_per}
            hp_fold_args.append(
                (hp_i, ti, vi, X, y, w, cmdr_ids, hp_params, FORGE_FEATURE_NAMES)
            )

        print(f"    {n_hp_workers} parallel workers, {threads_per} threads each", flush=True)
        with ProcessPoolExecutor(max_workers=n_hp_workers) as executor:
            hp_results = list(executor.map(_train_one_fold, hp_fold_args))

        best_hp_ndcg = -1
        best_hp_config = _hp_configs[0]
        for (hp_i, ndcg, best_iter), hp in zip(
            sorted(hp_results, key=lambda x: x[0]), _hp_configs
        ):
            print(f"    leaves={hp['num_leaves']} lr={hp['learning_rate']} "
                  f"min_child={hp['min_child_samples']}: NDCG@30={ndcg:.4f} "
                  f"({best_iter} rounds)")
            if ndcg > best_hp_ndcg:
                best_hp_ndcg = ndcg
                best_hp_config = hp
        print(f"  Best: leaves={best_hp_config['num_leaves']} "
              f"lr={best_hp_config['learning_rate']}")
    else:
        best_hp_config = _hp_default
        print(f"  Using default HP: leaves={best_hp_config['num_leaves']} "
              f"lr={best_hp_config['learning_rate']} (use --tune to search)")

    params = {**base_params, **best_hp_config}

    fold_ndcgs, fold_best_iters = _run_cv_folds(
        splits, X, y, w, cmdr_ids, params, FORGE_FEATURE_NAMES, quick)

    print(f"  Mean NDCG@30: {np.mean(fold_ndcgs):.4f}")

    # Train final model on all data, using CV-derived round count
    avg_best = int(np.mean(fold_best_iters) * _FINAL_ROUND_MULTIPLIER)
    print(f"  Final model: {avg_best} rounds (avg CV best x {_FINAL_ROUND_MULTIPLIER})")
    all_group = _build_group_array(cmdr_ids, np.arange(len(cmdr_ids)))
    all_data = lgb.Dataset(X, label=y, weight=w, group=all_group,
                           feature_name=FORGE_FEATURE_NAMES)
    # Final model uses all cores (no parallel folds competing)
    final_params = {k: v for k, v in params.items() if k != "num_threads"}
    final_booster = lgb.train(final_params, all_data, num_boost_round=avg_best)

    model_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    final_booster.save_model(model_path)
    print(f"  Forge model saved to {model_path}")

    return final_booster, {"mean_ndcg30": float(np.mean(fold_ndcgs)),
                           "fold_ndcgs": fold_ndcgs}


def make_cv_splits(cmdr_ids, n_folds=5, seed=42):
    """Leave-commander-group-out CV splits.

    Ensures no commander appears in both train and test within the same fold.
    Returns list of (train_idx, test_idx) tuples.
    """
    unique_cmdrs = np.unique(cmdr_ids)
    rng = np.random.RandomState(seed)
    rng.shuffle(unique_cmdrs)
    fold_size = len(unique_cmdrs) // n_folds

    # Build cmdr_id → fold assignment lookup (vectorized)
    cmdr_fold = np.full(unique_cmdrs.max() + 1, -1, dtype=np.int8)
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else len(unique_cmdrs)
        cmdr_fold[unique_cmdrs[start:end]] = i

    # Vectorized: assign each row to its fold
    row_folds = cmdr_fold[cmdr_ids]
    all_idx = np.arange(len(cmdr_ids))

    splits = []
    for i in range(n_folds):
        mask = row_folds == i
        splits.append((all_idx[~mask], all_idx[mask]))
    return splits


def _load_pairs_for_features(conn):
    """Load training pairs from edhrec_card_synergy with section-based grading.

    Combines two EDHREC data sources:
    - edhrec_card_synergy: section labels + continuous synergy scores
    - edhrec_average_decks: practical deck inclusion (grade boost)

    Cards in the average deck get +1 grade boost (capped at 5), because
    actual deck inclusion validates theoretical synergy.

    Grades:
        5: "High Synergy Cards" section, or boosted Top Cards
        4: "Top Cards" section, or boosted moderate-synergy in-deck cards
        3: Other sections synergy > 0.1, or boosted low-synergy in-deck cards
        2: Other sections synergy 0-0.1
        1: Any section synergy < 0 (anti-synergy)
        0: Not in table (random negatives)

    Returns dict[cmdr_oid -> list[(card_oid, grade)]].
    """
    slug_to_oid, name_to_oid = _resolve_slugs_to_oids(conn, "edhrec_card_synergy")

    card_name_to_oid = {}
    for row in conn.execute(
        "SELECT name, oracle_id, type_line FROM cards "
        "ORDER BY CASE WHEN type_line LIKE '%Token%' THEN 1 ELSE 0 END"
    ):
        if row[0] not in card_name_to_oid:
            card_name_to_oid[row[0]] = row[1]

    # Load average deck membership for grade boost
    avg_deck_set = set()
    for row in conn.execute("SELECT commander_slug, card_name FROM edhrec_average_decks"):
        avg_deck_set.add((row[0], row[1]))

    print("\nLoading EDHREC card synergy data...")
    positives_by_cmdr = {}
    n_by_grade = {}
    card_cmdr_count = {}
    cmdr_count = len(slug_to_oid)
    n_boosted = 0

    for row in conn.execute(
        "SELECT commander_slug, card_name, synergy, section FROM edhrec_card_synergy"
    ):
        slug, card_name, synergy, section = row
        cmdr_oid = slug_to_oid.get(slug)
        card_oid = card_name_to_oid.get(card_name)
        if cmdr_oid is None or card_oid is None:
            continue

        card_cmdr_count[card_oid] = card_cmdr_count.get(card_oid, 0) + 1

        # Grade by continuous synergy score (EDHREC synergy = deck% - color_baseline%)
        # This captures commander-specific synergy regardless of which section EDHREC
        # placed the card in. Previously we graded by section name which lost signal.
        syn = synergy if synergy is not None else 0.0
        if syn >= _GRADE_BOUNDARIES[0]:
            grade = 5
        elif syn >= _GRADE_BOUNDARIES[1]:
            grade = 4
        elif syn >= _GRADE_BOUNDARIES[2]:
            grade = 3
        elif syn >= _GRADE_BOUNDARIES[3]:
            grade = 2
        else:
            grade = 1

        # Boost grade if card is in the average deck (practical validation)
        if (slug, card_name) in avg_deck_set and grade < 5 and grade >= 2:
            grade = min(grade + 1, 5)
            n_boosted += 1

        positives_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, grade))
        n_by_grade[grade] = n_by_grade.get(grade, 0) + 1

    total_pairs = sum(len(v) for v in positives_by_cmdr.values())
    print(f"  Synergy pairs: {total_pairs:,} across {len(positives_by_cmdr)} commanders")
    print(f"  Boosted by avg deck membership: {n_boosted:,}")
    for g in sorted(n_by_grade.keys(), reverse=True):
        print(f"    Grade {g}: {n_by_grade[g]:,}")

    # Filter staples from top grades
    staple_oids = {oid for oid, cnt in card_cmdr_count.items()
                   if cnt / cmdr_count > _STAPLE_THRESHOLD}
    n_filtered = 0
    for cmdr_oid in positives_by_cmdr:
        filtered = []
        for card_oid, grade in positives_by_cmdr[cmdr_oid]:
            if card_oid in staple_oids and grade >= 4:
                grade = 3
                n_filtered += 1
            filtered.append((card_oid, grade))
        positives_by_cmdr[cmdr_oid] = filtered
    if n_filtered:
        print(f"  Filtered {n_filtered} staple pairs (demoted from grade 4/5 to 3)")

    # Sample negatives
    card_colors = {}
    for row in conn.execute("SELECT oracle_id, color_identity FROM cards WHERE legal_commander = 1"):
        card_colors[row[0]] = set(json.loads(row[1] or "[]"))

    type_lines = {}
    for row in conn.execute("SELECT oracle_id, type_line FROM cards"):
        type_lines[row[0]] = row[1] or ""

    basic_land_names = {"Plains", "Island", "Swamp", "Mountain", "Forest",
                        "Snow-Covered Plains", "Snow-Covered Island",
                        "Snow-Covered Swamp", "Snow-Covered Mountain",
                        "Snow-Covered Forest", "Wastes"}
    ph = ",".join("?" * len(basic_land_names))
    basic_land_oids = {r[0] for r in conn.execute(
        f"SELECT oracle_id FROM cards WHERE name IN ({ph})",
        list(basic_land_names)
    ).fetchall()}

    card_pool = {r[0] for r in conn.execute(
        "SELECT oracle_id FROM cards WHERE legal_commander = 1")}
    all_card_oids = [oid for oid in card_pool
                     if oid not in basic_land_oids
                     and "Token" not in type_lines.get(oid, "")]

    card_strats = {}
    for oid, s in conn.execute(
        "SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"
    ):
        card_strats.setdefault(oid, set()).add(s)

    card_subtypes = {}
    for row in conn.execute(
        "SELECT oracle_id, type_line FROM cards WHERE type_line LIKE '%\u2014%'"
    ):
        try:
            subs = {s.lower() for s in row[1].split("\u2014")[1].strip().split()}
            if subs:
                card_subtypes[row[0]] = subs
        except (IndexError, AttributeError):
            pass

    # Load card has-tags for tag-overlap hard negatives
    card_has_tags = {}
    for row in conn.execute(
        "SELECT fnm.oracle_id, fdt.tag FROM forge_deck_tags fdt "
        "JOIN forge_name_map fnm ON fnm.forge_name = fdt.card_name "
        "WHERE fdt.tag_type = 'has'"
    ):
        card_has_tags.setdefault(row[0], set()).add(row[1])

    # Build positives set for negative exclusion
    positives_for_neg = {cmdr: {oid for oid, _ in pairs}
                         for cmdr, pairs in positives_by_cmdr.items()}

    print(f"\nSampling negatives (ratio={_NEGATIVE_RATIO}, 1/3 subtype + 1/3 tag + 1/3 random)...")
    neg_pairs = sample_negatives(
        positives_for_neg, all_card_oids, card_colors, card_colors,
        ratio=_NEGATIVE_RATIO,
        card_strats=card_strats, card_subtypes=card_subtypes,
        card_has_tags=card_has_tags,
    )
    print(f"  Negative pairs: {len(neg_pairs)}")

    for cmdr_oid, card_oid, label in neg_pairs:
        positives_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, 0))

    grade_counts = {}
    for pairs in positives_by_cmdr.values():
        for _, g in pairs:
            grade_counts[g] = grade_counts.get(g, 0) + 1
    print("\n  Final grade distribution:")
    for g in sorted(grade_counts.keys(), reverse=True):
        print(f"    Grade {g}: {grade_counts[g]:,}")

    return positives_by_cmdr


def main():
    parser = argparse.ArgumentParser(description="Train forge LightGBM model")
    parser.add_argument(
        "--rebuild-features",
        action="store_true",
        help="Force rebuild feature matrix (ignore cache)",
    )
    parser.add_argument(
        "--tune",
        action="store_true",
        help="Run hyperparameter search (6 configs, slower)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Single-fold validation only (faster iteration during development)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run full pipeline validation after training (checks scoring + penalties)",
    )
    parser.add_argument(
        "--validate-top",
        type=int,
        default=100,
        help="Number of commanders to validate (default: 100)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("FORGE-ONLY MODEL \u2014 EDHREC labels, forge features")
    print("=" * 60)

    # Check for cached feature matrix (skip 3+ min rebuild)
    cache_path = os.path.join(DATA_DIR, "forge_features_cache.npz")
    if os.path.exists(cache_path) and not args.rebuild_features:
        print(f"\nLoading cached feature matrix from {cache_path}")
        cached = np.load(cache_path)
        X_forge = cached["X"]
        y_forge = cached["y"]
        cmdr_ids_forge = cached["cmdr_ids"]
        print(f"  Matrix: {X_forge.shape}, positives: {int((y_forge > 0).sum())}, "
              f"negatives: {int((y_forge == 0).sum())}")
    else:
        # Use EDHREC labels (external ground truth) with forge features
        # Self-supervised causal labels overfit because features ARE the causal graph
        conn = sqlite3.connect(DB_PATH)
        pairs_by_cmdr = _load_pairs_for_features(conn)
        conn.close()

        print("\n--- Building FORGE-ONLY feature matrix ---")
        X_forge, y_forge, cmdr_ids_forge = build_forge_feature_matrix(pairs_by_cmdr)

        # Cache for fast reruns
        np.savez(cache_path, X=X_forge, y=y_forge, cmdr_ids=cmdr_ids_forge)
        print(f"  Feature matrix cached to {cache_path}")

    # Train forge GBM
    print("\n" + "=" * 60)
    print("Training FORGE-ONLY model (EDHREC labels, forge features)")
    print("=" * 60)
    _, forge_scores = train_forge_gbm(X_forge, y_forge, cmdr_ids_forge,
                                      tune=args.tune, quick=args.quick)

    if "mean_ndcg30" in forge_scores:
        print(f"\n  Forge-only NDCG@30: {forge_scores['mean_ndcg30']:.4f}")
    elif "mean_auc" in forge_scores:
        print(f"\n  Forge-only AUC: {forge_scores['mean_auc']:.4f}")

    # Print forge model feature importance
    import lightgbm as lgb
    forge_model_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    try:
        booster = lgb.Booster(model_file=forge_model_path)
        importances = booster.feature_importance(importance_type="split")
        names = booster.feature_name()
    except Exception:
        import joblib
        m = joblib.load(forge_model_path)
        importances = m.feature_importances_
        names = FORGE_FEATURE_NAMES
    print("\n  Forge model feature importance:")
    total_imp = sum(importances)
    for name, imp in sorted(zip(names, importances), key=lambda x: -x[1]):
        print(f"    {name:25s} {imp:6d} ({imp/total_imp*100:5.1f}%)")

    # ── Post-training pipeline validation ──
    if args.validate:
        print("\n" + "=" * 60)
        print("POST-TRAINING VALIDATION — full pipeline (model + scoring)")
        print("=" * 60)
        _run_pipeline_validation(args.validate_top)


def _run_pipeline_validation(top_n=50):
    """Run full recommendation pipeline validation after training.

    Tests the COMPLETE pipeline: model scoring → penalties → mechanical bonus.
    NDCG only tests model quality; this catches scoring/penalty bugs.
    """
    from validate_recommendations import (
        get_top_commanders, validate_commanders
    )
    from mtg_synergy.recommend.scoring import batch_recommend
    from mtg_synergy.recommend.forge_features import ForgeFeatureContext

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    commanders = get_top_commanders(conn, top_n)
    print(f"  Validating {len(commanders)} commanders, 30 recs each...")

    all_recs = batch_recommend(conn, commanders, top_n=30, verbose=False)
    ctx = ForgeFeatureContext(conn, preload_edges=False)

    results = validate_commanders(conn, commanders, ctx, all_recs)

    total_issues = 0
    for cmdr, issues in results:
        total_issues += len(issues)
        for rank, name, score, warnings in issues:
            print(f"  WARNING: {cmdr} #{rank}: {name} — {'; '.join(warnings)}")

    total_recs = len(commanders) * 30
    rate = total_issues / max(total_recs, 1) * 100
    print(f"\n  Validation: {total_issues}/{total_recs} flags ({rate:.1f}%)")

    if total_issues == 0:
        print("  PASS — no suspicious recommendations")
    else:
        print(f"  FAIL — {total_issues} suspicious card(s) found")
        print("  Run: python3 scripts/validate_recommendations.py --top 50")
        print("  to investigate and fix scoring penalties")

    conn.close()
    return total_issues


if __name__ == "__main__":
    main()
