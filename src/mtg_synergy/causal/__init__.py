"""Causal interaction graph — deterministic synergy analysis."""
import json
from collections import defaultdict, namedtuple
from mtg_synergy.causal.types import Edge, EdgeDetail
from mtg_synergy.causal.graph_builder import build_causal_edges
from mtg_synergy.parse.ast_types import Ability

# Lightweight edge for CausalContext scoring — uses 5 fields instead of 7 + full EdgeDetail.
# Saves ~20MB by avoiding json.loads of full detail dict for 60k+ edges.
_LightEdge = namedtuple("_LightEdge", ["source", "target", "edge_type", "strength", "event"])


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
            filter_precision TEXT,
            PRIMARY KEY (source_id, target_id, edge_type, ability_a, ability_b)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON interaction_edges(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON interaction_edges(target_id)")
    conn.commit()


def _batch_load_card_column(conn, card_ids: set[str], column: str,
                            chunk_size: int = 500) -> dict[str, str]:
    """Batch-load a single column from cards table for the given oracle_ids."""
    if not card_ids:
        return {}
    result = {}
    card_list = list(card_ids)
    for i in range(0, len(card_list), chunk_size):
        chunk = card_list[i:i + chunk_size]
        ph = ",".join("?" * len(chunk))
        for oid, val in conn.execute(
            f"SELECT oracle_id, {column} FROM cards WHERE oracle_id IN ({ph})", chunk
        ).fetchall():
            if val:
                result[oid] = val
    return result


def build_and_store_graph(conn, cards: dict[str, list[Ability]]):
    oracle_texts = _batch_load_card_column(conn, set(cards.keys()), "oracle_text")
    type_lines = _batch_load_card_column(conn, set(cards.keys()), "type_line")
    edges = build_causal_edges(cards, oracle_texts=oracle_texts, type_lines=type_lines)
    conn.execute("DELETE FROM interaction_edges")
    for e in edges:
        conn.execute(
            "INSERT OR REPLACE INTO interaction_edges VALUES (?,?,?,?,?,?,?)",
            (e.source, e.target, e.edge_type, e.ability_a, e.ability_b,
             e.strength, json.dumps(e.detail.to_dict())))
    conn.commit()
    return len(edges)


def store_edges(conn, edges: list[Edge]) -> int:
    """Store edges in interaction_edges table."""
    conn.execute("DELETE FROM interaction_edges")
    for e in edges:
        detail_dict = e.detail.to_dict()
        precision = detail_dict.get("filter_precision")
        conn.execute(
            "INSERT OR REPLACE INTO interaction_edges "
            "(source_id, target_id, edge_type, ability_a, ability_b, strength, detail, filter_precision) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (e.source, e.target, e.edge_type, e.ability_a, e.ability_b,
             e.strength, json.dumps(detail_dict), precision))
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


def _parse_edge_rows(rows) -> list[Edge]:
    """Parse raw DB rows into full Edge objects (for storage/display)."""
    return [Edge(r[0], r[1], r[2], r[3], r[4], r[5],
                 EdgeDetail(**json.loads(r[6]))) for r in rows]


