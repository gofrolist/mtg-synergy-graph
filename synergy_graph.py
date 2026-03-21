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

    # ── Spellslinger / Drain bridges ──

    # Life drain → life gain events (draining opponents = gaining life for payoffs)
    ("life-drain", "life-gain-events"): 0.9,
    ("life-gain", "life-gain-events"): 0.9,
    ("life-drain", "life-payment"): 0.5,

    # Card discard → discard events (Windfall → Bloodchief Ascension)
    ("card-discard", "discard-events"): 0.9,

    # Flash grant → spell casting (flash enables casting on every turn)
    ("flash-grant", "spell-casting"): 0.8,

    # Stax/tax → opponent spell casting (taxing opponents when they cast)
    ("stax-tax", "opponent-spell-casting"): 0.8,
    ("stax", "opponent-spell-casting"): 0.7,

    # Card draw payoff → card draw events
    ("card-draw-payoff", "card-draw-events"): 0.9,
    ("card-draw", "card-draw-events"): 0.8,

    # Graveyard casting → spell casting (casting from GY = casting spells)
    ("graveyard-casting", "spell-casting"): 0.7,

    # Blink → creature ETB (blinking = re-entering)
    ("blink", "creature-etb"): 0.8,

    # Board protection ↔ board threats
    ("board-protection", "board-threats"): 0.5,

    # Tutor → opponent search (Opposition Agent pattern — both care about searches)
    ("tutor", "opponent-spell-casting"): 0.3,

    # Cost reduction → spell casting (cheaper spells = more spells cast)
    ("cost-reduction", "spell-casting"): 0.6,
    ("cost-reduction", "opponent-spell-casting"): 0.3,

    # Card discard → card draw events (discard wheels trigger draw payoffs)
    ("card-discard", "card-draw-events"): 0.5,

    # Reactive protection → spell casting (protects key spells)
    ("reactive-protection", "spell-casting"): 0.3,

    # Life gain ↔ life gain events (same concept, different field)
    ("life-drain", "spell-casting"): 0.3,

    # Stax tax ↔ card draw events (Rhystic Study pattern)
    ("stax-tax", "card-draw-events"): 0.6,
}

