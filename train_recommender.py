#!/usr/bin/env python3
"""Train a recommendation model from EDHREC per-commander data.

Learns what features predict high synergy between a commander and a card,
then applies the model to recommend cards for any commander.

Usage:
    python3 train_recommender.py --build-data     # build training data
    python3 train_recommender.py --train           # train the model
    python3 train_recommender.py --predict krenko  # predict for a deck
    python3 train_recommender.py --evaluate        # cross-validation
"""

import json
import math
import os
import re
import sqlite3
import sys
import random

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
CACHE_DIR = os.path.join(DATA_DIR, "edhrec_commanders")
TRAINING_PATH = os.path.join(DATA_DIR, "recommender_training.json")
MODEL_PATH = os.path.join(DATA_DIR, "recommender_weights.json")

sys.path.insert(0, os.path.dirname(__file__))
from synergy_graph import SEMANTIC_BRIDGES


def compute_features(commander: dict, candidate: dict) -> dict:
    """Compute feature vector for a commander-candidate pair."""
    cmdr_provides = set(commander.get("provides", []))
    cmdr_wants = set(commander.get("wants", []))
    cmdr_oracle = (commander.get("oracle_text") or "").lower()
    cmdr_keywords = {k.lower() for k in (commander.get("keywords") or [])}
    cmdr_type_line = (commander.get("type_line") or "").lower()

    card_provides = set(candidate.get("provides", []))
    card_wants = set(candidate.get("wants", []))
    card_oracle = (candidate.get("oracle_text") or "").lower()
    card_keywords = {k.lower() for k in (candidate.get("keywords") or [])}
    card_type_line = (candidate.get("type_line") or "").lower()

    # Feature 1: Direct tag connections
    provides_to_wants = len(card_provides & cmdr_wants)
    wants_from_provides = len(card_wants & cmdr_provides)

    # Feature 2: Bridge connections
    bridge_provides = {}
    for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
        bridge_provides.setdefault(w_tag, []).append((p_tag, weight))

    bridge_score = 0.0
    for want in cmdr_wants:
        for p_tag, weight in bridge_provides.get(want, []):
            if p_tag in card_provides:
                bridge_score += weight
    for want in card_wants:
        for p_tag, weight in bridge_provides.get(want, []):
            if p_tag in cmdr_provides:
                bridge_score += weight

    # Feature 3: Oracle text concept overlap
    concepts = set()
    for word in ["human", "goblin", "elf", "zombie", "vampire", "dragon", "angel",
                 "demon", "sliver", "artifact", "enchantment", "equipment", "aura",
                 "instant", "sorcery", "token", "counter", "poison",
                 "mill", "draw", "discard", "sacrifice", "exile", "return",
                 "graveyard", "library", "damage", "life", "mana",
                 "untap", "tap", "equip", "proliferate", "attack", "combat",
                 "enters", "dies", "cast", "noncreature", "creature"]:
        if re.search(r'\b' + word + r's?\b', cmdr_oracle):
            concepts.add(word)

    oracle_overlap = 0
    for concept in concepts:
        if re.search(r'\b' + concept + r's?\b', card_oracle):
            oracle_overlap += 1

    # Feature 4: Keyword overlap
    keyword_overlap = len(card_keywords & cmdr_keywords)

    # Feature 5: Shared creature types
    cmdr_subtypes = set()
    if " — " in cmdr_type_line:
        cmdr_subtypes = {s.strip().lower() for s in cmdr_type_line.split(" — ")[1].split()}
    card_subtypes = set()
    if " — " in card_type_line:
        card_subtypes = {s.strip().lower() for s in card_type_line.split(" — ")[1].split()}
    shared_types = len(cmdr_subtypes & card_subtypes)

    # Feature 6: Type category match
    type_match = 0
    for t in ["creature", "artifact", "enchantment", "instant", "sorcery", "planeswalker"]:
        if t in cmdr_type_line and t in card_type_line:
            type_match = 1
            break

    # Feature 7: CMC proximity
    cmdr_cmc = commander.get("cmc", 0) or 0
    card_cmc = candidate.get("cmc", 0) or 0
    cmc_diff = abs(cmdr_cmc - card_cmc)

    # Feature 8: EDHREC rank of candidate (popularity)
    card_rank = candidate.get("edhrec_rank") or 50000
    rank_log = math.log10(max(card_rank, 1))

    # Feature 9: Total tag count (card complexity)
    total_tags = len(card_provides) + len(card_wants)

    # Feature 14: Spellbook combo potential
    # How many Spellbook combos include BOTH the commander and this candidate?
    spellbook_combos = 0
    cmdr_oid = commander.get("oracle_id", "")
    card_oid = candidate.get("oracle_id", "")
    if cmdr_oid and card_oid and hasattr(compute_features, '_spellbook_cache'):
        cmdr_combos = compute_features._spellbook_cache.get(cmdr_oid, set())
        card_combos = compute_features._spellbook_cache.get(card_oid, set())
        spellbook_combos = len(cmdr_combos & card_combos)

    # Feature 15: Ability trigger-effect match
    # Does candidate's effect match commander's trigger, or vice versa?
    ability_match = 0
    cmdr_triggers = set()
    cmdr_effects = set()
    card_triggers = set()
    card_effects = set()
    if hasattr(compute_features, '_ability_cache'):
        for tt, et in compute_features._ability_cache.get(cmdr_oid, []):
            if tt: cmdr_triggers.update(json.loads(tt) if isinstance(tt, str) else tt)
            if et: cmdr_effects.update(json.loads(et) if isinstance(et, str) else et)
        for tt, et in compute_features._ability_cache.get(card_oid, []):
            if tt: card_triggers.update(json.loads(tt) if isinstance(tt, str) else tt)
            if et: card_effects.update(json.loads(et) if isinstance(et, str) else et)
    # Commander effect triggers candidate, or candidate effect triggers commander
    ability_match = len(cmdr_effects & card_triggers) + len(card_effects & cmdr_triggers)

    # Feature 16: Color identity match score
    cmdr_ci = set(json.loads(commander.get("color_identity", "[]")) if commander.get("color_identity") else [])
    card_ci = set(json.loads(candidate.get("color_identity", "[]")) if candidate.get("color_identity") else [])
    color_overlap = len(cmdr_ci & card_ci) / max(len(cmdr_ci), 1) if cmdr_ci else 0

    # Feature 17: Card rarity (mythic=4, rare=3, uncommon=2, common=1)
    rarity_map = {"mythic": 4, "rare": 3, "uncommon": 2, "common": 1}
    card_rarity = rarity_map.get(candidate.get("rarity", ""), 1)

    # Feature 21: EDHREC saltiness (how powerful/disruptive the card is)
    card_saltiness = candidate.get("edhrec_saltiness") or 0.0

    # Feature 18: Total Spellbook combos candidate appears in (overall combo versatility)
    total_spellbook = 0
    if card_oid and hasattr(compute_features, '_spellbook_cache'):
        total_spellbook = len(compute_features._spellbook_cache.get(card_oid, set()))

    # Feature 19: Strategy alignment (shared strategies between commander and candidate)
    strategy_match = 0
    if hasattr(compute_features, '_strategy_cache'):
        cmdr_strats = compute_features._strategy_cache.get(cmdr_oid, set())
        card_strats = compute_features._strategy_cache.get(card_oid, set())
        strategy_match = len(cmdr_strats & card_strats)

    # Feature 20: EDHREC theme overlap
    theme_overlap = 0
    if hasattr(compute_features, '_theme_cache'):
        cmdr_themes = compute_features._theme_cache.get(commander.get("name", ""), set())
        card_themes = compute_features._theme_cache.get(candidate.get("name", ""), set())
        theme_overlap = len(cmdr_themes & card_themes)

    # Feature 10: Bidirectional connection strength
    # Cards that both provide-to-wants AND want-from-provides are stronger
    bidirectional = min(provides_to_wants, wants_from_provides)

    # Feature 11: Oracle text SPECIFIC concept overlap (not generic words)
    # Only count concepts that are RARE (not "creature" or "damage")
    rare_concepts = {"mill", "graveyard", "poison", "infect", "proliferate",
                     "equipment", "equip", "aura", "enchant", "sacrifice",
                     "exile", "counter", "token", "planeswalker", "land",
                     "draw", "discard", "untap"}
    rare_overlap = 0
    for concept in rare_concepts:
        if re.search(r'\b' + concept + r's?\b', cmdr_oracle) and \
           re.search(r'\b' + concept + r's?\b', card_oracle):
            rare_overlap += 1

    # Feature 12: Commander creature type appears in candidate's oracle text
    # E.g., Kyler is a Human — does the candidate mention "Human"?
    cmdr_type_in_oracle = 0
    for subtype in cmdr_subtypes:
        if len(subtype) > 3 and re.search(r'\b' + re.escape(subtype) + r's?\b', card_oracle):
            cmdr_type_in_oracle += 1

    # Feature 13: Candidate creature type matches commander's oracle text types
    card_type_in_cmdr_oracle = 0
    for subtype in card_subtypes:
        if len(subtype) > 3 and re.search(r'\b' + re.escape(subtype) + r's?\b', cmdr_oracle):
            card_type_in_cmdr_oracle += 1

    return {
        "provides_to_wants": provides_to_wants,
        "wants_from_provides": wants_from_provides,
        "bridge_score": round(bridge_score, 2),
        "oracle_overlap": oracle_overlap,
        "keyword_overlap": keyword_overlap,
        "shared_types": shared_types,
        "type_match": type_match,
        "cmc_diff": cmc_diff,
        "rank_log": round(rank_log, 2),
        "total_tags": total_tags,
        "bridge_x_oracle": round(bridge_score * oracle_overlap, 2),
        "provides_x_bridge": round(provides_to_wants * bridge_score, 2),
        "bidirectional": bidirectional,
        "rare_overlap": rare_overlap,
        "cmdr_type_in_oracle": cmdr_type_in_oracle,
        "card_type_in_cmdr_oracle": card_type_in_cmdr_oracle,
        "spellbook_combos": spellbook_combos,
        "ability_match": ability_match,
        "color_overlap": round(color_overlap, 2),
        "card_rarity": card_rarity,
        "total_spellbook": min(total_spellbook, 20),  # Cap at 20
        "strategy_match": strategy_match,
        "theme_overlap": theme_overlap,
        "card_saltiness": round(card_saltiness, 2),
    }