def _parse_light_edges(rows) -> list:
    """Parse rows into lightweight edges for scoring (skips full JSON parse).

    Only extracts the 'event' field from the detail JSON, saving ~20MB
    on 60k+ edges by avoiding full EdgeDetail construction.
    """
    import re
    _event_re = re.compile(r'"event"\s*:\s*"([^"]*)"')
    result = []
    for r in rows:
        # Fast event extraction — avoid full json.loads
        detail_str = r[6]
        m = _event_re.search(detail_str)
        event = m.group(1) if m else None
        result.append(_LightEdge(r[0], r[1], r[2], r[5], event))
    return result


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
# Keyed by Forge verb names (from forge_abilities table).
EFFECT_IMPACT = {
    "DealDamage": 1.5,      # direct win condition
    "DamageAll": 1.5,       # board-wide damage
    "LoseLife": 1.3,        # drain is strong
    "Token": 1.2,           # token generation compounds
    "Draw": 1.2,            # card advantage is always good
    "Destroy": 1.1,         # removal
    "DestroyAll": 1.1,      # board wipe
    "Sacrifice": 1.1,       # forced sacrifice
    "SacrificeAll": 1.1,    # mass sacrifice
    "ChangeZone": 1.0,      # exile/reanimate/bounce (context-dependent)
    "PutCounter": 1.0,      # buffs
    "PutCounterAll": 1.0,   # mass buffs
    "Mana": 0.8,            # mana production (enabler, not win-con)
    "GainLife": 0.5,        # lifegain rarely wins games
    "Scry": 0.4,            # marginal card selection
    "Surveil": 0.6,         # better than scry (graveyard value)
    "Untap": 0.8,           # enabler
    "Mill": 0.9,            # alternate win condition
    "Pump": 0.7,            # combat buff
    "PumpAll": 0.9,         # board-wide buff
    "Counter": 1.0,         # counterspell
    "GainControl": 1.2,     # steal effects
    "CopyPermanent": 1.1,   # clone
    "Discard": 0.9,         # hand disruption
    "Proliferate": 1.0,     # counter synergy
    "Connive": 0.8,         # draw + discard + counter
    "Explore": 0.7,         # card advantage
    "Fight": 0.8,           # removal
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

        # Load edges only for relevant cards (commander + deck), not all 7M+
        # Candidate edges are loaded on demand via batch_load()
        self._conn = conn
        self._loaded = {commander_id} | deck_oids
        relevant_ids = list({commander_id} | deck_oids)
        self._outgoing = defaultdict(list)  # {source: [_LightEdge]}
        self._incoming = defaultdict(list)  # {target: [_LightEdge]}
        # Batch-load all deck edges in 2 chunked queries (not 2 per card)
        _EDGE_COLS = "source_id, target_id, edge_type, ability_a, ability_b, strength, detail"
        chunk_size = 500
        for i in range(0, len(relevant_ids), chunk_size):
            chunk = relevant_ids[i:i + chunk_size]
            ph = ",".join("?" * len(chunk))
            for e in _parse_light_edges(conn.execute(
                f"SELECT {_EDGE_COLS} FROM interaction_edges WHERE source_id IN ({ph})", chunk
            ).fetchall()):
                self._outgoing[e.source].append(e)
                self._incoming[e.target].append(e)
            for e in _parse_light_edges(conn.execute(
                f"SELECT {_EDGE_COLS} FROM interaction_edges WHERE target_id IN ({ph})", chunk
            ).fetchall()):
                self._outgoing[e.source].append(e)
                self._incoming[e.target].append(e)

        # Commander relevance: inversely proportional to ability count
        cmdr_ability_count = 1
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM forge_abilities fa "
                "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
                "WHERE fnm.oracle_id = ?",
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
            if edge.edge_type == "triggers" and edge.event:
                self._cmdr_events_produced.add(edge.event)
        for edge in self._incoming.get(commander_id, []):
            if edge.edge_type == "triggers" and edge.event:
                self._cmdr_events_consumed.add(edge.event)

        # Pre-load card effect verbs for impact weighting (from Forge abilities)
        self._card_impact = {}
        try:
            for row in conn.execute(
                "SELECT fnm.oracle_id, fa.verb FROM forge_abilities fa "
                "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
                "WHERE fa.verb IS NOT NULL"
            ):
                oid, verb = row
                impact = EFFECT_IMPACT.get(verb, 0.7)
                prev = self._card_impact.get(oid, 0.0)
                self._card_impact[oid] = max(prev, impact)
        except Exception:
            pass

        # Anti-synergy detection: cards that prevent events the deck relies on
        # {card_id: set of events this card prevents}
        self._card_prevents = {}
        try:
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
                if edge.edge_type == "triggers" and edge.event:
                    self._deck_relies_on.add(edge.event)
        # Also add events the commander produces (the deck cares about these)
        self._deck_relies_on.update(self._cmdr_events_produced)

        # Build commander forward map: {card_id: strength} for direct cmdr outgoing
        self._cmdr_forward_map = {}
        for edge in self._outgoing.get(commander_id, []):
            mid = edge.target
            if mid not in self._cmdr_forward_map or edge.strength > self._cmdr_forward_map[mid]:
                self._cmdr_forward_map[mid] = edge.strength

    _EDGE_COLS = "source_id, target_id, edge_type, ability_a, ability_b, strength, detail"

    def _ensure_loaded(self, card_id: str):
        """Lazy-load edges for a card not in the initial set."""
        if card_id in self._outgoing or card_id in self._loaded:
            return
        self._loaded.add(card_id)
        for e in _parse_light_edges(self._conn.execute(
            f"SELECT {self._EDGE_COLS} FROM interaction_edges WHERE source_id = ?",
            (card_id,)).fetchall()):
            self._outgoing[e.source].append(e)
            self._incoming[e.target].append(e)
        for e in _parse_light_edges(self._conn.execute(
            f"SELECT {self._EDGE_COLS} FROM interaction_edges WHERE target_id = ?",
            (card_id,)).fetchall()):
            self._outgoing[e.source].append(e)
            self._incoming[e.target].append(e)

    def batch_load(self, card_ids: list[str]):
        """Bulk-load edges between candidates and deck+commander cards.

        Only loads edges relevant to scoring (candidate↔deck), not all edges.
        This avoids parsing millions of irrelevant candidate↔candidate edges.
        """
        new_ids = [cid for cid in card_ids if cid not in self._loaded and cid not in self._outgoing]
        if not new_ids:
            return
        for cid in new_ids:
            self._loaded.add(cid)

        # Only load edges connecting candidates to deck/commander cards
        relevant_targets = list(self.deck_oids | {self.commander_id})
        tgt_ph = ",".join("?" * len(relevant_targets))

        chunk_size = 500
        for i in range(0, len(new_ids), chunk_size):
            chunk = new_ids[i:i + chunk_size]
            src_ph = ",".join("?" * len(chunk))
            # Edges FROM candidates TO deck cards
            for e in _parse_light_edges(self._conn.execute(
                f"SELECT {self._EDGE_COLS} FROM interaction_edges "
                f"WHERE source_id IN ({src_ph}) AND target_id IN ({tgt_ph})",
                chunk + relevant_targets
            ).fetchall()):
                self._outgoing[e.source].append(e)
                self._incoming[e.target].append(e)
            # Edges FROM deck cards TO candidates
            for e in _parse_light_edges(self._conn.execute(
                f"SELECT {self._EDGE_COLS} FROM interaction_edges "
                f"WHERE source_id IN ({tgt_ph}) AND target_id IN ({src_ph})",
                relevant_targets + chunk
            ).fetchall()):
                self._outgoing[e.source].append(e)
                self._incoming[e.target].append(e)

    def _chain_bonus(self, candidate_id: str) -> float:
        """Score chain paths: commander → candidate → deck cards.

        Only fires if the candidate is directly linked FROM the commander.
        """
        cmdr_link = self._cmdr_forward_map.get(candidate_id, 0)
        if cmdr_link == 0:
            return 0.0
        bonus = 0.0
        for edge in self._outgoing.get(candidate_id, []):
            if edge.target in self.deck_oids:
                bonus += cmdr_link * edge.strength * 0.5
        return bonus

    def causal_score(self, candidate_id: str) -> float:
        """Score a candidate with commander-centric weighting."""
        self._ensure_loaded(candidate_id)
        # 1. Commander edges (weighted by relevance + strategy alignment)
        cmdr_to_candidate = 0.0
        candidate_to_cmdr = 0.0

        for edge in self._outgoing.get(self.commander_id, []):
            if edge.target == candidate_id:
                w = edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
                # Strategy bonus: if this edge's event is one the commander produces,
                # the candidate is responding to the commander's core strategy
                if edge.event in self._cmdr_events_produced:
                    w *= 1.3
                cmdr_to_candidate += w

        for edge in self._outgoing.get(candidate_id, []):
            if edge.target == self.commander_id:
                w = edge.strength * EDGE_WEIGHTS.get(edge.edge_type, 1.0)
                # Strategy bonus: candidate produces events the commander consumes
                if edge.event in self._cmdr_events_consumed:
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
                if edge.edge_type == "triggers" and edge.event:
                    if edge.event in self._deck_relies_on:
                        w *= 1.0  # full weight — strategy relevant
                    else:
                        w *= 0.3  # diminished — not what the deck cares about
                deck_strength_sum += w
        for edge in self._incoming.get(candidate_id, []):
            if edge.source in self.deck_oids:
                w = edge.strength
                if edge.edge_type == "triggers" and edge.event:
                    if edge.event in self._deck_relies_on:
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
        chain_bonus = self._chain_bonus(candidate_id)

        score = cmdr_score + deck_density + chain_bonus + anti_synergy
        return max(min(score, 10.0), -5.0)  # allow negative for strong anti-synergy


def causal_score(candidate_id: str, commander_id: str,
                 deck_cards: set[str], conn) -> float:
    """Single-candidate scoring. For batch use, create CausalContext instead."""
    ctx = CausalContext(conn, commander_id, deck_cards)
    return ctx.causal_score(candidate_id)
