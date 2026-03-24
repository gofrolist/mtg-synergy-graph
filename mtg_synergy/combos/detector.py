"""Combo detection: Spellbook confirmed, trigger chains, synergy cycles."""
import json
import os
import re
import sqlite3
from collections import defaultdict

from mtg_synergy.constants import SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES
from mtg_synergy.config import DB_PATH


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
_COMBO_RE = [re.compile(p, re.IGNORECASE) for p in _COMBO_PATTERNS]


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


def find_combos_tiered(deck_oids, db_path=None):
    """Three-tier combo detection: infinite-confirmed, combo-likely, synergy.

    Args:
        deck_oids: set of oracle_ids in the deck
        db_path: optional DB path override

    Returns:
        list of combo dicts with 'tier', 'cards', 'result', 'reason' fields
    """
    if db_path is None:
        db_path = str(DB_PATH)
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

    # --- Load provides/wants for deck cards (batch) ---
    deck_list = list(deck_oids)
    provides_by_card = {}
    wants_by_card = {}
    _chunk_size = 500
    for _ci in range(0, len(deck_list), _chunk_size):
        _chunk = deck_list[_ci:_ci + _chunk_size]
        _ph = ",".join("?" * len(_chunk))
        for row in conn.execute(
            f"SELECT oracle_id, tag FROM provides WHERE oracle_id IN ({_ph})", _chunk
        ).fetchall():
            provides_by_card.setdefault(row[0], set()).add(row[1])
        for row in conn.execute(
            f"SELECT oracle_id, tag FROM wants WHERE oracle_id IN ({_ph})", _chunk
        ).fetchall():
            wants_by_card.setdefault(row[0], set()).add(row[1])

    # --- Load abilities for deck cards (batch) ---
    abilities_by_card = {}
    for _ci in range(0, len(deck_list), _chunk_size):
        _chunk = deck_list[_ci:_ci + _chunk_size]
        _ph = ",".join("?" * len(_chunk))
        for row in conn.execute(
            f"SELECT oracle_id, trigger_tags, effect_tags FROM abilities WHERE oracle_id IN ({_ph})",
            _chunk
        ).fetchall():
            oid = row[0]
            ab = abilities_by_card.get(oid, {"trigger_tags": set(), "effect_tags": set()})
            if row[1]:
                ab["trigger_tags"].update(json.loads(row[1]))
            if row[2]:
                ab["effect_tags"].update(json.loads(row[2]))
            abilities_by_card[oid] = ab
    # Remove empty entries
    abilities_by_card = {k: v for k, v in abilities_by_card.items()
                         if v["trigger_tags"] or v["effect_tags"]}

    conn_names = {}
    for _ci in range(0, len(deck_list), _chunk_size):
        _chunk = deck_list[_ci:_ci + _chunk_size]
        _ph = ",".join("?" * len(_chunk))
        for row in conn.execute(
            f"SELECT oracle_id, name FROM cards WHERE oracle_id IN ({_ph})", _chunk
        ).fetchall():
            conn_names[row[0]] = row[1]

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
                # tokens-creature (effect) matches etb-value (trigger).
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
    if not active_strategies:
        return 1.0

    if db_path is None:
        db_path = str(DB_PATH)
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


def find_partial_combos(deck_oids, db_path=None, color_identity=None):
    """Find Spellbook combos where deck is missing exactly 1 card.

    Args:
        deck_oids: set of oracle_ids in the deck
        db_path: optional DB path override
        color_identity: set of colors (e.g. {"G", "W"}) to filter missing cards.
                        If provided, missing cards outside this color identity are excluded.

    Returns list of dicts with: combo_id, result, present_cards, missing_cards, missing_oids.
    """
    if db_path is None:
        db_path = str(DB_PATH)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    combos = conn.execute("SELECT * FROM spellbook_combos").fetchall()

    partials = []
    for combo in combos:
        combo_oids = json.loads(combo["card_oracle_ids"])
        combo_names = json.loads(combo["card_names"])

        present = [oid for oid in combo_oids if oid in deck_oids]
        missing = [oid for oid in combo_oids if oid not in deck_oids]

        if len(missing) == 1:
            missing_oid = missing[0]

            # Filter by color identity if specified
            if color_identity is not None:
                row = conn.execute(
                    "SELECT color_identity FROM cards WHERE oracle_id = ?",
                    (missing_oid,)
                ).fetchone()
                if row and row["color_identity"]:
                    card_colors = set(json.loads(row["color_identity"]))
                    if not card_colors <= color_identity:
                        continue  # Card has colors outside commander's identity

            missing_idx = combo_oids.index(missing_oid)
            partials.append({
                "combo_id": combo["combo_id"],
                "result": combo["result"],
                "present_cards": [combo_names[combo_oids.index(oid)] for oid in present],
                "missing_cards": [combo_names[missing_idx]],
                "missing_oids": missing,
            })

    conn.close()
    return partials
