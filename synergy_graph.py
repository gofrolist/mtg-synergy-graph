"""
Build a synergy graph from merged card profiles.

Creates directed edges between cards based on provides/wants relationships
and categorical clustering from Scryfall function tags.

Edge types:
  1. provides→wants: Card A provides X, Card B wants X → directed edge A→B
  2. shared-tag: Cards sharing the same Scryfall function tag → undirected edge
  3. role-complement: Cards fulfilling complementary roles → weak undirected edge

Usage:
    python3 synergy_graph.py --deck kyler              # build + print top synergies
    python3 synergy_graph.py --deck krenko --validate  # compare vs hand-curated pairs
    python3 synergy_graph.py --deck kyler --card "Hardened Scales"
    python3 synergy_graph.py --deck kyler --visualize  # interactive HTML graph
    python3 synergy_graph.py --deck kyler --export
"""

import argparse
import json
import os
from collections import defaultdict

from mtg_synergy.constants import (
    SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES,
    _provides_satisfies_want,
)
from mtg_synergy.graph import (
    build_graph, build_provides_wants_edges,
    build_peer_edges, build_shared_wants_edges,
    build_embedding_edges,
)
from mtg_synergy.combos import (
    find_combos, find_combos_tiered, find_partial_combos,
    compute_strategy_relevance, find_anti_synergy,
)
from mtg_synergy.combos.display import (
    show_combos, show_combos_tiered, validate_against_curated,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_merged(path: str) -> list[dict]:
    from normalize_tags import normalize_cards

    with open(path) as f:
        cards = json.load(f)
    # Normalize provides/wants vocabulary + infer missing wants
    normalize_cards(cards)
    return cards


def show_card_synergies(graph: dict, card_name: str, top_n: int = 20):
    """Show top synergies for a specific card."""
    adj = graph["adjacency"]
    edges = adj.get(card_name, [])

    if not edges:
        print(f"No synergies found for '{card_name}'")
        # Try fuzzy match
        matches = [k for k in adj if card_name.lower() in k.lower()]
        if matches:
            print(f"Did you mean: {', '.join(matches[:5])}?")
        return

    ranked = sorted(edges, key=lambda e: e["score"], reverse=True)

    print(f"\nTop synergies for: {card_name}")
    print(f"{'─' * 60}")
    for edge in ranked[:top_n]:
        sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
        print(f"\n  {edge['target']}  (score: {edge['score']}, {sig})")
        for reason in edge["reasons"][:3]:
            print(f"    {reason}")


def show_deck_synergies(graph: dict, deck_cards: set[str], commander: str,
                        cards: list[dict] = None, top_n: int = 30):
    """Show the synergy network within the deck — which cards synergize with each other."""
    adj = graph["adjacency"]

    # Collect all edges between deck cards
    deck_edges = []
    seen = set()
    for card in deck_cards:
        for edge in adj.get(card, []):
            if edge["target"] in deck_cards:
                pair = tuple(sorted([edge["source"], edge["target"]]))
                if pair not in seen:
                    seen.add(pair)
                    deck_edges.append(edge)

    deck_edges.sort(key=lambda e: e["score"], reverse=True)

    print(f"\n{'═' * 70}")
    print(f"DECK SYNERGY MAP — {len(deck_edges)} edges between {len(deck_cards)} deck cards")
    print(f"{'═' * 70}")

    # Top edges within the deck
    print(f"\nTop {top_n} in-deck synergy pairs:")
    print(f"{'─' * 70}")
    for edge in deck_edges[:top_n]:
        sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
        print(f"  [{edge['score']:5.1f} {sig}] {edge['source']} ↔ {edge['target']}")
        for r in edge["reasons"][:2]:
            print(f"       {r}")

    # Per-card connectivity within the deck
    card_synergy = defaultdict(float)
    card_partners = defaultdict(int)
    for edge in deck_edges:
        card_synergy[edge["source"]] += edge["score"]
        card_synergy[edge["target"]] += edge["score"]
        card_partners[edge["source"]] += 1
        card_partners[edge["target"]] += 1

    ranked = sorted(card_synergy.items(), key=lambda x: x[1], reverse=True)
    print(f"\nDeck card synergy ranking (total score across in-deck edges):")
    print(f"{'─' * 70}")
    print(f"  {'Card':<35} {'Score':>7} {'Partners':>10}")
    for card, total in ranked:
        partners = card_partners[card]
        marker = " ★" if card == commander else ""
        print(f"  {card:<35} {total:7.1f} {partners:>10}{marker}")

    # Weakly connected cards (potential cuts)
    if ranked:
        median_score = ranked[len(ranked) // 2][1]
        weak = [(c, s) for c, s in ranked if s < median_score * 0.3]
        if weak:
            # Classify cards to distinguish cuttable from infrastructure
            card_list = cards or []
            slot_labels = {c: _classify_card_slot(c, card_list) for c, _ in weak}
            cuttable = [(c, s) for c, s in weak if slot_labels[c] == "spell"]
            protected = [(c, s) for c, s in weak if slot_labels[c] != "spell"]

            if cuttable:
                print(f"\nWeakly connected cards (potential cut candidates):")
                for card, total in cuttable:
                    print(f"  {card:<35} {total:7.1f} ({card_partners[card]} partners)")
            if protected:
                print(f"\nLow synergy but protected (infrastructure / lands):")
                for card, total in protected:
                    label = slot_labels[card]
                    print(f"  {card:<35} {total:7.1f} ({card_partners[card]} partners) [{label}]")


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

    # Inject LLM-scored cards that aren't in the graph candidate pool.
    # High LLM scores (≥7) should be recommended even without graph edges.
    if commander and db_path:
        commander_oid_lookup = {}
        for c in cards:
            if c["name"] == commander:
                commander_oid_lookup[commander] = c.get("oracle_id", "")
                break
        _cmdr_oid = commander_oid_lookup.get(commander, "")
        if _cmdr_oid:
            import sqlite3 as _sql2
            _ic = _sql2.connect(db_path)
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
            _ic.close()

    # Build card metadata lookup (name→dict) and oid lookup (name→oid)
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
    strategy_map = {}  # oid → set of strategies
    ability_counts = {}  # oid → count of non-keyword abilities
    if db_path:
        import sqlite3
        try:
            _qconn = sqlite3.connect(db_path)
            _qconn.execute("SELECT 1")  # verify connection
        except Exception as e:
            print(f"  Warning: Strategy/ability loading skipped ({e})")
            _qconn = None
        if _qconn:
            # Batch strategy query
            if active_strategies:
                for row in _qconn.execute(
                    "SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"
                ).fetchall():
                    strategy_map.setdefault(row[0], set()).add(row[1])
            # Batch ability count query
            for row in _qconn.execute(
                "SELECT oracle_id, COUNT(*) FROM abilities "
                "WHERE ability_type NOT IN ('keyword') GROUP BY oracle_id"
            ).fetchall():
                ability_counts[row[0]] = row[1]
            _qconn.close()

    # Commander affinity: compute once via O(1) dict lookup
    affinities = {}
    if commander:
        commander_card_data = cards_by_name.get(commander)
        candidate_cards_data = [c for c in cards if c["name"] not in deck_cards]
        affinities = _compute_commander_affinity(commander_card_data, candidate_cards_data)

    # === Single consolidated loop over all candidates ===
    # Applies: tribal, strategy, combo, CMC penalty, quality, popularity, affinity
    import math
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
                import sqlite3 as _sql3
                _mc = _sql3.connect(db_path)
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
                    _mech_card_data = {}  # oid → (name, type_line, mana_cost, cmc, edhrec_rank, color_identity)
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
                _mc.close()
        except ImportError:
            pass
        except Exception as e:
            pass

    # Synergy scoring: 3-tier fallback
    # 1. LLM scores (pre-computed via score_synergies.py) — best quality
    # 2. Tower model (via train_tower_model.py) — generalizes to any commander
    # 3. Graph-only scoring (current multipliers) — always available
    if commander and db_path:
        commander_oid = card_oid_lookup.get(commander, "")
        if commander_oid:
            import sqlite3 as _sql
            _sconn = _sql.connect(db_path)

            # Tier 1: Direct LLM scores for this commander
            llm_scores = {}
            has_table = _sconn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='synergy_scores'"
            ).fetchone()[0]
            if has_table:
                for row in _sconn.execute(
                    "SELECT card_oid, score FROM synergy_scores WHERE commander_oid = ?",
                    (commander_oid,)
                ).fetchall():
                    llm_scores[row[0]] = row[1]
            _sconn.close()

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

            # Apply LLM/model scores.
            # LLM score is the PRIMARY ranking signal.
            # Tiebreaker: EDHREC rank (card popularity/quality) instead of graph.
            # This avoids generic cards with high graph edges outranking
            # specific synergy cards that are well-known staples.
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
                        # 1. Tower model prediction (continuous, captures synergy nuance)
                        # 2. EDHREC rank (card quality/popularity)
                        meta = card_meta.get(card_name, {})
                        rank = meta.get("edhrec_rank") or 50000
                        import math
                        rank_tiebreak = max(0, 10.0 - 2.0 * math.log10(max(rank, 1)))
                        tower_score = model_scores.get(card_name, 5.0) if card_name in model_scores else 5.0
                        # LLM primary (×1000), tower sub-tiebreak (×10), rank micro-tiebreak (×0.1)
                        info["total"] = score_val * 1000.0 + tower_score * 10.0 + rank_tiebreak * 0.1
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


def _compute_commander_affinity(commander_card: dict, candidate_cards: list[dict],
                                db_path: str = None) -> dict:
    """Compute commander-specific affinity for each candidate.

    Three signals:
    1. Tag overlap: provides↔wants connections (direct + bridges)
    2. Oracle text: candidate references the same mechanics/creature types
    3. Keyword synergy: shared or complementary keywords

    Returns {candidate_name: affinity_score}.
    """
    if not commander_card:
        return {}

    cmdr_provides = set(commander_card.get("provides", []))
    cmdr_wants = set(commander_card.get("wants", []))
    cmdr_oracle = (commander_card.get("oracle_text") or "").lower()
    cmdr_keywords = {k.lower() for k in (commander_card.get("keywords") or [])}

    # Extract key concepts from commander's oracle text
    import re
    cmdr_concepts = set()

    _CONCEPT_WORDS = [
        "human", "goblin", "elf", "zombie", "vampire", "dragon", "angel",
        "demon", "sliver", "artifact", "enchantment", "equipment", "aura",
        "instant", "sorcery", "planeswalker", "land", "token", "counter",
        "poison", "infect", "mill", "draw", "discard", "sacrifice", "exile",
        "return", "graveyard", "library", "damage", "life", "mana", "untap",
        "tap", "equip", "enchant", "proliferate", "toxic", "attack", "combat",
        "enters", "dies", "cast",
    ]

    # Pre-compile concept regexes once
    _concept_regexes = {w: re.compile(r'\b' + w + r's?\b') for w in _CONCEPT_WORDS}
    _reminder_re = re.compile(r'\([^)]*\)')

    for word, rx in _concept_regexes.items():
        if rx.search(cmdr_oracle):
            cmdr_concepts.add(word)

    # Pre-compile concept patterns for candidate matching
    cmdr_concept_rxs = [(c, _concept_regexes[c]) for c in cmdr_concepts]

    # Load bridges (cached at module level would be better, but this is called rarely)
    bridge_provides = {}
    for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
        bridge_provides.setdefault(w_tag, []).append((p_tag, weight))

    # Pre-compute bridge lookups for commander wants/provides
    cmdr_want_bridges = {}
    for want in cmdr_wants:
        cmdr_want_bridges[want] = bridge_provides.get(want, [])
    cmdr_provide_bridge_targets = {}
    for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
        if p_tag in cmdr_provides:
            cmdr_provide_bridge_targets.setdefault(w_tag, []).append((p_tag, weight))

    affinities = {}
    for card in candidate_cards:
        name = card["name"]
        card_provides = set(card.get("provides", []))
        card_wants = set(card.get("wants", []))

        score = 0.0

        # Signal 1: Tag connections (direct + bridges)
        score += 3.0 * len(card_provides & cmdr_wants)
        score += 3.0 * len(card_wants & cmdr_provides)
        # Best bridge per commander want
        for want, bridges in cmdr_want_bridges.items():
            best = 0.0
            for p_tag, weight in bridges:
                if p_tag in card_provides and weight > best:
                    best = weight
            score += best * 1.5
        # Best bridge per card want matching commander provides
        for want in card_wants:
            bridges = cmdr_provide_bridge_targets.get(want, [])
            if bridges:
                best = max(w for _, w in bridges)
                score += best * 1.5

        # Signal 2: Oracle text concept overlap (strip reminder text)
        if cmdr_concept_rxs:
            card_oracle = (card.get("oracle_text") or "").lower()
            card_oracle_stripped = _reminder_re.sub('', card_oracle)
            if card_oracle_stripped:
                concept_matches = sum(1 for _, rx in cmdr_concept_rxs if rx.search(card_oracle_stripped))
                if concept_matches > 0:
                    score += concept_matches ** 1.5

        # Signal 3: Keyword synergy
        card_keywords = {k.lower() for k in (card.get("keywords") or [])}
        n_shared = len(card_keywords & cmdr_keywords)
        if n_shared:
            score += n_shared * 0.5

        affinities[name] = score

    return affinities


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


def _classify_card_slot(name: str, cards: list[dict],
                        deck_types: set = None) -> str:
    """Classify a card as 'land', 'staple', or 'spell' for swap bucketing.

    Lands swap with lands, staples are protected, spells swap with spells.
    Protected from cuts: removal, protection, ramp, card draw, commander's tribe.
    """
    from card_db import CARD_DB, NAME_INDEX

    INFRASTRUCTURE_ROLES = {"removal", "ramp", "protection", "draw", "tutor"}

    # Provides tags that indicate infrastructure function (should be protected)
    INFRASTRUCTURE_PROVIDES = {
        "targeted-removal", "board-wide-removal", "board-protection",
        "reactive-protection", "mana-acceleration", "card-draw",
        "graveyard-recursion", "indestructible-grant",
    }

    # Provides tags that indicate synergy value (okay to keep as "spell")
    SYNERGY_PROVIDES = {
        "token-generation", "counter-placement", "board-wide-counter-placement",
        "counter-amplification", "trigger-doubling", "creature-pump",
        "card-draw-payoff", "etb-payoff", "sacrifice-payoff", "goblin-tribal",
        "combat-trigger",
    }

    card_data = None
    for card in cards:
        if card["name"] == name:
            card_data = card
            break

    if card_data:
        role = card_data.get("role", "")
        type_line = card_data.get("type_line", "")
        provides = set(card_data.get("provides", []))

        # Land detection
        if role == "land" or "Land" in type_line:
            return "land"

        # Role-based protection
        if role in INFRASTRUCTURE_ROLES:
            if provides & SYNERGY_PROVIDES:
                pass  # Has synergy value, classify as spell
            else:
                return "staple"

        # Provides-based protection: cards providing removal/protection/ramp
        if provides & INFRASTRUCTURE_PROVIDES:
            if not (provides & SYNERGY_PROVIDES):
                return "staple"

        # Tribal protection: creatures matching the deck's tribal type
        # A Human in a Human deck shouldn't be cut for a non-Human
        # Changelings/Shapeshifters count as all types
        if deck_types and type_line:
            if "Shapeshifter" in type_line or "Changeling" in type_line:
                return "staple"
            for dt in deck_types:
                if dt in type_line:
                    return "staple"

    # Fallback: Scryfall type_line
    oid = NAME_INDEX.get(name.lower())
    if oid and oid in CARD_DB:
        type_line = CARD_DB[oid].get("type_line", "")
        if "Land" in type_line:
            return "land"

    return "spell"


def suggest_swaps(graph: dict, deck_cards: set[str], commander: str,
                  cards: list[dict], top_n: int = 15,
                  active_strategies: set = None, db_path: str = None,
                  deck_types: set = None) -> list[dict]:
    """Suggest swaps: pair weak deck cards with strong non-deck candidates.

    Lands swap with lands, spells swap with spells. Commander and staple
    infrastructure (mana rocks, removal, protection) are never cut.
    Strategy-weighted: candidates matching active strategies score higher,
    deck cards with zero strategy overlap are prioritized for cutting.
    """
    deck_scores = _deck_card_scores(graph, deck_cards)
    cand_scores = _candidate_scores(graph, deck_cards)

    # Build card metadata for strategy/anti-synergy checks
    card_oid_lookup = {}
    card_meta = {}
    for c in cards:
        card_oid_lookup[c["name"]] = c.get("oracle_id", "")
        card_meta[c["name"]] = {
            "type_line": c.get("type_line", ""),
            "cmc": c.get("cmc", 0),
        }

    # Apply strategy relevance to candidate scores
    if active_strategies and db_path:
        for card_name, info in cand_scores.items():
            oid = card_oid_lookup.get(card_name, "")
            if oid:
                rel = compute_strategy_relevance(oid, active_strategies, db_path)
                info["total"] *= rel
                info["strategy_rel"] = rel

    # Apply mana cost penalty to candidates
    deck_cmc_values = [card_meta[n]["cmc"] for n in deck_cards
                       if n in card_meta and "Land" not in card_meta[n].get("type_line", "")
                       and card_meta[n]["cmc"]]
    deck_avg_cmc = sum(deck_cmc_values) / max(len(deck_cmc_values), 1)
    for card_name, info in cand_scores.items():
        meta = card_meta.get(card_name, {})
        cmc = meta.get("cmc", 0) or 0
        if cmc > deck_avg_cmc + 3:
            penalty = max(0.3, 1.0 - 0.15 * (cmc - deck_avg_cmc - 3))
            info["total"] *= penalty

    # Check which deck cards have zero strategy overlap (cut priority)
    anti_synergy_cards = set()
    if active_strategies and db_path:
        import sqlite3 as _sq
        _conn = _sq.connect(db_path)
        for card_name in deck_cards:
            oid = card_oid_lookup.get(card_name, "")
            if not oid:
                continue
            card_strats = {r[0] for r in _conn.execute(
                "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
                (oid,)
            ).fetchall()}
            if not (card_strats & active_strategies):
                anti_synergy_cards.add(card_name)
        _conn.close()

    # Protect cards with high commander synergy from cuts.
    # Cards that strongly synergize with the commander are key pieces.
    commander_protected = set()
    cmdr_adj = graph["adjacency"].get(commander, [])
    if cmdr_adj:
        cmdr_edge_scores = {e["target"]: e["score"] for e in cmdr_adj if e["target"] in deck_cards}
        if cmdr_edge_scores:
            # Protect top 20 commander-synergy cards (generous — these are key pieces)
            n_protect = min(20, len(cmdr_edge_scores))
            threshold = sorted(cmdr_edge_scores.values(), reverse=True)[n_protect - 1]
            commander_protected = {name for name, score in cmdr_edge_scores.items() if score >= threshold}

    # Protect cards with high mechanics synergy with the commander.
    try:
        from mechanics_matcher import load_mechanics, compute_synergy
        _all_mechs = load_mechanics(db_path) if db_path else {}
        cmdr_oid_swap = card_oid_lookup.get(commander, "")
        cmdr_mechs_swap = _all_mechs.get(cmdr_oid_swap, [])
        cmdr_type_swap = card_meta.get(commander, {}).get("type_line", "")
        if cmdr_mechs_swap:
            for card_name in deck_cards:
                if card_name == commander:
                    continue
                card_oid_s = card_oid_lookup.get(card_name, "")
                card_mechs_s = _all_mechs.get(card_oid_s, [])
                if card_mechs_s:
                    ms = compute_synergy(cmdr_mechs_swap, card_mechs_s,
                                         cmdr_type_swap, card_meta.get(card_name, {}).get("type_line", ""))
                    if ms >= 2.0:  # Strong mechanics match = protected
                        commander_protected.add(card_name)
    except Exception:
        pass

    # Protect cards in known Spellbook combos with the commander.
    combo_protected = set()
    if db_path:
        import sqlite3 as _sq2
        _cc = _sq2.connect(db_path)
        cmdr_oid = card_oid_lookup.get(commander, "")
        if cmdr_oid:
            # Find combos including the commander
            combo_ids = [r[0] for r in _cc.execute(
                "SELECT combo_id FROM spellbook_combo_cards WHERE oracle_id = ?", (cmdr_oid,))]
            for cid in combo_ids:
                combo_card_oids = {r[0] for r in _cc.execute(
                    "SELECT oracle_id FROM spellbook_combo_cards WHERE combo_id = ?", (cid,))}
                # If all combo cards are in our deck, protect them all
                combo_names = set()
                for coid in combo_card_oids:
                    name_row = _cc.execute("SELECT name FROM cards WHERE oracle_id = ?", (coid,)).fetchone()
                    if name_row:
                        combo_names.add(name_row[0])
                if combo_names.issubset(deck_cards):
                    combo_protected.update(combo_names - {commander})
        _cc.close()

    # Classify every deck card and candidate by slot type
    deck_slots = {name: _classify_card_slot(name, cards, deck_types=deck_types) for name in deck_cards}
    cand_slots = {name: _classify_card_slot(name, cards, deck_types=deck_types) for name in cand_scores}

    # Split into buckets: lands vs spells (protected cards excluded from cuts)
    cuttable = {"land": [], "spell": []}
    for card, info in deck_scores.items():
        slot = deck_slots.get(card, "spell")
        if card == commander or slot == "staple":
            continue
        if card in commander_protected:
            continue  # High commander synergy — don't cut
        if card in combo_protected:
            continue  # Part of a known combo — don't cut
        info["anti_synergy"] = card in anti_synergy_cards
        cuttable[slot].append((card, info))

    # Sort cuttable: anti-synergy cards first (worst cuts), then by lowest synergy
    for bucket in cuttable.values():
        bucket.sort(key=lambda x: (not x[1].get("anti_synergy", False), x[1]["total"]))

    candidate_lists = {"land": [], "spell": []}
    for card, info in sorted(cand_scores.items(), key=lambda x: x[1]["total"], reverse=True):
        slot = cand_slots.get(card, "spell")
        if slot == "staple":
            slot = "spell"
        candidate_lists[slot].append((card, info))

    # Pair within each bucket
    used_candidates = set()
    swaps = []

    for bucket in ("spell", "land"):
        for cut_card, cut_info in cuttable[bucket]:
            if len(swaps) >= top_n * 2:
                break

            for add_card, add_info in candidate_lists[bucket]:
                if add_card in used_candidates:
                    continue
                net = add_info["total"] - cut_info["total"]
                if net <= 0:
                    break

                used_candidates.add(add_card)
                top_partners = sorted(add_info["partners"], key=lambda x: x[1], reverse=True)
                swaps.append({
                    "cut": cut_card,
                    "cut_score": round(cut_info["total"], 1),
                    "cut_partners": cut_info["partners"],
                    "cut_anti_synergy": cut_info.get("anti_synergy", False),
                    "slot": bucket,
                    "add": add_card,
                    "add_score": round(add_info["total"], 1),
                    "add_partners": len(add_info["partners"]),
                    "add_multi_sig": add_info["multi_sig"],
                    "add_top_partners": top_partners[:5],
                    "add_strategy_rel": add_info.get("strategy_rel"),
                    "net_delta": round(net, 1),
                })
                break

    swaps.sort(key=lambda s: s["net_delta"], reverse=True)
    return swaps[:top_n]


def show_swaps(swaps: list[dict], top_n: int = 15):
    """Display suggested swaps with strategy annotations."""
    print(f"\n{'═' * 70}")
    print(f"SUGGESTED SWAPS — {len(swaps)} upgrades found")
    print(f"{'═' * 70}")

    if not swaps:
        print("  No beneficial swaps found.")
        return

    # Normalize net_delta to percentage (best swap = 100%)
    max_delta = max(s["net_delta"] for s in swaps) if swaps else 1.0
    if max_delta <= 0:
        max_delta = 1.0

    for i, swap in enumerate(swaps[:top_n], 1):
        pct = round(swap["net_delta"] / max_delta * 100, 1)
        bar_len = round(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        slot_label = f" [land]" if swap.get("slot") == "land" else ""
        anti = " ← no strategy match" if swap.get("cut_anti_synergy") else ""
        strat_rel = swap.get("add_strategy_rel")
        strat_str = f" [strat×{strat_rel:.1f}]" if strat_rel and strat_rel != 1.0 else ""

        print(f"\n  {pct:5.1f}% {bar}{slot_label}")
        print(f"    OUT: {swap['cut']:<35} (synergy: {swap['cut_score']:>5.1f}, "
              f"{swap['cut_partners']} partners){anti}")
        print(f"     IN: {swap['add']:<35} (synergy: {swap['add_score']:>5.1f}, "
              f"{swap['add_partners']} partners){strat_str}")
        if swap["add_top_partners"]:
            top = swap["add_top_partners"][:3]
            partners_str = ", ".join(
                f"{p[0]} ({p[1]:.1f})" for p in top
            )
            print(f"         top synergies: {partners_str}")


def generate_visualization(graph: dict, cards: list[dict], deck_set: set,
                           commander: str, deck_name: str, combos: list = None,
                           output_path: str = None, min_edge_score: float = 0.8,
                           tiered_combos: list = None):
    """Generate a self-contained interactive HTML visualization of the deck synergy graph."""

    card_by_name = {c["name"]: c for c in cards}
    adj = graph["adjacency"]

    # Build nodes (deck cards only)
    nodes = []
    for name in sorted(deck_set):
        card = card_by_name.get(name, {})
        edges_for_card = [e for e in adj.get(name, []) if e["target"] in deck_set]
        total_syn = sum(e["score"] for e in edges_for_card)
        nodes.append({
            "name": name,
            "role": card.get("role", "unknown"),
            "provides": card.get("provides", []),
            "wants": card.get("wants", []),
            "is_commander": name == commander,
            "edge_count": len(edges_for_card),
            "total_synergy": round(total_syn, 1),
        })

    # Build card-pair sets for tiered combo edge highlighting
    confirmed_edge_pairs: set = set()
    likely_edge_pairs: set = set()
    if tiered_combos:
        for tc in tiered_combos:
            tc_cards = tc.get("cards", [])
            pairs = set()
            for i in range(len(tc_cards)):
                for j in range(i + 1, len(tc_cards)):
                    pairs.add(tuple(sorted([tc_cards[i], tc_cards[j]])))
            if tc.get("tier") == "infinite-confirmed":
                confirmed_edge_pairs.update(pairs)
            elif tc.get("tier") == "combo-likely":
                likely_edge_pairs.update(pairs)

    # Build edges (deck-internal only, above threshold)
    edges = []
    seen = set()
    for edge in graph["edges"]:
        if edge["source"] in deck_set and edge["target"] in deck_set:
            if edge["score"] >= min_edge_score:
                key = tuple(sorted([edge["source"], edge["target"]]))
                if key not in seen:
                    seen.add(key)
                    if key in confirmed_edge_pairs:
                        combo_tier = "infinite-confirmed"
                    elif key in likely_edge_pairs:
                        combo_tier = "combo-likely"
                    else:
                        combo_tier = None
                    edges.append({
                        "source": edge["source"],
                        "target": edge["target"],
                        "score": edge["score"],
                        "signals": edge["signals"],
                        "reasons": edge["reasons"],
                        "combo_tier": combo_tier,
                    })

    # Combos (legacy triangles for the combo overlay)
    combo_data = []
    if combos:
        triangles = combos.get("triangles", []) if isinstance(combos, dict) else combos
        for combo in triangles[:20]:
            combo_data.append({
                "cards": list(combo["cards"]),
                "score": combo["score"],
                "type": combo.get("type", "synergy-triangle"),
            })

    # Tiered combos for the side panel
    tiered_combo_data = []
    if tiered_combos:
        for tc in tiered_combos:
            tiered_combo_data.append({
                "cards": tc.get("cards", []),
                "tier": tc.get("tier", "synergy"),
                "result": tc.get("result", ""),
                "reason": tc.get("reason", ""),
            })

    n_confirmed = sum(1 for tc in tiered_combos if tc.get("tier") == "infinite-confirmed") if tiered_combos else 0
    n_likely = sum(1 for tc in tiered_combos if tc.get("tier") == "combo-likely") if tiered_combos else 0

    viz_data = json.dumps({
        "nodes": nodes,
        "edges": edges,
        "combos": combo_data,
        "tiered_combos": tiered_combo_data,
        "meta": {
            "deck": deck_name,
            "commander": commander,
            "total_cards": len(nodes),
            "total_edges": len(edges),
            "confirmed_combos": n_confirmed,
            "likely_combos": n_likely,
        },
    })

    html = _VIZ_HTML_TEMPLATE.replace("__GRAPH_DATA__", viz_data)

    if not output_path:
        output_path = os.path.join(DATA_DIR, f"{deck_name}_synergy_viz.html")
    with open(output_path, "w") as f:
        f.write(html)
    print(f"\nVisualization written to {output_path}")
    print(f"  {len(nodes)} nodes, {len(edges)} edges, {len(combo_data)} combos")
    if n_confirmed:
        print(f"  Spellbook confirmed combos: {n_confirmed} (highlighted gold in visualization)")
    if n_likely:
        print(f"  Likely combos: {n_likely} (highlighted orange in visualization)")


_VIZ_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MTG Synergy Graph</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #e0e0e0; overflow: hidden; }
#graph-container { width: 100vw; height: 100vh; }
svg { width: 100%; height: 100%; }

/* Controls */
#controls { position: fixed; top: 12px; left: 12px; z-index: 10; display: flex; flex-direction: column; gap: 8px; }
#search-box { width: 260px; padding: 8px 12px; border-radius: 6px; border: 1px solid #444; background: #16213e; color: #e0e0e0; font-size: 14px; }
#search-box::placeholder { color: #888; }
#search-results { background: #16213e; border: 1px solid #444; border-radius: 6px; max-height: 200px; overflow-y: auto; display: none; }
#search-results div { padding: 6px 12px; cursor: pointer; font-size: 13px; }
#search-results div:hover { background: #0f3460; }

.controls-row { display: flex; gap: 6px; flex-wrap: wrap; }
.ctrl-btn { padding: 4px 10px; border-radius: 4px; border: 1px solid #444; background: #16213e; color: #ccc; font-size: 11px; cursor: pointer; }
.ctrl-btn:hover { background: #0f3460; }
.ctrl-btn.active { background: #0f3460; border-color: #e94560; color: #fff; }

#score-slider-container { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #aaa; }
#score-slider { width: 140px; accent-color: #e94560; }

/* Side panel */
#side-panel { position: fixed; top: 0; right: -360px; width: 360px; height: 100vh; background: #16213e; border-left: 2px solid #0f3460; padding: 20px; overflow-y: auto; transition: right 0.3s; z-index: 20; }
#side-panel.open { right: 0; }
#panel-close { position: absolute; top: 10px; right: 14px; cursor: pointer; font-size: 20px; color: #888; }
#panel-close:hover { color: #e94560; }
#panel-card-name { font-size: 18px; font-weight: 700; margin-bottom: 4px; color: #fff; }
#panel-role { font-size: 13px; color: #aaa; margin-bottom: 12px; }
.panel-section { margin-bottom: 14px; }
.panel-section h4 { font-size: 12px; text-transform: uppercase; color: #e94560; margin-bottom: 4px; letter-spacing: 0.5px; }
.panel-section .tag { display: inline-block; padding: 2px 8px; margin: 2px; border-radius: 3px; background: #1a1a3e; font-size: 12px; border: 1px solid #333; }
.panel-section .tag.provides { border-color: #4CAF50; color: #81C784; }
.panel-section .tag.wants { border-color: #FF9800; color: #FFB74D; }
.panel-section .tag.synergy { border-color: #2196F3; color: #64B5F6; }
#panel-connections { font-size: 13px; }
#panel-connections .conn { padding: 4px 0; border-bottom: 1px solid #222; display: flex; justify-content: space-between; }
#panel-connections .conn-name { cursor: pointer; }
#panel-connections .conn-name:hover { color: #e94560; }
#panel-connections .conn-score { color: #888; font-size: 12px; }
#panel-notes { font-size: 13px; color: #bbb; line-height: 1.4; font-style: italic; }

/* Legend */
#legend { position: fixed; bottom: 12px; left: 12px; background: rgba(22,33,62,0.9); border: 1px solid #333; border-radius: 6px; padding: 10px 14px; z-index: 10; font-size: 11px; }
#legend h5 { margin-bottom: 6px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; }
.legend-item { display: flex; align-items: center; gap: 6px; margin: 3px 0; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; }

/* Tooltip */
#tooltip { position: fixed; background: rgba(22,33,62,0.95); border: 1px solid #444; border-radius: 6px; padding: 8px 12px; font-size: 12px; pointer-events: none; display: none; z-index: 30; max-width: 350px; }
#tooltip .tt-header { font-weight: 600; margin-bottom: 4px; }
#tooltip .tt-reason { color: #aaa; margin: 2px 0; }

/* Stats bar */
#stats-bar { position: fixed; bottom: 12px; right: 12px; font-size: 11px; color: #666; z-index: 10; text-align: right; }
</style>
</head>
<body>
<div id="graph-container"><svg></svg></div>

<div id="controls">
  <input id="search-box" type="text" placeholder="Search cards...">
  <div id="search-results"></div>
  <div class="controls-row" id="role-filters"></div>
  <div id="score-slider-container">
    <span>Min score:</span>
    <input id="score-slider" type="range" min="0" max="15" step="0.5" value="0.8">
    <span id="score-value">0.8</span>
  </div>
  <div class="controls-row">
    <button class="ctrl-btn" id="btn-combos">Show Combos</button>
    <button class="ctrl-btn" id="btn-labels">Labels</button>
    <button class="ctrl-btn" id="btn-reset">Reset View</button>
  </div>
</div>

<div id="side-panel">
  <span id="panel-close">&times;</span>
  <div id="panel-card-name"></div>
  <div id="panel-role"></div>
  <div class="panel-section"><h4>Provides</h4><div id="panel-provides"></div></div>
  <div class="panel-section"><h4>Wants</h4><div id="panel-wants"></div></div>
  <div class="panel-section"><h4>Synergy Tags</h4><div id="panel-synergy"></div></div>
  <div class="panel-section"><h4>Notes</h4><div id="panel-notes"></div></div>
  <div class="panel-section"><h4>Connections</h4><div id="panel-connections"></div></div>
</div>

<div id="legend">
  <h5>Roles</h5>
  <div id="legend-items"></div>
</div>

<div id="tooltip">
  <div class="tt-header"></div>
  <div class="tt-body"></div>
</div>

<div id="stats-bar"></div>

<script>
const DATA = __GRAPH_DATA__;

const ROLE_COLORS = {
  enabler:    "#4CAF50",
  threat:     "#f44336",
  ramp:       "#8BC34A",
  removal:    "#FF9800",
  protection: "#2196F3",
  draw:       "#9C27B0",
  utility:    "#00BCD4",
  tutor:      "#795548",
  land:       "#607D8B",
  unknown:    "#9E9E9E",
};

const width = window.innerWidth;
const height = window.innerHeight;
const svg = d3.select("svg");
const g = svg.append("g");

// Zoom
const zoom = d3.zoom().scaleExtent([0.2, 5]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// State
let showLabels = false;
let showCombos = false;
let scoreThreshold = 0.8;
let selectedNode = null;
let activeRoles = new Set(Object.keys(ROLE_COLORS));

// Prep data
const nodeMap = {};
DATA.nodes.forEach(n => { nodeMap[n.name] = n; });
let visibleEdges = DATA.edges.filter(e => e.score >= scoreThreshold);

// Edge lookup
function getEdgesForNode(name) {
  return DATA.edges.filter(e => (e.source.name || e.source) === name || (e.target.name || e.target) === name);
}

function nodeRadius(d) {
  let r = 6 + Math.sqrt(d.total_synergy) * 1.2;
  if (d.is_commander) r *= 1.4;
  return Math.min(r, 30);
}

// Build role filters
const roles = [...new Set(DATA.nodes.map(n => n.role))].sort();
const roleFilters = d3.select("#role-filters");
roles.forEach(role => {
  const btn = roleFilters.append("button")
    .attr("class", "ctrl-btn active")
    .style("border-left", `3px solid ${ROLE_COLORS[role] || ROLE_COLORS.unknown}`)
    .text(role)
    .on("click", function() {
      if (activeRoles.has(role)) { activeRoles.delete(role); d3.select(this).classed("active", false); }
      else { activeRoles.add(role); d3.select(this).classed("active", true); }
      updateVisibility();
    });
});

// Legend
const legendItems = d3.select("#legend-items");
roles.forEach(role => {
  const item = legendItems.append("div").attr("class", "legend-item");
  item.append("div").attr("class", "legend-dot").style("background", ROLE_COLORS[role] || ROLE_COLORS.unknown);
  item.append("span").text(role);
});
// Edge color legend
const edgeLegend = legendItems.append("div").attr("class", "legend-item").style("margin-top", "6px");
edgeLegend.append("div").style("width", "24px").style("height", "3px").style("background", "#FFD700").style("border-radius", "2px");
edgeLegend.append("span").text("Spellbook combo");
const edgeLegend2 = legendItems.append("div").attr("class", "legend-item");
edgeLegend2.append("div").style("width", "24px").style("height", "3px").style("background", "#FF8C00").style("border-radius", "2px");
edgeLegend2.append("span").text("Likely combo");

// Stats
const comboStats = DATA.meta.confirmed_combos > 0
  ? ` | ${DATA.meta.confirmed_combos} confirmed combos` + (DATA.meta.likely_combos > 0 ? ` | ${DATA.meta.likely_combos} likely` : "")
  : "";
d3.select("#stats-bar").html(
  `${DATA.meta.deck} | ${DATA.meta.total_cards} cards | ${DATA.meta.total_edges} edges | Commander: ${DATA.meta.commander}${comboStats}`
);

// Force simulation
const simulation = d3.forceSimulation(DATA.nodes)
  .force("link", d3.forceLink(visibleEdges).id(d => d.name).distance(d => Math.max(60, 180 - d.score * 8)).strength(d => Math.min(0.5, d.score / 20)))
  .force("charge", d3.forceManyBody().strength(-180))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collision", d3.forceCollide().radius(d => nodeRadius(d) + 3))
  .alphaDecay(0.02);

function edgeBaseColor(d) {
  if (d.combo_tier === "infinite-confirmed") return "#FFD700";
  if (d.combo_tier === "combo-likely") return "#FF8C00";
  return "#555";
}
function edgeBaseOpacity(d) {
  if (d.combo_tier === "infinite-confirmed") return 0.75;
  if (d.combo_tier === "combo-likely") return 0.55;
  return Math.max(0.08, Math.min(0.6, d.score / 15));
}
function edgeBaseWidth(d) {
  if (d.combo_tier === "infinite-confirmed") return Math.max(2, Math.min(5, d.score / 3));
  if (d.combo_tier === "combo-likely") return Math.max(1.5, Math.min(4, d.score / 3));
  return Math.max(0.5, Math.min(4, d.score / 3));
}

// Render edges
const edgeGroup = g.append("g").attr("class", "edges");
let edgeElements = edgeGroup.selectAll("line").data(visibleEdges).join("line")
  .attr("stroke", d => edgeBaseColor(d))
  .attr("stroke-width", d => edgeBaseWidth(d))
  .attr("stroke-opacity", d => edgeBaseOpacity(d));

// Combo overlays
const comboGroup = g.append("g").attr("class", "combos").style("display", "none");

// Render nodes
const nodeGroup = g.append("g").attr("class", "nodes");
const nodeElements = nodeGroup.selectAll("g").data(DATA.nodes).join("g")
  .call(d3.drag()
    .on("start", (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on("end", (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; })
  );

nodeElements.append("circle")
  .attr("r", d => nodeRadius(d))
  .attr("fill", d => ROLE_COLORS[d.role] || ROLE_COLORS.unknown)
  .attr("stroke", d => d.is_commander ? "#FFD700" : "#333")
  .attr("stroke-width", d => d.is_commander ? 3 : 1)
  .style("cursor", "pointer");

// Labels
const labelElements = nodeElements.append("text")
  .text(d => d.name)
  .attr("dy", d => nodeRadius(d) + 12)
  .attr("text-anchor", "middle")
  .attr("fill", "#ccc")
  .attr("font-size", "10px")
  .style("pointer-events", "none")
  .style("display", "none");

// Commander label always visible
labelElements.filter(d => d.is_commander).style("display", "block").attr("font-size", "12px").attr("font-weight", "700").attr("fill", "#FFD700");

// Tooltip
const tooltip = d3.select("#tooltip");

edgeGroup.selectAll("line")
  .on("mouseover", (e, d) => {
    const src = d.source.name || d.source;
    const tgt = d.target.name || d.target;
    tooltip.select(".tt-header").text(`${src} ↔ ${tgt} (${d.score})`);
    tooltip.select(".tt-body").html(d.reasons.map(r => `<div class="tt-reason">${r}</div>`).join(""));
    tooltip.style("display", "block").style("left", (e.clientX + 15) + "px").style("top", (e.clientY - 10) + "px");
  })
  .on("mousemove", e => {
    tooltip.style("left", (e.clientX + 15) + "px").style("top", (e.clientY - 10) + "px");
  })
  .on("mouseout", () => tooltip.style("display", "none"));

// Node click
nodeElements.on("click", (e, d) => {
  e.stopPropagation();
  selectNode(d);
});

svg.on("click", () => clearSelection());

function selectNode(d) {
  selectedNode = d;
  const connected = new Set();
  const connEdges = [];
  DATA.edges.forEach(e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    if (src === d.name) { connected.add(tgt); connEdges.push({name: tgt, score: e.score, reasons: e.reasons}); }
    if (tgt === d.name) { connected.add(src); connEdges.push({name: src, score: e.score, reasons: e.reasons}); }
  });
  connected.add(d.name);

  // Dim non-connected
  nodeElements.select("circle").attr("opacity", n => connected.has(n.name) ? 1 : 0.1);
  labelElements.style("display", n => connected.has(n.name) ? "block" : "none");
  edgeElements.attr("stroke-opacity", e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    return (src === d.name || tgt === d.name) ? 0.9 : 0.02;
  }).attr("stroke", e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    if (src === d.name || tgt === d.name) return "#e94560";
    return edgeBaseColor(e);
  }).attr("stroke-width", e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    return (src === d.name || tgt === d.name) ? Math.max(1.5, Math.min(5, e.score / 3)) : edgeBaseWidth(e);
  });

  // Side panel
  connEdges.sort((a, b) => b.score - a.score);
  d3.select("#panel-card-name").text(d.name);
  d3.select("#panel-role").text(`Role: ${d.role} | Edges: ${d.edge_count} | Total synergy: ${d.total_synergy}`);
  d3.select("#panel-provides").html(d.provides.map(t => `<span class="tag provides">${t}</span>`).join(""));
  d3.select("#panel-wants").html(d.wants.map(t => `<span class="tag wants">${t}</span>`).join(""));
  d3.select("#panel-connections").html(
    connEdges.slice(0, 20).map(c => `<div class="conn"><span class="conn-name" data-name="${c.name}">${c.name}</span><span class="conn-score">${c.score}</span></div>`).join("")
  );
  // Click connection names
  d3.selectAll(".conn-name").on("click", function() {
    const name = this.dataset.name;
    const node = DATA.nodes.find(n => n.name === name);
    if (node) selectNode(node);
  });
  d3.select("#side-panel").classed("open", true);
}

function clearSelection() {
  selectedNode = null;
  nodeElements.select("circle").attr("opacity", 1);
  if (!showLabels) labelElements.filter(d => !d.is_commander).style("display", "none");
  edgeElements
    .attr("stroke-opacity", d => edgeBaseOpacity(d))
    .attr("stroke", d => edgeBaseColor(d))
    .attr("stroke-width", d => edgeBaseWidth(d));
  d3.select("#side-panel").classed("open", false);
}

// Score slider
d3.select("#score-slider").on("input", function() {
  scoreThreshold = +this.value;
  d3.select("#score-value").text(scoreThreshold);
  updateEdges();
});

function updateEdges() {
  visibleEdges = DATA.edges.filter(e => e.score >= scoreThreshold &&
    activeRoles.has((nodeMap[e.source.name || e.source] || {}).role) &&
    activeRoles.has((nodeMap[e.target.name || e.target] || {}).role));

  edgeElements = edgeGroup.selectAll("line").data(visibleEdges, d => (d.source.name || d.source) + "-" + (d.target.name || d.target));
  edgeElements.exit().remove();
  const newEdges = edgeElements.enter().append("line")
    .attr("stroke", d => edgeBaseColor(d))
    .attr("stroke-width", d => edgeBaseWidth(d))
    .attr("stroke-opacity", d => edgeBaseOpacity(d))
    .on("mouseover", (ev, d) => {
      const src = d.source.name || d.source;
      const tgt = d.target.name || d.target;
      tooltip.select(".tt-header").text(`${src} ↔ ${tgt} (${d.score})`);
      tooltip.select(".tt-body").html(d.reasons.map(r => `<div class="tt-reason">${r}</div>`).join(""));
      tooltip.style("display", "block").style("left", (ev.clientX + 15) + "px").style("top", (ev.clientY - 10) + "px");
    })
    .on("mousemove", ev => tooltip.style("left", (ev.clientX + 15) + "px").style("top", (ev.clientY - 10) + "px"))
    .on("mouseout", () => tooltip.style("display", "none"));
  edgeElements = newEdges.merge(edgeElements);

  simulation.force("link", d3.forceLink(visibleEdges).id(d => d.name).distance(d => Math.max(60, 180 - d.score * 8)).strength(d => Math.min(0.5, d.score / 20)));
  simulation.alpha(0.3).restart();
}

function updateVisibility() {
  nodeElements.style("display", d => activeRoles.has(d.role) ? "block" : "none");
  updateEdges();
}

// Labels toggle
d3.select("#btn-labels").on("click", function() {
  showLabels = !showLabels;
  d3.select(this).classed("active", showLabels);
  if (showLabels) labelElements.style("display", "block");
  else { labelElements.filter(d => !d.is_commander && !(selectedNode && d.name === selectedNode.name)).style("display", "none"); }
});

// Combos toggle
d3.select("#btn-combos").on("click", function() {
  showCombos = !showCombos;
  d3.select(this).classed("active", showCombos);
  comboGroup.style("display", showCombos ? "block" : "none");
  if (showCombos) renderCombos();
});

function renderCombos() {
  comboGroup.selectAll("*").remove();
  // Legacy synergy triangles
  if (DATA.combos.length) {
    const comboColors = { "infinite-combo": "#e94560", "sac-combo": "#FF5722", "counter-combo": "#4CAF50", "token-combo": "#FFEB3B", "etb-combo": "#2196F3", "tribal-combo": "#9C27B0", "damage-combo": "#f44336" };
    DATA.combos.forEach(combo => {
      const positions = combo.cards.map(name => DATA.nodes.find(n => n.name === name)).filter(Boolean);
      if (positions.length >= 3) {
        const color = comboColors[combo.type] || "#e94560";
        comboGroup.append("polygon")
          .datum(positions)
          .attr("fill", color)
          .attr("fill-opacity", 0.12)
          .attr("stroke", color)
          .attr("stroke-width", 2)
          .attr("stroke-opacity", 0.5)
          .attr("stroke-dasharray", "4,2");
      }
    });
  }
  // Tiered combos: confirmed (gold) and likely (orange) overlays
  if (DATA.tiered_combos && DATA.tiered_combos.length) {
    DATA.tiered_combos.forEach(tc => {
      const positions = tc.cards.map(name => DATA.nodes.find(n => n.name === name)).filter(Boolean);
      if (positions.length < 2) return;
      const isConfirmed = tc.tier === "infinite-confirmed";
      const isLikely = tc.tier === "combo-likely";
      if (!isConfirmed && !isLikely) return;
      const color = isConfirmed ? "#FFD700" : "#FF8C00";
      const opacity = isConfirmed ? 0.18 : 0.12;
      if (positions.length === 2) {
        // Draw a thick highlighted line between the two cards
        comboGroup.append("line")
          .attr("stroke", color)
          .attr("stroke-width", isConfirmed ? 4 : 3)
          .attr("stroke-opacity", isConfirmed ? 0.8 : 0.6)
          .attr("stroke-dasharray", isConfirmed ? "none" : "6,3")
          .datum(positions);
      } else {
        comboGroup.append("polygon")
          .datum(positions)
          .attr("fill", color)
          .attr("fill-opacity", opacity)
          .attr("stroke", color)
          .attr("stroke-width", isConfirmed ? 3 : 2)
          .attr("stroke-opacity", isConfirmed ? 0.8 : 0.6)
          .attr("stroke-dasharray", isConfirmed ? "none" : "6,3");
      }
    });
  }
}

// Reset
d3.select("#btn-reset").on("click", () => {
  clearSelection();
  svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity);
  activeRoles = new Set(Object.keys(ROLE_COLORS));
  roleFilters.selectAll(".ctrl-btn").classed("active", true);
  d3.select("#score-slider").property("value", 0.8);
  scoreThreshold = 0.8;
  d3.select("#score-value").text("0.8");
  updateEdges();
  updateVisibility();
});

// Search
const searchBox = d3.select("#search-box");
const searchResults = d3.select("#search-results");

searchBox.on("input", function() {
  const q = this.value.toLowerCase();
  if (q.length < 2) { searchResults.style("display", "none"); return; }
  const matches = DATA.nodes.filter(n => n.name.toLowerCase().includes(q)).slice(0, 10);
  searchResults.html("").style("display", matches.length ? "block" : "none");
  matches.forEach(m => {
    searchResults.append("div").text(m.name).on("click", () => {
      selectNode(m);
      searchBox.property("value", "");
      searchResults.style("display", "none");
      // Center on node
      const t = d3.zoomIdentity.translate(width/2 - m.x, height/2 - m.y);
      svg.transition().duration(500).call(zoom.transform, t);
    });
  });
});

// Node hover: show name
nodeElements
  .on("mouseover", (e, d) => {
    if (!showLabels) labelElements.filter(n => n.name === d.name).style("display", "block");
  })
  .on("mouseout", (e, d) => {
    if (!showLabels && !d.is_commander && !(selectedNode && selectedNode.name === d.name))
      labelElements.filter(n => n.name === d.name).style("display", "none");
  });

// Tick
simulation.on("tick", () => {
  edgeElements
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x).attr("y2", d => d.target.y);

  nodeElements.attr("transform", d => `translate(${d.x},${d.y})`);

  // Update combo polygons and lines
  if (showCombos) {
    comboGroup.selectAll("polygon").attr("points", d =>
      d.map(n => `${n.x},${n.y}`).join(" ")
    );
    comboGroup.selectAll("line")
      .attr("x1", d => d[0].x).attr("y1", d => d[0].y)
      .attr("x2", d => d[1].x).attr("y2", d => d[1].y);
  }
});
</script>
</body>
</html>"""


def _detect_deck_types(cards: list[dict], deck_cards: set[str],
                       threshold: float = 0.3) -> set[str]:
    """Auto-detect dominant creature types in the deck.

    If >30% of creatures share a type, it's a tribal deck for that type.
    Returns set of dominant types (e.g. {'Human'}) or empty set.
    """
    from collections import Counter
    type_counts = Counter()
    creature_count = 0

    for c in cards:
        if c["name"] not in deck_cards:
            continue
        type_line = c.get("type_line", "")
        if "Creature" not in type_line:
            continue
        creature_count += 1
        if " — " in type_line:
            subtypes = type_line.split(" — ")[1].split()
            for st in subtypes:
                type_counts[st.strip(",")] += 1

    if creature_count == 0:
        return set()

    dominant = set()
    for t, count in type_counts.items():
        if count / creature_count >= threshold:
            dominant.add(t)

    if dominant:
        print(f"  Detected tribal types: {', '.join(sorted(dominant))} "
              f"(>{threshold:.0%} of {creature_count} creatures)")

    return dominant


def _filter_candidates(candidates: list[dict], color_identity: set[str],
                       db_path: str = None) -> list[dict]:
    """Filter candidates by color identity, commander legality, and paper availability.

    Uses Scryfall metadata from the DB (backfilled via tag_db.py backfill).
    """
    import sqlite3
    if db_path is None:
        from tag_db import DB_PATH
        db_path = DB_PATH

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Batch-load metadata for all candidates
    oids = [c["oracle_id"] for c in candidates]
    filtered = []

    chunk_size = 500
    legal_oids = set()
    for i in range(0, len(oids), chunk_size):
        chunk = oids[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT oracle_id, color_identity, legal_commander FROM cards "
            f"WHERE oracle_id IN ({placeholders})", chunk
        ).fetchall()
        for row in rows:
            # Check commander legality
            if not row["legal_commander"]:
                continue
            # Check color identity subset
            try:
                card_colors = set(json.loads(row["color_identity"]))
            except (json.JSONDecodeError, TypeError):
                card_colors = set()
            if card_colors <= color_identity:
                legal_oids.add(row["oracle_id"])

    conn.close()

    filtered = [c for c in candidates if c["oracle_id"] in legal_oids]
    return filtered


def _find_embedding_candidates(deck_cards: list[dict], deck_oids: set[str],
                               db_path: str, top_per_card: int = 3,
                               min_similarity: float = 0.70) -> list[dict]:
    """Find recommendation candidates via embedding similarity.

    For each deck card, finds top-N most similar cards not already in the deck.
    Returns deduplicated list of candidate cards loaded from DB.
    """
    try:
        from card_embeddings import load_embeddings
        import numpy as np
    except ImportError:
        return []

    import os
    emb_path = os.path.join(DATA_DIR, "embeddings.npy")
    if not os.path.exists(emb_path):
        return []

    embeddings, oracle_ids = load_embeddings()
    oid_to_idx = {oid: i for i, oid in enumerate(oracle_ids)}

    # Get indices for deck cards
    deck_indices = []
    for card in deck_cards:
        idx = oid_to_idx.get(card["oracle_id"])
        if idx is not None:
            deck_indices.append(idx)

    if not deck_indices:
        return []

    # Compute average deck embedding for centroid-based search
    deck_matrix = embeddings[np.array(deck_indices)]
    deck_centroid = deck_matrix.mean(axis=0)
    deck_centroid = deck_centroid / np.linalg.norm(deck_centroid)

    # Find cards similar to the deck centroid
    all_sims = embeddings @ deck_centroid

    # Also find per-card similar cards (catches specific synergies)
    candidate_oids = set()
    for deck_idx in deck_indices:
        card_sims = embeddings[deck_idx] @ embeddings.T
        top_idx = np.argpartition(-card_sims, top_per_card + 1)[:top_per_card + 1]
        for idx in top_idx:
            oid = oracle_ids[idx]
            if oid not in deck_oids and card_sims[idx] >= min_similarity:
                candidate_oids.add(oid)

    # Also add top centroid-similar cards
    centroid_top = np.argpartition(-all_sims, 100)[:100]
    for idx in centroid_top:
        oid = oracle_ids[idx]
        if oid not in deck_oids and all_sims[idx] >= min_similarity:
            candidate_oids.add(oid)

    if not candidate_oids:
        return []

    from tag_db import get_cards_by_oids
    return get_cards_by_oids(list(candidate_oids), db_path)


def build_from_commander(commander_name: str, top_n: int = 30):
    """Build a deck recommendation from scratch based on commander card alone.

    1. Load commander from DB, read its provides/wants
    2. Extract creature types from commander's type_line
    3. Find all commander-legal cards in the commander's color identity
    4. Score each card by how well it connects to the commander's strategy
    5. Group and display by strategy
    """
    import sqlite3
    from tag_db import DB_PATH, get_cards_by_names

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Find commander
    row = conn.execute("SELECT * FROM cards WHERE name = ?", (commander_name,)).fetchone()
    if not row:
        # Try fuzzy match
        row = conn.execute("SELECT * FROM cards WHERE name LIKE ?",
                          (f"%{commander_name}%",)).fetchone()
    if not row:
        print(f"Commander not found: {commander_name}")
        return

    cmd_oid = row["oracle_id"]
    cmd_name = row["name"]
    cmd_type = row["type_line"]
    cmd_text = row["oracle_text"]
    try:
        cmd_colors = set(json.loads(row["color_identity"]))
    except (json.JSONDecodeError, TypeError):
        cmd_colors = set()

    # Get commander's tags
    cmd_provides = [r[0] for r in conn.execute(
        "SELECT tag FROM provides WHERE oracle_id=?", (cmd_oid,))]
    cmd_wants = [r[0] for r in conn.execute(
        "SELECT tag FROM wants WHERE oracle_id=?", (cmd_oid,))]

    # Extract creature types from commander
    cmd_subtypes = set()
    if " — " in cmd_type:
        cmd_subtypes = {s.strip(",") for s in cmd_type.split(" — ")[1].split()}

    # Build expanded wants: what the commander wants + semantic bridges from provides
    # e.g. commander provides token-generation → also find cards wanting token-events, creature-board
    expanded_wants = set(cmd_wants)
    for p_tag in cmd_provides:
        for (bridge_p, bridge_w), weight in SEMANTIC_BRIDGES.items():
            if bridge_p == p_tag and weight >= 0.5:
                expanded_wants.add(bridge_w)

    # Build expanded provides: what the commander provides + semantic bridges from wants
    expanded_provides = set(cmd_provides)
    for w_tag in cmd_wants:
        for (bridge_p, bridge_w), weight in SEMANTIC_BRIDGES.items():
            if bridge_w == w_tag and weight >= 0.5:
                expanded_provides.add(bridge_p)

    print(f"\n{'═' * 70}")
    print(f"COMMANDER: {cmd_name}")
    print(f"  {cmd_type} | CMC {row['cmc']}")
    print(f"  {cmd_text}")
    print(f"  Colors: {','.join(sorted(cmd_colors)) or 'C'}")
    print(f"  Provides: {cmd_provides}")
    print(f"  Wants: {cmd_wants}")
    if expanded_wants - set(cmd_wants):
        print(f"  Expanded wants (via bridges): {sorted(expanded_wants - set(cmd_wants))}")
    if expanded_provides - set(cmd_provides):
        print(f"  Expanded provides (via bridges): {sorted(expanded_provides - set(cmd_provides))}")
    if cmd_subtypes:
        print(f"  Creature types: {', '.join(sorted(cmd_subtypes))}")
    print(f"{'═' * 70}")

    # Load ALL legal cards in commander's colors from DB
    all_rows = conn.execute(
        "SELECT oracle_id, name, type_line, cmc, mana_cost, color_identity, oracle_text "
        "FROM cards WHERE legal_commander = 1 AND oracle_id != ?",
        (cmd_oid,)
    ).fetchall()

    # Filter by color identity
    legal_cards = {}
    for r in all_rows:
        try:
            card_colors = set(json.loads(r["color_identity"]))
        except (json.JSONDecodeError, TypeError):
            card_colors = set()
        if card_colors <= cmd_colors:
            legal_cards[r["oracle_id"]] = {
                "name": r["name"], "type_line": r["type_line"],
                "cmc": r["cmc"], "mana_cost": r["mana_cost"] or "",
                "oracle_text": r["oracle_text"] or "",
            }

    # Load all provides/wants for legal cards
    legal_oids = list(legal_cards.keys())
    card_provides = defaultdict(set)
    card_wants = defaultdict(set)

    chunk_size = 500
    for i in range(0, len(legal_oids), chunk_size):
        chunk = legal_oids[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT oracle_id, tag FROM provides WHERE oracle_id IN ({placeholders})", chunk
        ):
            card_provides[r[0]].add(r[1])
        for r in conn.execute(
            f"SELECT oracle_id, tag FROM wants WHERE oracle_id IN ({placeholders})", chunk
        ):
            card_wants[r[0]].add(r[1])

    conn.close()

    # Score each card
    scores = {}
    for oid, meta in legal_cards.items():
        name = meta["name"]
        c_provides = card_provides.get(oid, set())
        c_wants = card_wants.get(oid, set())

        enabler_tags = []  # this card provides what commander wants
        payoff_tags = []   # this card wants what commander provides
        score = 0.0

        # Exact + semantic: card provides what commander wants
        for p_tag in c_provides:
            if p_tag in expanded_wants:
                weight = 1.0 if p_tag in cmd_wants else 0.7  # bridge match = lower weight
                score += weight
                enabler_tags.append(p_tag)

        # Exact + semantic: card wants what commander provides
        for w_tag in c_wants:
            if w_tag in expanded_provides:
                weight = 1.0 if w_tag in cmd_provides else 0.7
                score += weight
                payoff_tags.append(w_tag)

        # Tribal boost
        tribal = False
        if cmd_subtypes and any(t in meta["type_line"] for t in cmd_subtypes):
            score *= 1.5
            tribal = True

        if score > 0:
            scores[name] = {
                "score": round(score, 1),
                "type_line": meta["type_line"],
                "cmc": meta["cmc"],
                "mana_cost": meta["mana_cost"],
                "enabler_tags": enabler_tags,
                "payoff_tags": payoff_tags,
                "tribal": tribal,
                "is_enabler": len(enabler_tags) > 0,
                "is_payoff": len(payoff_tags) > 0,
            }

    ranked = sorted(scores.items(), key=lambda x: -x[1]["score"])

    # Split into categories
    both = [(n, s) for n, s in ranked if s["is_enabler"] and s["is_payoff"]]
    enablers_only = [(n, s) for n, s in ranked if s["is_enabler"] and not s["is_payoff"]]
    payoffs_only = [(n, s) for n, s in ranked if s["is_payoff"] and not s["is_enabler"]]

    print(f"\nFound {len(scores)} synergy cards ({len(both)} both, "
          f"{len(enablers_only)} enablers, {len(payoffs_only)} payoffs)")

    # Display BEST FIT first
    if both:
        print(f"\nBEST FIT — enable AND benefit from {cmd_name} ({len(both)} found)")
        print(f"{'─' * 70}")
        for name, info in both[:top_n]:
            tribal = " [tribal]" if info["tribal"] else ""
            e_tags = ", ".join(info["enabler_tags"])
            p_tags = ", ".join(info["payoff_tags"])
            print(f"  {name}{tribal}  (score {info['score']})")
            print(f"    {info['type_line']} | CMC {info['cmc']}")
            print(f"    enables: {e_tags} | benefits: {p_tags}")

    if enablers_only:
        print(f"\nENABLERS — provide what {cmd_name} wants ({len(enablers_only)} found)")
        print(f"{'─' * 70}")
        for name, info in enablers_only[:top_n]:
            tribal = " [tribal]" if info["tribal"] else ""
            tags = ", ".join(info["enabler_tags"])
            print(f"  {name}{tribal}  (score {info['score']})")
            print(f"    {info['type_line']} | CMC {info['cmc']}")
            print(f"    enables: {tags}")

    if payoffs_only:
        print(f"\nPAYOFFS — benefit from {cmd_name} ({len(payoffs_only)} found)")
        print(f"{'─' * 70}")
        for name, info in payoffs_only[:top_n]:
            tribal = " [tribal]" if info["tribal"] else ""
            tags = ", ".join(info["payoff_tags"])
            print(f"  {name}{tribal}  (score {info['score']})")
            print(f"    {info['type_line']} | CMC {info['cmc']}")
            print(f"    benefits from: {tags}")


def show_deck_analysis(deck_cards, deck_oids, active_strategies, commander_name, db_path=None, graph=None, deck_set=None):
    """Enhanced deck analysis with strategy coverage."""
    import sqlite3
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)

    # Count cards per strategy
    strat_counts = {}
    for oid in deck_oids:
        for row in conn.execute(
            "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3", (oid,)
        ):
            strat_counts[row[0]] = strat_counts.get(row[0], 0) + 1

    # Count non-land cards
    non_land = sum(1 for c in deck_cards if "Land" not in (c.get("type_line") or ""))

    # Count strategy-aligned cards
    aligned = 0
    if active_strategies:
        placeholders = ','.join('?' * len(active_strategies))
        for oid in deck_oids:
            rows = conn.execute(
                f"SELECT 1 FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3 AND strategy IN ({placeholders})",
                (oid, *active_strategies)
            ).fetchall()
            if rows:
                aligned += 1

    combos = find_combos_tiered(deck_oids, db_path)
    anti = find_anti_synergy(deck_oids, active_strategies, db_path, graph=graph, deck_cards_set=deck_set)
    conn.close()

    print(f"\n{'='*60}")
    print(f"DECK ANALYSIS: {commander_name}")
    print(f"{'='*60}")

    print(f"Detected strategies:")
    for strat in sorted(active_strategies):
        cnt = strat_counts.get(strat, 0)
        if cnt > 0:
            print(f"  {strat}: {cnt} cards")

    coverage = aligned * 100 // max(non_land, 1)
    print(f"Strategy coverage: {coverage}% of {non_land} non-land cards align with >=1 strategy")

    confirmed = sum(1 for c in combos if c["tier"] == "infinite-confirmed")
    likely = sum(1 for c in combos if c["tier"] == "combo-likely")
    synergy = sum(1 for c in combos if c["tier"] == "synergy")
    print(f"Confirmed combos: {confirmed} (Spellbook)")
    print(f"Likely combos: {likely} (trigger chain)")
    print(f"Synergy pairs: {synergy}")

    if anti:
        print(f"Anti-synergy cards: {len(anti)} (swap candidates)")
        for a in anti[:5]:
            print(f"  {a['name']} ({a['role'] or 'unknown'}) — {a['partners']} partners, score {a['synergy_score']}")


def run():
    from decks import list_decks

    parser = argparse.ArgumentParser(description="Build MTG synergy graph")
    parser.add_argument("--deck", choices=list_decks(), help="Deck config to use")
    parser.add_argument("--commander", type=str, help="Build recommendations from commander alone")
    parser.add_argument("--build", action="store_true",
                        help="Build deck from commander (use with --commander)")
    parser.add_argument("--input", type=str, help="Override: load cards from JSON file instead of DB")
    parser.add_argument("--card", type=str, help="Show synergies for specific card")
    parser.add_argument("--deck-view", action="store_true",
                        help="Show synergy network within the deck")
    parser.add_argument("--recommend", action="store_true",
                        help="Recommend cards based on synergy with the deck")
    parser.add_argument("--combos", action="store_true",
                        help="Detect 3- and 4-card combos in the deck")
    parser.add_argument("--swaps", action="store_true",
                        help="Suggest card swaps to improve deck synergy")
    parser.add_argument("--validate", action="store_true",
                        help="Validate against hand-curated synergy pairs")
    parser.add_argument("--visualize", action="store_true",
                        help="Generate interactive HTML visualization")
    parser.add_argument("--export", action="store_true", help="Export graph as JSON")
    parser.add_argument("--top", type=int, default=30, help="Top N edges to show")
    parser.add_argument("--strategies", default="auto",
                        help="Comma-separated strategies to focus (default: auto-detect)")
    parser.add_argument("--exclude-strategies", default=None,
                        help="Comma-separated strategies to exclude")
    args = parser.parse_args()

    # Commander build mode — no deck needed
    if args.commander and args.build:
        build_from_commander(args.commander, args.top)
        return

    if not args.deck:
        parser.error("--deck is required (or use --commander with --build)")

    if args.input:
        # Manual override: load from JSON file
        cards = load_merged(args.input)
        print(f"Loaded {len(cards)} cards from {args.input}")
    else:
        # Default: load deck cards from SQLite DB
        from tag_db import get_cards_by_names, find_synergy_candidates, DB_PATH
        from decks import load_deck
        deck = load_deck(args.deck)
        deck_names = deck.DECKLIST + [deck.COMMANDER]

        cards = get_cards_by_names(deck_names, DB_PATH)
        print(f"Loaded {len(cards)} deck cards from DB")

        if args.recommend or args.swaps:
            # Find synergy candidates from DB (targeted + commander bridge expansion)
            commander_card = next((c for c in cards if c["name"] == deck.COMMANDER), None)
            candidates = find_synergy_candidates(cards, DB_PATH, commander=commander_card)
            print(f"Found {len(candidates)} tag-based candidates from DB")
            deck_oids = {c["oracle_id"] for c in cards}

            # Hybrid: also find candidates via embedding similarity
            emb_candidates = _find_embedding_candidates(cards, deck_oids, DB_PATH)
            if emb_candidates:
                print(f"Found {len(emb_candidates)} embedding-based candidates")

            # Filter candidates by color identity + commander legality
            color_id = deck.COLOR_IDENTITY
            candidates = _filter_candidates(candidates, color_id, DB_PATH)
            if emb_candidates:
                emb_candidates = _filter_candidates(emb_candidates, color_id, DB_PATH)
            print(f"After filter (color={','.join(sorted(color_id))}, legal, paper): "
                  f"{len(candidates)} tag + {len(emb_candidates)} embedding candidates")

            # Merge: union of tag-based and embedding-based candidates
            all_candidate_oids = set()
            for c in candidates:
                if c["oracle_id"] not in deck_oids:
                    all_candidate_oids.add(c["oracle_id"])
                    cards.append(c)
            for c in emb_candidates:
                if c["oracle_id"] not in deck_oids and c["oracle_id"] not in all_candidate_oids:
                    cards.append(c)

            print(f"Building graph for {len(cards)} cards (deck + candidates)")

    # --- Strategy detection ---
    active_strategies = set()
    db_path = None
    if not args.input:
        from tag_db import DB_PATH as _db_path
        db_path = _db_path
        from strategy_detector import detect_strategies
        commander_card = next((c for c in cards if c["name"] == deck.COMMANDER), None)
        commander_oid = commander_card["oracle_id"] if commander_card else None
        if args.strategies == "auto" and commander_oid:
            detected = detect_strategies(commander_oid, db_path)
            active_strategies = {s["name"] for s in detected if s["confidence"] >= 0.3}
            # Also detect strategies from deck composition:
            # 1. Tribal strategies from creature type distribution
            deck_names_set = set(deck.DECKLIST) | {deck.COMMANDER}
            deck_cards_for_types = [c for c in cards if c["name"] in deck_names_set]
            deck_types = _detect_deck_types(deck_cards_for_types, deck_names_set)
            if deck_types:
                from strategy_detector import CREATURE_TYPE_STRATEGIES
                import sqlite3 as _sqlite3
                _conn = _sqlite3.connect(db_path)
                for dtype in deck_types:
                    strat = CREATURE_TYPE_STRATEGIES.get(dtype.lower())
                    if strat and strat not in active_strategies:
                        has_cards = _conn.execute(
                            "SELECT 1 FROM card_strategies WHERE strategy = ? LIMIT 1",
                            (strat,)
                        ).fetchone()
                        if has_cards:
                            active_strategies.add(strat)
                _conn.close()

            # 2. Strategies shared by 20%+ of non-land deck cards
            import sqlite3 as _sqlite3
            _conn = _sqlite3.connect(db_path)
            deck_oid_set = {c["oracle_id"] for c in cards if c["name"] in deck_names_set}
            non_land_count = sum(1 for c in cards
                                 if c["name"] in deck_names_set and "Land" not in c.get("type_line", ""))
            if non_land_count > 0:
                from collections import Counter as _Counter
                strat_counts = _Counter()
                for oid in deck_oid_set:
                    for row in _conn.execute(
                        "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
                        (oid,)
                    ):
                        strat_counts[row[0]] += 1
                for strat, cnt in strat_counts.items():
                    if cnt / non_land_count >= 0.2 and strat not in active_strategies:
                        active_strategies.add(strat)
            _conn.close()
        elif args.strategies != "auto":
            active_strategies = set(args.strategies.split(","))
        if args.exclude_strategies:
            active_strategies -= set(args.exclude_strategies.split(","))
        if active_strategies:
            print(f"Active strategies: {', '.join(sorted(active_strategies))}")

    # Collect deck oracle_ids for fan-out cap preservation
    _build_deck_oids = None
    if not args.input:
        deck_names_for_oids = set(deck.DECKLIST) | {deck.COMMANDER}
        _build_deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_names_for_oids}

    graph = build_graph(cards, deck_oids=_build_deck_oids)
    stats = graph["stats"]
    print(f"\nGraph stats:")
    print(f"  raw signal edges:      {stats['total_raw_edges']}")
    print(f"    provides→wants:      {stats['provides_wants_edges']}")
    print(f"    peer-enabler:        {stats['peer_enabler_edges']}")
    print(f"    shared-wants:        {stats['shared_wants_edges']}")
    print(f"    embedding:           {stats.get('embedding_edges', 0)}")
    print(f"  composite edges:       {stats['pruned_edges']} (unique card pairs)")
    print(f"  cards with edges:      {stats['cards_with_edges']}/{stats['cards_total']}")

    # Ensure deck config is loaded (already set in DB path, need it for --input path)
    if args.input:
        from decks import load_deck
        deck = load_deck(args.deck)

    if args.card:
        show_card_synergies(graph, args.card)
    elif args.visualize:
        deck_set = set(deck.DECKLIST) | {deck.COMMANDER}
        combos = find_combos(graph, cards, deck_set, deck.COMMANDER, top_n=20)
        # Enrich with Spellbook / inferred tiered combo data if DB is available
        tiered = None
        if db_path:
            deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_set}
            tiered = find_combos_tiered(deck_oids, db_path)
            confirmed = [c for c in tiered if c["tier"] == "infinite-confirmed"]
            if confirmed:
                print(f"\n  Spellbook confirmed combos: {len(confirmed)} (highlighted in visualization)")
        generate_visualization(graph, cards, deck_set, deck.COMMANDER, args.deck, combos,
                               tiered_combos=tiered)
    elif args.deck_view or args.recommend or args.combos or args.swaps:
        deck_set = set(deck.DECKLIST) | {deck.COMMANDER}
        deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_set}
        if args.deck_view:
            show_deck_synergies(graph, deck_set, deck.COMMANDER, cards, args.top)
            if db_path and active_strategies:
                deck_cards_in_set = [c for c in cards if c["name"] in deck_set]
                show_deck_analysis(deck_cards_in_set, deck_oids, active_strategies, deck.COMMANDER, db_path, graph=graph, deck_set=deck_set)
        if args.combos:
            if db_path:
                # Use enhanced 3-tier combo detection
                show_combos_tiered(deck_oids, deck.COMMANDER, db_path, color_identity=deck.COLOR_IDENTITY)
            else:
                # Fallback to legacy combo detection
                combos = find_combos(graph, cards, deck_set, deck.COMMANDER, args.top)
                show_combos(combos, deck.COMMANDER, args.top)
        if args.swaps:
            swap_deck_types = _detect_deck_types(cards, deck_set)
            swaps = suggest_swaps(graph, deck_set, deck.COMMANDER, cards, args.top,
                                  active_strategies=active_strategies, db_path=db_path,
                                  deck_types=swap_deck_types)
            show_swaps(swaps, args.top)
        if args.recommend:
            # Auto-detect dominant creature types for tribal boost
            deck_types = _detect_deck_types(cards, deck_set)
            recommend_cards(graph, deck_set, cards, deck_types, args.top,
                            active_strategies=active_strategies, db_path=db_path,
                            color_identity=deck.COLOR_IDENTITY, commander=deck.COMMANDER)
    elif args.validate:
        validate_against_curated(graph, deck.SYNERGY_PAIRS)
    elif args.export:
        graph_output = os.path.join(DATA_DIR, f"{args.deck}_synergy_graph.json")
        export = {
            "edges": graph["edges"],
            "stats": graph["stats"],
        }
        with open(graph_output, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\nExported graph to {graph_output}")
    else:
        # Show top edges
        print(f"\nTop {args.top} synergy edges:")
        print(f"{'─' * 70}")
        for edge in graph["edges"][:args.top]:
            sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
            print(f"  [{edge['score']:5.1f} {sig}] {edge['source']} ↔ {edge['target']}")
            for r in edge["reasons"][:2]:
                print(f"       {r}")


if __name__ == "__main__":
    run()
