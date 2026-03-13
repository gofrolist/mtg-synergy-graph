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

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_merged(path: str) -> list[dict]:
    from normalize_tags import normalize_cards

    with open(path) as f:
        cards = json.load(f)
    # Normalize provides/wants vocabulary + infer missing wants
    normalize_cards(cards)
    return cards


def _provides_satisfies_want(provide_tag: str, want_tag: str) -> float:
    """Check if a provides tag satisfies a wants tag. Returns weight 0.0-1.0.

    With normalized vocabulary, uses exact match + semantic bridges.
    Semantic bridges connect provides concepts to wants concepts that
    the vocabulary normalization can't capture (different word roots).
    """
    # Exact match (most common after normalization)
    if provide_tag == want_tag:
        return 1.0

    # Semantic bridges: provides → wants connections with different names
    # Each entry: (provides_tag, wants_tag) → weight
    pair = (provide_tag, want_tag)
    return SEMANTIC_BRIDGES.get(pair, 0.0)


# Semantic bridges between provides and wants with different names.
# These capture relationships that can't be found by string matching.
SEMANTIC_BRIDGES = {
    # Counter placement provides what counter-placement-events wants
    ("counter-placement", "counter-placement-events"): 1.0,
    ("board-wide-counter-placement", "counter-placement-events"): 1.0,
    ("counter-distribution", "counter-placement-events"): 1.0,
    ("proliferate", "counter-placement-events"): 0.8,
    ("counter-amplification", "counter-amplification"): 1.0,

    # Token generation → creature ETB (tokens entering = creatures entering)
    ("token-generation", "creature-etb"): 0.8,
    ("token-doubling", "creature-etb"): 0.6,

    # Creature pump ↔ creature power
    ("creature-pump", "creature-power"): 0.8,
    ("board-wide-counter-placement", "creature-power"): 0.7,
    ("counter-placement", "creature-power"): 0.6,

    # Evasion/trample → combat & attack synergy
    ("trample-grant", "attack-events"): 0.5,
    ("evasion-grant", "attack-events"): 0.5,
    ("haste-grant", "attack-events"): 0.6,
    ("combat-enabler", "attack-events"): 0.7,
    ("combat-enabler", "combat-events"): 0.7,
    ("combat-trigger", "combat-events"): 0.8,
    ("combat-trigger", "attack-events"): 0.7,

    # Tribal connections
    ("human-tribal", "creature-type-selection"): 0.7,
    ("tribal-enabler", "creature-type-selection"): 0.8,
    ("creature-type-flexibility", "creature-type-selection"): 0.9,

    # Token generation → creature board & token events
    ("token-generation", "creature-board"): 0.6,
    ("token-generation", "token-events"): 0.8,
    ("token-doubling", "token-events"): 0.8,

    # Card draw → card draw events
    ("card-draw", "card-draw-events"): 0.8,
    ("top-of-library", "card-draw-events"): 0.5,

    # Removal ↔ targeted spells
    ("targeted-removal", "targeted-spells"): 0.6,
    ("artifact-enchantment-removal", "artifact-presence"): 0.4,

    # Protection synergies
    ("hexproof-grant", "creature-targeting"): 0.4,
    ("indestructible-grant", "creature-death"): 0.5,
    ("reactive-protection", "creature-targeting"): 0.4,
    ("board-protection", "creature-board"): 0.5,

    # Sacrifice payoff → sacrifice events
    ("sacrifice-payoff", "sacrifice-events"): 0.8,

    # Graveyard connections
    ("graveyard-recursion", "graveyard-filling"): 0.7,
    ("graveyard-hate", "graveyard-filling"): 0.4,

    # Mana acceleration → mana needs
    ("mana-acceleration", "mana-needs"): 0.8,
    ("mana-flexibility", "mana-needs"): 0.7,
    ("cost-reduction", "mana-needs"): 0.6,
    ("land-search", "land-density"): 0.9,

    # ETB payoff connections
    ("etb-payoff", "creature-etb"): 0.8,

    # Life gain
    ("life-gain", "life-gain-events"): 0.9,
    ("life-drain", "life-payment"): 0.5,

    # Trigger doubling wants triggered abilities
    ("trigger-doubling", "triggered-abilities"): 0.7,

    # Counter mover → counter placement events (moving = placing on new target)
    ("counter-mover", "counter-placement-events"): 0.7,

    # Graveyard recursion benefits from creature death
    ("graveyard-recursion", "creature-death"): 0.6,

    # Counter amplification provides counter-placement-events (amplified placement = placement)
    ("counter-amplification", "counter-placement-events"): 0.8,

    # Token generation → sacrifice events (tokens are fodder for sac outlets)
    ("token-generation", "sacrifice-events"): 0.8,
    ("token-generation", "creature-death"): 0.6,

    # Sacrifice payoff wants creature death / sacrifice events
    ("sacrifice-payoff", "creature-death"): 0.8,
    ("sacrifice-payoff", "sacrifice-events"): 0.9,
    ("sacrifice-payoff", "creature-etb"): 0.4,

    # Untap provides what tap-ability commanders need
    ("untap", "triggered-abilities"): 0.7,

    # Haste enables tap abilities
    ("haste-grant", "triggered-abilities"): 0.5,

    # Goblin tribal connections
    ("goblin-tribal", "goblin-tribal"): 1.0,
    ("token-generation", "goblin-tribal"): 0.5,
    ("cost-reduction", "goblin-tribal"): 0.5,
    ("tutor", "goblin-tribal"): 0.6,

    # ETB payoff wants creature ETB
    ("etb-payoff", "creature-board"): 0.5,
    ("etb-payoff", "token-events"): 0.6,

    # Damage dealing provides what creature board/tokens want
    ("damage-dealing", "creature-etb"): 0.4,

    # Sacrifice payoff → mana needs (sac outlets produce mana)
    ("sacrifice-payoff", "mana-needs"): 0.5,

    # Creature pump → attack events (pumped creatures want to attack)
    ("creature-pump", "attack-events"): 0.5,

    # Counter-related provides → counter-placement-events
    # (evasion/pump payoffs that care about counters)
    ("evasion-grant", "counter-placement-events"): 0.5,

    # Artifact/enchantment removal benefits from creature ETB (Aura Shards pattern)
    ("artifact-enchantment-removal", "creature-etb"): 0.4,

    # Counter placement → creature-board (placing counters means having creatures)
    ("counter-placement", "creature-board"): 0.4,
    ("board-wide-counter-placement", "creature-board"): 0.5,
}


