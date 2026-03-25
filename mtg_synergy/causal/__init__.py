"""Causal interaction graph — deterministic synergy analysis."""
import json
import sqlite3
from collections import defaultdict
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.causal.graph_builder import build_causal_edges
from mtg_synergy.parse.ast_types import Ability


def ensure_causal_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interaction_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            ability_a INTEGER NOT NULL,
            ability_b INTEGER NOT NULL,
            strength  REAL NOT NULL,
            detail    TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id, edge_type, ability_a, ability_b)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON interaction_edges(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON interaction_edges(target_id)")
    conn.commit()


def _load_oracle_texts(conn, card_ids: set[str]) -> dict[str, str]:
    """Load oracle_text from cards table for the given oracle_ids."""
    if not card_ids:
        return {}
    oracle_texts = {}
    # Batch query in chunks to avoid huge IN clauses
    card_list = list(card_ids)
    chunk_size = 500
    for i in range(0, len(card_list), chunk_size):
        chunk = card_list[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT oracle_id, oracle_text FROM cards WHERE oracle_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for oid, text in rows:
            if text:
                oracle_texts[oid] = text
    return oracle_texts


def build_and_store_graph(conn, cards: dict[str, list[Ability]]):
    oracle_texts = _load_oracle_texts(conn, set(cards.keys()))
    edges = build_causal_edges(cards, oracle_texts=oracle_texts)
    conn.execute("DELETE FROM interaction_edges")
    for e in edges:
        conn.execute(
            "INSERT OR REPLACE INTO interaction_edges VALUES (?,?,?,?,?,?,?)",
            (e.source, e.target, e.edge_type, e.ability_a, e.ability_b,
             e.strength, json.dumps(e.detail.to_dict())))
    conn.commit()
    return len(edges)


def load_edges(conn, source_id=None, target_id=None) -> list[Edge]:
    query = "SELECT source_id, target_id, edge_type, ability_a, ability_b, strength, detail FROM interaction_edges"
    params = []
    conditions = []
    if source_id:
        conditions.append("source_id = ?")
        params.append(source_id)
    if target_id:
        conditions.append("target_id = ?")
        params.append(target_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    rows = conn.execute(query, params).fetchall()
    return [Edge(r[0], r[1], r[2], r[3], r[4], r[5],
                 EdgeDetail(**json.loads(r[6]))) for r in rows]


EDGE_WEIGHTS = {"triggers": 2.0, "feeds": 1.5, "amplifies": 1.8, "enables": 1.0}


class CausalContext:
    """Pre-loaded edge data for fast per-candidate scoring.
    Load once per recommendation run, then score each candidate with dict lookups."""
    def __init__(self, conn, commander_id: str, deck_oids: set[str]):
        self.commander_id = commander_id
        self.deck_oids = deck_oids
        all_edges = load_edges(conn)
        self._outgoing = defaultdict(list)
        self._incoming = defaultdict(list)
        for e in all_edges:
            self._outgoing[e.source].append(e)
            self._incoming[e.target].append(e)

    def causal_score(self, candidate_id: str) -> float:
        score = 0.0
        for edge in self._outgoing.get(candidate_id, []):
            if edge.target == self.commander_id:
                score += edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
        for edge in self._incoming.get(candidate_id, []):
            if edge.source == self.commander_id:
                score += edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
        deck_interactions = 0
        for edge in self._outgoing.get(candidate_id, []):
            if edge.target in self.deck_oids:
                deck_interactions += 1
        for edge in self._incoming.get(candidate_id, []):
            if edge.source in self.deck_oids:
                deck_interactions += 1
        score += deck_interactions * 0.3
        return min(score, 10.0)


def causal_score(candidate_id: str, commander_id: str,
                 deck_cards: set[str], conn) -> float:
    ctx = CausalContext(conn, commander_id, deck_cards)
    return ctx.causal_score(candidate_id)
