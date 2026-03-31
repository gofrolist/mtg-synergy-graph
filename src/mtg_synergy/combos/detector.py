"""Combo detection: Spellbook confirmed, trigger chains, synergy cycles."""
import json
import sqlite3
from collections import defaultdict
from itertools import combinations

from mtg_synergy.config import DB_PATH


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
            for pair in combinations(combo_oids, 2):
                confirmed_pairs.add(frozenset(pair))

    # --- Load causal edges + card names in batched queries ---
    deck_list = list(deck_oids)
    _chunk_size = 500

    card_edges = {}  # (source_oid, target_oid) -> max strength
    conn_names = {}

    for _ci in range(0, len(deck_list), _chunk_size):
        _chunk = deck_list[_ci:_ci + _chunk_size]
        _ph = ",".join("?" * len(_chunk))

        for row in conn.execute(
            f"SELECT source_id, target_id, strength FROM interaction_edges "
            f"WHERE source_id IN ({_ph})", _chunk
        ).fetchall():
            src, tgt, strength = row
            if tgt in deck_oids:
                key = (src, tgt)
                card_edges[key] = max(card_edges.get(key, 0), strength)

        for row in conn.execute(
            f"SELECT oracle_id, name FROM cards WHERE oracle_id IN ({_ph})", _chunk
        ).fetchall():
            conn_names[row[0]] = row[1]

    conn.close()

    # --- Find causal edge cycles (bidirectional edges = combo-likely) ---
    for i, oid_a in enumerate(deck_list):
        for oid_b in deck_list[i + 1:]:
            pair = frozenset([oid_a, oid_b])
            if pair in confirmed_pairs:
                continue

            a_to_b = card_edges.get((oid_a, oid_b), 0)
            b_to_a = card_edges.get((oid_b, oid_a), 0)

            name_a = conn_names.get(oid_a, oid_a)
            name_b = conn_names.get(oid_b, oid_b)

            if a_to_b > 0 and b_to_a > 0:
                # Bidirectional causal edges = circular trigger chain
                combos.append({
                    "tier": "combo-likely",
                    "cards": [name_a, name_b],
                    "card_oids": [oid_a, oid_b],
                    "result": f"Circular triggers: {name_a} ({a_to_b:.2f}) <-> {name_b} ({b_to_a:.2f})",
                    "reason": f"Bidirectional causal edges (strength {a_to_b:.2f} / {b_to_a:.2f})",
                })
            elif a_to_b > 0 or b_to_a > 0:
                # One-way causal edge = synergy
                strength = max(a_to_b, b_to_a)
                combos.append({
                    "tier": "synergy",
                    "cards": [name_a, name_b],
                    "card_oids": [oid_a, oid_b],
                    "result": f"Causal link: {name_a} -> {name_b} (strength {strength:.2f})",
                    "reason": "One-way causal edge",
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
