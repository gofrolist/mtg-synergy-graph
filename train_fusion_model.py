#!/usr/bin/env python3
"""Train forge-only LightGBM model for card recommendation.

LambdaRank GBM trained on EDHREC labels with 71 forge-native features.
No tower model, no embeddings, no neural network.

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

import math

from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext,
    CmdrFeatureContext,
    compute_card_features,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

FORGE_FEATURE_NAMES = [
    "causal_cmdr_to_card",   # [0] commander → card edge strength
    "causal_card_to_cmdr",   # [1] card → commander edge strength
    "causal_bidirectional",  # [2] 1.0 if both directions have edges
    "causal_event_diversity", # [3] distinct event types connecting cmdr↔card
    "deck_edge_count",       # [4] deck cards with causal edges to this card
    "strategy_overlap",      # [5] shared strategies count
    "strategy_cosine",       # [6] strategy vector cosine similarity
    "forge_ability_cosine",  # [7] Forge ability vector cosine similarity
    "phase_match",           # [8] cmdr and card trigger in same phase window
    "has_phase_trigger",     # [9] card has any phase-based trigger
    "tribal_match",          # [10] creature type match
    "type_creature",         # [11] card is a Creature
    "type_instant_sorcery",  # [12] card is Instant or Sorcery
    "type_artifact",         # [13] card is an Artifact
    "type_enchantment",      # [14] card is an Enchantment
    "type_land",             # [15] card is a Land
    "type_planeswalker",     # [16] card is a Planeswalker
    "cmc",                   # [17] mana cost
    "deck_exact_edge_ratio", # [18] fraction of deck edges with exact filter precision
    "cmdr_exact_edge",       # [19] 1.0 if any exact-precision edge to commander
    "causal_composite",      # [20] combined causal signal (strength × events × exact)
    "card_hub_score",        # [21] total unique causal neighbors (connectedness)
    "deck_exact_count",      # [22] absolute count of exact-precision deck connections
    "forge_type_synergy",    # [23] card's Forge trigger_filter/target references cmdr's creature type
    "cmdr_forge_type_match", # [24] commander's Forge trigger_filter/target references card's type
    "shared_forge_mechanics", # [25] shared Forge verbs/trigger_modes/keywords count
    "forge_ability_depth",  # [26] total distinct mechanical components (verbs+triggers+keywords+counters)
    "forge_anti_tribal",    # [27] card's Forge trigger_filter requires conflicting creature subtype
    "forge_verb_alignment", # [28] card's verbs produce events that commander's triggers consume
    "forge_mech_fwd",       # [29] card produces what commander consumes (mechanics vector dot)
    "forge_mech_rev",       # [30] commander produces what card consumes (mechanics vector dot)
    "counter_type_match",   # [31] card uses same counter type as commander
    "ability_type_ratio_T", # [32] fraction of card's abilities that are Triggered
    "ability_type_ratio_A", # [33] fraction of card's abilities that are Activated
    "zone_alignment",       # [34] card's trigger zones match commander's zones
    "target_alignment",     # [35] card targets what commander produces
    "forge_keyword_synergy", # [36] card keywords synergize with cmdr mechanics
    "activated_ability_count", # [37] number of activated abilities
    "granted_keyword_synergy", # [38] card grants keywords cmdr cares about
    "shared_conditions",     # [39] card and cmdr share conditions (need same board state)
    "is_permanent_effect",   # [40] card produces permanent effects (counters, not pump)
    "is_temporary_effect",   # [41] card effects are temporary (until EOT)
    "duration_match",        # [42] card and cmdr share duration type
    "combat_damage_flag",    # [43] card has combat damage triggers (voltron)
    "effect_zone_match",     # [44] card works from zones cmdr cares about
    "scales_with_board",     # [45] card P/T or effect scales with game state
    "grants_types_match",    # [46] card creates types matching cmdr's subtypes
    "is_secondary_trigger",  # [47] card triggers on multiple events
    "gain_control",          # [48] card steals permanents
    "granted_keyword_count", # [49] how many keywords card grants
    "condition_count",       # [50] how many conditions card requires
    "deck_hints_to_has",     # [51] cmdr hints X, card has X (Forge deck-building AI)
    "deck_has_to_hints",     # [52] card hints X, cmdr has X
    "deck_needs_to_has",     # [53] card needs X, cmdr has X
    "deck_has_overlap",      # [54] shared has tags (theme alignment)
    "deck_hints_overlap",    # [55] both want same deck themes
    "damage_scales",         # [56] card damage is X/Y (scales with game state)
    "draw_scales",           # [57] card draw is X/Y (scales with game state)
    "life_scales",           # [58] card life effect is X/Y
    "produces_mana",         # [59] card produces mana (mana rock/dork)
    "counter_num_variable",  # [60] card places X/Y counters (scales)
    "grants_abilities",      # [61] card grants abilities to other permanents
    "token_amount_variable", # [62] card creates X tokens (scales)
    "total_ability_count",     # [63] total abilities per card (combo potential)
    "triggered_ability_count", # [64] triggered ability count (ordinal)
    "token_power_toughness",   # [65] max P+T of tokens created
    "token_keyword_count",     # [66] max keywords on tokens created
    "zone_graveyard_interact", # [67] both card+cmdr interact with graveyard
    "zone_exile_interact",     # [68] both card+cmdr interact with exile
    "ability_density",         # [69] abilities per mana cost (efficiency)
    "cmdr_needs_to_card_has",  # [70] commander needs X, card has X
    "card_needs_satisfied",    # [71] fraction of card's needs met by commander
    "needs_rarity",            # [72] how rare/specific are card's needs
    "temp_buff_counter_cmdr",  # [73] card gives temporary buffs, cmdr wants permanent counters
    "put_counter_ratio",       # [74] fraction of buff verbs that are PutCounter (not Pump)
    "cmdr_counter_x_put_counter", # [75] commander uses +1/+1 counters AND card places counters
    "static_anthem_counter_cmdr", # [76] static anthem (not counters) but cmdr wants counters
    "counters_on_lands",       # [77] card places counters on lands (earthbend etc.)
]


def _resolve_slugs_to_oids(conn, table="edhrec_average_decks"):
    """Resolve EDHREC commander slugs to oracle_ids via Python string matching.

    Builds a normalized name→oid lookup once, then matches all slugs in-memory
    instead of doing N separate SQL LIKE queries. ~100x faster for ~3000 slugs.

    Returns (slug_to_oid, name_to_oid) dicts.
    """
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
                     ratio=3, card_strats=None, card_subtypes=None,
                     card_has_tags=None):
    """Sample negative pairs (cards NOT in a commander's EDHREC page).

    For each commander, samples ratio * |positives| negative cards in 3 tiers:
    - 1/3 strategy/subtype overlap (same tribe/archetype, wrong card)
    - 1/3 tag overlap (same has-tags as commander, e.g., Ability$Counters)
    - 1/3 random color-legal cards

    The tag-overlap tier teaches the model that sharing a broad tag like
    has=Ability$Counters isn't enough — the specific mechanics must match.

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


def build_forge_feature_matrix(pairs_by_cmdr, tower_model_path=None, verbose=True):
    """Build forge-only feature matrix (no EDHREC, no tower, no embeddings).

    Pure Forge-native features: 71 features from causal graph, strategies,
    card mechanics, and Forge ability data.

    Delegates per-card feature computation to the shared forge_features module
    (ForgeFeatureContext / CmdrFeatureContext / compute_card_features).
    """
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

    # ── Shared forge feature context (strategies, phases, mechanics) ──
    ctx = ForgeFeatureContext(conn, preload_edges=True, preload_strength=True)

    if verbose:
        print(f"  Strategy vector: {ctx._n_strats} strategies")
        print(f"  Forge ability vectors: {ctx._n_abilities} vocab, {len(ctx._ability_vectors)} cards with vectors")

    # ── Pre-allocate output arrays (no intermediate all_rows list) ────
    cmdr_oids_ordered = sorted(pairs_by_cmdr.keys())
    cmdr_to_idx = {oid: i for i, oid in enumerate(cmdr_oids_ordered)}

    N = sum(len(pairs_by_cmdr[c]) for c in cmdr_oids_ordered)
    X = np.zeros((N, len(FORGE_FEATURE_NAMES)), dtype=np.float32)
    y = np.zeros(N, dtype=np.float32)
    cmdr_ids = np.zeros(N, dtype=np.int32)

    n_cmdrs = len(cmdr_oids_ordered)
    row_idx = 0

    for ci, cmdr_oid in enumerate(cmdr_oids_ordered):
        if verbose and ((ci + 1) % 200 == 0 or ci == 0):
            print(f"  Commander {ci+1}/{n_cmdrs}...", flush=True)

        pairs = pairs_by_cmdr.get(cmdr_oid, [])
        if not pairs:
            continue

        deck_oids_for_cmdr = {oid for oid, lbl in pairs if lbl > 0}
        cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids_for_cmdr)

        cmdr_type_line = card_meta.get(cmdr_oid, {}).get("type_line", "")
        if "\u2014" in cmdr_type_line:
            try:
                cmdr_ctx.cmdr_subtypes = {
                    s.lower() for s in cmdr_type_line.split("\u2014")[1].strip().split()
                }
            except (IndexError, AttributeError):
                pass

        cmdr_idx = cmdr_to_idx[cmdr_oid]
        for card_oid, label in pairs:
            meta = card_meta.get(card_oid, {})
            feats = compute_card_features(
                card_oid, meta.get("type_line", ""), float(meta.get("cmc", 0.0)),
                ctx, cmdr_ctx,
            )
            X[row_idx] = feats
            y[row_idx] = float(label)
            cmdr_ids[row_idx] = cmdr_idx
            row_idx += 1

    X = X[:row_idx]
    y = y[:row_idx]
    cmdr_ids = cmdr_ids[:row_idx]
    conn.close()

    if verbose:
        print(f"\nForge feature matrix: {X.shape}")
        n_pos = int((y > 0).sum())
        print(f"  Positive (grade>0): {n_pos}, Negative (grade=0): {int(len(y) - n_pos)}")
        if y.max() > 1:
            for g in range(int(y.max()), 0, -1):
                print(f"    Grade {g}: {int((y == g).sum())}")

        print(f"  Commanders: {len(cmdr_oids_ordered)}")
        print(f"\nPer-feature statistics:")
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
    groups = []
    prev = sub_cmdrs[0]
    count = 1
    for i in range(1, len(sub_cmdrs)):
        if sub_cmdrs[i] == prev:
            count += 1
        else:
            groups.append(count)
            prev = sub_cmdrs[i]
            count = 1
    groups.append(count)
    return groups


