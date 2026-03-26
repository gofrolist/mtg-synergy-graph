"""Dynamic feature-based scoring — computes synergy at recommendation time.

Replaces static LLM scores with a weighted combination of:
  tower model, tag overlap, strategy, tribal, rank, EDHREC,
  causal graph, forge overlap.

All features are computed on the fly from the DB and models, so scores
adapt to the current deck composition and never go stale.
"""
import math
import os

from mtg_synergy.config import DATA_DIR, SCORING_WEIGHTS



class DeckContext:
    """Pre-computed deck-level data for scoring candidates against."""

    def __init__(self, conn, commander: str, deck_cards: set, cards: list,
                 deck_types: set = None, active_strategies: set = None,
                 edhrec_slug: str = None):
        self.commander = commander
        self.deck_cards = deck_cards
        self.deck_types = deck_types or set()
        self.active_strategies = active_strategies or set()
        self.is_tribal = bool(deck_types)

        # Commander OID
        self.cmdr_oid = ""
        for c in cards:
            if c["name"] == commander:
                self.cmdr_oid = c.get("oracle_id", "")
                break

        # Card OID lookup
        self.card_oid = {}
        for c in cards:
            self.card_oid[c["name"]] = c.get("oracle_id", "")

        # EDHREC synergy map (DFC-aware)
        self.edhrec = {}
        if edhrec_slug:
            has = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_card_synergy'"
            ).fetchone()[0]
            if has:
                raw = {}
                for r in conn.execute(
                    "SELECT card_name, synergy FROM edhrec_card_synergy WHERE commander_slug = ?",
                    (edhrec_slug,)):
                    raw[r[0]] = r[1]
                dfc_map = {}
                for r in conn.execute("SELECT name FROM cards WHERE name LIKE '%//%'"):
                    dfc_map[r[0].split(" // ")[0]] = r[0]
                for name, syn in raw.items():
                    self.edhrec[name] = syn
                    dfc = dfc_map.get(name)
                    if dfc and dfc not in self.edhrec:
                        self.edhrec[dfc] = syn

        # Forge DeckHas/DeckHints tags (cached for all candidates)
        self.forge_cmdr_has = set()
        self.forge_cmdr_hints = set()
        try:
            for r in conn.execute(
                "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
                (commander,)).fetchall():
                if r[0] == "has":
                    self.forge_cmdr_has.add(r[1])
                elif r[0] == "hints":
                    self.forge_cmdr_hints.add(r[1])
        except Exception:
            pass

        # Bulk-load all candidate forge tags
        self.forge_card_has = {}   # {card_name: set of tags}
        self.forge_card_hints = {} # {card_name: set of tags}
        try:
            for r in conn.execute("SELECT card_name, tag_type, tag FROM forge_deck_tags"):
                if r[1] == "has":
                    self.forge_card_has.setdefault(r[0], set()).add(r[2])
                elif r[1] == "hints":
                    self.forge_card_hints.setdefault(r[0], set()).add(r[2])
        except Exception:
            pass

        # Tower model (loaded once, cached on class)
        self.tower_model = _load_tower_model()

        # LLM synergy scores removed — superseded by fusion model

        # Commander profile fallback (when no strategies provided by caller)
        if not self.active_strategies and self.cmdr_oid:
            try:
                from mtg_synergy.recommend.commander_profile import load_profile
                profile = load_profile(conn, self.cmdr_oid)
                if profile:
                    self.active_strategies = profile.strategies
                    if profile.tribal_type and not self.deck_types:
                        self.deck_types = {profile.tribal_type}
                        self.is_tribal = True
            except Exception:
                pass

        # Causal graph (pre-loaded, O(1) per candidate)
        self.causal_ctx = None
        try:
            from mtg_synergy.causal import CausalContext
            deck_oids = {self.card_oid.get(c) for c in self.deck_cards
                         if c in self.card_oid} - {None}
            self.causal_ctx = CausalContext(conn, self.cmdr_oid, deck_oids)
        except Exception:
            pass



def compute_forge_deck_overlap(conn, commander_name: str, candidate_name: str) -> int:
    """Count matching DeckHas/DeckHints between commander and candidate.

    DeckHas = what the card provides (abilities, types)
    DeckHints = what the card wants in the deck

    Overlap = (candidate provides what commander wants) +
              (commander provides what candidate wants)
    """
    cmdr_has = set()
    cmdr_hints = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (commander_name,)
    ).fetchall():
        if r[0] == "has":
            cmdr_has.add(r[1])
        elif r[0] == "hints":
            cmdr_hints.add(r[1])

    cand_has = set()
    cand_hints = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (candidate_name,)
    ).fetchall():
        if r[0] == "has":
            cand_has.add(r[1])
        elif r[0] == "hints":
            cand_hints.add(r[1])

    return len(cand_has & cmdr_hints) + len(cand_hints & cmdr_has)


