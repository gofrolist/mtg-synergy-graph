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

        # Tower model (lazy-loaded on first use — not needed when fusion model is available)
        self._tower_model = None
        self._tower_model_loaded = False

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

    @property
    def tower_model(self):
        """Lazy-load old tower model on first access (display feature only)."""
        if not self._tower_model_loaded:
            self._tower_model = _load_tower_model()
            self._tower_model_loaded = True
        return self._tower_model


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
                         conn, verbose: bool = True,
                         tower_probs: dict = None) -> None:
    """Score all candidates using dynamic features. Modifies candidate_scores in-place.

    Candidates are pre-selected by tower_prefilter() or graph edges.
    Uses batch GBM prediction for speed.

    Args:
        tower_probs: optional {card_name: tower_prob} from prefilter to avoid recomputation.
    """
    # Build card data lookup — ensure all candidates have card data
    card_data = {}
    for c in cards:
        card_data[c["name"]] = c

    # Batch-load card data for candidates not yet in cards list (from tower prefilter)
    missing = [n for n in candidate_scores if n not in card_data]
    if missing:
        # Batch query
        chunk_size = 500
        for i in range(0, len(missing), chunk_size):
            chunk = missing[i:i + chunk_size]
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank "
                f"FROM cards WHERE name IN ({ph}) AND type_line NOT LIKE '%Token%'", chunk
            ).fetchall():
                cd = {"oracle_id": row[0], "name": row[1], "type_line": row[2] or "",
                      "mana_cost": row[3] or "", "cmc": row[4] or 0, "edhrec_rank": row[5]}
                cards.append(cd)
                card_data[row[1]] = cd
                ctx.card_oid[row[1]] = row[0]

    if ctx.edhrec and verbose:
        print(f"  EDHREC synergy: {len(ctx.edhrec)} cards loaded for scoring")

    # Prepare ordered list of candidates with card data
    cand_list = [(name, card_data[name]) for name in candidate_scores if name in card_data]
    if not cand_list:
        return

    w = SCORING_WEIGHTS

    # --- Batch causal loading ---
    if ctx.causal_ctx:
        cand_oids = [cd.get("oracle_id") or ctx.card_oid.get(name, "")
                     for name, cd in cand_list]
        ctx.causal_ctx.batch_load([oid for oid in cand_oids if oid])

    # --- Batch strategy query ---
    all_oids = [(cd.get("oracle_id") or ctx.card_oid.get(name, ""))
                for name, cd in cand_list]
    oid_strats = {}
    if ctx.active_strategies:
        for i in range(0, len(all_oids), 500):
            chunk = all_oids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT oracle_id, strategy FROM card_strategies "
                f"WHERE oracle_id IN ({ph}) AND confidence >= 0.3", chunk
            ):
                oid_strats.setdefault(row[0], set()).add(row[1])

    # --- Compute per-card features + build batch feature matrix for GBM ---
    from mtg_synergy.config import USE_FUSION_MODEL
    fusion = _load_fusion_model() if USE_FUSION_MODEL else None
    use_batch_gbm = fusion is not None

    feature_rows = []  # parallel to cand_list
    per_card_features = []  # dicts for display

    for idx, (card_name, cd) in enumerate(cand_list):
        oid = cd.get("oracle_id") or ctx.card_oid.get(card_name, "")
        type_line = cd.get("type_line", "")
        is_creature = "Creature" in type_line
        rank = cd.get("edhrec_rank") or 50000

        # Tower score (old model, used as display feature)
        tower = _get_tower_score(ctx, oid)

        # Strategy overlap
        strat_overlap = len(oid_strats.get(oid, set()) & ctx.active_strategies) if ctx.active_strategies else 0

        # Tribal match
        tribal_match = False
        tribal_adj = 0.0
        if ctx.is_tribal and is_creature:
            type_lower = type_line.lower()
            if any(t.lower() in type_lower for t in ctx.deck_types):
                tribal_adj = w["TRIBAL_BONUS"]
                tribal_match = True
            else:
                tribal_adj = w["TRIBAL_PENALTY"]

        # Rank
        rank_score = max(0, 3.0 - 0.6 * math.log10(max(rank, 1)))

        # EDHREC synergy
        edhrec_syn = max(0, ctx.edhrec.get(card_name, 0.0))

        # Causal score
        causal = ctx.causal_ctx.causal_score(oid) if ctx.causal_ctx else 0.0

        # Forge overlap
        forge_overlap = 0
        if ctx.forge_cmdr_hints or ctx.forge_cmdr_has:
            cand_has = ctx.forge_card_has.get(card_name, set())
            cand_hints = ctx.forge_card_hints.get(card_name, set())
            forge_overlap = len(cand_has & ctx.forge_cmdr_hints) + len(cand_hints & ctx.forge_cmdr_has)

        # Tower prob (reuse from prefilter if available)
        tower_prob = (tower_probs or {}).get(card_name, None)
        if tower_prob is None and fusion:
            tower_prob = _get_fusion_score(fusion, ctx.cmdr_oid, oid)
        elif tower_prob is None:
            tower_prob = 0.0

        per_card_features.append({
            "tower": round(tower, 1),
            "tribal_match": tribal_match,
            "edhrec_syn": round(edhrec_syn, 3) if edhrec_syn > 0 else 0,
            # Fallback score (used when GBM unavailable)
            "fallback_total": (tower * w["TOWER"]
                               + strat_overlap * w["STRATEGY"]
                               + tribal_adj
                               + rank_score * w["RANK"]
                               + edhrec_syn * w["EDHREC_SYNERGY"]
                               + causal * w.get("CAUSAL", 0)
                               + forge_overlap * w.get("FORGE_DECK_OVERLAP", 0)),
        })

        if use_batch_gbm:
            feature_rows.append([
                tower_prob,
                causal,
                forge_overlap,
                1.0 if tribal_match else 0.0,
                edhrec_syn,
                math.log10(max(rank, 1)),
                cd.get("cmc", 0),
                1.0 if is_creature else 0.0,
            ])

    # --- Batch GBM prediction ---
    if use_batch_gbm and feature_rows:
        import numpy as np
        import warnings
        feature_matrix = np.array(feature_rows, dtype=np.float64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            gbm_scores = fusion["gbm"].predict(feature_matrix, raw_score=True)

        for idx, (card_name, _) in enumerate(cand_list):
            info = candidate_scores[card_name]
            fusion_score = float(gbm_scores[idx])
            pcf = per_card_features[idx]
            info["total"] = fusion_score * w.get("FUSION", 10.0) if fusion_score > 0 else pcf["fallback_total"]
            if pcf["tower"] > 0:
                info["tower_score"] = pcf["tower"]
            if pcf["tribal_match"]:
                info["tribal_match"] = True
            if pcf["edhrec_syn"] > 0:
                info["edhrec_syn"] = pcf["edhrec_syn"]
            if fusion_score > 0:
                info["fusion_score"] = round(fusion_score, 4)
    else:
        for idx, (card_name, _) in enumerate(cand_list):
            info = candidate_scores[card_name]
            pcf = per_card_features[idx]
            info["total"] = pcf["fallback_total"]
            if pcf["tower"] > 0:
                info["tower_score"] = pcf["tower"]
            if pcf["tribal_match"]:
                info["tribal_match"] = True
            if pcf["edhrec_syn"] > 0:
                info["edhrec_syn"] = pcf["edhrec_syn"]

    if verbose:
        print(f"  Dynamic scoring: {len(cand_list)} candidates scored "
              f"({'batch GBM' if use_batch_gbm else 'feature-based'})")


def score_forge_candidates(candidate_scores: dict, cards: list, conn,
                           commander: str, deck_cards: set,
                           deck_types: set = None, active_strategies: set = None,
                           tower_probs: dict = None) -> None:
    """Score candidates using forge-only model (20 features, no EDHREC).

    Uses forge GBM (data/fusion_model_forge.lgb) with features from
    causal graph, strategies, oracle text, and card properties.
    """
    import warnings
    import joblib
    import numpy as np
    from train_tower_model import load_embeddings
    from mtg_synergy.recommend.forge_features import (
        ForgeFeatureContext, CmdrFeatureContext, compute_card_features,
    )

    forge_gbm_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    if not os.path.exists(forge_gbm_path):
        print("  ERROR: forge model not found. Run: python3 train_fusion_model.py --forge-only")
        return

    forge_gbm = joblib.load(forge_gbm_path)

    # Build card data lookup
    card_data = {c["name"]: c for c in cards}
    missing = [n for n in candidate_scores if n not in card_data]
    if missing:
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank, oracle_text "
                f"FROM cards WHERE name IN ({ph}) AND type_line NOT LIKE '%Token%'", chunk
            ).fetchall():
                cd = {"oracle_id": row[0], "name": row[1], "type_line": row[2] or "",
                      "mana_cost": row[3] or "", "cmc": row[4] or 0,
                      "oracle_text": row[6] or ""}
                cards.append(cd)
                card_data[row[1]] = cd

    # Commander OID
    cmdr_oid = ""
    for c in cards:
        if c["name"] == commander:
            cmdr_oid = c.get("oracle_id", "")
            break

    # Load embeddings and build shared context
    normed_emb, _, oid_to_idx = load_embeddings()
    ctx = ForgeFeatureContext(conn, normed_emb, oid_to_idx)

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
        tp = (tower_probs or {}).get(name, 0.0)
        features.append(compute_card_features(oid, tl, cmc, tp, ctx, cmdr_ctx))

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

    print(f"  Forge scoring: {len(cand_list)} candidates scored (22 features, no EDHREC)")