# Maps effect tags to the trigger tags they would cause in-game.
# Used by find_combos_tiered to expand effect_tags before intersection,
# so that e.g. token-generation (effect) can match creature-etb (trigger).
TRIGGER_EFFECT_BRIDGES = {
    # Creating tokens/creatures triggers ETB
    "token-generation": {"creature-etb"},
    "graveyard-recursion": {"creature-etb"},
    "copy-effect": {"creature-etb"},

    # Removal/sacrifice causes death triggers
    "spot-removal": {"creature-death"},
    "sacrifice-outlet": {"creature-death"},
    "exile-removal": {"leaves-battlefield"},

    # Damage triggers life-loss which can trigger life-gain (Exquisite Blood pattern)
    "life-drain": {"life-gain"},   # Sanguine Bond: drain → opponent loses life → you gain
    "life-gain": {"life-drain"},   # Exquisite Blood: gain → opponent loses life

    # Direct damage can trigger damage events
    "direct-damage": {"combat-damage-events"},
    "group-damage": {"combat-damage-events"},

    # Card draw triggers draw events
    "card-draw": {"draw-events"},

    # Counter placement triggers counter events
    "counter-placement": {"counter-placement-events"},

    # Mana can enable untap loops
    "mana-acceleration": {"untap"},
    "untap": {"tap-cost"},

    # Discard triggers discard events
    "discard": {"discard-events"},

    # Mill triggers graveyard events
    "mill": {"graveyard-events"},

    # Creature pump with wide board triggers attack events
    "creature-pump": {"attack-events"},
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

    Optimized: iterates per-card wants → finds providers via inverted index.
    Fan-out capped to avoid O(n²) explosion on common tags.
    """
    # Compute IDF multipliers
    idf = _compute_idf(cards)

    # Build provides inverted index: tag -> [(card_name, oracle_id)]
    provides_index = defaultdict(list)
    for card in cards:
        name = card["name"]
        oid = card["oracle_id"]
        for p in card.get("provides", []):
            provides_index[p].append((name, oid))

    # Pre-compute reverse bridge lookup: want_tag -> [(provide_tag, base_weight)]
    want_to_provides = defaultdict(list)
    for (p_tag, w_tag), weight in SEMANTIC_BRIDGES.items():
        if p_tag in provides_index:
            want_to_provides[w_tag].append((p_tag, weight))
    # Add identity matches (exact tag matches)
    for tag in provides_index:
        want_to_provides[tag].append((tag, 1.0))

    # Fan-out cap: skip provides tags with too many cards.
    # Common tags have low IDF anyway, so capping preserves quality.
    # Scale: <=2k cards → 500, 10k cards → 100
    n = len(cards)
    MAX_PROVIDERS = max(50, min(500, 2000 * 500 // max(n, 1)))

    # Pre-filter provides_index to stay within memory budget
    for tag in list(provides_index):
        if len(provides_index[tag]) > MAX_PROVIDERS:
            del provides_index[tag]

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
        for want_tag in card.get("wants", []):
            if want_tag in large_want_tags:
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
            "reason": f"provides '{best_ptag}' → wants '{best_wtag}' ({count} matches, best={best_weight:.0%})",
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

    import os
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


def build_shared_tag_edges(cards: list[dict], min_weight: int = 2) -> list[dict]:
    """Build undirected edges between cards sharing synergy_tags.

    Uses LLM-generated synergy_tags only (not Scryfall community tags,
    which are kept separate as a validation signal). Currently dormant
    until the synergy_tags table is populated with our own tag vocabulary.

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

    max_members = max(50, len(cards) // 100)
    for tag, members in tag_cards.items():
        if len(members) > max_members:
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
                    active_strategies: set = None, db_path: str = None):
    """Rank non-deck cards by total synergy with the current decklist.

    If deck_types is provided (e.g. {'Human'}), cards matching those types
    get a synergy boost. If active_strategies is provided, cards matching
    strategies get a relevance multiplier. Combo completions get x2.0.
    """
    candidate_scores = _candidate_scores(graph, deck_cards)

    # Build card metadata lookup from cards list
    card_meta = {}
    card_oid_lookup = {}
    for c in cards:
        card_meta[c["name"]] = {
            "type_line": c.get("type_line", ""),
            "cmc": c.get("cmc", 0),
            "mana_cost": c.get("mana_cost", ""),
            "oracle_id": c.get("oracle_id", ""),
        }
        card_oid_lookup[c["name"]] = c.get("oracle_id", "")

    # Calculate deck average CMC (excluding lands)
    deck_cmc_values = [card_meta[n]["cmc"] for n in deck_cards
                       if n in card_meta and "Land" not in card_meta[n].get("type_line", "")]
    deck_avg_cmc = sum(deck_cmc_values) / max(len(deck_cmc_values), 1)

    # Find partial Spellbook combos for combo completion bonus
    partial_missing_oids = set()
    partial_combos = []
    if db_path:
        deck_oids = {card_oid_lookup[n] for n in deck_cards if n in card_oid_lookup}
        partial_combos = find_partial_combos(deck_oids, db_path)
        for pc in partial_combos:
            for oid in pc.get("missing_oids", []):
                partial_missing_oids.add(oid)

    # Apply tribal boost if deck has dominant creature types
    if deck_types:
        for card_name, info in candidate_scores.items():
            meta = card_meta.get(card_name, {})
            type_line = meta.get("type_line", "")
            if any(t.lower() in type_line.lower() for t in deck_types):
                info["tribal_match"] = True
                info["total"] *= 1.3  # 30% boost for tribal match
            else:
                info["tribal_match"] = False

    # Apply strategy relevance multiplier
    if active_strategies and db_path:
        for card_name, info in candidate_scores.items():
            oid = card_oid_lookup.get(card_name, "")
            if oid:
                rel = compute_strategy_relevance(oid, active_strategies, db_path)
                info["total"] *= rel
                info["strategy_rel"] = rel

    # Apply combo completion multiplier
    for card_name, info in candidate_scores.items():
        oid = card_oid_lookup.get(card_name, "")
        if oid in partial_missing_oids:
            info["total"] *= 2.0
            info["combo_completion"] = True
        else:
            info["combo_completion"] = False

    # Apply mana cost penalty for high-CMC cards
    for card_name, info in candidate_scores.items():
        meta = card_meta.get(card_name, {})
        cmc = meta.get("cmc", 0) or 0
        if cmc > deck_avg_cmc + 3:
            penalty = max(0.3, 1.0 - 0.15 * (cmc - deck_avg_cmc - 3))
            info["total"] *= penalty
            info["high_cmc"] = True
        else:
            info["high_cmc"] = False

    # Sort by total synergy
    ranked = sorted(candidate_scores.items(), key=lambda x: x[1]["total"], reverse=True)

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
        print(f"\n  {card}{tribal}{combo}{strat_str}{high_cmc}  — synergy: {info['total']:.1f}, "
              f"{len(partners)} partners{multi}")
        print(f"    {type_line} | CMC {cmc}")
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

    # Provides tags that indicate synergy value beyond infrastructure role.
    # Cards with these are synergy pieces even if their role is "protection" etc.
    SYNERGY_PROVIDES = {
        "token-generation", "counter-placement", "board-wide-counter-placement",
        "counter-amplification", "trigger-doubling", "creature-pump",
        "card-draw-payoff", "etb-payoff", "sacrifice-payoff", "goblin-tribal",
        "life-gain", "life-drain", "combat-trigger",
    }

    # Check merged card data
    for card in cards:
        if card["name"] != name:
            continue
        role = card.get("role", "")
        categories = set(card.get("categories", []))
        if role == "land" or "staple-land" in categories:
            return "land"
        if role in INFRASTRUCTURE_ROLES:
            # Check if the card also has synergy-relevant provides tags —
            # if so, it's a synergy piece that happens to have an infra role
            provides = set(card.get("provides", []))
            if provides & SYNERGY_PROVIDES:
                break  # fall through to "spell"
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


def find_combos(graph: dict, cards: list[dict], deck_cards: set[str], commander: str,
                top_n: int = 15, min_triangle_score: float = 5.0) -> list[dict]:
    """Find 2/3/4-card combos in the deck's synergy subgraph.

    - 2-card: provides→wants cycles (potential infinite combos)
    - 3-card: triangles via adjacency intersection
    - 4-card: merged triangle pairs sharing an edge
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

    # Find 2-card infinite combos (provides→wants cycles)
    pairs = _find_two_card_combos(cards, deck_cards, commander)

    return {
        "pairs": pairs,
        "triangles": triangles[:top_n],
        "quads": quads[:top_n],
        "total_pairs": len(pairs),
        "total_triangles": len(triangles),
        "total_quads": len(quads),
    }


def _expand_through_bridges(tags: set[str]) -> set[str]:
    """Expand provides tags through semantic bridges to match wants tags."""
    expanded = set(tags)
    for p_tag in tags:
        for (bridge_p, bridge_w), weight in SEMANTIC_BRIDGES.items():
            if bridge_p == p_tag and weight >= 0.6:
                expanded.add(bridge_w)
    return expanded


# Oracle text patterns that indicate combo/loop potential
_COMBO_PATTERNS = [
    r"whenever .+ you gain life",
    r"whenever .+ you lose life",
    r"whenever .+ opponent loses life",
    r"untap (it|target|all|~)",
    r"return .+ from .+ graveyard to the battlefield",
    r"create a .+ copy",
    r"you may repeat this process",
    r"infinite",
    r"whenever .+ enters .+ put .+ counter",
    r"whenever .+ dies",
    r"whenever .+ is dealt damage",
    r"whenever .+ is put into .+ graveyard",
    r"sacrifice .+:",
]
import re as _re
_COMBO_RE = [_re.compile(p, _re.IGNORECASE) for p in _COMBO_PATTERNS]


def _combo_potential(oracle_text: str) -> float:
    """Score how likely a card is to be part of an infinite combo (0-1)."""
    if not oracle_text:
        return 0.0
    hits = sum(1 for pat in _COMBO_RE if pat.search(oracle_text))
    return min(hits / 3.0, 1.0)  # 3+ patterns = max combo potential


def _find_two_card_combos(cards: list[dict], deck_cards: set[str],
                          commander: str) -> list[dict]:
    """Find 2-card combo candidates via provides→wants cycles with semantic bridges.

    Uses SEMANTIC_BRIDGES to expand provides before matching (e.g. life-gain
    matches life-gain-events). Scores by tag overlap * combo potential from
    oracle text pattern matching.
    """
    # Build card lookup
    card_lookup = {}
    for c in cards:
        name = c["name"]
        if name in deck_cards:
            card_lookup[name] = {
                "provides": set(c.get("provides", [])),
                "wants": set(c.get("wants", [])),
                "oracle_text": c.get("oracle_text", ""),
            }

    pairs = []
    deck_sorted = sorted(deck_cards)

    for i, a in enumerate(deck_sorted):
        if a not in card_lookup:
            continue
        a_data = card_lookup[a]
        a_provides = a_data["provides"]
        a_wants = a_data["wants"]
        if not a_provides or not a_wants:
            continue
        # Expand provides through bridges
        a_expanded = _expand_through_bridges(a_provides)

        for b in deck_sorted[i + 1:]:
            if b not in card_lookup:
                continue
            b_data = card_lookup[b]
            b_provides = b_data["provides"]
            b_wants = b_data["wants"]
            if not b_provides or not b_wants:
                continue
            b_expanded = _expand_through_bridges(b_provides)

            # A provides (expanded) something B wants
            a_to_b = a_expanded & b_wants
            # B provides (expanded) something A wants
            b_to_a = b_expanded & a_wants

            if a_to_b and b_to_a:
                # Circular dependency found
                # Base score from tag overlap
                score = len(a_to_b) + len(b_to_a)

                # Combo potential bonus from oracle text
                cp_a = _combo_potential(a_data["oracle_text"])
                cp_b = _combo_potential(b_data["oracle_text"])
                combo_bonus = 1.0 + (cp_a + cp_b)  # 1.0 to 3.0 multiplier

                score *= combo_bonus

                has_commander = commander in (a, b)
                if has_commander:
                    score *= 1.5

                combo_label = "combo" if (cp_a + cp_b) >= 0.6 else "synergy"

                pairs.append({
                    "cards": (a, b),
                    "score": round(score, 1),
                    "a_provides_b_wants": sorted(a_to_b),
                    "b_provides_a_wants": sorted(b_to_a),
                    "commander": has_commander,
                    "combo_potential": round(cp_a + cp_b, 2),
                    "label": combo_label,
                })

    pairs.sort(key=lambda p: p["score"], reverse=True)
    return pairs


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
    pairs = combos.get("pairs", [])
    triangles = combos["triangles"]
    quads = combos["quads"]

    print(f"\n{'═' * 70}")
    print(f"COMBO DETECTION — {combos.get('total_pairs', 0)} pairs, "
          f"{combos['total_triangles']} triangles, "
          f"{combos['total_quads']} four-card combos found")
    print(f"{'═' * 70}")

    if pairs:
        print(f"\nTop 2-card pairs (provides→wants cycles):")
        print(f"{'─' * 70}")
        for i, pair in enumerate(pairs[:top_n], 1):
            star = " ★ commander" if pair["commander"] else ""
            label = pair.get("label", "synergy").upper()
            cp = pair.get("combo_potential", 0)
            a, b = pair["cards"]
            print(f"\n  #{i} [{label}] score: {pair['score']}{star}")
            print(f"     {a} + {b}")
            print(f"     {a} → {b}: {pair['a_provides_b_wants']}")
            print(f"     {b} → {a}: {pair['b_provides_a_wants']}")

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


def validate_against_curated(graph: dict, synergy_pairs: list[tuple]):
    """Compare graph edges against hand-curated synergy pairs."""

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


def find_combos_tiered(deck_oids, db_path=None):
    """Three-tier combo detection: infinite-confirmed, combo-likely, synergy.

    Args:
        deck_oids: set of oracle_ids in the deck
        db_path: optional DB path override

    Returns:
        list of combo dicts with 'tier', 'cards', 'result', 'reason' fields
    """
    import sqlite3
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    combos = []

    # --- Tier 1: Spellbook confirmed ---
    spellbook_combos = conn.execute("""
        SELECT combo_id, card_oracle_ids, card_names, result, prerequisites
        FROM spellbook_combos
    """).fetchall()

    confirmed_pairs = set()  # Track confirmed pairs to avoid duplicate synergy entries
    for row in spellbook_combos:
        combo_oids = set(json.loads(row["card_oracle_ids"]))
        if combo_oids <= deck_oids:  # All combo cards in deck
            combo_names = json.loads(row["card_names"])
            combos.append({
                "tier": "infinite-confirmed",
                "cards": combo_names,
                "card_oids": list(combo_oids),
                "result": row["result"],
                "reason": f"Spellbook #{row['combo_id']}",
            })
            # Mark all pairs as confirmed
            oid_list = list(combo_oids)
            for i in range(len(oid_list)):
                for j in range(i + 1, len(oid_list)):
                    confirmed_pairs.add(frozenset([oid_list[i], oid_list[j]]))

    # --- Load provides/wants for deck cards ---
    deck_list = list(deck_oids)
    provides_by_card = {}
    wants_by_card = {}
    for oid in deck_list:
        for tag, in conn.execute("SELECT tag FROM provides WHERE oracle_id = ?", (oid,)):
            provides_by_card.setdefault(oid, set()).add(tag)
        for tag, in conn.execute("SELECT tag FROM wants WHERE oracle_id = ?", (oid,)):
            wants_by_card.setdefault(oid, set()).add(tag)

    # --- Load abilities for deck cards ---
    abilities_by_card = {}
    for oid in deck_list:
        rows = conn.execute("""
            SELECT ability_type, trigger_tags, effect_tags
            FROM abilities WHERE oracle_id = ?
        """, (oid,)).fetchall()
        trigger_tags = set()
        effect_tags = set()
        for row in rows:
            if row["trigger_tags"]:
                trigger_tags.update(json.loads(row["trigger_tags"]))
            if row["effect_tags"]:
                effect_tags.update(json.loads(row["effect_tags"]))
        if trigger_tags or effect_tags:
            abilities_by_card[oid] = {"trigger_tags": trigger_tags, "effect_tags": effect_tags}

    conn_names = {}
    for oid in deck_list:
        row = conn.execute("SELECT name FROM cards WHERE oracle_id = ?", (oid,)).fetchone()
        if row:
            conn_names[oid] = row["name"]

    conn.close()

    # --- Find provides->wants cycles ---
    for i, oid_a in enumerate(deck_list):
        for oid_b in deck_list[i + 1:]:
            pair = frozenset([oid_a, oid_b])
            if pair in confirmed_pairs:
                continue

            prov_a = provides_by_card.get(oid_a, set())
            want_a = wants_by_card.get(oid_a, set())
            prov_b = provides_by_card.get(oid_b, set())
            want_b = wants_by_card.get(oid_b, set())

            # Check cycle: A provides what B wants AND B provides what A wants
            a_to_b = prov_a & want_b
            b_to_a = prov_b & want_a

            if not (a_to_b and b_to_a):
                continue

            name_a = conn_names.get(oid_a, oid_a)
            name_b = conn_names.get(oid_b, oid_b)

            # --- Tier 2: Check trigger chain ---
            ab_a = abilities_by_card.get(oid_a)
            ab_b = abilities_by_card.get(oid_b)

            if ab_a and ab_b:
                # Expand effect tags with bridge mappings so that e.g.
                # token-generation (effect) matches creature-etb (trigger).
                a_effects_expanded = set(ab_a["effect_tags"])
                for et in ab_a["effect_tags"]:
                    a_effects_expanded |= TRIGGER_EFFECT_BRIDGES.get(et, set())
                b_effects_expanded = set(ab_b["effect_tags"])
                for et in ab_b["effect_tags"]:
                    b_effects_expanded |= TRIGGER_EFFECT_BRIDGES.get(et, set())

                a_triggers_b = a_effects_expanded & ab_b["trigger_tags"]
                b_triggers_a = b_effects_expanded & ab_a["trigger_tags"]

                if a_triggers_b and b_triggers_a:
                    combos.append({
                        "tier": "combo-likely",
                        "cards": [name_a, name_b],
                        "card_oids": [oid_a, oid_b],
                        "result": f"Trigger chain: {name_a} -> {', '.join(a_triggers_b)} -> {name_b} -> {', '.join(b_triggers_a)}",
                        "reason": f"Circular triggers via {a_triggers_b} / {b_triggers_a}",
                    })
                    continue

            # --- Tier 3: Synergy ---
            combos.append({
                "tier": "synergy",
                "cards": [name_a, name_b],
                "card_oids": [oid_a, oid_b],
                "result": f"Provides/wants cycle: {a_to_b} / {b_to_a}",
                "reason": "Tag cycle without trigger chain",
            })

    # Sort: confirmed first, then likely, then synergy
    tier_order = {"infinite-confirmed": 0, "combo-likely": 1, "synergy": 2}
    combos.sort(key=lambda c: tier_order.get(c["tier"], 9))

    return combos


def compute_strategy_relevance(oracle_id, active_strategies, db_path=None):
    """Compute strategy relevance multiplier for a card.

    Returns: float multiplier (0.5 for no match, 1.0+ for matches)
    """
    import sqlite3
    if not active_strategies:
        return 1.0

    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)
    card_strats = {row[0] for row in conn.execute(
        "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
        (oracle_id,)
    ).fetchall()}
    conn.close()

    overlap = card_strats & active_strategies
    if not overlap:
        return 0.5
    return 1.0 + 0.2 * len(overlap)


def find_partial_combos(deck_oids, db_path=None):
    """Find Spellbook combos where deck is missing exactly 1 card.

    Returns list of dicts with: combo_id, result, present_cards, missing_cards, missing_oids.
    """
    import sqlite3
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    combos = conn.execute("SELECT * FROM spellbook_combos").fetchall()
    conn.close()

    partials = []
    for combo in combos:
        combo_oids = json.loads(combo["card_oracle_ids"])
        combo_names = json.loads(combo["card_names"])

        present = [oid for oid in combo_oids if oid in deck_oids]
        missing = [oid for oid in combo_oids if oid not in deck_oids]

        if len(missing) == 1:
            missing_idx = combo_oids.index(missing[0])
            partials.append({
                "combo_id": combo["combo_id"],
                "result": combo["result"],
                "present_cards": [combo_names[combo_oids.index(oid)] for oid in present],
                "missing_cards": [combo_names[missing_idx]],
                "missing_oids": missing,
            })

    return partials


STAPLE_ROLES = {"ramp", "draw", "removal", "protection", "land"}


def find_anti_synergy(deck_oids, active_strategies, db_path=None):
    """Find deck cards with zero strategy overlap that aren't staples.

    Returns list of dicts: {oracle_id, name, role}.
    """
    import sqlite3
    if not active_strategies:
        return []
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)

    anti = []
    for oid in deck_oids:
        row = conn.execute("SELECT name, role FROM cards WHERE oracle_id = ?", (oid,)).fetchone()
        if not row:
            continue
        name, role = row

        # Skip staples
        if role and role.lower() in STAPLE_ROLES:
            continue

        # Check strategy overlap
        card_strats = {r[0] for r in conn.execute(
            "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
            (oid,)
        ).fetchall()}

        if not (card_strats & active_strategies):
            anti.append({"oracle_id": oid, "name": name, "role": role})

    conn.close()
    return anti


def show_combos_tiered(deck_oids, commander_name=None, db_path=None):
    """Display 3-tier combo output."""
    combos = find_combos_tiered(deck_oids, db_path)
    partials = find_partial_combos(deck_oids, db_path)

    confirmed = [c for c in combos if c["tier"] == "infinite-confirmed"]
    likely = [c for c in combos if c["tier"] == "combo-likely"]
    synergy = [c for c in combos if c["tier"] == "synergy"]

    if confirmed:
        print(f"\n{'='*60}")
        print(f"CONFIRMED INFINITE COMBOS ({len(confirmed)})")
        print(f"{'='*60}")
        for c in confirmed:
            print(f"  {' + '.join(c['cards'])}")
            print(f"    Result: {c['result']}")
            print(f"    Source: {c['reason']}")

    if likely:
        print(f"\n{'='*60}")
        print(f"LIKELY COMBOS ({len(likely)})")
        print(f"{'='*60}")
        for c in likely:
            print(f"  {' + '.join(c['cards'])}")
            print(f"    Chain: {c['result']}")

    if partials:
        print(f"\n{'='*60}")
        print(f"NEAR-COMPLETE COMBOS — 1 card away ({len(partials)})")
        print(f"{'='*60}")
        for p in partials:
            print(f"  {' + '.join(p['present_cards'])} + [{p['missing_cards'][0]}]")
            print(f"    Result: {p['result']}")

    if synergy:
        print(f"\n  Synergy pairs: {len(synergy)} (use --verbose to list)")

    print(f"\n  Total: {len(confirmed)} confirmed, {len(likely)} likely, {len(synergy)} synergy")


def show_recommendations_enhanced(candidates, active_strategies, partial_combos, deck_name):
    """Enhanced recommendation output with strategy annotations."""
    print(f"\n{'='*60}")
    print(f"RECOMMENDATIONS for {deck_name} (strategies: {', '.join(sorted(active_strategies)) or 'none'})")
    print(f"{'='*60}")

    # Combo completions
    combo_cards = set()
    for pc in partial_combos:
        for name in pc["missing_cards"]:
            combo_cards.add(name)

    completions = [c for c in candidates if c["name"] in combo_cards]
    if completions:
        print(f"\nCOMBO COMPLETIONS (1 card away from confirmed infinite):")
        for c in completions[:5]:
            matching = [pc for pc in partial_combos if c["name"] in pc["missing_cards"]]
            for pc in matching:
                print(f"  {' + '.join(pc['present_cards'])} + [{c['name']}] -> {pc['result']}")

    # Best fit
    best = [c for c in candidates if c["name"] not in combo_cards]
    print(f"\nBEST FIT:")
    for i, c in enumerate(best[:15], 1):
        strats = c.get("strategies", [])
        strat_str = f" [{', '.join(strats)}]" if strats else ""
        tribal = " [tribal]" if c.get("tribal") else ""
        print(f"  {i}. {c['name']}{strat_str}{tribal} score: {c['score']:.1f}")


def show_deck_analysis(deck_cards, deck_oids, active_strategies, commander_name, db_path=None):
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
    anti = find_anti_synergy(deck_oids, active_strategies, db_path)
    conn.close()

    print(f"\n{'='*60}")
    print(f"DECK ANALYSIS: {commander_name}")
    print(f"{'='*60}")

    print(f"Detected strategies:")
    for strat in sorted(active_strategies):
        cnt = strat_counts.get(strat, 0)
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
            print(f"  {a['name']} ({a['role'] or 'unknown role'})")


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

        if args.recommend:
            # Find synergy candidates from DB (targeted, not full 10k)
            candidates = find_synergy_candidates(cards, DB_PATH)
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
            # Also detect tribal strategies from deck composition
            deck_names_set = set(deck.DECKLIST) | {deck.COMMANDER}
            deck_cards_for_types = [c for c in cards if c["name"] in deck_names_set]
            deck_types = _detect_deck_types(deck_cards_for_types, deck_names_set)
            if deck_types:
                from strategy_detector import CREATURE_TYPE_STRATEGIES
                for dtype in deck_types:
                    strat = CREATURE_TYPE_STRATEGIES.get(dtype.lower())
                    if strat and strat not in active_strategies:
                        active_strategies.add(strat)
        elif args.strategies != "auto":
            active_strategies = set(args.strategies.split(","))
        if args.exclude_strategies:
            active_strategies -= set(args.exclude_strategies.split(","))
        if active_strategies:
            print(f"Active strategies: {', '.join(sorted(active_strategies))}")

    graph = build_graph(cards)
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
                show_deck_analysis(deck_cards_in_set, deck_oids, active_strategies, deck.COMMANDER, db_path)
        if args.combos:
            if db_path:
                # Use enhanced 3-tier combo detection
                show_combos_tiered(deck_oids, deck.COMMANDER, db_path)
            else:
                # Fallback to legacy combo detection
                combos = find_combos(graph, cards, deck_set, deck.COMMANDER, args.top)
                show_combos(combos, deck.COMMANDER, args.top)
        if args.swaps:
            swaps = suggest_swaps(graph, deck_set, deck.COMMANDER, cards, args.top)
            show_swaps(swaps, args.top)
        if args.recommend:
            # Auto-detect dominant creature types for tribal boost
            deck_types = _detect_deck_types(cards, deck_set)
            recommend_cards(graph, deck_set, cards, deck_types, args.top,
                            active_strategies=active_strategies, db_path=db_path)
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