def _compute_idf(cards: list[dict]) -> dict[str, float]:
    """Compute IDF multipliers for provides and wants tags.

    Rare tags get higher weight (up to 2.0x), common tags get lower (down to 0.5x).
    This prevents ubiquitous tags like trigger-doubling (on 27% of cards) from
    dominating edge scores while boosting rare, specific matches.
    """
    from collections import Counter
    import math

    n = len(cards)
    if n == 0:
        return {}

    freq = Counter()
    for card in cards:
        for t in card.get("provides", []):
            freq[t] += 1
        for t in card.get("wants", []):
            freq[t] += 1

    idf = {}
    # Raw IDF range is ~1.3 (27% freq) to ~6.8 (1 card).
    # Normalize to 0.5-2.0 multiplier range.
    max_idf = math.log(n)  # theoretical max (tag on 1 card)
    min_idf = math.log(2)  # theoretical min (tag on n/2 cards)
    span = max_idf - min_idf if max_idf > min_idf else 1.0

    for tag, count in freq.items():
        raw = math.log(n / count)
        # Linear map: min_idf → 0.5, max_idf → 2.0
        normalized = 0.5 + 1.5 * (raw - min_idf) / span
        idf[tag] = round(max(0.5, min(2.0, normalized)), 3)

    return idf


def build_provides_wants_edges(cards: list[dict]) -> list[dict]:
    """Build directed edges: card A provides X, card B wants X.

    Uses exact matching on normalized vocabulary plus semantic bridges
    for cross-concept connections. Weights are scaled by IDF — rare tag
    matches score higher than common ones.
    """
    # Compute IDF multipliers
    idf = _compute_idf(cards)

    # Index: what each card provides and wants
    provides_index = defaultdict(list)  # tag -> [(card_name, oracle_id)]
    wants_index = defaultdict(list)

    for card in cards:
        name = card["name"]
        oid = card["oracle_id"]
        for p in card.get("provides", []):
            provides_index[p].append((name, oid))
        for w in card.get("wants", []):
            wants_index[w].append((name, oid))

    # Accumulate matches per card pair
    pair_matches = defaultdict(list)  # (provider_oid, wanter_oid) -> [(provide_tag, want_tag, weight)]

    for want_tag, wanters in wants_index.items():
        for provide_tag, providers in provides_index.items():
            base_weight = _provides_satisfies_want(provide_tag, want_tag)
            if base_weight <= 0:
                continue
            # IDF: geometric mean of provider and wanter tag rarity
            p_idf = idf.get(provide_tag, 1.0)
            w_idf = idf.get(want_tag, 1.0)
            weight = base_weight * (p_idf * w_idf) ** 0.5
            for provider_name, provider_oid in providers:
                for wanter_name, wanter_oid in wanters:
                    if provider_oid == wanter_oid:
                        continue
                    pair_matches[(provider_oid, wanter_oid)].append(
                        (provide_tag, want_tag, weight)
                    )

    edges = []
    card_names = {c["oracle_id"]: c["name"] for c in cards}

    for (prov_oid, want_oid), matches in pair_matches.items():
        # Pick the best match for the reason
        best = max(matches, key=lambda m: m[2])
        # Weight = sum of match weights (preserves fractional weights from bridges)
        total_weight = min(sum(m[2] for m in matches), 5.0)

        edges.append({
            "source": card_names.get(prov_oid, prov_oid),
            "source_id": prov_oid,
            "target": card_names.get(want_oid, want_oid),
            "target_id": want_oid,
            "type": "provides-wants",
            "reason": f"provides '{best[0]}' → wants '{best[1]}' ({len(matches)} matches, best={best[2]:.0%})",
            "weight": round(total_weight, 2),
        })

    return edges


