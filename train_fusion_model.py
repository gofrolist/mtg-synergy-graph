#!/usr/bin/env python3
"""Train forge-only LightGBM model for card recommendation.

LambdaRank GBM trained on EDHREC labels with 105 forge-native features.
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
    compute_batch_features,
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
    "cmdr_p1p1_card_no_counters", # [78] cmdr uses P1P1 but card has zero counter interaction
    "func_produces_amplifies",    # [79] cmdr produces X, card amplifies X (dot product)
    "func_requires_produces",     # [80] cmdr requires trigger X, card produces X
    "func_card_requires_cmdr",    # [81] card requires trigger X, cmdr produces X
    "func_full_cosine",           # [82] overall functional fingerprint similarity
    # ── 2-hop graph features ──
    "cmdr_2hop_count",           # [83] commander's causal partners that connect to this card
    "cmdr_2hop_ratio",           # [84] 2-hop count / hub score (transitive vs generic)
    # ── Card quality / noise suppression ──
    "forge_ability_richness",    # [85] total distinct mechanical components (Forge-native)
    "card_in_forge",             # [84] card has Forge ability data (vs all-zero features)
    "card_strategy_count",       # [85] number of strategies assigned to card
    "deck_tag_count",            # [86] Forge deck-building AI tags (has+hints+needs)
    "edhrec_deck_pct",           # [87] fraction of EDHREC commanders including this card
    # ── Theme-based features ──
    "cmdr_equipment_theme",       # [88] commander wants equipment
    "card_equipment_payoff",      # [84] card is equipment or cares about equipment
    "equipment_theme_match",      # [85] both align on equipment
    "cmdr_enchantress_theme",     # [86] commander wants enchantments
    "card_enchantress_payoff",    # [87] card triggers on or cares about enchantments
    "enchantress_theme_match",    # [88] both align on enchantress
    "cmdr_defender_theme",        # [89] commander cares about defenders/walls
    "card_has_defender",          # [90] card has defender or is a Wall
    "defender_theme_match",       # [91] both align on defender
    "card_is_etb_doubler",       # [92] Panharmonicon-class ETB doubler
    "cmdr_etb_density",          # [93] how many ETB-related verbs/triggers commander has
    "etb_doubler_match",         # [94] doubler × commander ETB density
    "tribal_lord_for_cmdr",      # [95] card is a lord/anthem for commander's creature type
    "tribal_member_of_cmdr",     # [96] card IS the creature type commander cares about
    "tribal_synergy_depth",      # [97] combined tribal signal (subtype + lord + token + filter)
]


_ALLOWED_SLUG_TABLES = frozenset({"edhrec_average_decks", "edhrec_card_synergy"})


def _resolve_slugs_to_oids(conn, table="edhrec_average_decks"):
    """Resolve EDHREC commander slugs to oracle_ids via Python string matching.

    Builds a normalized name→oid lookup once, then matches all slugs in-memory
    instead of doing N separate SQL LIKE queries. ~100x faster for ~3000 slugs.

    Returns (slug_to_oid, name_to_oid) dicts.
    """
    if table not in _ALLOWED_SLUG_TABLES:
        raise ValueError(f"table must be one of {_ALLOWED_SLUG_TABLES}, got {table!r}")

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
                     card_has_tags=None, cmdr_popularity=None):
    """Sample negative pairs (cards NOT in a commander's EDHREC page).

    For each commander, samples ratio * |positives| negative cards in 3 tiers:
    - 1/3 strategy/subtype overlap (same tribe/archetype, wrong card)
    - 1/3 tag overlap (same has-tags as commander, e.g., Ability$Counters)
    - 1/3 random color-legal cards

    Popular commanders (>=1000 decks) get 4:1 negative ratio instead of 3:1,
    giving the model more hard negatives for well-known commanders.

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


def build_forge_feature_matrix(pairs_by_cmdr, tower_model_path=None, verbose=True):
    """Build forge-only feature matrix (no EDHREC, no tower, no embeddings).

    Pure Forge-native features: 105 features from causal graph, strategies,
    card mechanics, and Forge ability data.

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


def train_forge_gbm(X, y, cmdr_ids, tune=False, quick=False, cmdr_pop_weight=None):
    """Train LightGBM LambdaRank on forge features with graded relevance.

    Uses synergy-based relevance grades (0-5) instead of binary labels.
    LambdaRank optimizes NDCG, teaching the model to rank high-synergy
    cards above low-synergy ones.

    cmdr_pop_weight: dict[cmdr_idx -> float] popularity-based weight per commander.
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

    # Commander popularity weighting: popular commanders have more reliable labels
    if cmdr_pop_weight:
        pop_w = np.array([cmdr_pop_weight.get(int(c), 1.0) for c in cmdr_ids], dtype=np.float32)
        w *= pop_w
        n_boosted = int((pop_w > 1.0).sum())
        print(f"  Sample weights: grade 4→{_grade_weight_arr[4]}x, grade 5→{_grade_weight_arr[5]}x, "
              f"popularity boost on {n_boosted}/{len(w)} samples")
    else:
        print(f"  Sample weights: grade 4→{_grade_weight_arr[4]}x, grade 5→{_grade_weight_arr[5]}x")

    n_folds = 1 if quick else 3
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
        "label_gain": [0, 1, 3, 6, 15, 30],  # 6 grades: neg, anti-syn, low, moderate, top, high-syn
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
        train_group = _build_group_array(cmdr_ids, ti)
        test_group = _build_group_array(cmdr_ids, vi)

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
    avg_best = int(np.mean(fold_best_iters) * 1.1)
    print(f"  Final model: {avg_best} rounds (avg CV best x 1.1)")
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
        n_by_grade[grade] = n_by_grade.get(grade, 0) + 1

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

    # Commander popularity: max num_decks per commander (for weighting + neg ratio)
    cmdr_popularity = {}
    for row in conn.execute(
        "SELECT commander_slug, MAX(num_decks) FROM edhrec_card_synergy "
        "WHERE num_decks IS NOT NULL GROUP BY commander_slug"
    ):
        cmdr_oid = slug_to_oid.get(row[0])
        if cmdr_oid:
            cmdr_popularity[cmdr_oid] = row[1] or 0

    n_pop = sum(1 for v in cmdr_popularity.values() if v >= 1000)
    print(f"\n  Commander popularity: {n_pop} popular (>=1000 decks), "
          f"{len(cmdr_popularity) - n_pop} niche")

    print(f"\nSampling negatives (ratio={3}, 1/3 subtype + 1/3 tag + 1/3 random)...")
    neg_pairs = sample_negatives(
        positives_for_neg, all_card_oids, card_colors, card_colors, ratio=3,
        card_strats=card_strats, card_subtypes=card_subtypes,
        card_has_tags=card_has_tags, cmdr_popularity=cmdr_popularity,
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

    return positives_by_cmdr, cmdr_popularity


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
    args = parser.parse_args()

    print("=" * 60)
    print("FORGE-ONLY MODEL \u2014 EDHREC labels, forge features")
    print("=" * 60)

    # Check for cached feature matrix (skip 3+ min rebuild)
    cache_path = os.path.join(DATA_DIR, "forge_features_cache.npz")
    pop_cache_path = os.path.join(DATA_DIR, "forge_cmdr_pop_cache.npz")
    cmdr_pop_weight = None

    if os.path.exists(cache_path) and not args.rebuild_features:
        print(f"\nLoading cached feature matrix from {cache_path}")
        cached = np.load(cache_path)
        X_forge = cached["X"]
        y_forge = cached["y"]
        cmdr_ids_forge = cached["cmdr_ids"]
        print(f"  Matrix: {X_forge.shape}, positives: {int((y_forge > 0).sum())}, "
              f"negatives: {int((y_forge == 0).sum())}")
        # Load popularity weights cache
        if os.path.exists(pop_cache_path):
            pop_cached = np.load(pop_cache_path)
            cmdr_pop_weight = dict(zip(pop_cached["cmdr_idx"].astype(int),
                                       pop_cached["weight"].astype(float)))
            print(f"  Popularity weights: {len(cmdr_pop_weight)} commanders")
    else:
        # Use EDHREC labels (external ground truth) with forge features
        # Self-supervised causal labels overfit because features ARE the causal graph
        conn = sqlite3.connect(DB_PATH)
        pairs_by_cmdr, cmdr_popularity = _load_pairs_for_features(conn)
        conn.close()

        print("\n--- Building FORGE-ONLY feature matrix ---")
        X_forge, y_forge, cmdr_ids_forge = build_forge_feature_matrix(pairs_by_cmdr)

        # Build cmdr_idx → popularity weight mapping
        # cmdr_ids are sequential ints assigned by sorted(pairs_by_cmdr.keys())
        cmdr_oids_ordered = sorted(pairs_by_cmdr.keys())
        cmdr_pop_weight = {}
        # Disabled: popularity sample weighting hurts NDCG.
        # The extra negatives for popular commanders (4:1 vs 3:1) are the real win.
        # for i, cmdr_oid in enumerate(cmdr_oids_ordered):
        #     decks = cmdr_popularity.get(cmdr_oid, 0)
        #     if decks >= 1000: cmdr_pop_weight[i] = 1.3
        #     elif decks >= 100: cmdr_pop_weight[i] = 1.0
        #     else: cmdr_pop_weight[i] = 0.8
        n_high = sum(1 for w in cmdr_pop_weight.values() if w > 1.0)
        n_low = sum(1 for w in cmdr_pop_weight.values() if w < 1.0)
        print(f"  Popularity weights: {n_high} high (1.5x), "
              f"{len(cmdr_pop_weight) - n_high - n_low} medium (1.0x), "
              f"{n_low} niche (0.7x)")

        # Cache for fast reruns
        np.savez(cache_path, X=X_forge, y=y_forge, cmdr_ids=cmdr_ids_forge)
        np.savez(pop_cache_path,
                 cmdr_idx=np.array(list(cmdr_pop_weight.keys())),
                 weight=np.array(list(cmdr_pop_weight.values())))
        print(f"  Feature matrix cached to {cache_path}")

    # Train forge GBM
    print("\n" + "=" * 60)
    print("Training FORGE-ONLY model (EDHREC labels, forge features)")
    print("=" * 60)
    _, forge_scores = train_forge_gbm(X_forge, y_forge, cmdr_ids_forge,
                                      tune=args.tune, quick=args.quick,
                                      cmdr_pop_weight=cmdr_pop_weight)

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
