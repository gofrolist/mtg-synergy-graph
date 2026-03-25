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


def _load_type_lines(conn, card_ids: set[str]) -> dict[str, str]:
    """Load type_line from cards table for the given oracle_ids."""
    if not card_ids:
        return {}
    type_lines = {}
    card_list = list(card_ids)
    for i in range(0, len(card_list), 500):
        chunk = card_list[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for oid, tl in conn.execute(
            f"SELECT oracle_id, type_line FROM cards WHERE oracle_id IN ({ph})", chunk
        ).fetchall():
            if tl:
                type_lines[oid] = tl
    return type_lines


def build_and_store_graph(conn, cards: dict[str, list[Ability]]):
    oracle_texts = _load_oracle_texts(conn, set(cards.keys()))
    type_lines = _load_type_lines(conn, set(cards.keys()))
    edges = build_causal_edges(cards, oracle_texts=oracle_texts, type_lines=type_lines)
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


def _detect_prevented_events(oracle_text: str) -> set[str]:
    """Detect which game events a card prevents from oracle text.

    Returns set of event names that this card blocks.
    Used for anti-synergy: if the deck relies on 'dies' triggers
    and a candidate prevents 'dies', that's a nonbo.
    """
    text = oracle_text.lower()
    prevented = set()

    # ETB trigger prevention (Torpor Orb, Hushbringer, Elesh Norn opponent-side)
    # "Creatures entering don't cause abilities to trigger"
    # "entering causes a triggered ability ... won't trigger"
    if "entering" in text and ("don't cause" in text or "won't" in text or "triggered abilit" in text and "don't" in text):
        prevented.add("enters_the_battlefield")
        prevented.add("creature_enters")

    # Death trigger prevention (Hushbringer: "dying don't cause abilities to trigger")
    if "dying" in text and ("don't cause" in text or "won't" in text):
        prevented.add("dies")

    # Graveyard replacement (Rest in Peace, Leyline of the Void)
    if "would be put into a graveyard" in text and "exile" in text:
        prevented.add("dies")
        prevented.add("enters_graveyard")

    # Can't gain life (Erebos, Stigma Lasher effect)
    if "can't gain life" in text:
        prevented.add("life_gained")

    # Can't search libraries (Stranglehold, Aven Mindcensor partially)
    if "can't search" in text:
        prevented.add("search")

    # Damage can't be prevented (Quakebringer, Stomp — this is niche)
    # Skipping — too rare to matter

    # Can't attack (Propaganda doesn't prevent, it taxes — skip)

    # Can't draw extra cards (Narset, Spirit Dragon — niche)
    if "can't draw" in text and "more than" not in text:
        prevented.add("card_drawn")

    # Tokens can't enter (no common card does this exactly, but future-proof)
    if "token" in text and ("can't enter" in text or "can't be created" in text):
        prevented.add("creature_enters")

    return prevented

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

    Six scoring dimensions:
    1. Commander relevance: fewer abilities = each edge more defining
    2. Effect impact: deal_damage > gain_life
    3. Bidirectional bonus: mutual interaction > one-way
    4. Strategy alignment: edges matching commander's event profile score higher
    5. Deck density: strength-weighted interactions normalized by deck size
    6. Chain participation: cards in multi-card chains/loops with commander
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

        # Commander relevance: inversely proportional to ability count
        cmdr_ability_count = 1
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM parsed_abilities WHERE oracle_id = ?",
                (commander_id,)
            ).fetchone()
            cmdr_ability_count = row[0] if row else 1
        except Exception:
            pass
        self._cmdr_relevance = min(1.0, 2.0 / max(cmdr_ability_count, 1))

        # Commander strategy profile: derive from edges (no parsing needed)
        # What events does the commander produce? = events on commander's outgoing trigger edges
        # What events does the commander consume? = events on commander's incoming trigger edges
        self._cmdr_events_produced = set()
        self._cmdr_events_consumed = set()
        for edge in self._outgoing.get(commander_id, []):
            if edge.edge_type == "triggers" and edge.detail.event:
                self._cmdr_events_produced.add(edge.detail.event)
        for edge in self._incoming.get(commander_id, []):
            if edge.edge_type == "triggers" and edge.detail.event:
                self._cmdr_events_consumed.add(edge.detail.event)

        # Pre-load card effect verbs for impact weighting
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
                    prev = self._card_impact.get(oid, 0.0)
                    self._card_impact[oid] = max(prev, ability_impact if ability_impact > 0 else 0.7)
                except Exception:
                    if oid not in self._card_impact:
                        self._card_impact[oid] = 0.7
        except Exception:
            pass

        # Anti-synergy detection: cards that prevent events the deck relies on
        # {card_id: set of events this card prevents}
        self._card_prevents = {}
        try:
            import json as _json2
            for row in conn.execute("SELECT oracle_id, oracle_text FROM cards WHERE oracle_text IS NOT NULL"):
                oid, oracle = row
                if not oracle:
                    continue
                prevented = _detect_prevented_events(oracle)
                if prevented:
                    self._card_prevents[oid] = prevented
        except Exception:
            pass

        # Deck event profile: what events does the deck's strategy rely on?
        # Derived from what the commander + deck cards trigger on
        self._deck_relies_on = set(self._cmdr_events_consumed)
        for deck_oid in deck_oids:
            for edge in self._incoming.get(deck_oid, []):
                if edge.edge_type == "triggers" and edge.detail.event:
                    self._deck_relies_on.add(edge.detail.event)
        # Also add events the commander produces (the deck cares about these)
        self._deck_relies_on.update(self._cmdr_events_produced)

        # Chain participation bonus (pre-computed offline, not at query time)
        self._chain_bonus = {}

    def causal_score(self, candidate_id: str) -> float:
        """Score a candidate with commander-centric weighting."""
        # 1. Commander edges (weighted by relevance + strategy alignment)
        cmdr_to_candidate = 0.0
        candidate_to_cmdr = 0.0

        for edge in self._outgoing.get(self.commander_id, []):
            if edge.target == candidate_id:
                w = edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
                # Strategy bonus: if this edge's event is one the commander produces,
                # the candidate is responding to the commander's core strategy
                if edge.detail.event in self._cmdr_events_produced:
                    w *= 1.3
                cmdr_to_candidate += w

        for edge in self._outgoing.get(candidate_id, []):
            if edge.target == self.commander_id:
                w = edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
                # Strategy bonus: candidate produces events the commander consumes
                if edge.detail.event in self._cmdr_events_consumed:
                    w *= 1.3
                candidate_to_cmdr += w

        # Effect impact multiplier
        impact = self._card_impact.get(candidate_id, 0.7)

        cmdr_score = (cmdr_to_candidate + candidate_to_cmdr) * self._cmdr_relevance * impact

        # 2. Bidirectional bonus
        if cmdr_to_candidate > 0 and candidate_to_cmdr > 0:
            cmdr_score *= 1.5

        # 3. Deck synergy density — only count STRATEGY-RELEVANT edges
        # Edges whose event matches what the commander/deck cares about
        # score full strength. Other edges get diminished.
        deck_strength_sum = 0.0
        for edge in self._outgoing.get(candidate_id, []):
            if edge.target in self.deck_oids:
                w = edge.strength
                if edge.edge_type == "triggers" and edge.detail.event:
                    if edge.detail.event in self._deck_relies_on:
                        w *= 1.0  # full weight — strategy relevant
                    else:
                        w *= 0.3  # diminished — not what the deck cares about
                deck_strength_sum += w
        for edge in self._incoming.get(candidate_id, []):
            if edge.source in self.deck_oids:
                w = edge.strength
                if edge.edge_type == "triggers" and edge.detail.event:
                    if edge.detail.event in self._deck_relies_on:
                        w *= 1.0
                    else:
                        w *= 0.3
                deck_strength_sum += w

        deck_density = deck_strength_sum / max(len(self.deck_oids), 1) * 10 * impact

        # 4. Anti-synergy penalty
        # If the candidate prevents events the deck relies on, penalize heavily
        anti_synergy = 0.0
        prevented = self._card_prevents.get(candidate_id, set())
        if prevented:
            overlap = prevented & self._deck_relies_on
            if overlap:
                # Severe penalty: each prevented event that the deck uses = -3
                anti_synergy = len(overlap) * -3.0

        # 5. Chain participation bonus
        chain_bonus = self._chain_bonus.get(candidate_id, 0.0)

        score = cmdr_score + deck_density + chain_bonus + anti_synergy
        return max(min(score, 10.0), -5.0)  # allow negative for strong anti-synergy


def causal_score(candidate_id: str, commander_id: str,
                 deck_cards: set[str], conn) -> float:
    """Single-candidate scoring. For batch use, create CausalContext instead."""
    ctx = CausalContext(conn, commander_id, deck_cards)
    return ctx.causal_score(candidate_id)
