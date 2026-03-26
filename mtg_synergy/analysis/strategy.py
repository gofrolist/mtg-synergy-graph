"""Strategy detection, candidate filtering, and commander-based deck building."""
import json
import os
import sqlite3
from collections import defaultdict

from mtg_synergy.config import DATA_DIR


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

    # Get commander's Forge abilities (verbs = what it does, triggers = what it responds to)
    cmd_verbs = set()
    cmd_triggers = set()
    for r in conn.execute(
        "SELECT fa.verb, fa.trigger_mode, fa.keyword FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fnm.oracle_id = ?", (cmd_oid,)):
        if r[0]: cmd_verbs.add(r[0])
        if r[1]: cmd_triggers.add(r[1])
        if r[2]: cmd_verbs.add(r[2])

    # Extract creature types from commander
    cmd_subtypes = set()
    if " — " in cmd_type:
        cmd_subtypes = {s.strip(",") for s in cmd_type.split(" — ")[1].split()}

    print(f"\n{'═' * 70}")
    print(f"COMMANDER: {cmd_name}")
    print(f"  {cmd_type} | CMC {row['cmc']}")
    print(f"  {cmd_text}")
    print(f"  Colors: {','.join(sorted(cmd_colors)) or 'C'}")
    print(f"  Verbs: {sorted(cmd_verbs)}")
    print(f"  Triggers: {sorted(cmd_triggers)}")
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

    # Load Forge verbs and triggers for all legal cards (bulk)
    card_verbs = defaultdict(set)    # oid -> set of verbs + keywords
    card_triggers = defaultdict(set)  # oid -> set of trigger_modes
    for r in conn.execute(
        "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.keyword "
        "FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"):
        oid = r[0]
        if oid in legal_cards:
            if r[1]: card_verbs[oid].add(r[1])
            if r[2]: card_triggers[oid].add(r[2])
            if r[3]: card_verbs[oid].add(r[3])

    conn.close()

    # Score each card by Forge verb/trigger overlap with commander
    scores = {}
    for oid, meta in legal_cards.items():
        name = meta["name"]
        c_verbs = card_verbs.get(oid, set())
        c_triggers = card_triggers.get(oid, set())

        enabler_verbs = []  # card produces events the commander triggers on
        payoff_verbs = []   # card triggers on events the commander produces
        score = 0.0

        # Card's verbs match commander's triggers (card enables commander)
        for verb in c_verbs:
            if verb in cmd_triggers:
                score += 1.0
                enabler_verbs.append(verb)

        # Card's triggers match commander's verbs (card benefits from commander)
        for trigger in c_triggers:
            if trigger in cmd_verbs:
                score += 1.0
                payoff_verbs.append(trigger)

        # Shared verbs (same strategy)
        shared = c_verbs & cmd_verbs
        if shared:
            score += len(shared) * 0.5

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
                "enabler_tags": enabler_verbs,
                "payoff_tags": payoff_verbs,
                "tribal": tribal,
                "is_enabler": len(enabler_verbs) > 0,
                "is_payoff": len(payoff_verbs) > 0,
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
