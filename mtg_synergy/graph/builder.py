"""Main graph builder -- composes all edge types into a single graph."""
from collections import defaultdict

from mtg_synergy.graph.edges import (
    build_provides_wants_edges,
    build_peer_edges,
    build_shared_wants_edges,
    build_embedding_edges,
)


def build_graph(cards: list[dict], min_score: float = 0.5, deck_oids: set = None) -> dict:
    """Build the complete synergy graph with composite scoring.

    Merges all edge types per card pair into a single composite score.
    All edges above min_score are kept — use adjacency + sorting for
    per-card views.

    deck_oids: if provided, ensures deck cards always get edges even
    when tags are capped for fan-out control.
    """
    pw_edges = build_provides_wants_edges(cards, deck_oids=deck_oids)
    pe_edges = build_peer_edges(cards)
    sw_edges = build_shared_wants_edges(cards)
    emb_edges = build_embedding_edges(cards)

    raw_edges = pw_edges + pe_edges + sw_edges + emb_edges

    # Merge all signals per card pair into composite scores
    # Key by sorted (name_a, name_b) to deduplicate direction
    pair_signals = defaultdict(lambda: {
        "provides_wants": [],
        "shared_tag": [],
        "peer_enabler": [],
        "shared_wants": [],
        "embedding": [],
    })

    for edge in raw_edges:
        key = tuple(sorted([edge["source"], edge["target"]]))
        etype = edge["type"].replace("-", "_")
        pair_signals[key][etype].append(edge)

    # Score each pair
    composite_edges = []
    for (name_a, name_b), signals in pair_signals.items():
        reasons = []
        score = 0.0

        # Provides-wants: strongest signal (directional synergy)
        # Sum weights from both directions (A provides for B + B provides for A)
        pw = signals["provides_wants"]
        if pw:
            pw_weight = sum(e["weight"] for e in pw)
            score += pw_weight * 1.5  # 1.5x multiplier for directed synergy
            best_pw = max(pw, key=lambda e: e["weight"])
            reasons.append(f"[p→w] {best_pw['reason'][:60]}")

        # Shared tags: community-validated
        st = signals["shared_tag"]
        if st:
            st_weight = max(e["weight"] for e in st)
            score += st_weight * 1.5  # 1.5x for crowd-sourced signal
            best_st = max(st, key=lambda e: e["weight"])
            reasons.append(f"[tags] {best_st['reason'][:60]}")

        # Peer enablers: same ecosystem
        pe = signals["peer_enabler"]
        if pe:
            pe_weight = max(e["weight"] for e in pe)
            score += pe_weight * 1.0
            best_pe = max(pe, key=lambda e: e["weight"])
            reasons.append(f"[peer] {best_pe['reason'][:60]}")

        # Shared wants: benefit from same conditions
        sw = signals["shared_wants"]
        if sw:
            sw_weight = max(e["weight"] for e in sw)
            score += sw_weight * 1.0
            best_sw = max(sw, key=lambda e: e["weight"])
            reasons.append(f"[wants] {best_sw['reason'][:60]}")

        # Embedding similarity: mechanical text similarity
        emb = signals["embedding"]
        if emb:
            emb_weight = max(e["weight"] for e in emb)
            score += emb_weight * 1.0
            best_emb = max(emb, key=lambda e: e["weight"])
            reasons.append(f"[emb] {best_emb['reason'][:60]}")

        # Bonus for multi-signal edges (confirmed from multiple sources)
        n_signals = sum(1 for v in signals.values() if v)
        if n_signals >= 3:
            score *= 1.3  # 30% bonus for 3+ signal types
        elif n_signals >= 2:
            score *= 1.1  # 10% bonus for 2 signal types

        # Find source IDs from any edge
        any_edge = next(e for edges in signals.values() for e in edges)
        source_id = any_edge["source_id"] if any_edge["source"] == name_a else any_edge["target_id"]
        target_id = any_edge["target_id"] if any_edge["source"] == name_a else any_edge["source_id"]

        composite_edges.append({
            "source": name_a,
            "source_id": source_id,
            "target": name_b,
            "target_id": target_id,
            "score": round(score, 1),
            "signals": n_signals,
            "reasons": reasons,
        })

    # Sort by composite score
    composite_edges.sort(key=lambda e: e["score"], reverse=True)

    # Filter by minimum score
    pruned_edges = [e for e in composite_edges if e["score"] >= min_score]

    # Build adjacency index (undirected)
    adjacency = defaultdict(list)
    for edge in pruned_edges:
        adjacency[edge["source"]].append(edge)
        adjacency[edge["target"]].append({
            **edge,
            "source": edge["target"],
            "source_id": edge["target_id"],
            "target": edge["source"],
            "target_id": edge["source_id"],
        })

    return {
        "edges": pruned_edges,
        "adjacency": dict(adjacency),
        "stats": {
            "total_raw_edges": len(raw_edges),
            "unique_pairs": len(composite_edges),
            "pruned_edges": len(pruned_edges),
            "provides_wants_edges": len(pw_edges),
            "peer_enabler_edges": len(pe_edges),
            "shared_wants_edges": len(sw_edges),
            "embedding_edges": len(emb_edges),
            "cards_with_edges": len(adjacency),
            "cards_total": len(cards),
        },
    }
