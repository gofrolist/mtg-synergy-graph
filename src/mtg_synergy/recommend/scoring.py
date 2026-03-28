"""Forge-only scoring — scores card candidates using the forge GBM model.

Two public functions:
  color_identity_filter() — returns all color-legal non-token cards
  score_forge_candidates() — batch-scores candidates with forge LightGBM (63 features)

No tower model, no embeddings, no EDHREC features.
"""
import os

from mtg_synergy.config import DATA_DIR


def color_identity_filter(conn, cmdr_oid: str, color_identity: set,
                          deck_cards: set = None) -> list:
    """Return all color-legal non-token cards as (oid, name) pairs."""
    import json
    results = []
    deck_cards = deck_cards or set()
    for row in conn.execute(
        "SELECT oracle_id, name, color_identity FROM cards "
        "WHERE type_line NOT LIKE '%Token%'"
    ):
        oid, name, ci_json = row
        if name in deck_cards or oid == cmdr_oid:
            continue
        ci = set(json.loads(ci_json)) if ci_json else set()
        if ci <= color_identity:  # card CI must be subset of commander CI
            results.append((oid, name))
    return results


def score_forge_candidates(candidate_scores: dict, cards: list, conn,
                           commander: str, deck_cards: set,
                           deck_types: set = None, active_strategies: set = None) -> None:
    """Score candidates using forge-only model (63 features, no EDHREC).

    Uses forge GBM (data/fusion_model_forge.lgb) with pure Forge-native
    features from causal graph, strategies, and card mechanics.
    No tower model or oracle-text embeddings.
    """
    import warnings
    import joblib
    import numpy as np
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features,
    )

    forge_gbm_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    if not os.path.exists(forge_gbm_path):
        print("  ERROR: forge model not found. Run: python3 train_fusion_model.py --forge-only")
        return

    # Load model: detect format by file header
    import lightgbm as lgb
    with open(forge_gbm_path, 'rb') as f:
        header = f.read(4)
    if header[:4] == b'tree':
        forge_gbm = lgb.Booster(model_file=forge_gbm_path)
    else:
        forge_gbm = joblib.load(forge_gbm_path)

    # Build card data lookup
    card_data = {c["name"]: c for c in cards}
    missing = [n for n in candidate_scores if n not in card_data]
    if missing:
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank "
                f"FROM cards WHERE name IN ({ph}) AND type_line NOT LIKE '%Token%'", chunk
            ).fetchall():
                cd = {"oracle_id": row[0], "name": row[1], "type_line": row[2] or "",
                      "mana_cost": row[3] or "", "cmc": row[4] or 0}
                cards.append(cd)
                card_data[row[1]] = cd

    # Commander OID
    cmdr_oid = ""
    for c in cards:
        if c["name"] == commander:
            cmdr_oid = c.get("oracle_id", "")
            break

    # Build shared context with edge index (avoids 80s+ SQL queries for deck edges)
    ctx = ForgeFeatureContext(conn, preload_edges=True)

    # Card OID lookup
    card_oid = {c["name"]: c.get("oracle_id", "") for c in cards}

    # Deck OIDs for CmdrFeatureContext
    deck_oids = {card_oid.get(n) for n in deck_cards if n in card_oid} - {None, ""}

    # Per-commander context
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids)

    # Set commander subtypes for tribal matching
    cmdr_type = next((c.get("type_line", "") for c in cards if c.get("oracle_id") == cmdr_oid), "")
    if "\u2014" in cmdr_type:
        try:
            cmdr_ctx.cmdr_subtypes = {s.lower() for s in cmdr_type.split("\u2014")[1].strip().split()}
        except (IndexError, AttributeError):
            pass

    # Build feature matrix
    cand_list = [(n, card_data[n]) for n in candidate_scores if n in card_data]
    features = []
    for name, cd in cand_list:
        oid = cd.get("oracle_id") or card_oid.get(name, "")
        tl = cd.get("type_line", "")
        cmc = float(cd.get("cmc", 0))
        features.append(compute_card_features(oid, tl, cmc, ctx, cmdr_ctx))

    if features:
        X = np.array(features, dtype=np.float64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scores = forge_gbm.predict(X, raw_score=True)
        for i, (name, _) in enumerate(cand_list):
            info = candidate_scores[name]
            info["total"] = float(scores[i]) * 10.0
            if info["total"] > 0:
                info["fusion_score"] = round(float(scores[i]), 4)

    print(f"  Forge scoring: {len(cand_list)} candidates scored ({len(features[0]) if features else 0} features, no EDHREC)")