# === Tower model singleton ===

_tower_cache = {}


def _load_tower_model():
    """Load tower model data (cached). Reuses fusion model embeddings when available."""
    if _tower_cache:
        return _tower_cache
    tower_path = os.path.join(DATA_DIR, "tower_model.npz")
    if not os.path.exists(tower_path):
        return None
    try:
        import numpy as np
        td = np.load(tower_path)
        model = {k: td[k] for k in td.files if k not in ("struct_means", "struct_stds")}
        if model["W1"].shape[0] != 140:
            return None

        # Reuse fusion model's pre-loaded embeddings if available
        fusion = _fusion_cache
        if fusion:
            normed_emb = fusion["emb"]
            oid_to_idx = fusion["oid_to_idx"]
            sf_data = fusion["sf_data"]
            compute_sf = fusion["compute_sf"]
            forward_fn = fusion["forward"]
        else:
            from train_tower_model import (load_embeddings, load_structural_features,
                                            compute_struct_features, forward)
            normed_emb, _, oid_to_idx = load_embeddings()
            sf_data = load_structural_features()
            compute_sf = compute_struct_features
            forward_fn = forward

        _tower_cache.update({
            "model": model,
            "means": td["struct_means"],
            "stds": td["struct_stds"],
            "emb": normed_emb,
            "oid_to_idx": oid_to_idx,
            "sf_data": sf_data,
            "compute_sf": compute_sf,
            "forward": forward_fn,
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
                    top_n: int = 3000, deck_cards: set = None,
                    tower_path: str = None) -> list:
    """Score all color-legal cards with the fusion tower and return top N.

    Uses batch forward pass for speed (<200ms for 10k+ cards).
    Returns: [(oracle_id, name, tower_prob), ...] sorted by probability descending.
    Returns empty list if tower model is not available.

    If tower_path is provided, loads that tower instead of the default fusion model.
    """
    fusion = _load_fusion_model()
    if fusion is None:
        return []

    import json
    import numpy as np

    # Use custom tower if provided (e.g., forge tower)
    if tower_path:
        try:
            from train_tower_model import (load_embeddings, load_structural_features,
                                            compute_struct_features, forward as tower_forward)
            custom_td = np.load(tower_path)
            custom_tower = {k: custom_td[k] for k in custom_td.files
                            if k not in ("struct_means", "struct_stds")}
            custom_means = custom_td["struct_means"]
            custom_stds = custom_td["struct_stds"]
        except Exception:
            tower_path = None  # fall back to fusion tower

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
        card_ci = set(json.loads(ci_json or "[]"))
        if cmdr_ci and not card_ci.issubset(cmdr_ci):
            continue
        legal.append((oid, name))

    if not legal:
        return []

    # Chunked tower scoring — process 2000 cards at a time to limit peak memory
    # (avoids allocating full 13k×768 float32 arrays = ~80MB)
    chunk_size = 2000
    cmdr_emb_f32 = emb[cmdr_idx].astype(np.float32)
    all_probs = np.empty(len(legal), dtype=np.float64)

    for ci in range(0, len(legal), chunk_size):
        chunk = legal[ci:ci + chunk_size]
        chunk_indices = [oid_to_idx[oid] for oid, _ in chunk]
        X_card = emb[chunk_indices].astype(np.float32)
        X_cmdr = np.broadcast_to(cmdr_emb_f32, X_card.shape).copy()

        sf_batch = np.array(
            [fusion["compute_sf"](cmdr_oid, oid, *fusion["sf_data"]) for oid, _ in chunk],
            dtype=np.float32
        )
        if tower_path:
            sf_norm = (sf_batch - custom_means) / (custom_stds + 1e-8)
            scores, _ = tower_forward(custom_tower, X_cmdr, X_card, sf_norm)
        else:
            sf_norm = (sf_batch - fusion["struct_means"]) / (fusion["struct_stds"] + 1e-8)
            scores, _ = fusion["forward"](fusion["tower"], X_cmdr, X_card, sf_norm)
        all_probs[ci:ci + len(chunk)] = 1.0 / (1.0 + np.exp(-scores.astype(np.float64)))

    # Sort and take top N
    ranked_idx = np.argpartition(-all_probs, min(top_n, len(all_probs) - 1))[:top_n]
    ranked_idx = ranked_idx[np.argsort(-all_probs[ranked_idx])]
    return [(legal[i][0], legal[i][1], float(all_probs[i])) for i in ranked_idx]


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
