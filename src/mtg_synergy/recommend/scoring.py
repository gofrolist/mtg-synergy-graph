"""Forge-only scoring — scores card candidates using the forge GBM model.

Public functions:
  color_identity_filter() — returns all color-legal non-token cards
  score_forge_candidates() — scores candidates for a single commander
  batch_recommend() — scores multiple commanders, loading model once
"""
import json
import os
import warnings

import numpy as np
import lightgbm as lgb

from mtg_synergy.config import DATA_DIR
from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext, CmdrFeatureContext, compute_card_features,
)


def _load_gbm():
    """Load the forge GBM model (singleton-ish, but cheap to reload)."""
    forge_gbm_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    if not os.path.exists(forge_gbm_path):
        return None
    return lgb.Booster(model_file=forge_gbm_path)


def _load_all_cards(conn):
    """Load all card metadata from DB into a dict[name -> card_dict]."""
    card_data = {}
    for row in conn.execute(
        "SELECT oracle_id, name, type_line, mana_cost, cmc FROM cards "
        "WHERE type_line NOT LIKE '%Token%' AND legal_commander = 1"
    ):
        card_data[row[1]] = {
            "oracle_id": row[0], "name": row[1],
            "type_line": row[2] or "", "mana_cost": row[3] or "",
            "cmc": row[4] or 0,
        }
    return card_data


def color_identity_filter(conn, cmdr_oid: str, color_identity: set,
                          deck_cards: set = None) -> list:
    """Return all color-legal non-token cards as (oid, name) pairs."""
    results = []
    deck_cards = deck_cards or set()
    for row in conn.execute(
        "SELECT oracle_id, name, color_identity FROM cards "
        "WHERE type_line NOT LIKE '%Token%' AND legal_commander = 1"
    ):
        oid, name, ci_json = row
        if name in deck_cards or oid == cmdr_oid:
            continue
        ci = set(json.loads(ci_json)) if ci_json else set()
        if ci <= color_identity:
            results.append((oid, name))
    return results


def _score_commander(cmdr_oid, cmdr_name, color_identity, deck_cards,
                     ctx, gbm, card_data, top_n=30):
    """Score all color-legal candidates for one commander. Returns ranked list of (name, score).

    Uses pre-loaded ctx and gbm to avoid re-initialization.
    """
    # Color identity filter
    deck_cards = deck_cards or {cmdr_name}
    candidates = {}
    for name, cd in card_data.items():
        if name in deck_cards or cd["oracle_id"] == cmdr_oid:
            continue
        ci = set(json.loads(
            cd.get("_ci_json", "[]")
        )) if "_ci_json" in cd else set()
        # We already filtered tokens in _load_all_cards
        if ci <= color_identity:
            candidates[name] = cd

    # Commander context
    deck_oids = set()
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids)
    cmdr_type = card_data.get(cmdr_name, {}).get("type_line", "")
    if "\u2014" in cmdr_type:
        try:
            cmdr_ctx.cmdr_subtypes = {
                s.lower() for s in cmdr_type.split("\u2014")[1].strip().split()
            }
        except (IndexError, AttributeError):
            pass

    # Compute features
    cand_list = list(candidates.items())
    features = []
    for name, cd in cand_list:
        features.append(compute_card_features(
            cd["oracle_id"], cd["type_line"], float(cd["cmc"]), ctx, cmdr_ctx))

    if not features:
        return []

    X = np.array(features, dtype=np.float64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        scores = gbm.predict(X, raw_score=True)

    # Rank and return top N
    ranked = sorted(zip([n for n, _ in cand_list], scores),
                    key=lambda x: -x[1])
    return ranked[:top_n]


def batch_recommend(conn, commander_names: list[str], top_n: int = 30,
                    verbose: bool = True) -> dict[str, list[tuple[str, float]]]:
    """Score multiple commanders in batch, loading model and context once.

    Returns dict[commander_name -> list[(card_name, score)]].
    ~0.3s per commander after initial 3s setup (vs 7s per commander in subprocess).
    """
    gbm = _load_gbm()
    if gbm is None:
        print("ERROR: forge model not found. Run: python3 train_fusion_model.py --forge-only")
        return {}

    if verbose:
        print("Loading shared context...")
    ctx = ForgeFeatureContext(conn, preload_edges=True)

    # Load all card data + color identities
    card_data = _load_all_cards(conn)
    # Add color identity to card_data for fast filtering
    for row in conn.execute("SELECT name, color_identity FROM cards"):
        if row[0] in card_data:
            card_data[row[0]]["_ci_json"] = row[1] or "[]"

    # Resolve commander names to oids + color identities
    results = {}
    for i, cmdr_name in enumerate(commander_names):
        row = conn.execute(
            "SELECT oracle_id, color_identity FROM cards WHERE LOWER(name) = LOWER(?)",
            (cmdr_name,)
        ).fetchone()
        if not row:
            if verbose:
                print(f"  [{i+1}/{len(commander_names)}] {cmdr_name}: NOT FOUND")
            continue

        cmdr_oid, ci_json = row
        color_identity = set(json.loads(ci_json or "[]"))

        ranked = _score_commander(
            cmdr_oid, cmdr_name, color_identity, {cmdr_name},
            ctx, gbm, card_data, top_n=top_n)
        results[cmdr_name] = ranked

        if verbose and ((i + 1) % 50 == 0 or i == 0):
            print(f"  [{i+1}/{len(commander_names)}] {cmdr_name}: {len(ranked)} recs")

    return results


def score_forge_candidates(candidate_scores: dict, cards: list, conn,
                           commander: str, deck_cards: set,
                           deck_types: set = None, active_strategies: set = None) -> None:
    """Score candidates for a single commander. Modifies candidate_scores in-place.

    For batch scoring of multiple commanders, use batch_recommend() instead.
    """
    gbm = _load_gbm()
    if gbm is None:
        print("  ERROR: forge model not found. Run: python3 train_fusion_model.py --forge-only")
        return

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

    cmdr_oid = ""
    for c in cards:
        if c["name"] == commander:
            cmdr_oid = c.get("oracle_id", "")
            break

    ctx = ForgeFeatureContext(conn, preload_edges=True)
    card_oid = {c["name"]: c.get("oracle_id", "") for c in cards}
    deck_oids = {card_oid.get(n) for n in deck_cards if n in card_oid} - {None, ""}
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids)

    cmdr_type = next((c.get("type_line", "") for c in cards if c.get("oracle_id") == cmdr_oid), "")
    if "\u2014" in cmdr_type:
        try:
            cmdr_ctx.cmdr_subtypes = {s.lower() for s in cmdr_type.split("\u2014")[1].strip().split()}
        except (IndexError, AttributeError):
            pass

    cand_list = [(n, card_data[n]) for n in candidate_scores if n in card_data]
    features = []
    for name, cd in cand_list:
        oid = cd.get("oracle_id") or card_oid.get(name, "")
        features.append(compute_card_features(oid, cd.get("type_line", ""),
                                               float(cd.get("cmc", 0)), ctx, cmdr_ctx))

    if features:
        X = np.array(features, dtype=np.float64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scores = gbm.predict(X, raw_score=True)
        for i, (name, _) in enumerate(cand_list):
            info = candidate_scores[name]
            info["total"] = float(scores[i]) * 10.0
            if info["total"] > 0:
                info["fusion_score"] = round(float(scores[i]), 4)