def compute_dynamic_score(card_name: str, card_data: dict, ctx: DeckContext,
                          conn) -> dict:
    """Compute feature-based synergy score for a single card.

    Returns dict with 'total' score and individual feature values.
    """
    w = SCORING_WEIGHTS
    oid = card_data.get("oracle_id") or ctx.card_oid.get(card_name, "")
    type_line = card_data.get("type_line", "")
    is_creature = "Creature" in type_line
    rank = card_data.get("edhrec_rank") or 50000

    # --- Feature 1: Tower model (semantic synergy) ---
    tower = _get_tower_score(ctx, oid)

    # --- Features 2-4: removed (causal graph F10 captures these relationships) ---

    # --- Feature 5: Strategy overlap (tag-based) ---
    strat_overlap = 0
    if ctx.active_strategies:
        card_strats = set(
            r[0] for r in conn.execute(
                "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
                (oid,))
        )
        strat_overlap = len(card_strats & ctx.active_strategies)

    # --- Feature 6: Tribal match ---
    tribal_adj = 0.0
    tribal_match = False
    if ctx.is_tribal and is_creature:
        type_lower = type_line.lower()
        matches = any(t.lower() in type_lower for t in ctx.deck_types)
        if matches:
            tribal_adj = w["TRIBAL_BONUS"]
            tribal_match = True
        else:
            tribal_adj = w["TRIBAL_PENALTY"]

    # --- Feature 7: Card quality (EDHREC rank) ---
    rank_score = max(0, 3.0 - 0.6 * math.log10(max(rank, 1)))

    # --- Feature 8: EDHREC synergy ---
    edhrec_syn = max(0, ctx.edhrec.get(card_name, 0.0))

    # --- Feature 9: (removed — STRATEGY_KEYWORDS superseded by forge + causal) ---

    # --- Feature 10: Causal graph score ---
    causal = 0.0
    if ctx.causal_ctx:
        causal = ctx.causal_ctx.causal_score(oid)

    # --- Feature 11: (removed — LLM scores superseded by fusion model) ---

    # --- Feature 12: Forge DeckHas/DeckHints overlap ---
    forge_overlap = 0
    if ctx.forge_cmdr_hints or ctx.forge_cmdr_has:
        cand_has = ctx.forge_card_has.get(card_name, set())
        cand_hints = ctx.forge_card_hints.get(card_name, set())
        forge_overlap = len(cand_has & ctx.forge_cmdr_hints) + len(cand_hints & ctx.forge_cmdr_has)

    # --- Fusion model (hybrid tower + GBM) ---
    fusion_score = 0.0
    from mtg_synergy.config import USE_FUSION_MODEL
    if USE_FUSION_MODEL:
        fusion = _load_fusion_model()
        if fusion is not None:
            import numpy as np
            tower_prob = _get_fusion_score(fusion, ctx.cmdr_oid, oid)
            features = np.array([[
                tower_prob,
                causal,
                forge_overlap,
                1.0 if tribal_match else 0.0,
                edhrec_syn,
                math.log10(max(rank, 1)),
                card_data.get("cmc", 0),
                1.0 if is_creature else 0.0,
            ]])
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                fusion_score = float(fusion["gbm"].predict(features, raw_score=True)[0])

    # --- Combine ---
    if fusion_score > 0:
        total = fusion_score * w.get("FUSION", 10.0)
    else:
        total = (tower * w["TOWER"]
                 + strat_overlap * w["STRATEGY"]
                 + tribal_adj
                 + rank_score * w["RANK"]
                 + edhrec_syn * w["EDHREC_SYNERGY"]
                 + causal * w.get("CAUSAL", 0)
                 + forge_overlap * w.get("FORGE_DECK_OVERLAP", 0))

    return {
        "total": total,
        "tower": round(tower, 1),
        "strat_overlap": strat_overlap,
        "tribal_match": tribal_match,
        "rank_score": round(rank_score, 2),
        "edhrec_syn": round(edhrec_syn, 3) if edhrec_syn > 0 else 0,
        "causal": round(causal, 1),
        "forge_overlap": forge_overlap,
        "fusion": round(fusion_score, 4),
    }


