"""Edge computation for the synergy graph."""
import os
from collections import defaultdict

from mtg_synergy.graph.idf import compute_idf

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")

# Wants tags that are always broadly inferred (not meaningful for peer detection).
# Uses board-generic instead of removed parent tag creature-board.
SKIP_WANTS = {"board-generic", "mana-needs"}


def build_provides_wants_edges(cards: list[dict], deck_oids: set = None) -> list[dict]:
    """Build directed edges: card A provides X, card B wants X.

    Uses exact matching on normalized vocabulary plus semantic bridges
    for cross-concept connections. Weights are scaled by IDF — rare tag
    matches score higher than common ones.

    Optimized: iterates per-card wants -> finds providers via inverted index.
    Fan-out capped to avoid O(n^2) explosion on common tags.
    """
    # Compute IDF multipliers
    idf = compute_idf(cards)

    # Sort cards deterministically to ensure reproducible index ordering
    cards = sorted(cards, key=lambda c: c["oracle_id"])

    # Build provides inverted index: tag -> [(card_name, oracle_id)]
    provides_index = defaultdict(list)
    for card in cards:
        name = card["name"]
        oid = card["oracle_id"]
        for p in card.get("provides", []):
            provides_index[p].append((name, oid))

    # Build exact-match lookup: want_tag -> [(provide_tag, weight)]
    # No bridges needed — provides and wants use the same Forge-derived vocabulary
    want_to_provides = defaultdict(list)
    for tag in provides_index:
        want_to_provides[tag].append((tag, 1.0))

    # Fan-out cap: skip provides tags with too many cards.
    # Common tags have low IDF anyway, so capping preserves quality.
    # Scale: <=2k cards -> 500, 10k cards -> 100
    n = len(cards)
    MAX_PROVIDERS = max(50, min(500, 2000 * 500 // max(n, 1)))

    # Pre-filter provides_index to stay within memory budget.
    # Keep a capped sample of providers per tag.
    # Sort deterministically: deck cards first (always kept), then by oracle_id.
    _deck_oid_set = deck_oids or set()
    for tag in list(provides_index):
        if len(provides_index[tag]) > MAX_PROVIDERS:
            provides_index[tag].sort(key=lambda x: (0 if x[1] in _deck_oid_set else 1, x[1]))
            provides_index[tag] = provides_index[tag][:MAX_PROVIDERS]

    # Accumulate best match and total weight per card pair.
    # Stores (best_weight, best_ptag, best_wtag, total_weight, match_count) compactly.
    pair_data = {}  # (provider_oid, wanter_oid) -> [best_weight, best_ptag, best_wtag, total_weight, count]

    # For large card sets, also cap wanters per tag to control cross-product
    MAX_WANTERS = max(50, min(500, 2000 * 500 // max(n, 1)))
    wants_index = defaultdict(list)
    for card in cards:
        for w in card.get("wants", []):
            wants_index[w].append(card["oracle_id"])
    large_want_tags = {tag for tag, members in wants_index.items() if len(members) > MAX_WANTERS}

    for card in cards:
        wanter_oid = card["oracle_id"]
        is_deck_card = wanter_oid in _deck_oid_set
        for want_tag in card.get("wants", []):
            # Skip large want tags UNLESS the card is a deck card
            # (deck cards always get their edges, even on common wants)
            if want_tag in large_want_tags and not is_deck_card:
                continue
            for provide_tag, base_weight in want_to_provides.get(want_tag, []):
                providers = provides_index.get(provide_tag)
                if not providers:
                    continue

                p_idf = idf.get(provide_tag, 1.0)
                w_idf = idf.get(want_tag, 1.0)
                weight = base_weight * (p_idf * w_idf) ** 0.5

                for provider_name, provider_oid in providers:
                    if provider_oid == wanter_oid:
                        continue
                    key = (provider_oid, wanter_oid)
                    cur = pair_data.get(key)
                    if cur is None:
                        pair_data[key] = [weight, provide_tag, want_tag, weight, 1]
                    else:
                        cur[3] += weight  # total_weight
                        cur[4] += 1       # count
                        if weight > cur[0]:
                            cur[0] = weight
                            cur[1] = provide_tag
                            cur[2] = want_tag

    edges = []
    card_names = {c["oracle_id"]: c["name"] for c in cards}

    for (prov_oid, want_oid), data in pair_data.items():
        best_weight, best_ptag, best_wtag, total_weight, count = data
        total_weight = min(total_weight, 5.0)

        edges.append({
            "source": card_names.get(prov_oid, prov_oid),
            "source_id": prov_oid,
            "target": card_names.get(want_oid, want_oid),
            "target_id": want_oid,
            "type": "provides-wants",
            "reason": f"provides '{best_ptag}' -> wants '{best_wtag}' ({count} matches, best={best_weight:.0%})",
            "weight": round(total_weight, 2),
        })

    return edges


def build_peer_edges(cards: list[dict], min_shared: int = 2) -> list[dict]:
    """Build undirected edges between cards that both provide AND want the same things.

    Peer enablers are cards in the same ecosystem — both benefit from the same
    conditions and both contribute to them. E.g., two counter-amplifiers both
    provide counter-amplification and want counter-placement-events.
    """
    # For each card, compute (provides intersection wants_of_other) bidirectionally
    # But simpler: cards sharing >=2 provides tags are peers
    provides_index = defaultdict(list)  # tag -> [(name, oid)]
    for card in cards:
        name = card["name"]
        oid = card["oracle_id"]
        for p in card.get("provides", []):
            provides_index[p].append((name, oid))

    # Find pairs sharing provides tags
    pair_shared = defaultdict(list)  # (oid_a, oid_b) -> [shared_provides]
    max_members = max(30, len(cards) // 100)
    for tag, members in provides_index.items():
        if len(members) > max_members:
            continue  # skip overly common provides
        for i, (name_a, oid_a) in enumerate(members):
            for name_b, oid_b in members[i+1:]:
                key = tuple(sorted([oid_a, oid_b]))
                pair_shared[key].append(tag)

    edges = []
    card_names = {c["oracle_id"]: c["name"] for c in cards}

    for (oid_a, oid_b), shared in pair_shared.items():
        if len(shared) < min_shared:
            continue
        edges.append({
            "source": card_names[oid_a],
            "source_id": oid_a,
            "target": card_names[oid_b],
            "target_id": oid_b,
            "type": "peer-enabler",
            "reason": f"both provide: {', '.join(shared[:4])}{'...' if len(shared) > 4 else ''}",
            "weight": len(shared),
        })

    return edges


def build_shared_wants_edges(cards: list[dict], min_shared: int = 2) -> list[dict]:
    """Build undirected edges between cards that want the same things.

    Cards wanting the same conditions naturally synergize — they benefit from
    the same board state and each card's presence makes the other better.
    E.g., Aura Shards and Kyler both want creature-enters events.

    Uses IDF-like weighting: common wants (shared by many cards) contribute
    less than rare wants. Only creates edges when total weight >= min_shared.
    """
    # Skip wants that are always broadly inferred (not meaningful for peer detection)

    wants_index = defaultdict(list)  # tag -> [(name, oid)]
    for card in cards:
        name = card["name"]
        oid = card["oracle_id"]
        for w in card.get("wants", []):
            if w not in SKIP_WANTS:
                wants_index[w].append((name, oid))

    # IDF-like weight: rare wants are more meaningful
    total_cards = len(cards)
    tag_weight = {}
    for tag, members in wants_index.items():
        freq = len(members) / total_cards
        # Scale: tags wanted by <5% of cards = 1.0, by 50%+ = 0.1
        tag_weight[tag] = max(0.1, min(1.0, 1.0 - freq))

    # Build pairs with weighted overlap
    max_wants_members = max(50, len(cards) // 50)
    pair_data = defaultdict(lambda: {"tags": [], "weight": 0.0})
    for tag, members in wants_index.items():
        w = tag_weight[tag]
        if w < 0.15:  # skip extremely common wants (>85% of cards)
            continue
        if len(members) > max_wants_members:
            continue  # skip tags shared by too many cards
        for i, (name_a, oid_a) in enumerate(members):
            for name_b, oid_b in members[i+1:]:
                key = tuple(sorted([oid_a, oid_b]))
                pair_data[key]["tags"].append(tag)
                pair_data[key]["weight"] += w

    edges = []
    card_names = {c["oracle_id"]: c["name"] for c in cards}

    for (oid_a, oid_b), data in pair_data.items():
        if data["weight"] < min_shared:
            continue
        shared = data["tags"]
        edges.append({
            "source": card_names[oid_a],
            "source_id": oid_a,
            "target": card_names[oid_b],
            "target_id": oid_b,
            "type": "shared-wants",
            "reason": f"both want: {', '.join(shared[:4])}{'...' if len(shared) > 4 else ''}",
            "weight": max(1, round(data["weight"])),
        })

    return edges


def build_embedding_edges(cards: list[dict], min_similarity: float = 0.75,
                          max_edges_per_card: int = 5) -> list[dict]:
    """Build undirected edges from card embedding cosine similarity.

    Uses pre-computed embeddings from card_embeddings.py. Only creates edges
    above min_similarity threshold, capped to top-N per card to avoid noise.

    This is the 5th signal type — catches mechanical similarity that
    tag-based signals miss (e.g., cards with similar oracle text patterns).
    """
    try:
        from card_embeddings import load_embeddings
    except ImportError:
        print("  [embedding] card_embeddings.py not available, skipping")
        return []

    emb_path = os.path.join(DATA_DIR, "embeddings.npy")
    if not os.path.exists(emb_path):
        print("  [embedding] No embeddings found, skipping. Run: python3 card_embeddings.py")
        return []

    import numpy as np
    embeddings, oracle_ids = load_embeddings()
    oid_to_idx = {oid: i for i, oid in enumerate(oracle_ids)}

    # Filter to cards in our working set
    card_indices = []
    card_oids = []
    card_names = {}
    for card in cards:
        oid = card["oracle_id"]
        idx = oid_to_idx.get(oid)
        if idx is not None:
            card_indices.append(idx)
            card_oids.append(oid)
            card_names[oid] = card["name"]

    if len(card_indices) < 2:
        return []

    # Extract sub-matrix for our cards
    indices = np.array(card_indices)
    sub_embeddings = embeddings[indices]  # (n_cards, 768)

    # Compute pairwise similarities via dot product (already L2-normalized)
    sim_matrix = sub_embeddings @ sub_embeddings.T  # (n_cards, n_cards)

    # Build edges: for each card, take top-N similar above threshold
    edges = []
    n = len(card_indices)
    seen = set()

    for i in range(n):
        # Get similarities for card i, excluding self
        sims = sim_matrix[i].copy()
        sims[i] = -1.0  # exclude self

        # Top-N above threshold
        top_idx = np.argpartition(-sims, min(max_edges_per_card, n - 1))[:max_edges_per_card]
        top_idx = [j for j in top_idx if sims[j] >= min_similarity]
        top_idx.sort(key=lambda j: -sims[j])

        for j in top_idx:
            pair = tuple(sorted([card_oids[i], card_oids[j]]))
            if pair in seen:
                continue
            seen.add(pair)

            sim = float(sims[j])
            edges.append({
                "source": card_names[card_oids[i]],
                "source_id": card_oids[i],
                "target": card_names[card_oids[j]],
                "target_id": card_oids[j],
                "type": "embedding",
                "reason": f"embedding similarity {sim:.0%}",
                "weight": round(sim * 3.0, 2),  # scale to comparable range with other signals
            })

    return edges
