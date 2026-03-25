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

# Effect impact: how game-relevant is the triggered card's output?
# Used to differentiate cards with the same trigger but different effects.
EFFECT_IMPACT = {
    "deal_damage": 1.5,     # direct win condition
    "lose_life": 1.3,       # drain is strong
    "create": 1.2,          # token generation compounds
    "draw": 1.2,            # card advantage is always good
    "destroy": 1.1,         # removal
    "sacrifice": 1.1,       # forced sacrifice
    "exile": 1.1,           # premium removal
    "put_counter": 1.0,     # buffs
    "return": 0.9,          # recursion/bounce (context-dependent)
    "add_mana": 0.8,        # mana production (enabler, not win-con)
    "gain_life": 0.5,       # lifegain rarely wins games
    "scry": 0.4,            # marginal card selection
    "untap": 0.8,           # enabler
    "mill": 0.9,            # alternate win condition
    "pump": 0.7,            # combat buff
    "grant_keyword": 0.6,   # marginal unless haste
}


class CausalContext:
    """Pre-loaded edge data for fast per-candidate scoring.

    Three refinements over naive edge counting:
    1. Commander relevance: fewer abilities = each edge more defining
    2. Bidirectional bonus: mutual interaction > one-way
    3. Deck density: strength-weighted interactions normalized by deck size
    """
    def __init__(self, conn, commander_id: str, deck_oids: set[str]):
        self.commander_id = commander_id
        self.deck_oids = deck_oids

        # Load all edges into adjacency dicts
        all_edges = load_edges(conn)
        self._outgoing = defaultdict(list)  # {source: [Edge]}
        self._incoming = defaultdict(list)  # {target: [Edge]}
        for e in all_edges:
            self._outgoing[e.source].append(e)
            self._incoming[e.target].append(e)

        # Commander relevance: inversely proportional to ability count.
        # Krenko (1 ability) = 1.0, Purphoros (4 abilities) = 0.5
        cmdr_ability_count = 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM parsed_abilities WHERE oracle_id = ?",
                (commander_id,)
            ).fetchone()
            cmdr_ability_count = row[0] if row else 1
        except Exception:
            cmdr_ability_count = 1
        self._cmdr_relevance = min(1.0, 2.0 / max(cmdr_ability_count, 1))

        # Pre-load card effect verbs for impact weighting
        # {oracle_id: max_impact_score}
        self._card_impact = {}
        try:
            import json as _json
            for row in conn.execute("SELECT oracle_id, ast_json FROM parsed_abilities"):
                oid, ast_json = row
                try:
                    d = _json.loads(ast_json)
                    effects = d.get("effects", [])
                    ability_impact = 0.0
                    for eff in effects:
                        verb = eff.get("verb", "")
                        ability_impact = max(ability_impact, EFFECT_IMPACT.get(verb, 0.7))
                    # Keep the MAX impact across all abilities for this card
                    prev = self._card_impact.get(oid, 0.0)
                    self._card_impact[oid] = max(prev, ability_impact if ability_impact > 0 else 0.7)
                except Exception:
                    if oid not in self._card_impact:
                        self._card_impact[oid] = 0.7
        except Exception:
            pass  # parsed_abilities table may not exist

    def causal_score(self, candidate_id: str) -> float:
        """Score a candidate with commander-centric weighting."""
        # 1. Commander edges (weighted by relevance)
        cmdr_to_candidate = 0.0  # commander produces → candidate responds
        candidate_to_cmdr = 0.0  # candidate produces → commander responds

        for edge in self._outgoing.get(self.commander_id, []):
            if edge.target == candidate_id:
                cmdr_to_candidate += edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)

        for edge in self._outgoing.get(candidate_id, []):
            if edge.target == self.commander_id:
                candidate_to_cmdr += edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)

        # Apply candidate's effect impact multiplier
        # Impact Tremors (deal_damage=1.5) vs Soul's Attendant (gain_life=0.5)
        impact = self._card_impact.get(candidate_id, 0.7)

        cmdr_score = (cmdr_to_candidate + candidate_to_cmdr) * self._cmdr_relevance * impact

        # 2. Bidirectional bonus: mutual interaction is much stronger
        if cmdr_to_candidate > 0 and candidate_to_cmdr > 0:
            cmdr_score *= 1.5

        # 3. Deck synergy density: strength-weighted, normalized by deck size
        deck_strength_sum = 0.0
        for edge in self._outgoing.get(candidate_id, []):
            if edge.target in self.deck_oids:
                deck_strength_sum += edge.strength
        for edge in self._incoming.get(candidate_id, []):
            if edge.source in self.deck_oids:
                deck_strength_sum += edge.strength

        deck_density = deck_strength_sum / max(len(self.deck_oids), 1) * 10 * impact

        score = cmdr_score + deck_density
        return min(score, 10.0)


def causal_score(candidate_id: str, commander_id: str,
                 deck_cards: set[str], conn) -> float:
    """Single-candidate scoring. For batch use, create CausalContext instead."""
    ctx = CausalContext(conn, commander_id, deck_cards)
    return ctx.causal_score(candidate_id)