def score_all_candidates(candidate_scores: dict, cards: list, ctx: DeckContext,
                         conn, verbose: bool = True) -> None:
    """Score all candidates using dynamic features. Modifies candidate_scores in-place.

    Candidates are pre-selected by tower_prefilter() or graph edges.
    EDHREC synergy is used as a scoring feature (F8), not for injection.
    """
    # Build card data lookup — ensure all candidates have card data
    card_data = {}
    for c in cards:
        card_data[c["name"]] = c

    # Load card data for candidates not yet in cards list (from tower prefilter)
    missing = [n for n in candidate_scores if n not in card_data]
    if missing:
        import json as _json
        for name in missing:
            row = conn.execute(
                "SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank "
                "FROM cards WHERE name = ?", (name,)).fetchone()
            if row:
                cd = {"oracle_id": row[0], "name": row[1], "type_line": row[2] or "",
                      "mana_cost": row[3] or "", "cmc": row[4] or 0, "edhrec_rank": row[5]}
                cards.append(cd)
                card_data[name] = cd
                ctx.card_oid[name] = row[0]

    if ctx.edhrec and verbose:
        print(f"  EDHREC synergy: {len(ctx.edhrec)} cards loaded for scoring")

    # Score all candidates
    scored = 0
    for card_name, info in candidate_scores.items():
        cd = card_data.get(card_name)
        if not cd:
            continue
        features = compute_dynamic_score(card_name, cd, ctx, conn)
        info["total"] = features["total"]
        # Copy feature values for display
        if features["tower"] > 0:
            info["tower_score"] = features["tower"]
        if features["tribal_match"]:
            info["tribal_match"] = True
        if features["edhrec_syn"] > 0:
            info["edhrec_syn"] = features["edhrec_syn"]
        if features["fusion"] > 0:
            info["fusion_score"] = features["fusion"]
        scored += 1

    if verbose:
        print(f"  Dynamic scoring: {scored} candidates scored "
              f"(tower + tags + strategy + tribal)")


# === Tower model singleton ===

_tower_cache = {}


def _load_tower_model():
    """Load tower model data (cached). Returns dict or None."""
    if _tower_cache:
        return _tower_cache
    tower_path = os.path.join(DATA_DIR, "tower_model.npz")
    if not os.path.exists(tower_path):
        return None
    try:
        import numpy as np
        from train_tower_model import (load_embeddings, load_structural_features,
                                        compute_struct_features, forward)
        td = np.load(tower_path)
        model = {k: td[k] for k in td.files if k not in ("struct_means", "struct_stds")}
        if model["W1"].shape[0] != 140:
            return None
        normed_emb, oid_list, oid_to_idx = load_embeddings()
        sf_data = load_structural_features()
        _tower_cache.update({
            "model": model,
            "means": td["struct_means"],
            "stds": td["struct_stds"],
            "emb": normed_emb,
            "oid_to_idx": oid_to_idx,
            "sf_data": sf_data,
            "compute_sf": compute_struct_features,
            "forward": forward,
        })
        return _tower_cache
    except Exception:
        return None


def _get_tower_score(ctx: DeckContext, card_oid: str) -> float:
    """Get tower model prediction for a (commander, card) pair."""
    tm = ctx.tower_model
    if not tm or not ctx.cmdr_oid:
        return 5.0  # neutral default
    try:
        import numpy as np
        cmdr_idx = tm["oid_to_idx"].get(ctx.cmdr_oid)
        card_idx = tm["oid_to_idx"].get(card_oid)
        if cmdr_idx is None or card_idx is None:
            return 5.0
        sf = tm["compute_sf"](ctx.cmdr_oid, card_oid, *tm["sf_data"])
        sf_norm = ((sf - tm["means"]) / tm["stds"]).reshape(1, -1).astype(np.float32)
        X_cmdr = tm["emb"][cmdr_idx].reshape(1, -1).astype(np.float32)
        X_card = tm["emb"][card_idx].reshape(1, -1).astype(np.float32)
        score, _ = tm["forward"](tm["model"], X_cmdr, X_card, sf_norm)
        return float(np.clip(score, 1, 10)[0])
    except Exception:
        return 5.0


# === Fusion model singleton (hybrid tower + LightGBM) ===

_fusion_cache = None


