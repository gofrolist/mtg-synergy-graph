#!/usr/bin/env python3
"""Diagnostic: compare GBM feature vectors for our top recs vs EDHREC misses.

Usage: uv run python3 scripts/diagnose_features.py
"""
import json
import sqlite3
import sys

import numpy as np
import lightgbm as lgb

from mtg_synergy.config import DB_PATH, ARTIFACT_DIR
from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext, CmdrFeatureContext, compute_batch_features,
)

# ── Feature names (must match train_fusion_model.py) ──
FEATURE_NAMES = [
    "causal_cmdr_to_card", "causal_card_to_cmdr", "deck_edge_count",
    "mech_cosine", "forge_ability_cosine",
    "phase_match", "has_phase_trigger",
    "tribal_match", "cmc",
    "deck_exact_edge_ratio", "causal_composite", "card_hub_score", "deck_exact_count",
    "forge_type_synergy", "cmdr_forge_type_match", "forge_ability_depth",
    "forge_anti_tribal", "forge_verb_alignment",
    "counter_type_match", "ability_type_ratio_T", "ability_type_ratio_A",
    "zone_alignment", "target_alignment", "forge_keyword_synergy",
    "activated_ability_count", "is_permanent_effect", "is_temporary_effect",
    "duration_match", "combat_damage_flag", "effect_zone_match",
    "scales_with_board", "is_secondary_trigger", "gain_control",
    "granted_keyword_count", "condition_count",
    "deck_hints_to_has", "deck_has_to_hints", "deck_needs_to_has",
    "deck_has_overlap", "deck_hints_overlap",
    "damage_scales", "draw_scales", "life_scales", "produces_mana",
    "granted_ability_match", "token_amount_variable",
    "total_ability_count", "triggered_ability_count",
    "token_power_toughness", "token_keyword_count",
    "zone_graveyard_interact", "ability_density",
    "cmdr_needs_to_card_has", "card_needs_satisfied", "needs_rarity",
    "put_counter_ratio", "cmdr_counter_x_put_counter", "cmdr_p1p1_card_no_counters",
    "func_produces_amplifies", "func_requires_produces",
    "func_card_requires_cmdr", "func_full_cosine",
    "cmdr_2hop_count", "cmdr_2hop_ratio",
    "forge_ability_richness", "mech_nonzero_count", "deck_tag_count", "edhrec_deck_pct",
    "tribal_lord_for_cmdr", "tribal_member_of_cmdr", "tribal_synergy_depth",
    "verb_demand_match", "type_demand_match",
    "mech_board_fwd", "mech_board_rev",
    "mech_resource_fwd", "mech_resource_rev",
    "mech_disruption_fwd", "mech_disruption_rev",
    "mech_tempo_fwd", "mech_tempo_rev",
    "mech_utility_fwd", "mech_utility_rev",
    "mech_zones_fwd", "mech_zones_rev",
    "mech_themes_fwd", "mech_themes_rev",
    "mech_tribal_fwd", "mech_tribal_rev",
    "affected_scope_ratio", "pump_magnitude", "pump_is_variable",
    "type_change_tribal_match",
    "graph_neighbor_overlap",
    "cost_feeds_cmdr", "trigger_specificity", "mech_density", "graph_pagerank",
]

COMMANDER = "Korvold, Fae-Cursed King"
EDHREC_MISSES = [
    "Mayhem Devil",
    "Pitiless Plunderer",
    "Chatterfang, Squirrel General",
    "Academy Manufactor",
    "Tireless Provisioner",
]


def resolve_card(conn, name):
    """Return (oracle_id, name, type_line, cmc) for a card name."""
    row = conn.execute(
        "SELECT oracle_id, name, type_line, cmc FROM cards WHERE LOWER(name) = LOWER(?)",
        (name,),
    ).fetchone()
    if not row:
        return None
    return {"oracle_id": row[0], "name": row[1], "type_line": row[2] or "", "cmc": row[3] or 0}