def build_peer_edges(cards: list[dict], min_shared: int = 2) -> list[dict]:
    """Build undirected edges between cards that both provide AND want the same things.

    Peer enablers are cards in the same ecosystem — both benefit from the same
    conditions and both contribute to them. E.g., two counter-amplifiers both
    provide counter-amplification and want counter-placement-events.
    """
    # For each card, compute (provides ∩ wants_of_other) bidirectionally
    # But simpler: cards sharing ≥2 provides tags are peers
    provides_index = defaultdict(list)  # tag -> [(name, oid)]
    for card in cards:
        name = card["name"]
        oid = card["oracle_id"]
        for p in card.get("provides", []):
            provides_index[p].append((name, oid))

    # Find pairs sharing provides tags
    pair_shared = defaultdict(list)  # (oid_a, oid_b) -> [shared_provides]
    for tag, members in provides_index.items():
        if len(members) > 30:
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
    E.g., Aura Shards and Kyler both want creature-etb events.

    Uses IDF-like weighting: common wants (shared by many cards) contribute
    less than rare wants. Only creates edges when total weight >= min_shared.
    """
    # Skip wants that are always inferred (not meaningful for peer detection)
    SKIP_WANTS = {"creature-board", "mana-needs"}

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
    pair_data = defaultdict(lambda: {"tags": [], "weight": 0.0})
    for tag, members in wants_index.items():
        w = tag_weight[tag]
        if w < 0.15:  # skip extremely common wants (>85% of cards)
            continue
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


def build_shared_tag_edges(cards: list[dict], min_weight: int = 2) -> list[dict]:
    """Build undirected edges between cards sharing Scryfall function tags.

    Only considers functional tags (not meta tags like 'activated ability').
    Weight = number of shared tags.
    """
    # Tags that are too generic to be useful for synergy
    GENERIC_TAGS = {
        "activated ability", "triggered ability", "alliteration",
        "single english word name", "single target instant/sorcery",
        "intervening if clause", "virtual french vanilla", "noncreature typal",
        "multiple targets", "self-replacement effect",
    }

    # Build tag -> cards index
    tag_cards = defaultdict(list)
    for card in cards:
        name = card["name"]
        oid = card["oracle_id"]
        for tag in card.get("synergy_tags", []):
            if tag not in GENERIC_TAGS:
                tag_cards[tag].append((name, oid))

    # Build edges from shared tags
    pair_tags = defaultdict(list)  # (oid1, oid2) -> [shared_tags]

    for tag, members in tag_cards.items():
        if len(members) > 50:
            continue  # skip overly common tags
        for i, (name_a, oid_a) in enumerate(members):
            for name_b, oid_b in members[i+1:]:
                key = tuple(sorted([oid_a, oid_b]))
                pair_tags[key].append(tag)

    edges = []
    card_names = {c["oracle_id"]: c["name"] for c in cards}

    for (oid_a, oid_b), shared in pair_tags.items():
        if len(shared) < min_weight:
            continue
        edges.append({
            "source": card_names[oid_a],
            "source_id": oid_a,
            "target": card_names[oid_b],
            "target_id": oid_b,
            "type": "shared-tag",
            "reason": f"shared tags: {', '.join(shared[:5])}{'...' if len(shared) > 5 else ''}",
            "weight": len(shared),
        })

    return edges


def build_graph(cards: list[dict], min_score: float = 0.5) -> dict:
    """Build the complete synergy graph with composite scoring.

    Merges all edge types per card pair into a single composite score.
    All edges above min_score are kept — use adjacency + sorting for
    per-card views.
    """
    pw_edges = build_provides_wants_edges(cards)
    st_edges = build_shared_tag_edges(cards)
    pe_edges = build_peer_edges(cards)
    sw_edges = build_shared_wants_edges(cards)

    raw_edges = pw_edges + st_edges + pe_edges + sw_edges

    # Merge all signals per card pair into composite scores
    # Key by sorted (name_a, name_b) to deduplicate direction
    pair_signals = defaultdict(lambda: {
        "provides_wants": [],
        "shared_tag": [],
        "peer_enabler": [],
        "shared_wants": [],
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
            "shared_tag_edges": len(st_edges),
            "peer_enabler_edges": len(pe_edges),
            "shared_wants_edges": len(sw_edges),
            "cards_with_edges": len(adjacency),
            "cards_total": len(cards),
        },
    }


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


def show_deck_synergies(graph: dict, deck_cards: set[str], commander: str, top_n: int = 30):
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
            print(f"\nWeakly connected cards (potential cut candidates):")
            for card, total in weak:
                print(f"  {card:<35} {total:7.1f} ({card_partners[card]} partners)")


def recommend_cards(graph: dict, deck_cards: set[str], top_n: int = 20):
    """Rank non-deck cards by total synergy with the current decklist."""
    candidate_scores = _candidate_scores(graph, deck_cards)

    # Sort by total synergy
    ranked = sorted(candidate_scores.items(), key=lambda x: x[1]["total"], reverse=True)

    print(f"\n{'═' * 70}")
    print(f"TOP {top_n} RECOMMENDED CARDS (not in deck)")
    print(f"{'═' * 70}")
    for card, info in ranked[:top_n]:
        partners = sorted(info["partners"], key=lambda x: x[1], reverse=True)
        multi = f" ({info['multi_sig']} multi-signal)" if info["multi_sig"] else ""
        print(f"\n  {card}  — total synergy: {info['total']:.1f}, "
              f"{len(partners)} deck partners{multi}")
        for partner, score, sigs in partners[:5]:
            sig = f"{sigs}sig" if sigs > 1 else "1sig"
            print(f"    ↔ {partner:<30} (score: {score}, {sig})")
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


def _candidate_scores(graph: dict, deck_cards: set[str]) -> dict:
    """Compute synergy totals for non-deck cards against the decklist."""
    adj = graph["adjacency"]
    scores = defaultdict(lambda: {"total": 0.0, "partners": [], "multi_sig": 0})

    for card in deck_cards:
        for edge in adj.get(card, []):
            target = edge["target"]
            if target not in deck_cards:
                info = scores[target]
                info["total"] += edge["score"]
                info["partners"].append((card, edge["score"], edge["signals"]))
                if edge["signals"] >= 2:
                    info["multi_sig"] += 1

    return dict(scores)


def _classify_card_slot(name: str, cards: list[dict]) -> str:
    """Classify a card as 'land', 'staple', or 'spell' for swap bucketing.

    Lands swap with lands, staples are protected, spells swap with spells.
    Uses merged tag data + Scryfall type_line fallback.
    """
    from card_db import CARD_DB, NAME_INDEX

    # Infrastructure roles serve structural purposes (interaction, mana, card flow)
    # and shouldn't be swapped for synergy pieces
    INFRASTRUCTURE_ROLES = {"removal", "ramp", "protection", "draw", "tutor"}

    # Check merged card data
    for card in cards:
        if card["name"] != name:
            continue
        role = card.get("role", "")
        categories = set(card.get("categories", []))
        if role == "land" or "staple-land" in categories:
            return "land"
        if role in INFRASTRUCTURE_ROLES:
            return "staple"
        break

    # Fallback: check Scryfall type_line for land detection
    oid = NAME_INDEX.get(name.lower())
    if oid and oid in CARD_DB:
        type_line = CARD_DB[oid].get("type_line", "")
        if "Land" in type_line:
            return "land"

    return "spell"


def suggest_swaps(graph: dict, deck_cards: set[str], commander: str,
                  cards: list[dict], top_n: int = 15) -> list[dict]:
    """Suggest swaps: pair weak deck cards with strong non-deck candidates.

    Lands swap with lands, spells swap with spells. Commander and staple
    infrastructure (mana rocks, removal, protection) are never cut.
    """
    deck_scores = _deck_card_scores(graph, deck_cards)
    cand_scores = _candidate_scores(graph, deck_cards)

    # Classify every deck card and candidate by slot type
    deck_slots = {name: _classify_card_slot(name, cards) for name in deck_cards}
    cand_slots = {name: _classify_card_slot(name, cards) for name in cand_scores}

    # Split into buckets: lands vs spells (staples excluded from cuts)
    cuttable = {"land": [], "spell": []}
    for card, info in deck_scores.items():
        slot = deck_slots.get(card, "spell")
        if card == commander or slot == "staple":
            continue
        cuttable[slot].append((card, info))

    for bucket in cuttable.values():
        bucket.sort(key=lambda x: x[1]["total"])

    candidate_lists = {"land": [], "spell": []}
    for card, info in sorted(cand_scores.items(), key=lambda x: x[1]["total"], reverse=True):
        slot = cand_slots.get(card, "spell")
        if slot == "staple":
            slot = "spell"  # staple candidates are fine to recommend adding
        candidate_lists[slot].append((card, info))

    # Pair within each bucket
    used_candidates = set()
    swaps = []

    for bucket in ("spell", "land"):
        for cut_card, cut_info in cuttable[bucket]:
            if len(swaps) >= top_n * 2:  # collect extras, sort later
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
                    "slot": bucket,
                    "add": add_card,
                    "add_score": round(add_info["total"], 1),
                    "add_partners": len(add_info["partners"]),
                    "add_multi_sig": add_info["multi_sig"],
                    "add_top_partners": top_partners[:5],
                    "net_delta": round(net, 1),
                })
                break

    swaps.sort(key=lambda s: s["net_delta"], reverse=True)
    return swaps[:top_n]


def show_swaps(swaps: list[dict], top_n: int = 15):
    """Display suggested swaps."""
    print(f"\n{'═' * 70}")
    print(f"SUGGESTED SWAPS — {len(swaps)} upgrades found")
    print(f"{'═' * 70}")

    for i, swap in enumerate(swaps[:top_n], 1):
        multi = f" ({swap['add_multi_sig']} multi-signal)" if swap["add_multi_sig"] else ""
        slot_label = f" [{swap['slot']}]" if swap.get("slot") == "land" else ""
        print(f"\n  #{i}  Net: +{swap['net_delta']}{slot_label}")
        print(f"    Cut: {swap['cut']:<35} (synergy: {swap['cut_score']:>6.1f}, "
              f"{swap['cut_partners']} partners)")
        print(f"    Add: {swap['add']:<35} (synergy: {swap['add_score']:>6.1f}, "
              f"{swap['add_partners']} partners{multi})")
        if swap["add_top_partners"]:
            top = swap["add_top_partners"][:3]
            partners_str = ", ".join(
                f"{p[0]} ({p[1]:.1f})" for p in top
            )
            print(f"         top synergies: {partners_str}")


def find_combos(graph: dict, deck_cards: set[str], commander: str,
                top_n: int = 15, min_triangle_score: float = 5.0) -> list[dict]:
    """Find 3-card combos (triangles) and 4-card combos in the deck's synergy subgraph.

    Triangle detection via adjacency intersection. Scores weighted by bottleneck
    (weakest edge matters most). Extends to 4-card combos by merging triangles
    that share an edge.
    """
    adj = graph["adjacency"]

    # Build in-deck adjacency: card -> {neighbor: edge}
    deck_adj = defaultdict(dict)
    for card in deck_cards:
        for edge in adj.get(card, []):
            if edge["target"] in deck_cards:
                deck_adj[card][edge["target"]] = edge

    # Find triangles via adjacency intersection
    deck_sorted = sorted(deck_cards)
    triangles = []

    for i, a in enumerate(deck_sorted):
        neighbors_a = set(deck_adj[a].keys())
        for b in sorted(neighbors_a):
            if b <= a:
                continue
            neighbors_b = set(deck_adj[b].keys())
            for c in sorted(neighbors_a & neighbors_b):
                if c <= b:
                    continue
                # Triangle: (a, b, c)
                ab = deck_adj[a][b]["score"]
                ac = deck_adj[a][c]["score"]
                bc = deck_adj[b][c]["score"]

                scores = sorted([ab, ac, bc])
                triangle_score = scores[0] * 1.5 + scores[1] * 1.0 + scores[2] * 0.5

                # Commander bonus
                has_commander = commander in (a, b, c)
                if has_commander:
                    triangle_score *= 1.2

                if triangle_score < min_triangle_score:
                    continue

                # Collect all reasons for classification
                all_reasons = []
                for edge in [deck_adj[a][b], deck_adj[a][c], deck_adj[b][c]]:
                    all_reasons.extend(edge.get("reasons", []))
                reason_text = " ".join(all_reasons).lower()

                combo_type = _classify_combo(reason_text)

                triangles.append({
                    "cards": (a, b, c),
                    "score": round(triangle_score, 1),
                    "min_edge": round(scores[0], 1),
                    "edge_scores": {"ab": ab, "ac": ac, "bc": bc},
                    "type": combo_type,
                    "commander": has_commander,
                    "reasons": all_reasons,
                })

    triangles.sort(key=lambda t: t["score"], reverse=True)

    # Extend to 4-card combos: pairs of triangles sharing an edge
    quads = _find_quad_combos(triangles, deck_adj, commander)

    return {
        "triangles": triangles[:top_n],
        "quads": quads[:top_n],
        "total_triangles": len(triangles),
        "total_quads": len(quads),
    }


def _classify_combo(reason_text: str) -> str:
    """Classify a combo type based on concatenated edge reasons."""
    checks = [
        ("infinite-combo", ["untap", "sac", "death"]),
        ("sac-combo", ["sacrifice", "death"]),
        ("counter-combo", ["counter", "placement", "amplif"]),
        ("token-combo", ["token", "generat"]),
        ("etb-combo", ["enters", "etb", "trigger"]),
        ("damage-combo", ["damage", "burn"]),
        ("tribal-combo", ["goblin", "human", "tribal"]),
    ]
    for combo_type, keywords in checks:
        if sum(1 for kw in keywords if kw in reason_text) >= 2:
            return combo_type
    return "synergy-triangle"


def _find_quad_combos(triangles: list[dict], deck_adj: dict,
                      commander: str) -> list[dict]:
    """Find 4-card combos by merging triangle pairs that share an edge."""
    if not triangles:
        return []

    # Index triangles by their edges
    edge_to_triangles = defaultdict(list)
    for i, tri in enumerate(triangles):
        a, b, c = tri["cards"]
        for edge_pair in [(a, b), (a, c), (b, c)]:
            edge_to_triangles[edge_pair].append(i)

    seen_quads = set()
    quads = []

    for edge_pair, tri_indices in edge_to_triangles.items():
        if len(tri_indices) < 2:
            continue
        for i in range(len(tri_indices)):
            for j in range(i + 1, len(tri_indices)):
                tri_a = triangles[tri_indices[i]]
                tri_b = triangles[tri_indices[j]]
                all_cards = tuple(sorted(set(tri_a["cards"] + tri_b["cards"])))
                if len(all_cards) != 4:
                    continue
                if all_cards in seen_quads:
                    continue
                seen_quads.add(all_cards)

                # Score: sum all pairwise edges with completeness bonus
                total_score = 0.0
                edge_count = 0
                min_edge = float("inf")
                all_reasons = []
                cards_list = list(all_cards)
                for ci in range(4):
                    for cj in range(ci + 1, 4):
                        ca, cb = cards_list[ci], cards_list[cj]
                        edge = deck_adj.get(ca, {}).get(cb)
                        if edge:
                            total_score += edge["score"]
                            edge_count += 1
                            min_edge = min(min_edge, edge["score"])
                            all_reasons.extend(edge.get("reasons", []))

                # Completeness bonus (6 possible edges for 4 cards)
                completeness = edge_count / 6.0
                total_score *= (1.0 + completeness * 0.3)

                has_commander = commander in all_cards
                if has_commander:
                    total_score *= 1.2

                reason_text = " ".join(all_reasons).lower()
                combo_type = _classify_combo(reason_text)

                quads.append({
                    "cards": all_cards,
                    "score": round(total_score, 1),
                    "min_edge": round(min_edge, 1) if min_edge != float("inf") else 0.0,
                    "edges": edge_count,
                    "type": combo_type,
                    "commander": has_commander,
                })

    quads.sort(key=lambda q: q["score"], reverse=True)
    return quads


def show_combos(combos: dict, commander: str, top_n: int = 15):
    """Display detected combos."""
    triangles = combos["triangles"]
    quads = combos["quads"]

    print(f"\n{'═' * 70}")
    print(f"COMBO DETECTION — {combos['total_triangles']} triangles, "
          f"{combos['total_quads']} four-card combos found")
    print(f"{'═' * 70}")

    if triangles:
        print(f"\nTop 3-card combos:")
        print(f"{'─' * 70}")
        for i, tri in enumerate(triangles[:top_n], 1):
            star = " ★ commander" if tri["commander"] else ""
            print(f"\n  #{i} [{tri['type']}] score: {tri['score']}{star}")
            print(f"     {' + '.join(tri['cards'])}")
            print(f"     min edge: {tri['min_edge']}")

    if quads:
        print(f"\nTop 4-card combos:")
        print(f"{'─' * 70}")
        for i, quad in enumerate(quads[:top_n], 1):
            star = " ★ commander" if quad["commander"] else ""
            print(f"\n  #{i} [{quad['type']}] score: {quad['score']} "
                  f"({quad['edges']}/6 edges){star}")
            print(f"     {' + '.join(quad['cards'])}")
            print(f"     min edge: {quad['min_edge']}")


def validate_against_curated(graph: dict, synergy_pairs: list[tuple] = None):
    """Compare graph edges against hand-curated synergy pairs.

    If synergy_pairs is not provided, falls back to scorer.SYNERGY_PAIRS (Kyler).
    """
    if synergy_pairs is None:
        from scorer import SYNERGY_PAIRS
        synergy_pairs = SYNERGY_PAIRS

    adj = graph["adjacency"]

    print(f"\nValidation: graph edges vs {len(synergy_pairs)} hand-curated synergy pairs")
    print(f"{'═' * 70}")

    found = 0
    missed = 0

    for card_a, card_b, reason in synergy_pairs:
        edges_a = adj.get(card_a, [])
        edges_b = adj.get(card_b, [])

        edge = next(
            (e for e in edges_a if e["target"] == card_b),
            next((e for e in edges_b if e["target"] == card_a), None)
        )

        if edge:
            found += 1
            sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
            print(f"  ✓  {card_a} ↔ {card_b}  (score={edge['score']}, {sig})")
            print(f"       curated: {reason}")
            for r in edge["reasons"][:2]:
                print(f"       {r}")
        else:
            missed += 1
            print(f"  ✗  {card_a} ↔ {card_b}")
            print(f"       curated: {reason}")
            a_exists = card_a in adj
            b_exists = card_b in adj
            if not a_exists:
                print(f"       ('{card_a}' not in graph)")
            if not b_exists:
                print(f"       ('{card_b}' not in graph)")

    print(f"\n{'═' * 70}")
    print(f"Found: {found}/{len(synergy_pairs)} ({100*found/len(synergy_pairs):.0f}%)")
    print(f"Missed: {missed}/{len(synergy_pairs)}")


def generate_visualization(graph: dict, cards: list[dict], deck_set: set,
                           commander: str, deck_name: str, combos: list = None,
                           output_path: str = None, min_edge_score: float = 0.8):
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
            "synergy_tags": card.get("synergy_tags", [])[:10],
            "notes": card.get("notes", ""),
            "is_commander": name == commander,
            "edge_count": len(edges_for_card),
            "total_synergy": round(total_syn, 1),
        })

    # Build edges (deck-internal only, above threshold)
    edges = []
    seen = set()
    for edge in graph["edges"]:
        if edge["source"] in deck_set and edge["target"] in deck_set:
            if edge["score"] >= min_edge_score:
                key = tuple(sorted([edge["source"], edge["target"]]))
                if key not in seen:
                    seen.add(key)
                    edges.append({
                        "source": edge["source"],
                        "target": edge["target"],
                        "score": edge["score"],
                        "signals": edge["signals"],
                        "reasons": edge["reasons"],
                    })

    # Combos
    combo_data = []
    if combos:
        triangles = combos.get("triangles", []) if isinstance(combos, dict) else combos
        for combo in triangles[:20]:
            combo_data.append({
                "cards": list(combo["cards"]),
                "score": combo["score"],
                "type": combo.get("type", "synergy-triangle"),
            })

    viz_data = json.dumps({
        "nodes": nodes,
        "edges": edges,
        "combos": combo_data,
        "meta": {
            "deck": deck_name,
            "commander": commander,
            "total_cards": len(nodes),
            "total_edges": len(edges),
        },
    })

    html = _VIZ_HTML_TEMPLATE.replace("__GRAPH_DATA__", viz_data)

    if not output_path:
        output_path = os.path.join(DATA_DIR, f"{deck_name}_synergy_viz.html")
    with open(output_path, "w") as f:
        f.write(html)
    print(f"\nVisualization written to {output_path}")
    print(f"  {len(nodes)} nodes, {len(edges)} edges, {len(combo_data)} combos")


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

// Stats
d3.select("#stats-bar").html(
  `${DATA.meta.deck} | ${DATA.meta.total_cards} cards | ${DATA.meta.total_edges} edges | Commander: ${DATA.meta.commander}`
);

// Force simulation
const simulation = d3.forceSimulation(DATA.nodes)
  .force("link", d3.forceLink(visibleEdges).id(d => d.name).distance(d => Math.max(60, 180 - d.score * 8)).strength(d => Math.min(0.5, d.score / 20)))
  .force("charge", d3.forceManyBody().strength(-180))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collision", d3.forceCollide().radius(d => nodeRadius(d) + 3))
  .alphaDecay(0.02);

// Render edges
const edgeGroup = g.append("g").attr("class", "edges");
let edgeElements = edgeGroup.selectAll("line").data(visibleEdges).join("line")
  .attr("stroke", "#555")
  .attr("stroke-width", d => Math.max(0.5, Math.min(4, d.score / 3)))
  .attr("stroke-opacity", d => Math.max(0.08, Math.min(0.6, d.score / 15)));

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
    return (src === d.name || tgt === d.name) ? 0.8 : 0.02;
  }).attr("stroke", e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    return (src === d.name || tgt === d.name) ? "#e94560" : "#555";
  });

  // Side panel
  connEdges.sort((a, b) => b.score - a.score);
  d3.select("#panel-card-name").text(d.name);
  d3.select("#panel-role").text(`Role: ${d.role} | Edges: ${d.edge_count} | Total synergy: ${d.total_synergy}`);
  d3.select("#panel-provides").html(d.provides.map(t => `<span class="tag provides">${t}</span>`).join(""));
  d3.select("#panel-wants").html(d.wants.map(t => `<span class="tag wants">${t}</span>`).join(""));
  d3.select("#panel-synergy").html(d.synergy_tags.map(t => `<span class="tag synergy">${t}</span>`).join(""));
  d3.select("#panel-notes").text(d.notes);
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
    .attr("stroke-opacity", d => Math.max(0.08, Math.min(0.6, d.score / 15)))
    .attr("stroke", "#555");
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
    .attr("stroke", "#555")
    .attr("stroke-width", d => Math.max(0.5, Math.min(4, d.score / 3)))
    .attr("stroke-opacity", d => Math.max(0.08, Math.min(0.6, d.score / 15)))
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
  if (!DATA.combos.length) return;
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

  // Update combo polygons
  if (showCombos) {
    comboGroup.selectAll("polygon").attr("points", d =>
      d.map(n => `${n.x},${n.y}`).join(" ")
    );
  }
});
</script>
</body>
</html>"""