def _load_fusion_model(tower_path=None, gbm_path=None):
    """Load fusion model (tower + GBM). Returns None on any failure.

    Caches the result as a singleton (like _load_tower_model).
    When tower_path/gbm_path are specified (for testing), bypasses the cache.
    """
    global _fusion_cache
    if tower_path is None and gbm_path is None and _fusion_cache is not None:
        return _fusion_cache

    from mtg_synergy.config import TOWER_EDHREC_PATH, FUSION_MODEL_PATH

    tp = tower_path or TOWER_EDHREC_PATH
    gp = gbm_path or FUSION_MODEL_PATH

    try:
        import numpy as np
        import joblib

        if not os.path.exists(str(tp)) or not os.path.exists(str(gp)):
            return None

        tower_data = np.load(str(tp))
        gbm = joblib.load(str(gp))

        from train_tower_model import (load_embeddings, load_structural_features,
                                        compute_struct_features, forward)

        emb, oid_list, oid_to_idx = load_embeddings()
        sf_data = load_structural_features()

        result = {
            "tower": {k: tower_data[k] for k in tower_data.files},
            "gbm": gbm,
            "emb": emb,
            "oid_to_idx": oid_to_idx,
            "sf_data": sf_data,
            "compute_sf": compute_struct_features,
            "forward": forward,
            "struct_means": tower_data["struct_means"],
            "struct_stds": tower_data["struct_stds"],
        }
        if tower_path is None and gbm_path is None:
            _fusion_cache = result
        return result
    except Exception:
        return None


def tower_prefilter(conn, cmdr_oid: str, color_identity: set,
                    top_n: int = 3000, deck_cards: set = None) -> list:
    """Score all color-legal cards with the fusion tower and return top N.

    Uses batch forward pass for speed (<200ms for 10k+ cards).
    Returns: [(oracle_id, name, tower_prob), ...] sorted by probability descending.
    Returns empty list if tower model is not available.
    """
    fusion = _load_fusion_model()
    if fusion is None:
        return []

    import json as _json
    import numpy as np

    oid_to_idx = fusion["oid_to_idx"]
    emb = fusion["emb"]
    cmdr_idx = oid_to_idx.get(cmdr_oid)
    if cmdr_idx is None:
        return []

    deck_cards = deck_cards or set()
    cmdr_ci = color_identity or set()

    # CI filter: find all legal non-token cards
    legal = []
    for row in conn.execute(
        "SELECT oracle_id, name, color_identity FROM cards "
        "WHERE color_identity IS NOT NULL "
        "AND type_line NOT LIKE '%Token%'"
    ):
        oid, name, ci_json = row
        if name in deck_cards or oid == cmdr_oid:
            continue
        if oid not in oid_to_idx:
            continue
        card_ci = set(_json.loads(ci_json or "[]"))
        if cmdr_ci and not card_ci.issubset(cmdr_ci):
            continue
        legal.append((oid, name))

    if not legal:
        return []

    # Batch tower scoring
    card_indices = [oid_to_idx[oid] for oid, _ in legal]
    X_card = emb[card_indices].astype(np.float32)

    sf_batch = np.array(
        [fusion["compute_sf"](cmdr_oid, oid, *fusion["sf_data"]) for oid, _ in legal],
        dtype=np.float32
    )
    sf_norm = (sf_batch - fusion["struct_means"]) / (fusion["struct_stds"] + 1e-8)

    X_cmdr = np.tile(emb[cmdr_idx], (len(legal), 1)).astype(np.float32)

    scores, _ = fusion["forward"](fusion["tower"], X_cmdr, X_card, sf_norm)
    probs = 1.0 / (1.0 + np.exp(-scores.astype(np.float64)))

    # Sort and take top N
    ranked = sorted(zip(legal, probs), key=lambda x: -x[1])
    return [(oid, name, float(prob)) for (oid, name), prob in ranked[:top_n]]


def _get_fusion_score(fusion, cmdr_oid, card_oid):
    """Get tower probability for a card (Stage 1). Returns 0.0 if unavailable."""
    import numpy as np

    oid_to_idx = fusion["oid_to_idx"]
    cmdr_idx = oid_to_idx.get(cmdr_oid)
    card_idx = oid_to_idx.get(card_oid)
    if cmdr_idx is None or card_idx is None:
        return 0.0

    emb = fusion["emb"]
    sf = fusion["compute_sf"](cmdr_oid, card_oid, *fusion["sf_data"])
    sf_norm = (sf - fusion["struct_means"]) / (fusion["struct_stds"] + 1e-8)

    cmdr_emb = emb[cmdr_idx:cmdr_idx + 1]
    card_emb = emb[card_idx:card_idx + 1]
    sf_batch = sf_norm.reshape(1, -1)

    pred, _ = fusion["forward"](fusion["tower"], cmdr_emb, card_emb, sf_batch)
    # Sigmoid to get probability (tower was trained with BCE, raw logits output)
    return float(1.0 / (1.0 + np.exp(-float(pred[0]))))
