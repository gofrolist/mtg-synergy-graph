"""Card recommendation engine — scoring pipeline."""
import json
from collections import defaultdict

from mtg_synergy.recommend.affinity import _compute_commander_affinity
from mtg_synergy.combos.detector import find_partial_combos


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
        edhrec_str = f" EDH={info['edhrec_syn']:.2f}" if info.get("edhrec_syn") else ""
        overlap_str = f" ov={info['cmdr_overlap']}" if info.get("cmdr_overlap") else ""
        print(f"\n  {pct:5.1f}% {bar} {card}{tribal}{combo}{tower_str}{edhrec_str}{overlap_str}{high_cmc}")
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
