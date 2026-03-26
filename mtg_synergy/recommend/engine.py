"""Card recommendation engine — 4-layer scoring pipeline."""
import json
import math
import os
from collections import defaultdict

from mtg_synergy.config import DATA_DIR, RECOMMENDATION_WEIGHTS, MECHANICS
from mtg_synergy.recommend.affinity import _compute_commander_affinity
from mtg_synergy.combos.detector import find_partial_combos

# Tags excluded from overlap tiebreaker (too common to be discriminative)
OVERLAP_EXCLUDE = {"board-generic"}


def recommend_cards(graph: dict, deck_cards: set[str], cards: list[dict],
                    deck_types: set[str] = None, top_n: int = 20,
                    active_strategies: set = None, db_path: str = None,
                    color_identity: set = None, commander: str = None,
                    edhrec_slug: str = None):
    """Rank non-deck cards by total synergy with the current decklist.

    Commander synergy is weighted 5x, key card synergy 3x.
    If deck_types is provided, cards matching those types get a synergy boost.
    If active_strategies is provided, cards matching strategies get a relevance multiplier.
    Combo completions get x2.0.
    """
    # Identify key cards: top 10 highest-synergy cards in the deck (excluding commander)
    deck_scores = _deck_card_scores(graph, deck_cards)
    key_cards_ranked = sorted(
        [(name, info["total"]) for name, info in deck_scores.items() if name != commander],
        key=lambda x: -x[1]
    )
    key_cards = {name for name, score in key_cards_ranked[:10]}

    candidate_scores = _candidate_scores(graph, deck_cards, commander=commander, key_cards=key_cards)


    # === Dynamic feature-based scoring ===
    # Replaces static LLM scores with features computed at recommendation time:
    # tower model + mechanics + tag overlap + strategy + tribal + rank + EDHREC
    import sqlite3 as _sql_dyn
    _shared_conn = None
    card_meta = {}
    card_oid_lookup = {}
    partial_combos = []

    if commander and db_path:
        try:
            _shared_conn = _sql_dyn.connect(db_path)
        except Exception:
            _shared_conn = None

    if _shared_conn:
        from mtg_synergy.recommend.scoring import DeckContext, score_all_candidates

        ctx = DeckContext(_shared_conn, commander, deck_cards, cards,
                          deck_types=deck_types, active_strategies=active_strategies,
                          edhrec_slug=edhrec_slug)

        # Score all candidates (handles EDHREC injection too)
        score_all_candidates(candidate_scores, cards, ctx, _shared_conn)

        # Build card metadata (after injection may have added cards)
        for c in cards:
            card_meta[c["name"]] = {
                "type_line": c.get("type_line", ""), "cmc": c.get("cmc", 0),
                "mana_cost": c.get("mana_cost", ""), "oracle_id": c.get("oracle_id", ""),
                "edhrec_rank": c.get("edhrec_rank"),
            }
            card_oid_lookup[c["name"]] = c.get("oracle_id", "")

        # Find partial Spellbook combos for display
        deck_oids = {card_oid_lookup[n] for n in deck_cards if n in card_oid_lookup}
        partial_combos = find_partial_combos(deck_oids, db_path, color_identity=color_identity)
        for pc in partial_combos:
            for oid in pc.get("missing_oids", []):
                for cn in candidate_scores:
                    if card_oid_lookup.get(cn) == oid:
                        candidate_scores[cn]["combo_completion"] = True

        _shared_conn.close()
    else:
        # Fallback: just use graph scores
        for c in cards:
            card_meta[c["name"]] = {
                "type_line": c.get("type_line", ""), "cmc": c.get("cmc", 0),
                "mana_cost": c.get("mana_cost", ""), "oracle_id": c.get("oracle_id", ""),
                "edhrec_rank": c.get("edhrec_rank"),
            }
            card_oid_lookup[c["name"]] = c.get("oracle_id", "")

    # Sort by total synergy
    ranked = sorted(candidate_scores.items(), key=lambda x: x[1]["total"], reverse=True)

    # Normalize scores to 0-100% scale (top card = 100%)
    max_score = ranked[0][1]["total"] if ranked else 1.0
    if max_score <= 0:
        max_score = 1.0
    for card, info in ranked:
        info["pct"] = round(info["total"] / max_score * 100, 1)

    # --- Output ---
    print(f"\n{'═' * 70}")
    header = f"TOP {top_n} RECOMMENDED CARDS (not in deck)"
    if active_strategies:
        header += f" | strategies: {', '.join(sorted(active_strategies))}"
    print(header)
    if deck_types:
        print(f"  Tribal boost: {', '.join(sorted(deck_types))} (+30%)")
    print(f"{'═' * 70}")

    # Show combo completions first
    completions = [(c, i) for c, i in ranked if i.get("combo_completion")]
    if completions:
        print(f"\n  COMBO COMPLETIONS (1 card away from confirmed infinite):")
        for card, info in completions[:5]:
            matching = [pc for pc in partial_combos
                        if card_oid_lookup.get(card) in pc.get("missing_oids", [])]
            for pc in matching[:2]:
                print(f"    {' + '.join(pc['present_cards'])} + [{card}]")
                print(f"      → {pc['result']}")
        print()

    for card, info in ranked[:top_n]:
        partners = sorted(info["partners"], key=lambda x: x[1], reverse=True)
        multi = f" ({info['multi_sig']} multi-signal)" if info["multi_sig"] else ""
        meta = card_meta.get(card, {})
        type_line = meta.get("type_line", "")
        cmc = meta.get("cmc", 0)
        tribal = " [tribal]" if info.get("tribal_match") else ""
        combo = " [COMBO]" if info.get("combo_completion") else ""
        strat_rel = info.get("strategy_rel")
        strat_str = f" [strat×{strat_rel:.1f}]" if strat_rel and strat_rel != 1.0 else ""
        high_cmc = " [high CMC]" if info.get("high_cmc") else ""
        affinity = info.get("commander_affinity", 0)
        affinity_str = f" [cmdr:{affinity:.0f}]" if affinity > 0 else ""
        pct = info["pct"]
        bar_len = round(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        tower_str = f" T={info['tower_score']}" if info.get("tower_score") else ""
        mech_str = f" M={info['mechanics_score']}" if info.get("mechanics_score") else ""
        edhrec_str = f" EDH={info['edhrec_syn']:.2f}" if info.get("edhrec_syn") else ""
        overlap_str = f" ov={info['cmdr_overlap']}" if info.get("cmdr_overlap") else ""
        sk_str = f" sk={info['strat_keywords']}" if info.get("strat_keywords") else ""
        print(f"\n  {pct:5.1f}% {bar} {card}{tribal}{combo}{tower_str}{mech_str}{sk_str}{edhrec_str}{overlap_str}{high_cmc}")
        print(f"    {type_line} | CMC {cmc} | {len(partners)} partners{multi}")
        for partner, score, sigs in partners[:5]:
            sig = f"{sigs}sig" if sigs > 1 else "1sig"
            print(f"    ↔ {partner:<30} ({score:.1f}, {sig})")
        if len(partners) > 5:
            print(f"    ... and {len(partners) - 5} more")


def _deck_card_scores(graph: dict, deck_cards: set[str]) -> dict:
    """Compute per-card synergy totals within the deck. Returns {card: {total, partners}}."""
    adj = graph["adjacency"]
    card_synergy = defaultdict(float)
    card_partners = defaultdict(int)

    seen = set()
    for card in deck_cards:
        for edge in adj.get(card, []):
            if edge["target"] in deck_cards:
                pair = tuple(sorted([edge["source"], edge["target"]]))
                if pair not in seen:
                    seen.add(pair)
                    card_synergy[edge["source"]] += edge["score"]
                    card_synergy[edge["target"]] += edge["score"]
                    card_partners[edge["source"]] += 1
                    card_partners[edge["target"]] += 1

    return {
        card: {"total": card_synergy.get(card, 0.0), "partners": card_partners.get(card, 0)}
        for card in deck_cards
    }


def _candidate_scores(graph: dict, deck_cards: set[str],
                      commander: str = None, key_cards: set = None) -> dict:
    """Compute synergy totals for non-deck cards against the decklist."""
    adj = graph["adjacency"]
    scores = defaultdict(lambda: {"total": 0.0, "partners": [], "multi_sig": 0,
                                  "commander_synergy": 0.0, "key_synergy": 0.0})

    if key_cards is None:
        key_cards = set()

    for card in deck_cards:
        for edge in adj.get(card, []):
            target = edge["target"]
            if target not in deck_cards:
                info = scores[target]
                base_score = edge["score"]

                if card == commander:
                    info["commander_synergy"] += base_score
                    info["total"] += base_score * 5.0  # Commander synergy weighted 5x
                elif card in key_cards:
                    info["key_synergy"] += base_score
                    info["total"] += base_score * 2.0  # Key card synergy weighted 2x
                else:
                    info["total"] += base_score

                info["partners"].append((card, edge["score"], edge["signals"]))
                if edge["signals"] >= 2:
                    info["multi_sig"] += 1

    return dict(scores)


# === Shared helpers for apply_llm_scoring (used by swaps) ===

def _load_edhrec_synergy(conn, edhrec_slug: str) -> dict:
    """Load EDHREC synergy map for a commander (DFC-aware). Returns {card_name: synergy}."""
    if not conn or not edhrec_slug:
        return {}
    has = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edhrec_card_synergy'"
    ).fetchone()[0]
    if not has:
        return {}
    raw = {}
    for row in conn.execute(
        "SELECT card_name, synergy FROM edhrec_card_synergy WHERE commander_slug = ?",
        (edhrec_slug,)).fetchall():
        raw[row[0]] = row[1]
    dfc_map = {}
    for row in conn.execute("SELECT name FROM cards WHERE name LIKE '%//%'").fetchall():
        dfc_map[row[0].split(" // ")[0]] = row[0]
    result = {}
    for name, syn in raw.items():
        result[name] = syn
        dfc_full = dfc_map.get(name)
        if dfc_full and dfc_full not in result:
            result[dfc_full] = syn
    return result


