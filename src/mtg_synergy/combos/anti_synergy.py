"""Anti-synergy detection for deck cards."""
import sqlite3

from mtg_synergy.constants import STAPLE_ROLES
from mtg_synergy.config import DB_PATH


def find_anti_synergy(deck_oids, active_strategies, db_path=None, graph=None, deck_cards_set=None):
    """Find deck cards with zero strategy overlap that aren't staples.

    Returns list of dicts: {oracle_id, name, role, synergy_score, partners}.
    Cards with strong synergy connections (>=5 partners or >=20.0 total score) are exempt
    even if their strategy doesn't match.
    """
    if not active_strategies:
        return []
    if db_path is None:
        db_path = str(DB_PATH)
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
            # Before flagging, check if card has strong synergy connections
            synergy_score = 0.0
            partner_count = 0
            if graph and deck_cards_set:
                adj = graph.get("adjacency", {})
                for edge in adj.get(name, []):
                    if edge["target"] in deck_cards_set:
                        synergy_score += edge["score"]
                        partner_count += 1

            # Only flag if also low synergy (< 5 partners or < 20.0 total score)
            if partner_count < 5 or synergy_score < 20.0:
                anti.append({
                    "oracle_id": oid,
                    "name": name,
                    "role": role,
                    "synergy_score": round(synergy_score, 1),
                    "partners": partner_count,
                })

    conn.close()
    return anti