def _init_caches(conn):
    """Initialize Spellbook and ability caches for feature computation."""
    # Spellbook cache: oracle_id -> set of combo_ids
    spellbook_cache = {}
    try:
        for combo_id, oid in conn.execute("SELECT combo_id, oracle_id FROM spellbook_combo_cards"):
            spellbook_cache.setdefault(oid, set()).add(combo_id)
    except:
        pass
    compute_features._spellbook_cache = spellbook_cache

    # Ability cache: oracle_id -> [(trigger_tags, effect_tags)]
    ability_cache = {}
    try:
        for oid, tt, et in conn.execute("SELECT oracle_id, trigger_tags, effect_tags FROM abilities"):
            ability_cache.setdefault(oid, []).append((tt, et))
    except:
        pass
    compute_features._ability_cache = ability_cache

    # Strategy cache: oracle_id -> set of strategy names
    strategy_cache = {}
    try:
        for oid, strat in conn.execute("SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"):
            strategy_cache.setdefault(oid, set()).add(strat)
    except:
        pass
    compute_features._strategy_cache = strategy_cache

    # Theme cache: card_name -> set of EDHREC themes
    theme_cache = {}
    edhrec_path = os.path.join(DATA_DIR, "edhrec_theme_cards.json")
    if os.path.exists(edhrec_path):
        try:
            with open(edhrec_path) as f:
                themes = json.load(f)
            for theme, cards in themes.items():
                for card in cards:
                    if card.get("synergy", 0) > 0.05:
                        theme_cache.setdefault(card["name"], set()).add(theme)
        except:
            pass
    compute_features._theme_cache = theme_cache

    print(f"  Caches: {len(spellbook_cache)} Spellbook, {len(ability_cache)} abilities, {len(strategy_cache)} strategies, {len(theme_cache)} themes")