def _inject_candidate(candidate_scores, cards, card_oid_lookup, card_meta,
                      oid, row_data):
    """Add a card to the candidate pool (scores, cards list, lookups)."""
    name = row_data[0]
    type_line = row_data[1] or ""
    mana_cost = row_data[2] or ""
    cmc = row_data[3] or 0
    edhrec_rank = row_data[4]
    candidate_scores[name] = {
        "total": 0.1, "partners": [], "multi_sig": 0,
        "commander_synergy": 0.0, "key_synergy": 0.0,
    }
    cards.append({"oracle_id": oid, "name": name, "type_line": type_line,
                  "mana_cost": mana_cost, "cmc": cmc, "edhrec_rank": edhrec_rank})
    card_oid_lookup[name] = oid
    card_meta[name] = {"type_line": type_line, "cmc": cmc, "mana_cost": mana_cost,
                       "oracle_id": oid, "edhrec_rank": edhrec_rank}


def _score_tower_model(cmdr_oid, candidate_scores, card_oid_lookup, llm_scores):
    """Score unscored candidates with the two-tower model. Returns {card_name: score}."""
    model_scores = {}
    tower_path = os.path.join(DATA_DIR, "tower_model.npz")
    if not cmdr_oid or not os.path.exists(tower_path):
        return model_scores
    try:
        import numpy as _np
        from train_tower_model import (load_embeddings, load_structural_features,
                                        compute_struct_features, forward)
        from mtg_synergy.combos.detector import compute_strategy_relevance as _csr
        if not hasattr(_csr, '_tower_model'):
            _td = _np.load(tower_path)
            _csr._tower_model = {k: _td[k] for k in _td.files if k not in ("struct_means", "struct_stds")}
            _csr._tower_means = _td["struct_means"]
            _csr._tower_stds = _td["struct_stds"]
            _csr._tower_emb = load_embeddings()
            _csr._tower_sf = load_structural_features()
            if _csr._tower_model["W1"].shape[0] != 140:
                raise ValueError("Tower model dimension mismatch")
        tm = _csr._tower_model
        t_means, t_stds = _csr._tower_means, _csr._tower_stds
        normed_emb, _, oid_to_idx = _csr._tower_emb
        sf_data = _csr._tower_sf
        cmdr_idx = oid_to_idx.get(cmdr_oid)
        if cmdr_idx is None:
            return model_scores
        batch_e, batch_s, batch_n = [], [], []
        for name in candidate_scores:
            oid = card_oid_lookup.get(name, "")
            if oid in llm_scores:
                continue
            idx = oid_to_idx.get(oid)
            if idx is None:
                continue
            batch_e.append(normed_emb[idx])
            batch_s.append((compute_struct_features(cmdr_oid, oid, *sf_data) - t_means) / t_stds)
            batch_n.append(name)
        if batch_e:
            X_card = _np.array(batch_e, dtype=_np.float32)
            X_struct = _np.array(batch_s, dtype=_np.float32)
            X_cmdr = _np.tile(normed_emb[cmdr_idx], (len(X_card), 1))
            arr, _ = forward(tm, X_cmdr, X_card, X_struct)
            for name, score in zip(batch_n, _np.clip(arr, 1, 10)):
                model_scores[name] = float(score)
    except Exception:
        pass
    return model_scores