def run():
    from decks import list_decks

    parser = argparse.ArgumentParser(description="Build MTG synergy graph")
    parser.add_argument("--deck", required=True, choices=list_decks(), help="Deck config to use")
    parser.add_argument("--input", type=str, help="Merged tags JSON (default: data/<deck>_merged.json)")
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
    args = parser.parse_args()

    input_path = args.input or os.path.join(DATA_DIR, f"{args.deck}_merged.json")

    cards = load_merged(input_path)
    print(f"Loaded {len(cards)} cards from {input_path}")

    graph = build_graph(cards)
    stats = graph["stats"]
    print(f"\nGraph stats:")
    print(f"  raw signal edges:      {stats['total_raw_edges']}")
    print(f"    provides→wants:      {stats['provides_wants_edges']}")
    print(f"    shared-tag:          {stats['shared_tag_edges']}")
    print(f"    peer-enabler:        {stats['peer_enabler_edges']}")
    print(f"    shared-wants:        {stats['shared_wants_edges']}")
    print(f"  composite edges:       {stats['pruned_edges']} (unique card pairs)")
    print(f"  cards with edges:      {stats['cards_with_edges']}/{stats['cards_total']}")

    if args.card:
        show_card_synergies(graph, args.card)
    elif args.visualize:
        from decks import load_deck
        deck = load_deck(args.deck)
        deck_set = set(deck.DECKLIST) | {deck.COMMANDER}
        combos = find_combos(graph, deck_set, deck.COMMANDER, top_n=20)
        generate_visualization(graph, cards, deck_set, deck.COMMANDER, args.deck, combos)
    elif args.deck_view or args.recommend or args.combos or args.swaps:
        from decks import load_deck
        deck = load_deck(args.deck)
        deck_set = set(deck.DECKLIST) | {deck.COMMANDER}
        if args.deck_view:
            show_deck_synergies(graph, deck_set, deck.COMMANDER, args.top)
        if args.combos:
            combos = find_combos(graph, deck_set, deck.COMMANDER, args.top)
            show_combos(combos, deck.COMMANDER, args.top)
        if args.swaps:
            swaps = suggest_swaps(graph, deck_set, deck.COMMANDER, cards, args.top)
            show_swaps(swaps, args.top)
        if args.recommend:
            recommend_cards(graph, deck_set, args.top)
    elif args.validate:
        from decks import load_deck
        deck = load_deck(args.deck)
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