def build_training_data():
    """Build training data from scraped EDHREC commander data."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Initialize caches for Spellbook and ability features
    _init_caches(conn)

    # Load all card data
    cards_by_name = {}
    for row in conn.execute("SELECT * FROM cards"):
        card = dict(row)
        card["provides"] = [r[0] for r in conn.execute(
            "SELECT tag FROM provides WHERE oracle_id = ?", (card["oracle_id"],))]
        card["wants"] = [r[0] for r in conn.execute(
            "SELECT tag FROM wants WHERE oracle_id = ?", (card["oracle_id"],))]
        try:
            card["keywords"] = json.loads(card["keywords"]) if card["keywords"] else []
        except:
            card["keywords"] = []
        cards_by_name[card["name"]] = card

    conn.close()

    training = []
    commander_count = 0

    for fname in os.listdir(CACHE_DIR):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(CACHE_DIR, fname)) as f:
            edata = json.load(f)

        # Find commander name
        header = edata.get("container", {}).get("json_dict", {}).get("header", "")
        cmdr_name = header  # Usually the commander name
        # Try to find it from the data
        cardlists = edata.get("container", {}).get("json_dict", {}).get("cardlists", [])

        # Find high synergy cards
        high_syn = []
        all_cards = []
        for cl in cardlists:
            for cv in cl.get("cardviews", []):
                all_cards.append(cv)
                if cv.get("synergy", 0) > 0.15:
                    high_syn.append(cv)

        if not high_syn:
            continue

        # Find commander in our DB
        cmdr_card = None
        for cl in cardlists:
            if "Commander" in cl.get("header", ""):
                for cv in cl.get("cardviews", []):
                    if cv["name"] in cards_by_name:
                        cmdr_card = cards_by_name[cv["name"]]
                        break
        if not cmdr_card:
            # Try slug to find commander
            slug = fname.replace(".json", "")
            for name, card in cards_by_name.items():
                name_slug = re.sub(r'[^a-z0-9 -]', '', name.lower())
                name_slug = re.sub(r'\s+', '-', name_slug.strip())
                if name_slug == slug:
                    cmdr_card = card
                    break
        if not cmdr_card:
            continue

        commander_count += 1
        high_syn_names = {cv["name"] for cv in high_syn}

        # Positive examples: high synergy cards
        for cv in high_syn:
            if cv["name"] not in cards_by_name:
                continue
            candidate = cards_by_name[cv["name"]]
            features = compute_features(cmdr_card, candidate)
            features["label"] = 1
            features["synergy"] = cv["synergy"]
            features["commander"] = cmdr_card["name"]
            features["candidate"] = cv["name"]
            training.append(features)

        # Negative examples: random cards NOT in high synergy
        # Sample from all cards in the same color identity
        cmdr_ci = set(json.loads(cmdr_card["color_identity"])) if cmdr_card.get("color_identity") else set()
        negatives = []
        for name, card in cards_by_name.items():
            if name in high_syn_names or name == cmdr_card["name"]:
                continue
            card_ci = set(json.loads(card["color_identity"])) if card.get("color_identity") else set()
            if card_ci <= cmdr_ci:  # Must be in commander's colors
                negatives.append(card)

        # Sample 2x negatives per positive
        neg_sample = random.sample(negatives, min(len(high_syn) * 2, len(negatives)))
        for card in neg_sample:
            features = compute_features(cmdr_card, card)
            features["label"] = 0
            features["synergy"] = 0.0
            features["commander"] = cmdr_card["name"]
            features["candidate"] = card["name"]
            training.append(features)

    with open(TRAINING_PATH, "w") as f:
        json.dump(training, f, indent=2)

    pos = sum(1 for t in training if t["label"] == 1)
    neg = sum(1 for t in training if t["label"] == 0)
    print(f"Training data: {len(training)} examples ({pos} positive, {neg} negative)")
    print(f"From {commander_count} commanders")
    return training


def train_model(training_data=None):
    """Train a logistic regression model from training data."""
    if training_data is None:
        with open(TRAINING_PATH) as f:
            training_data = json.load(f)

    feature_names = ["provides_to_wants", "wants_from_provides", "bridge_score",
                     "oracle_overlap", "keyword_overlap", "shared_types",
                     "type_match", "cmc_diff", "rank_log", "total_tags",
                     "bridge_x_oracle", "provides_x_bridge",
                     "bidirectional", "rare_overlap",
                     "cmdr_type_in_oracle", "card_type_in_cmdr_oracle",
                     "spellbook_combos", "ability_match", "color_overlap",
                     "card_rarity", "total_spellbook", "strategy_match", "theme_overlap",
                     "card_saltiness"]

    # Compute feature means and stds for normalization
    means = {f: 0.0 for f in feature_names}
    for row in training_data:
        for f in feature_names:
            means[f] += row[f]
    for f in feature_names:
        means[f] /= max(len(training_data), 1)

    stds = {f: 0.0 for f in feature_names}
    for row in training_data:
        for f in feature_names:
            stds[f] += (row[f] - means[f]) ** 2
    for f in feature_names:
        stds[f] = max((stds[f] / max(len(training_data), 1)) ** 0.5, 0.001)

    # Phase 1: Classification loss (logistic regression)
    weights = {f: 0.0 for f in feature_names}
    bias = 0.0
    lr = 0.1

    for epoch in range(50):
        total_loss = 0.0
        for row in training_data:
            x = {f: (row[f] - means[f]) / stds[f] for f in feature_names}
            y = row["label"]
            z = sum(weights[f] * x[f] for f in feature_names) + bias
            z = max(-20, min(20, z))
            pred = 1.0 / (1.0 + math.exp(-z))
            eps = 1e-7
            loss = -(y * math.log(pred + eps) + (1 - y) * math.log(1 - pred + eps))
            total_loss += loss
            error = pred - y
            for f in feature_names:
                weights[f] -= lr * error * x[f]
            bias -= lr * error
        if (epoch + 1) % 25 == 0:
            print(f"  Classification epoch {epoch+1}: loss={total_loss/len(training_data):.4f}")

    # Phase 2: Fine-tune with pairwise ranking loss (BPR)
    import random
    random.seed(42)
    by_commander = {}
    for row in training_data:
        cmdr = row["commander"]
        by_commander.setdefault(cmdr, {"pos": [], "neg": []})
        if row["label"] == 1:
            by_commander[cmdr]["pos"].append(row)
        else:
            by_commander[cmdr]["neg"].append(row)

    lr_rank = 0.01  # Lower LR for fine-tuning

    for epoch in range(20):
        total_loss = 0.0
        pairs = 0
        for cmdr, groups in by_commander.items():
            if not groups["pos"] or not groups["neg"]:
                continue
            for pos in groups["pos"]:
                neg = random.choice(groups["neg"])
                x_pos = {f: (pos[f] - means[f]) / stds[f] for f in feature_names}
                x_neg = {f: (neg[f] - means[f]) / stds[f] for f in feature_names}
                z_pos = sum(weights[f] * x_pos[f] for f in feature_names) + bias
                z_neg = sum(weights[f] * x_neg[f] for f in feature_names) + bias
                diff = max(-20, min(20, z_pos - z_neg))
                sigma = 1.0 / (1.0 + math.exp(-diff))
                eps = 1e-7
                loss = -math.log(sigma + eps)
                total_loss += loss
                pairs += 1
                error = sigma - 1.0
                for f in feature_names:
                    weights[f] -= lr_rank * error * (x_pos[f] - x_neg[f])
        if (epoch + 1) % 10 == 0:
            print(f"  Ranking epoch {epoch+1}: loss={total_loss/max(pairs,1):.4f}")

    # Save model
    model = {
        "weights": weights,
        "bias": bias,
        "means": means,
        "stds": stds,
        "feature_names": feature_names,
    }
    with open(MODEL_PATH, "w") as f:
        json.dump(model, f, indent=2)

    # Print feature importance
    print(f"\nFeature importance (|weight|):")
    for f in sorted(feature_names, key=lambda x: -abs(weights[x])):
        print(f"  {f:<25} {weights[f]:>7.3f}")

    return model


def predict(model: dict, commander: dict, candidate: dict) -> float:
    """Predict synergy score for a commander-candidate pair.

    Caps rank_log influence to prevent popular staples from dominating.
    """
    features = compute_features(commander, candidate)
    x = {f: (features[f] - model["means"][f]) / model["stds"][f]
         for f in model["feature_names"]}

    # Cap rank_log and cmc_diff contribution to prevent dominating
    # Without this, rank alone determines most recommendations
    for dampened in ["rank_log", "cmc_diff"]:
        if dampened in x:
            x[dampened] = max(-0.5, min(0.5, x[dampened]))

    z = sum(model["weights"][f] * x[f] for f in model["feature_names"]) + model["bias"]
    z = max(-20, min(20, z))
    return 1.0 / (1.0 + math.exp(-z))


def evaluate_model(model=None):
    """Leave-one-out cross-validation on commander data."""
    with open(TRAINING_PATH) as f:
        all_data = json.load(f)

    if model is None:
        with open(MODEL_PATH) as f:
            model = json.load(f)

    # Group by commander
    by_commander = {}
    for row in all_data:
        cmdr = row["commander"]
        by_commander.setdefault(cmdr, []).append(row)

    # For each commander, predict on its data using the full model
    # (not truly leave-one-out but gives a sense of generalization)
    results = {}
    for cmdr, rows in by_commander.items():
        positives = [r for r in rows if r["label"] == 1]
        negatives = [r for r in rows if r["label"] == 0]
        if not positives:
            continue

        # Score all
        pos_scores = []
        for r in positives:
            features = {f: r[f] for f in model["feature_names"]}
            x = {f: (features[f] - model["means"][f]) / model["stds"][f]
                 for f in model["feature_names"]}
            z = sum(model["weights"][f] * x[f] for f in model["feature_names"]) + model["bias"]
            z = max(-20, min(20, z))
            score = 1.0 / (1.0 + math.exp(-z))
            pos_scores.append(score)

        neg_scores = []
        for r in negatives:
            features = {f: r[f] for f in model["feature_names"]}
            x = {f: (features[f] - model["means"][f]) / model["stds"][f]
                 for f in model["feature_names"]}
            z = sum(model["weights"][f] * x[f] for f in model["feature_names"]) + model["bias"]
            z = max(-20, min(20, z))
            score = 1.0 / (1.0 + math.exp(-z))
            neg_scores.append(score)

        avg_pos = sum(pos_scores) / max(len(pos_scores), 1)
        avg_neg = sum(neg_scores) / max(len(neg_scores), 1)

        # AUC-like: what % of positives score higher than negatives?
        correct = sum(1 for ps in pos_scores for ns in neg_scores if ps > ns)
        total_pairs = len(pos_scores) * len(neg_scores)
        auc = correct / max(total_pairs, 1)

        results[cmdr] = {"auc": round(auc, 3), "avg_pos": round(avg_pos, 3),
                         "avg_neg": round(avg_neg, 3), "n_pos": len(positives)}

    # Summary
    aucs = [r["auc"] for r in results.values()]
    avg_auc = sum(aucs) / max(len(aucs), 1)
    print(f"\nEvaluation across {len(results)} commanders:")
    print(f"  Average AUC: {avg_auc:.3f}")
    print(f"  Top 10:")
    for cmdr in sorted(results, key=lambda x: -results[x]["auc"])[:10]:
        r = results[cmdr]
        print(f"    {cmdr:<35} AUC={r['auc']:.3f} pos={r['avg_pos']:.3f} neg={r['avg_neg']:.3f}")
    print(f"  Bottom 10:")
    for cmdr in sorted(results, key=lambda x: results[x]["auc"])[:10]:
        r = results[cmdr]
        print(f"    {cmdr:<35} AUC={r['auc']:.3f} pos={r['avg_pos']:.3f} neg={r['avg_neg']:.3f}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Train recommendation model")
    parser.add_argument("--build-data", action="store_true")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--all", action="store_true", help="Build + train + evaluate")
    args = parser.parse_args()

    if args.all or args.build_data:
        random.seed(42)
        build_training_data()

    if args.all or args.train:
        train_model()

    if args.all or args.evaluate:
        evaluate_model()