def _compute_tag_overlap(conn, cmdr_oid, candidate_scores, card_oid_lookup):
    """Compute commander tag overlap for each candidate. Returns {card_name: count}."""
    result = {}
    if not cmdr_oid:
        return result
    cmdr_p = set(r[0] for r in conn.execute(
        "SELECT tag FROM provides WHERE oracle_id = ?", (cmdr_oid,))) - OVERLAP_EXCLUDE
    cmdr_w = set(r[0] for r in conn.execute(
        "SELECT tag FROM wants WHERE oracle_id = ?", (cmdr_oid,))) - OVERLAP_EXCLUDE
    all_oids = [card_oid_lookup.get(cn, "") for cn in candidate_scores if card_oid_lookup.get(cn)]
    cand_p, cand_w = {}, {}
    for i in range(0, len(all_oids), 500):
        chunk = all_oids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT oracle_id, tag FROM provides WHERE oracle_id IN ({ph})", chunk).fetchall():
            cand_p.setdefault(r[0], set()).add(r[1])
        for r in conn.execute(f"SELECT oracle_id, tag FROM wants WHERE oracle_id IN ({ph})", chunk).fetchall():
            cand_w.setdefault(r[0], set()).add(r[1])
    for name in candidate_scores:
        oid = card_oid_lookup.get(name, "")
        result[name] = len((cand_p.get(oid, set()) - OVERLAP_EXCLUDE) & cmdr_w) + \
                        len((cand_w.get(oid, set()) - OVERLAP_EXCLUDE) & cmdr_p)
    return result