def train_forge_gbm(X, y, cmdr_ids):
    """Train LightGBM LambdaRank on forge features with graded relevance.

    Uses synergy-based relevance grades (0-4) instead of binary labels.
    LambdaRank optimizes NDCG, teaching the model to rank high-synergy
    cards above low-synergy ones.

    Saves to fusion_model_forge.lgb. Returns (model, cv_scores).
    """
    import lightgbm as lgb
    import joblib

    # Sort data by commander ID (required for LambdaRank groups)
    sort_order = np.argsort(cmdr_ids)
    X = X[sort_order]
    y = y[sort_order]
    cmdr_ids = cmdr_ids[sort_order]

    is_graded = y.max() > 1  # Check if we have graded labels
    if not is_graded:
        print("  WARNING: Binary labels detected, using classification instead of ranking")

    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [10, 30],
        "num_leaves": 255,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_samples": 20,
        "min_data_in_bin": 5,
        "verbose": -1,
        "label_gain": [0, 1, 3, 6, 15, 30],  # 6 grades: neg, anti-syn, low, moderate, top, high-syn
    }

    # Per-grade sample weights: upweight rare high-synergy examples
    # Grade 5 (High Synergy) and 4 (Top Cards) are ~12x rarer than grades 0-3
    _grade_weights = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 2.0, 5: 3.0}
    w = np.array([_grade_weights.get(int(g), 1.0) for g in y], dtype=np.float32)
    print(f"  Sample weights: grade 4→{_grade_weights[4]}x, grade 5→{_grade_weights[5]}x")

    splits = make_cv_splits(cmdr_ids, n_folds=3)
    fold_ndcgs = []
    fold_best_iters = []
    for fold_i, (train_idx, test_idx) in enumerate(splits):
        # Ensure data within each fold is sorted by commander
        train_sort = np.argsort(cmdr_ids[train_idx])
        test_sort = np.argsort(cmdr_ids[test_idx])
        ti = train_idx[train_sort]
        vi = test_idx[test_sort]

        train_group = _build_group_array(cmdr_ids, ti)
        test_group = _build_group_array(cmdr_ids, vi)

        train_data = lgb.Dataset(X[ti], label=y[ti], weight=w[ti], group=train_group,
                                 feature_name=FORGE_FEATURE_NAMES, free_raw_data=False)
        eval_data = lgb.Dataset(X[vi], label=y[vi], weight=w[vi], group=test_group,
                                reference=train_data, feature_name=FORGE_FEATURE_NAMES,
                                free_raw_data=False)

        booster = lgb.train(
            params, train_data,
            num_boost_round=2000,
            valid_sets=[eval_data],
            callbacks=[lgb.early_stopping(50, verbose=False),
                       lgb.log_evaluation(0)],
        )

        # Compute NDCG@30 manually for reporting
        preds = booster.predict(X[vi])
        test_cmdr_vals = cmdr_ids[vi]

        ndcg30_scores = []
        start = 0
        for g in test_group:
            end = start + g
            pred_slice = preds[start:end]
            label_slice = y[vi][start:end]
            # Compute NDCG@30
            k = min(30, g)
            top_k = np.argsort(-pred_slice)[:k]
            dcg = sum(params["label_gain"][int(label_slice[j])] / np.log2(i + 2)
                      for i, j in enumerate(top_k))
            ideal_sorted = sorted(label_slice, reverse=True)[:k]
            idcg = sum(params["label_gain"][int(ideal_sorted[i])] / np.log2(i + 2)
                       for i in range(len(ideal_sorted)))
            ndcg = dcg / idcg if idcg > 0 else 0.0
            ndcg30_scores.append(ndcg)
            start = end

        avg_ndcg = np.mean(ndcg30_scores) if ndcg30_scores else 0.0
        fold_ndcgs.append(avg_ndcg)
        best_iter = booster.best_iteration or booster.current_iteration()
        fold_best_iters.append(best_iter)
        print(f"  Fold {fold_i+1}: NDCG@30={avg_ndcg:.4f} "
              f"({best_iter} rounds)")

    print(f"  Mean NDCG@30: {np.mean(fold_ndcgs):.4f}")

    # Train final model on all data, using CV-derived round count
    avg_best = int(np.mean(fold_best_iters) * 1.1)
    print(f"  Final model: {avg_best} rounds (avg CV best x 1.1)")
    all_group = _build_group_array(cmdr_ids, np.arange(len(cmdr_ids)))
    all_data = lgb.Dataset(X, label=y, weight=w, group=all_group,
                           feature_name=FORGE_FEATURE_NAMES)
    final_booster = lgb.train(params, all_data, num_boost_round=avg_best)

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
    splits = []
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else len(unique_cmdrs)
        test_cmdrs = set(unique_cmdrs[start:end])
        test_idx = np.array([j for j, c in enumerate(cmdr_ids) if c in test_cmdrs])
        train_idx = np.array([j for j, c in enumerate(cmdr_ids) if c not in test_cmdrs])
        splits.append((train_idx, test_idx))
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
    oid_to_slug = {v: k for k, v in slug_to_oid.items()}

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
    n_by_grade = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
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

        if section == "High Synergy Cards":
            grade = 5
        elif section == "Top Cards":
            grade = 4
        elif synergy is not None and synergy < 0:
            grade = 1
        elif synergy is not None and synergy > 0.1:
            grade = 3
        else:
            grade = 2

        # Boost grade if card is in the average deck (practical validation)
        if (slug, card_name) in avg_deck_set and grade < 5 and grade >= 2:
            grade = min(grade + 1, 5)
            n_boosted += 1

        positives_by_cmdr.setdefault(cmdr_oid, []).append((card_oid, grade))
        n_by_grade[grade] += 1

    total_pairs = sum(len(v) for v in positives_by_cmdr.values())
    print(f"  Synergy pairs: {total_pairs:,} across {len(positives_by_cmdr)} commanders")
    print(f"  Boosted by avg deck membership: {n_boosted:,}")
    for g in sorted(n_by_grade.keys(), reverse=True):
        print(f"    Grade {g}: {n_by_grade[g]:,}")

    # Filter staples from top grades
    staple_threshold = 0.30
    staple_oids = {oid for oid, cnt in card_cmdr_count.items()
                   if cnt / cmdr_count > staple_threshold}
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
    basic_land_oids = set()
    for name in basic_land_names:
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        if row:
            basic_land_oids.add(row[0])

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

    print("\nSampling negatives (ratio=3, 1/3 subtype + 1/3 tag + 1/3 random)...")
    neg_pairs = sample_negatives(
        positives_for_neg, all_card_oids, card_colors, card_colors, ratio=3,
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
    print(f"\n  Final grade distribution:")
    for g in sorted(grade_counts.keys(), reverse=True):
        print(f"    Grade {g}: {grade_counts[g]:,}")

    return positives_by_cmdr


def main():
    parser = argparse.ArgumentParser(description="Train forge LightGBM model")
    parser.add_argument(
        "--forge-only",
        action="store_true",
        default=True,
        help="(default) Train forge-only GBM on EDHREC labels with forge features",
    )
    parser.add_argument(
        "--rebuild-features",
        action="store_true",
        help="Force rebuild feature matrix (ignore cache)",
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
    _, forge_scores = train_forge_gbm(X_forge, y_forge, cmdr_ids_forge)

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
    print(f"\n  Forge model feature importance:")
    total_imp = sum(importances)
    for name, imp in sorted(zip(names, importances), key=lambda x: -x[1]):
        print(f"    {name:25s} {imp:6d} ({imp/total_imp*100:5.1f}%)")


if __name__ == "__main__":
    main()