def main():
    conn = sqlite3.connect(str(DB_PATH))

    # Load model
    model_path = str(ARTIFACT_DIR / "fusion_model_forge.lgb")
    gbm = lgb.Booster(model_file=model_path)

    # Resolve commander
    cmdr_row = conn.execute(
        "SELECT oracle_id, color_identity FROM cards WHERE LOWER(name) = LOWER(?)",
        (COMMANDER,),
    ).fetchone()
    if not cmdr_row:
        print(f"Commander not found: {COMMANDER}")
        sys.exit(1)
    cmdr_oid = cmdr_row[0]
    color_identity = set(json.loads(cmdr_row[1] or "[]"))

    print(f"Commander: {COMMANDER} ({cmdr_oid})")
    print(f"Colors: {color_identity}\n")

    # Build context
    print("Loading ForgeFeatureContext...", flush=True)
    ctx = ForgeFeatureContext(conn, preload_edges=True)
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oids=[cmdr_oid], deck_oids=set())

    # Get our top recommendations first
    print("Computing scores for all color-legal cards...", flush=True)
    all_cards = []
    for row in conn.execute(
        "SELECT oracle_id, name, type_line, cmc FROM cards "
        "WHERE type_line NOT LIKE '%Token%' AND legal_commander = 1"
    ):
        ci_row = conn.execute(
            "SELECT color_identity FROM cards WHERE oracle_id = ?", (row[0],)
        ).fetchone()
        ci = set(json.loads(ci_row[0] or "[]")) if ci_row else set()
        if ci <= color_identity and row[0] != cmdr_oid:
            all_cards.append({
                "oracle_id": row[0], "name": row[1],
                "type_line": row[2] or "", "cmc": row[3] or 0,
            })

    card_oids = [c["oracle_id"] for c in all_cards]
    card_cmcs = np.array([float(c["cmc"]) for c in all_cards], dtype=np.float32)
    X_all = compute_batch_features(card_oids, card_cmcs, ctx, cmdr_ctx).astype(np.float64)
    scores_all = gbm.predict(X_all, raw_score=True)

    # Find our top 5
    top_indices = np.argsort(scores_all)[::-1][:5]
    our_top = [(all_cards[i]["name"], all_cards[i]["oracle_id"], scores_all[i]) for i in top_indices]

    print("\n" + "=" * 100)
    print("OUR TOP 5 RECOMMENDATIONS (raw GBM score, before penalties/bonuses)")
    print("=" * 100)

    # Gather cards for feature display
    display_cards = []
    for name, oid, score in our_top:
        cd = next(c for c in all_cards if c["oracle_id"] == oid)
        display_cards.append((name, oid, score, "OUR_TOP", cd))

    print("\n" + "=" * 100)
    print("EDHREC HIGH SYNERGY CARDS WE MISS")
    print("=" * 100)

    for miss_name in EDHREC_MISSES:
        cd = resolve_card(conn, miss_name)
        if not cd:
            print(f"  {miss_name}: NOT FOUND IN DB")
            continue
        # Find its score
        try:
            idx = card_oids.index(cd["oracle_id"])
            score = scores_all[idx]
            rank = int((scores_all > score).sum()) + 1
        except ValueError:
            score = float("nan")
            rank = -1
        display_cards.append((miss_name, cd["oracle_id"], score, f"EDHREC_MISS (rank #{rank})", cd))

    # Compute features for all display cards
    disp_oids = [oid for _, oid, _, _, _ in display_cards]
    disp_cmcs = np.array([float(cd["cmc"]) for _, _, _, _, cd in display_cards], dtype=np.float32)
    X_disp = compute_batch_features(disp_oids, disp_cmcs, ctx, cmdr_ctx).astype(np.float64)
    disp_scores = gbm.predict(X_disp, raw_score=True)

    # Print feature vectors
    for i, (name, oid, _, label, cd) in enumerate(display_cards):
        score = disp_scores[i]
        print(f"\n{'─' * 100}")
        print(f"  {label}: {name}  (cmc={cd['cmc']}, type={cd['type_line']})")
        print(f"  GBM raw score: {score:.4f}")
        print(f"  {'─' * 90}")
        feats = X_disp[i]
        for j, fname in enumerate(FEATURE_NAMES):
            val = feats[j]
            marker = ""
            if abs(val) > 0.01:
                marker = " ◄"  # highlight non-zero features
            print(f"    F{j:02d} {fname:>35s} = {val:10.4f}{marker}")

    # Summary: average feature values for our top vs misses
    print(f"\n{'=' * 100}")
    print("FEATURE COMPARISON SUMMARY (avg our_top_5 vs avg edhrec_misses)")
    print(f"{'=' * 100}")

    our_X = X_disp[:5]
    miss_X = X_disp[5:]
    our_avg = our_X.mean(axis=0)
    miss_avg = miss_X.mean(axis=0)

    print(f"\n  {'Feature':>35s}  {'Our Top5':>10s}  {'EDHREC Miss':>12s}  {'Diff':>10s}")
    print(f"  {'─' * 75}")

    # Sort by absolute difference
    diffs = miss_avg - our_avg
    sorted_idx = np.argsort(-np.abs(diffs))

    for j in sorted_idx:
        d = diffs[j]
        if abs(d) < 0.001 and abs(our_avg[j]) < 0.001 and abs(miss_avg[j]) < 0.001:
            continue  # skip features that are zero for both
        direction = "▲" if d > 0 else "▼" if d < 0 else " "
        print(f"  F{j:02d} {FEATURE_NAMES[j]:>35s}  {our_avg[j]:10.4f}  {miss_avg[j]:12.4f}  {d:+10.4f} {direction}")

    # Also show the Forge profile for missed cards (verbs, triggers)
    print(f"\n{'=' * 100}")
    print("FORGE PROFILES FOR EDHREC MISSES (verbs, triggers, keywords)")
    print(f"{'=' * 100}")

    cmdr_profile = cmdr_ctx.cmdr_profile
    print(f"\nCommander profile:")
    for key in sorted(cmdr_profile.keys()):
        val = cmdr_profile.get(key)
        if val and val != set() and val is not False and val != 0:
            print(f"  {key}: {val}")

    for miss_name in EDHREC_MISSES:
        cd = resolve_card(conn, miss_name)
        if not cd:
            continue
        profile = ctx._forge_profiles.get(cd["oracle_id"], {})
        print(f"\n  {miss_name}:")
        for key in sorted(profile.keys()):
            if key.startswith("_"):
                continue
            val = profile.get(key)
            if val and val != set() and val is not False and val != 0:
                print(f"    {key}: {val}")

    conn.close()


if __name__ == "__main__":
    main()