def apply_llm_scoring(candidate_scores, cards, deck_cards,
                      commander=None, db_path=None, edhrec_slug=None,
                      color_identity=None, verbose=True):
    """Apply LLM/tower/EDHREC scoring to candidate_scores (in-place).

    Shared pipeline used by suggest_swaps(). Handles injection, LLM loading,
    tower model, and the scoring formula. Modifies candidate_scores in place.
    """
    if not commander or not db_path:
        return

    import sqlite3 as _sql
    try:
        conn = _sql.connect(db_path)
    except Exception:
        return

    card_oid_lookup = {}
    card_meta = {}
    for c in cards:
        card_oid_lookup[c["name"]] = c.get("oracle_id", "")
        card_meta[c["name"]] = {
            "type_line": c.get("type_line", ""), "cmc": c.get("cmc", 0),
            "mana_cost": c.get("mana_cost", ""), "oracle_id": c.get("oracle_id", ""),
            "edhrec_rank": c.get("edhrec_rank"),
        }

    _cmdr_oid = card_oid_lookup.get(commander, "")

    # EDHREC loading + injection
    edhrec_synergy_map = _load_edhrec_synergy(conn, edhrec_slug)

    # Tower model
    model_scores = _score_tower_model(_cmdr_oid, candidate_scores, card_oid_lookup, {})

    # Fusion model (tower + LightGBM hybrid)
    from mtg_synergy.config import USE_FUSION_MODEL
    from mtg_synergy.recommend.scoring import _load_fusion_model, _get_fusion_score
    fusion = _load_fusion_model() if USE_FUSION_MODEL else None

    # Apply scoring formula
    max_graph = max((i["total"] for i in candidate_scores.values()), default=1.0) or 1.0
    w = RECOMMENDATION_WEIGHTS

    if fusion is not None:
        import numpy as np
        cmdr_tag_overlap = _compute_tag_overlap(conn, _cmdr_oid, candidate_scores, card_oid_lookup)
        for card_name, info in candidate_scores.items():
            oid = card_oid_lookup.get(card_name, "")
            if not oid:
                continue
            tower_prob = _get_fusion_score(fusion, _cmdr_oid, oid)
            edhrec_syn = max(0, edhrec_synergy_map.get(card_name, 0.0))
            overlap = cmdr_tag_overlap.get(card_name, 0)
            meta = card_meta.get(card_name, {})
            rank = meta.get("edhrec_rank") or 50000

            features_10 = np.array([[
                tower_prob, 0.0, 0.0,  # causal/forge not available in swaps
                overlap, 0.0, 0.0,
                edhrec_syn,
                math.log10(max(rank, 1)),
                meta.get("cmc", 0),
                1.0 if "Creature" in meta.get("type_line", "") else 0.0,
            ]])
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                score = float(fusion["gbm"].predict_proba(features_10)[0][1])
            info["total"] = score * 10000  # scale to match LLM formula range
            info["fusion_score"] = round(score, 3)
    elif model_scores:
        cmdr_tag_overlap = _compute_tag_overlap(conn, _cmdr_oid, candidate_scores, card_oid_lookup)
        for card_name, info in candidate_scores.items():
            oid = card_oid_lookup.get(card_name, "")
            ms = model_scores.get(card_name)
            if ms is not None:
                meta = card_meta.get(card_name, {})
                rank = meta.get("edhrec_rank") or 50000
                rank_tb = max(0, 10.0 - 2.0 * math.log10(max(rank, 1)))
                overlap = cmdr_tag_overlap.get(card_name, 0)
                edhrec_syn = max(0, edhrec_synergy_map.get(card_name, 0.0))
                info["total"] = (ms * w["TOWER"] + edhrec_syn * w["EDHREC_SYNERGY"]
                                 + overlap * w["OVERLAP"] + rank_tb * w["RANK_TIEBREAK"])
                if edhrec_syn > 0:
                    info["edhrec_syn"] = round(edhrec_syn, 3)
            else:
                edhrec_syn = max(0, edhrec_synergy_map.get(card_name, 0.0))
                graph_norm = info["total"] / max_graph
                if edhrec_syn > 0:
                    est = 4.0 + edhrec_syn * 6.0
                    info["total"] = est * w["TOWER"] + edhrec_syn * w["EDHREC_SYNERGY"] + graph_norm * w["TOWER"]
                else:
                    info["total"] = 2 * w["TOWER"] + graph_norm * w["TOWER"]

    conn.close()
