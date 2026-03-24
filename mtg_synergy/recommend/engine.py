"""Card recommendation engine — 4-layer scoring pipeline."""
import json
import math
import os
from collections import defaultdict

from mtg_synergy.config import DATA_DIR, RECOMMENDATION_WEIGHTS, MECHANICS
from mtg_synergy.recommend.affinity import _compute_commander_affinity
from mtg_synergy.combos.detector import find_partial_combos


def recommend_cards(graph: dict, deck_cards: set[str], cards: list[dict],
                    deck_types: set[str] = None, top_n: int = 20,
                    active_strategies: set = None, db_path: str = None,
                    color_identity: set = None, commander: str = None):
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

    # Open a single DB connection for all queries in this function
    _shared_conn = None
    if commander and db_path:
        import sqlite3 as _sql_shared
        try:
            _shared_conn = _sql_shared.connect(db_path)
        except Exception:
            _shared_conn = None

    # Inject LLM-scored cards that aren't in the graph candidate pool.
    # High LLM scores (>=7) should be recommended even without graph edges.
    if commander and db_path and _shared_conn:
        commander_oid_lookup = {}
        for c in cards:
            if c["name"] == commander:
                commander_oid_lookup[commander] = c.get("oracle_id", "")
                break
        _cmdr_oid = commander_oid_lookup.get(commander, "")
        if _cmdr_oid:
            _ic = _shared_conn
            _has = _ic.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='synergy_scores'"
            ).fetchone()[0]
            if _has:
                # Find high-scored cards not in candidate pool.
                # Query DB directly — these cards may have zero graph edges
                # and won't be in the 'cards' list at all.
                high_score_rows = _ic.execute(
                    "SELECT card_oid, score FROM synergy_scores "
                    "WHERE commander_oid = ? AND score >= 7",
                    (_cmdr_oid,)
                ).fetchall()

                # Batch-load card metadata for all high-scored oids
                high_oids = [r[0] for r in high_score_rows]
                _llm_card_data = {}
                _chunk_sz = 500
                for _ci in range(0, len(high_oids), _chunk_sz):
                    _chunk = high_oids[_ci:_ci + _chunk_sz]
                    _ph = ",".join("?" * len(_chunk))
                    for _r in _ic.execute(
                        f"SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank "
                        f"FROM cards WHERE oracle_id IN ({_ph})", _chunk
                    ).fetchall():
                        _llm_card_data[_r[0]] = _r[1:]

                injected = 0
                for oid, score in high_score_rows:
                    row_data = _llm_card_data.get(oid)
                    if not row_data:
                        continue
                    name = row_data[0]
                    if name not in deck_cards and name not in candidate_scores:
                        candidate_scores[name] = {
                            "total": 0.1,
                            "partners": [], "multi_sig": 0,
                            "commander_synergy": 0.0, "key_synergy": 0.0,
                        }
                        cards.append({
                            "oracle_id": oid, "name": name,
                            "type_line": row_data[1] or "", "mana_cost": row_data[2] or "",
                            "cmc": row_data[3] or 0, "edhrec_rank": row_data[4],
                        })
                        injected += 1
                if injected:
                    print(f"  LLM injection: {injected} high-scoring cards added to candidate pool")

    # Build card metadata lookup (name->dict) and oid lookup (name->oid)
    card_meta = {}
    card_oid_lookup = {}
    cards_by_name = {}
    for c in cards:
        name = c["name"]
        card_meta[name] = {
            "type_line": c.get("type_line", ""),
            "cmc": c.get("cmc", 0),
            "mana_cost": c.get("mana_cost", ""),
            "oracle_id": c.get("oracle_id", ""),
            "edhrec_rank": c.get("edhrec_rank"),
        }
        card_oid_lookup[name] = c.get("oracle_id", "")
        cards_by_name[name] = c

    # Calculate deck average CMC (excluding lands)
    deck_cmc_values = [card_meta[n]["cmc"] for n in deck_cards
                       if n in card_meta and "Land" not in card_meta[n].get("type_line", "")]
    deck_avg_cmc = sum(deck_cmc_values) / max(len(deck_cmc_values), 1)

    # Find partial Spellbook combos for combo completion bonus
    partial_missing_oids = set()
    partial_combos = []
    if db_path:
        deck_oids = {card_oid_lookup[n] for n in deck_cards if n in card_oid_lookup}
        partial_combos = find_partial_combos(deck_oids, db_path, color_identity=color_identity)
        for pc in partial_combos:
            for oid in pc.get("missing_oids", []):
                partial_missing_oids.add(oid)

    # Pre-compute lowercased tribal types for fast matching
    deck_types_lower = {t.lower() for t in deck_types} if deck_types else set()

    # Batch-load strategy data and ability counts from DB (1-2 queries instead of N)
    strategy_map = {}  # oid -> set of strategies
    ability_counts = {}  # oid -> count of non-keyword abilities
    if _shared_conn:
        try:
            # Batch strategy query
            if active_strategies:
                for row in _shared_conn.execute(
                    "SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"
                ).fetchall():
                    strategy_map.setdefault(row[0], set()).add(row[1])
            # Batch ability count query
            for row in _shared_conn.execute(
                "SELECT oracle_id, COUNT(*) FROM abilities "
                "WHERE ability_type NOT IN ('keyword') GROUP BY oracle_id"
            ).fetchall():
                ability_counts[row[0]] = row[1]
        except Exception as e:
            print(f"  Warning: Strategy/ability loading skipped ({e})")

    # Commander affinity: compute once via O(1) dict lookup
    affinities = {}
    if commander:
        commander_card_data = cards_by_name.get(commander)
        candidate_cards_data = [c for c in cards if c["name"] not in deck_cards]
        affinities = _compute_commander_affinity(commander_card_data, candidate_cards_data)

    # === Single consolidated loop over all candidates ===
    # Applies: tribal, strategy, combo, CMC penalty, quality, popularity, affinity
    cmc_threshold = deck_avg_cmc + 3
    for card_name, info in candidate_scores.items():
        meta = card_meta.get(card_name, {})
        oid = card_oid_lookup.get(card_name, "")
        type_line = meta.get("type_line", "")
        type_line_lower = type_line.lower()

        # Tribal boost
        if deck_types_lower:
            if any(t in type_line_lower for t in deck_types_lower):
                info["tribal_match"] = True
                info["total"] *= 1.3
            else:
                info["tribal_match"] = False
        else:
            info["tribal_match"] = False

        # Strategy relevance
        if active_strategies and oid:
            card_strats = strategy_map.get(oid, set())
            overlap = card_strats & active_strategies
            rel = 0.5 if not overlap else 1.0 + 0.2 * len(overlap)
            info["total"] *= rel
            info["strategy_rel"] = rel

        # Combo completion
        if oid in partial_missing_oids:
            info["total"] *= 2.0
            info["combo_completion"] = True
        else:
            info["combo_completion"] = False

        # Mana cost penalty
        cmc = meta.get("cmc", 0) or 0
        if cmc > cmc_threshold:
            penalty = max(0.3, 1.0 - 0.15 * (cmc - cmc_threshold))
            info["total"] *= penalty
            info["high_cmc"] = True
        else:
            info["high_cmc"] = False

        # Card quality filter: keyword-only creatures
        if "Creature" in type_line:
            non_kw = ability_counts.get(oid, 0)
            if non_kw == 0:
                info["total"] *= 0.15
                info["keyword_only"] = True
            else:
                if non_kw == 1:
                    info["total"] *= 0.7
                info["keyword_only"] = False
        else:
            info["keyword_only"] = False

        # EDHREC popularity weighting
        rank = meta.get("edhrec_rank")
        if rank and rank > 0:
            popularity = max(0.3, 2.0 - 0.25 * math.log10(max(rank, 1)))
            info["total"] *= popularity
            info["popularity"] = round(popularity, 2)
        else:
            info["total"] *= 0.3
            info["popularity"] = 0.3

        # Commander affinity
        affinity = affinities.get(card_name, 0.0)
        if affinity > 0:
            info["total"] *= 1.0 + 0.5 * affinity
            info["commander_affinity"] = round(affinity, 1)
        else:
            info["commander_affinity"] = 0.0

    # Compute max_graph before mechanics/synergy scoring — needed for normalization
    max_graph = max((i["total"] for i in candidate_scores.values()), default=1.0)
    if max_graph <= 0:
        max_graph = 1.0

    # Mechanics-based matching: uses structured game event data
    # extracted by extract_mechanics.py to find filter-aware synergies.
    # Two roles: (1) boost existing candidates, (2) inject new candidates
    # that have strong mechanics match but no graph edges.
    if commander and db_path:
        try:
            from mechanics_matcher import load_mechanics, compute_deck_synergies
            all_mechanics = load_mechanics(db_path)
            if all_mechanics:
                commander_oid = card_oid_lookup.get(commander, "")

                # Score ALL cards with mechanics (not just current candidates)
                all_oids_with_mechs = list(all_mechanics.keys())
                _mc = _shared_conn
                # Batch load type_lines in chunks instead of N+1 queries
                card_type_lookup = {}
                _chunk_size = 500
                for _ci in range(0, len(all_oids_with_mechs), _chunk_size):
                    _chunk = all_oids_with_mechs[_ci:_ci + _chunk_size]
                    _placeholders = ",".join("?" * len(_chunk))
                    for _r in _mc.execute(
                        f"SELECT oracle_id, type_line FROM cards WHERE oracle_id IN ({_placeholders})",
                        _chunk
                    ).fetchall():
                        card_type_lookup[_r[0]] = _r[1] or ""
                card_type_lookup[commander_oid] = card_meta.get(commander, {}).get("type_line", "")

                # Pass deck card oids for two-step chain detection
                deck_card_oids = {card_oid_lookup.get(cn, "") for cn in deck_cards if card_oid_lookup.get(cn)}
                mech_scores = compute_deck_synergies(commander_oid, all_oids_with_mechs,
                                                     all_mechanics, card_type_lookup,
                                                     deck_oids=deck_card_oids)

                if mech_scores:
                    max_mech = max(mech_scores.values()) if mech_scores else 1.0
                    mech_count = 0
                    mech_injected = 0

                    # Batch-load card metadata for high-scoring mechanics cards
                    high_mech_oids = [oid for oid, ms in mech_scores.items() if ms >= 1.5]
                    _mech_card_data = {}  # oid -> (name, type_line, mana_cost, cmc, edhrec_rank, color_identity)
                    for _ci in range(0, len(high_mech_oids), _chunk_size):
                        _chunk = high_mech_oids[_ci:_ci + _chunk_size]
                        _placeholders = ",".join("?" * len(_chunk))
                        for _r in _mc.execute(
                            f"SELECT oracle_id, name, type_line, mana_cost, cmc, edhrec_rank, color_identity "
                            f"FROM cards WHERE oracle_id IN ({_placeholders})",
                            _chunk
                        ).fetchall():
                            _mech_card_data[_r[0]] = _r[1:]

                    # Inject high-mechanics cards not in candidate pool
                    for oid, ms in mech_scores.items():
                        if ms < 1.5:
                            continue
                        row_data = _mech_card_data.get(oid)
                        if not row_data:
                            continue
                        name, type_line, mana_cost, cmc, edhrec_rank, ci_json = row_data
                        if name in deck_cards or name in candidate_scores:
                            continue
                        # Check color identity
                        card_ci = set(json.loads(ci_json or "[]"))
                        if not card_ci.issubset(color_identity or set()):
                            continue
                        candidate_scores[name] = {
                            "total": 0.1, "partners": [], "multi_sig": 0,
                            "commander_synergy": 0.0, "key_synergy": 0.0,
                        }
                        cards.append({
                            "oracle_id": oid, "name": name,
                            "type_line": type_line or "", "mana_cost": mana_cost or "",
                            "cmc": cmc or 0, "edhrec_rank": edhrec_rank,
                        })
                        card_meta[name] = {
                            "type_line": type_line or "", "cmc": cmc or 0,
                            "mana_cost": mana_cost or "",
                            "oracle_id": oid, "edhrec_rank": edhrec_rank,
                        }
                        card_oid_lookup[name] = oid
                        mech_injected += 1

                    # Boost using max(graph, mechanics) with relative normalization.
                    # (max_graph computed before mechanics block)

                    for card_name, info in candidate_scores.items():
                        oid = card_oid_lookup.get(card_name, "")
                        ms = mech_scores.get(oid, 0)
                        if ms > 0:
                            mech_as_graph = (ms / max_mech) * max_graph
                            info["total"] = max(info["total"], mech_as_graph)
                            info["mechanics_score"] = round(ms, 1)
                            mech_count += 1

                    if mech_count or mech_injected:
                        print(f"  Mechanics matching: {mech_count} boosted, "
                              f"{mech_injected} injected ({len(all_mechanics)} in DB)")
        except ImportError:
            pass
        except Exception as e:
            pass

    # Synergy scoring: 3-tier fallback
    # 1. LLM scores (pre-computed via score_synergies.py) — best quality
    # 2. Tower model (via train_tower_model.py) — generalizes to any commander
    # 3. Graph-only scoring (current multipliers) — always available
    if commander and db_path and _shared_conn:
        commander_oid = card_oid_lookup.get(commander, "")
        if commander_oid:
            # Tier 1: Direct LLM scores for this commander
            llm_scores = {}
            has_table = _shared_conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='synergy_scores'"
            ).fetchone()[0]
            if has_table:
                for row in _shared_conn.execute(
                    "SELECT card_oid, score FROM synergy_scores WHERE commander_oid = ?",
                    (commander_oid,)
                ).fetchall():
                    llm_scores[row[0]] = row[1]

            # Tier 2: Two-tower model (instant, for cards without LLM scores)
            model_scores = {}
            tower_model_path = os.path.join(DATA_DIR, "tower_model.npz")
            if os.path.exists(tower_model_path):
                try:
                    import numpy as _np
                    from train_tower_model import (load_embeddings as _load_emb,
                                                    load_structural_features as _load_sf,
                                                    compute_struct_features as _compute_sf,
                                                    forward as _forward)

                    from mtg_synergy.combos.detector import compute_strategy_relevance

                    if not hasattr(compute_strategy_relevance, '_tower_model'):
                        _td = _np.load(tower_model_path)
                        compute_strategy_relevance._tower_model = {
                            k: _td[k] for k in _td.files
                            if k not in ("struct_means", "struct_stds")
                        }
                        compute_strategy_relevance._tower_means = _td["struct_means"]
                        compute_strategy_relevance._tower_stds = _td["struct_stds"]
                        compute_strategy_relevance._tower_emb = _load_emb()
                        compute_strategy_relevance._tower_sf = _load_sf()

                        if compute_strategy_relevance._tower_model["W1"].shape[0] != 140:
                            print("  Warning: Tower model outdated, skipping. Retrain: python3 train_tower_model.py")
                            raise ValueError("Tower model dimension mismatch")

                    tm = compute_strategy_relevance._tower_model
                    t_means = compute_strategy_relevance._tower_means
                    t_stds = compute_strategy_relevance._tower_stds
                    normed_emb, oid_list, oid_to_idx = compute_strategy_relevance._tower_emb
                    sf_data = compute_strategy_relevance._tower_sf

                    cmdr_emb_idx = oid_to_idx.get(commander_oid)
                    if cmdr_emb_idx is not None:
                        # Batch score all unscored candidates
                        batch_embs = []
                        batch_structs = []
                        batch_names = []
                        for card_name in candidate_scores:
                            oid = card_oid_lookup.get(card_name, "")
                            if oid in llm_scores:
                                continue
                            card_emb_idx = oid_to_idx.get(oid)
                            if card_emb_idx is None:
                                continue
                            batch_embs.append(normed_emb[card_emb_idx])
                            sf = _compute_sf(commander_oid, oid, *sf_data)
                            batch_structs.append((sf - t_means) / t_stds)
                            batch_names.append(card_name)

                        if batch_embs:
                            X_card = _np.array(batch_embs, dtype=_np.float32)
                            X_struct = _np.array(batch_structs, dtype=_np.float32)
                            X_cmdr = _np.tile(normed_emb[cmdr_emb_idx], (len(X_card), 1))
                            scores_arr, _ = _forward(tm, X_cmdr, X_card, X_struct)
                            scores_arr = _np.clip(scores_arr, 1, 10)
                            for name, score in zip(batch_names, scores_arr):
                                model_scores[name] = float(score)

                except Exception:
                    pass

            # Pre-compute commander tag overlap for tiebreaking.
            # Cards that share provides/wants tags with the commander are more
            # specifically synergistic than generic staples — even within the
            # same LLM score tier.
            cmdr_tag_overlap = {}
            if _shared_conn and commander_oid:
                cmdr_provides = set(r[0] for r in _shared_conn.execute(
                    "SELECT tag FROM provides WHERE oracle_id = ?", (commander_oid,)))
                cmdr_wants = set(r[0] for r in _shared_conn.execute(
                    "SELECT tag FROM wants WHERE oracle_id = ?", (commander_oid,)))
                # Batch-load candidate tags
                _all_cand_oids = [card_oid_lookup.get(cn, "") for cn in candidate_scores if card_oid_lookup.get(cn)]
                _cand_provides = {}  # oid -> set of tags
                _cand_wants = {}
                _chunk_size = 500
                for _ci in range(0, len(_all_cand_oids), _chunk_size):
                    _chunk = _all_cand_oids[_ci:_ci + _chunk_size]
                    _ph = ",".join("?" * len(_chunk))
                    for r in _shared_conn.execute(
                        f"SELECT oracle_id, tag FROM provides WHERE oracle_id IN ({_ph})", _chunk
                    ).fetchall():
                        _cand_provides.setdefault(r[0], set()).add(r[1])
                    for r in _shared_conn.execute(
                        f"SELECT oracle_id, tag FROM wants WHERE oracle_id IN ({_ph})", _chunk
                    ).fetchall():
                        _cand_wants.setdefault(r[0], set()).add(r[1])
                for card_name in candidate_scores:
                    oid = card_oid_lookup.get(card_name, "")
                    cp = _cand_provides.get(oid, set())
                    cw = _cand_wants.get(oid, set())
                    # Card provides what commander wants + card wants what commander provides
                    cmdr_tag_overlap[card_name] = len(cp & cmdr_wants) + len(cw & cmdr_provides)

            # Apply LLM/model scores.
            # LLM score is the PRIMARY ranking signal.
            # Tiebreakers: commander tag overlap (commander-specific), then tower model,
            # then EDHREC rank. This ensures cards with direct commander synergy
            # outrank generic staples within the same LLM score tier.
            if llm_scores or model_scores:
                llm_count = 0
                model_count = 0

                for card_name, info in candidate_scores.items():
                    oid = card_oid_lookup.get(card_name, "")
                    llm = llm_scores.get(oid)
                    ms = model_scores.get(card_name) if llm is None else None

                    if llm is not None or ms is not None:
                        score_val = llm if llm is not None else ms
                        # Tiebreakers (all within same LLM score tier):
                        # 1. Commander tag overlap (direct synergy signal, commander-specific)
                        # 2. Tower model prediction (continuous, captures synergy nuance)
                        # 3. EDHREC rank (card quality/popularity)
                        meta = card_meta.get(card_name, {})
                        rank = meta.get("edhrec_rank") or 50000
                        rank_tiebreak = max(0, 10.0 - 2.0 * math.log10(max(rank, 1)))
                        tower_score = model_scores.get(card_name, 5.0) if card_name in model_scores else 5.0
                        overlap = cmdr_tag_overlap.get(card_name, 0)
                        # LLM primary (×1000), tower (×10), rank (×0.1)
                        # Note: commander tag overlap computed above but not weighted in —
                        # experiments showed it helps tribal decks but hurts others.
                        # Stored in info for display: info["cmdr_overlap"]
                        info["total"] = score_val * 1000.0 + tower_score * 10.0 + rank_tiebreak * 0.1
                        if overlap > 0:
                            info["cmdr_overlap"] = overlap
                        info["llm_score"] = score_val if llm is not None else round(ms, 1)
                        if llm is not None:
                            llm_count += 1
                        else:
                            model_count += 1
                    else:
                        # Unscored cards: treat as LLM score 2 (low) + graph tiebreaker
                        graph_norm = info["total"] / max_graph
                        info["total"] = 2 * 1000.0 + graph_norm * 10.0

                parts = []
                if llm_count:
                    parts.append(f"{llm_count} LLM-scored")
                if model_count:
                    parts.append(f"{model_count} model-scored")
                if parts:
                    print(f"  Synergy scoring: {', '.join(parts)} [LLM-primary]")

    # Close shared DB connection
    if _shared_conn:
        _shared_conn.close()

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
        print(f"\n  {pct:5.1f}% {bar} {card}{tribal}{combo}{strat_str}{affinity_str}{high_cmc}")
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
