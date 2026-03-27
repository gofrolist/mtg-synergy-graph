"""Shared forge feature computation for training and inference.

Extracts the 22-feature forge GBM feature vector computation into a
single module used by both train_fusion_model.py and scoring.py.
"""
import math
import re
import time
from collections import Counter

import numpy as np


# Phase timing: position in the turn cycle (0=start, 1=end).
_PHASE_ORDER = {
    "Upkeep": 0.0, "Draw": 0.1, "Main1": 0.2, "BeginCombat": 0.3,
    "DeclareAttackers": 0.4, "DeclareBlockers": 0.5, "CombatDamage": 0.6,
    "EndCombat": 0.7, "Main2": 0.8, "End": 0.9, "Cleanup": 1.0,
}

_PHASE_ALIASES = {
    "Main": "Main1", "Postcombat Main": "Main2", "Second Main": "Main2",
    "Combat Damage": "CombatDamage", "Declare Attackers": "DeclareAttackers",
    "Declare Blockers": "DeclareBlockers", "Begin Combat": "BeginCombat",
    "End of Combat": "EndCombat", "End Step": "End",
}


def normalize_phase(phase):
    """Normalize phase name to canonical form."""
    if not phase:
        return phase
    return _PHASE_ALIASES.get(phase, phase)


class ForgeFeatureContext:
    """Pre-loaded data for computing forge GBM features.

    Load once, use for many (commander, card) pairs. Shared between
    training (build_forge_feature_matrix) and inference (score_forge_candidates).
    """

    def __init__(self, conn, normed_emb, oid_to_idx, preload_edges=False):
        self.conn = conn
        self.normed_emb = normed_emb
        self.oid_to_idx = oid_to_idx
        self._has_edge_index = False

        # Load oracle texts for keyword features
        self._card_oracle = {}
        for oid, text in conn.execute("SELECT oracle_id, oracle_text FROM cards"):
            if text:
                self._card_oracle[oid] = text.lower()

        # Card strategies
        self.card_strats = {}
        for oid, strat in conn.execute(
            "SELECT oracle_id, strategy FROM card_strategies WHERE confidence >= 0.3"
        ):
            self.card_strats.setdefault(oid, set()).add(strat)

        # Strategy vector index
        self.all_strategies = sorted({s for strats in self.card_strats.values() for s in strats})
        self._strat_idx = {s: i for i, s in enumerate(self.all_strategies)}
        self._n_strats = len(self.all_strategies)

        # Phase data
        self.card_phase_order = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.trigger_phase FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_phase IS NOT NULL"
        ):
            oid, phase = row
            phase = normalize_phase(phase)
            if "," in (phase or ""):
                for p in phase.split(","):
                    o = _PHASE_ORDER.get(p.strip())
                    if o is not None:
                        self.card_phase_order.setdefault(oid, set()).add(o)
            else:
                order = _PHASE_ORDER.get(phase)
                if order is not None:
                    self.card_phase_order.setdefault(oid, set()).add(order)

        # Oracle text TF-IDF
        self._tokenize = re.compile(r'[a-z]{3,}')
        doc_freq = Counter()
        self._card_tokens = {}
        self._n_docs = 0
        for row in conn.execute("SELECT oracle_id, oracle_text FROM cards WHERE oracle_text IS NOT NULL"):
            tokens = Counter(self._tokenize.findall((row[1] or "").lower()))
            if tokens:
                self._card_tokens[row[0]] = tokens
                for w in tokens:
                    doc_freq[w] += 1
                self._n_docs += 1
        self._vocab = [w for w, _ in doc_freq.most_common(2000)]
        self._vocab_idx = {w: i for i, w in enumerate(self._vocab)}
        self._n_vocab = len(self._vocab)
        self._doc_freq = doc_freq

        # Forge mechanics vectors: encode each card's full mechanical profile
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        self._mech_produces, self._mech_consumes, self._mech_dim, _ = \
            build_mechanics_vectors(conn)

        if preload_edges:
            self._build_edge_index(conn)

    def _build_edge_index(self, conn):
        """Pre-load edge adjacency into memory for fast deck_edge_count computation.

        Eliminates per-commander deck edge DB queries (~0.5s each × 1355 cmds = 743s).
        Replaced by a single scan + fast numpy set intersections.
        Commander strength/events still use fast indexed DB queries (~1ms each).
        """
        print("  Building in-memory edge index...", flush=True)
        t0 = time.time()

        # Load edges into flat arrays (only need source, target, precision)
        src_list = []
        tgt_list = []
        exact_list = []

        # Use materialized column if available, fall back to json_extract
        has_col = any(
            r[1] == "filter_precision"
            for r in conn.execute("PRAGMA table_info(interaction_edges)")
        )
        prec_expr = "filter_precision" if has_col else "json_extract(detail, '$.filter_precision')"

        for row in conn.execute(
            f"SELECT source_id, target_id, {prec_expr} FROM interaction_edges"
        ):
            s = self.oid_to_idx.get(row[0])
            t = self.oid_to_idx.get(row[1])
            if s is not None and t is not None:
                src_list.append(s)
                tgt_list.append(t)
                exact_list.append(row[2] == "exact")

        n_edges = len(src_list)
        print(f"    Loaded {n_edges:,} edges ({time.time()-t0:.1f}s)")

        src = np.array(src_list, dtype=np.int32)
        tgt = np.array(tgt_list, dtype=np.int32)
        exact = np.array(exact_list, dtype=np.bool_)
        del src_list, tgt_list, exact_list

        # Build outgoing/incoming adjacency: card_idx → numpy array of unique neighbors
        self._adj_out = self._build_adj_arrays(src, tgt)
        self._adj_in = self._build_adj_arrays(tgt, src)

        # Exact-precision adjacency
        if exact.any():
            self._exact_out = self._build_adj_arrays(src[exact], tgt[exact])
            self._exact_in = self._build_adj_arrays(tgt[exact], src[exact])
        else:
            self._exact_out = {}
            self._exact_in = {}

        del src, tgt, exact

        self._idx_to_oid = {v: k for k, v in self.oid_to_idx.items()}
        self._n_cards_idx = len(self.oid_to_idx)
        self._has_edge_index = True

        elapsed = time.time() - t0
        n_unique = sum(len(v) for v in self._adj_out.values())
        print(f"    Edge index built: {elapsed:.1f}s, {n_unique:,} unique outgoing pairs")

    @staticmethod
    def _build_adj_arrays(keys, values):
        """Build adjacency dict: key_idx → sorted numpy array of unique value indices."""
        if len(keys) == 0:
            return {}
        order = np.argsort(keys)
        sk = keys[order]
        sv = values[order]
        # Find boundaries where key changes
        changes = np.concatenate([[0], np.where(sk[1:] != sk[:-1])[0] + 1, [len(sk)]])
        result = {}
        for i in range(len(changes) - 1):
            start, end = int(changes[i]), int(changes[i + 1])
            k = int(sk[start])
            result[k] = np.unique(sv[start:end])
        return result

    def strat_vector(self, oid):
        """Get one-hot strategy vector for a card."""
        strats = self.card_strats.get(oid, set())
        if not strats:
            return None
        v = np.zeros(self._n_strats, dtype=np.float32)
        for s in strats:
            v[self._strat_idx[s]] = 1.0
        return v

    def tfidf_vector(self, oid):
        """Get L2-normalized TF-IDF vector for a card's oracle text."""
        tokens = self._card_tokens.get(oid)
        if not tokens:
            return None
        v = np.zeros(self._n_vocab, dtype=np.float32)
        total = sum(tokens.values())
        for word, count in tokens.items():
            idx = self._vocab_idx.get(word)
            if idx is not None:
                v[idx] = (count / total) * math.log(self._n_docs / max(self._doc_freq[word], 1))
        norm = np.linalg.norm(v)
        return v / norm if norm > 0 else None


class CmdrFeatureContext:
    """Per-commander pre-loaded data for feature computation."""

    def __init__(self, ctx: ForgeFeatureContext, cmdr_oid: str, deck_oids: set):
        self.cmdr_oid = cmdr_oid

        # Commander vectors
        self.cmdr_strats = ctx.card_strats.get(cmdr_oid, set())
        self.cmdr_strat_vec = ctx.strat_vector(cmdr_oid)
        self.cmdr_tfidf = ctx.tfidf_vector(cmdr_oid)
        self.cmdr_phases = ctx.card_phase_order.get(cmdr_oid, set())

        # Commander subtypes for tribal matching
        self.cmdr_subtypes = set()

        # Commander mechanics vectors
        self.cmdr_produces = ctx._mech_produces.get(cmdr_oid)
        self.cmdr_consumes = ctx._mech_consumes.get(cmdr_oid)

        # Commander-specific keywords: top TF-IDF words from commander oracle
        cmdr_oracle = ctx._card_oracle.get(cmdr_oid, "")
        self.cmdr_keywords = set()
        if cmdr_oracle:
            words = re.findall(r"[a-z]{3,}", cmdr_oracle)
            # Exclude common MTG boilerplate
            stop = {"the", "and", "for", "you", "your", "each", "that", "this",
                    "its", "with", "from", "into", "than", "may", "can", "all",
                    "are", "has", "have", "one", "any", "other", "whenever",
                    "target", "card", "cards", "creature", "creatures", "spell",
                    "player", "players", "control", "controller", "opponent",
                    "opponents", "permanent", "permanents", "ability", "turn",
                    "end", "beginning", "step", "phase", "until", "put", "get",
                    "gets", "would", "instead", "also", "number", "choose",
                    "chosen", "where", "another", "then", "there", "their",
                    "they", "them", "already", "first", "give", "those", "been",
                    "being", "does", "only", "when", "more", "less", "don",
                    "among", "onto", "plus", "minus", "equal", "least", "many",
                    "much", "nor", "not", "either", "both", "every", "most",
                    "some", "such", "own", "over", "under", "before", "after"}
            # Also filter commander's own name words
            cmdr_name_parts = set(re.findall(r"[a-z]{3,}", cmdr_oid[:36]))  # won't match oid
            # Get commander name
            cmdr_name_row = ctx.conn.execute(
                "SELECT name FROM cards WHERE oracle_id = ?", (cmdr_oid,)).fetchone()
            if cmdr_name_row:
                cmdr_name_parts = set(re.findall(r"[a-z]{3,}", cmdr_name_row[0].lower()))
            self.cmdr_keywords = {w for w in set(words) if w not in stop and w not in cmdr_name_parts}

        if ctx._has_edge_index:
            self._init_from_index(ctx, cmdr_oid, deck_oids)
        else:
            self._init_from_db(ctx.conn, ctx.oid_to_idx, cmdr_oid, deck_oids)

    def _init_from_index(self, ctx, cmdr_oid, deck_oids):
        """Fast path: use pre-loaded edge index for deck edges, DB for commander edges."""
        conn = ctx.conn
        oid_to_idx = ctx.oid_to_idx
        idx_to_oid = ctx._idx_to_oid
        cmdr_idx = oid_to_idx.get(cmdr_oid)

        # Commander strength + events: fast indexed DB queries (~1ms each)
        self.cmdr_out = {}
        self.cmdr_in = {}
        self.cmdr_out_events = {}
        self.cmdr_in_events = {}
        try:
            for row in conn.execute(
                "SELECT target_id, SUM(strength), GROUP_CONCAT(DISTINCT json_extract(detail, '$.event')) "
                "FROM interaction_edges WHERE source_id = ? GROUP BY target_id", (cmdr_oid,)):
                self.cmdr_out[row[0]] = row[1]
                self.cmdr_out_events[row[0]] = set(row[2].split(",")) if row[2] else set()
            for row in conn.execute(
                "SELECT source_id, SUM(strength), GROUP_CONCAT(DISTINCT json_extract(detail, '$.event')) "
                "FROM interaction_edges WHERE target_id = ? GROUP BY source_id", (cmdr_oid,)):
                self.cmdr_in[row[0]] = row[1]
                self.cmdr_in_events[row[0]] = set(row[2].split(",")) if row[2] else set()
        except Exception:
            pass

        # Commander exact edges from adjacency index
        self.cmdr_exact = set()
        if cmdr_idx is not None:
            for tgt_idx in ctx._exact_out.get(cmdr_idx, np.array([], dtype=np.int32)):
                oid = idx_to_oid.get(int(tgt_idx))
                if oid:
                    self.cmdr_exact.add(oid)
            for src_idx in ctx._exact_in.get(cmdr_idx, np.array([], dtype=np.int32)):
                oid = idx_to_oid.get(int(src_idx))
                if oid:
                    self.cmdr_exact.add(oid)

        # Deck edge counts via numpy set intersection
        self.deck_edge_counts = {}
        self.deck_exact_counts = {}
        self.deck_broad_counts = {}

        if deck_oids:
            deck_indices = set()
            for oid in deck_oids:
                idx = oid_to_idx.get(oid)
                if idx is not None:
                    deck_indices.add(idx)

            if deck_indices:
                n_cards = ctx._n_cards_idx
                counts = np.zeros(n_cards, dtype=np.int32)
                exact_counts = np.zeros(n_cards, dtype=np.int32)

                for d_idx in deck_indices:
                    # Outgoing: deck card → candidate (distinct deck cards counted)
                    out_neighbors = ctx._adj_out.get(d_idx)
                    if out_neighbors is not None:
                        counts[out_neighbors] += 1
                    # Incoming: candidate → deck card
                    in_neighbors = ctx._adj_in.get(d_idx)
                    if in_neighbors is not None:
                        counts[in_neighbors] += 1
                    # Exact outgoing
                    exact_out = ctx._exact_out.get(d_idx)
                    if exact_out is not None:
                        exact_counts[exact_out] += 1
                    # Exact incoming
                    exact_in = ctx._exact_in.get(d_idx)
                    if exact_in is not None:
                        exact_counts[exact_in] += 1

                # Convert to dicts (only non-zero entries)
                nonzero = np.nonzero(counts)[0]
                for i in nonzero:
                    oid = idx_to_oid.get(int(i))
                    if oid:
                        c = int(counts[i])
                        self.deck_edge_counts[oid] = c
                        ec = int(exact_counts[i])
                        if ec > 0:
                            self.deck_exact_counts[oid] = ec
                        bc = c - ec
                        if bc > 0:
                            self.deck_broad_counts[oid] = bc

    def _init_from_db(self, conn, oid_to_idx, cmdr_oid, deck_oids):
        """Original DB query path (used for inference with small datasets)."""
        # Causal edges from/to commander
        self.cmdr_out = {}
        self.cmdr_in = {}
        self.cmdr_out_events = {}
        self.cmdr_in_events = {}
        try:
            for row in conn.execute(
                "SELECT target_id, SUM(strength), GROUP_CONCAT(DISTINCT json_extract(detail, '$.event')) "
                "FROM interaction_edges WHERE source_id = ? GROUP BY target_id", (cmdr_oid,)):
                self.cmdr_out[row[0]] = row[1]
                self.cmdr_out_events[row[0]] = set(row[2].split(",")) if row[2] else set()
            for row in conn.execute(
                "SELECT source_id, SUM(strength), GROUP_CONCAT(DISTINCT json_extract(detail, '$.event')) "
                "FROM interaction_edges WHERE target_id = ? GROUP BY source_id", (cmdr_oid,)):
                self.cmdr_in[row[0]] = row[1]
                self.cmdr_in_events[row[0]] = set(row[2].split(",")) if row[2] else set()
        except Exception:
            pass

        # Deck edge counts + precision
        self.deck_edge_counts = {}
        self.deck_exact_counts = {}
        self.deck_broad_counts = {}
        if deck_oids:
            dl = list(deck_oids)
            for i in range(0, len(dl), 500):
                chunk = dl[i:i + 500]
                ph = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT target_id, COUNT(DISTINCT source_id) FROM interaction_edges "
                    f"WHERE source_id IN ({ph}) GROUP BY target_id", chunk):
                    self.deck_edge_counts[row[0]] = self.deck_edge_counts.get(row[0], 0) + row[1]
                for row in conn.execute(
                    f"SELECT source_id, COUNT(DISTINCT target_id) FROM interaction_edges "
                    f"WHERE target_id IN ({ph}) GROUP BY source_id", chunk):
                    self.deck_edge_counts[row[0]] = self.deck_edge_counts.get(row[0], 0) + row[1]
                for row in conn.execute(
                    f"SELECT target_id, COALESCE(filter_precision, json_extract(detail, '$.filter_precision')), COUNT(*) "
                    f"FROM interaction_edges WHERE source_id IN ({ph}) "
                    f"GROUP BY target_id, COALESCE(filter_precision, json_extract(detail, '$.filter_precision'))", chunk):
                    if row[1] == "exact":
                        self.deck_exact_counts[row[0]] = self.deck_exact_counts.get(row[0], 0) + row[2]
                    else:
                        self.deck_broad_counts[row[0]] = self.deck_broad_counts.get(row[0], 0) + row[2]
                for row in conn.execute(
                    f"SELECT source_id, COALESCE(filter_precision, json_extract(detail, '$.filter_precision')), COUNT(*) "
                    f"FROM interaction_edges WHERE target_id IN ({ph}) "
                    f"GROUP BY source_id, COALESCE(filter_precision, json_extract(detail, '$.filter_precision'))", chunk):
                    if row[1] == "exact":
                        self.deck_exact_counts[row[0]] = self.deck_exact_counts.get(row[0], 0) + row[2]
                    else:
                        self.deck_broad_counts[row[0]] = self.deck_broad_counts.get(row[0], 0) + row[2]

        # Commander exact edges
        self.cmdr_exact = set()
        try:
            for row in conn.execute(
                "SELECT target_id FROM interaction_edges WHERE source_id = ? "
                "AND COALESCE(filter_precision, json_extract(detail, '$.filter_precision')) = 'exact'", (cmdr_oid,)):
                self.cmdr_exact.add(row[0])
            for row in conn.execute(
                "SELECT source_id FROM interaction_edges WHERE target_id = ? "
                "AND COALESCE(filter_precision, json_extract(detail, '$.filter_precision')) = 'exact'", (cmdr_oid,)):
                self.cmdr_exact.add(row[0])
        except Exception:
            pass


def compute_card_features(card_oid: str, card_type_line: str, card_cmc: float,
                          tower_prob: float,
                          ctx: ForgeFeatureContext, cmdr: CmdrFeatureContext) -> list:
    """Compute the 25-feature vector for a single (commander, card) pair.

    Returns a list of 25 floats matching FORGE_FEATURE_NAMES order.
    """
    out_s = cmdr.cmdr_out.get(card_oid, 0.0)
    in_s = cmdr.cmdr_in.get(card_oid, 0.0)
    tl = card_type_line

    # F0: tower_forge — binary indicator (tower already used for pre-filter;
    # continuous value hurts zero-tower cards which are 90% of candidates)
    f0 = 1.0 if tower_prob > 0.001 else 0.0

    # F1: embedding_cosine
    ci = ctx.oid_to_idx.get(cmdr.cmdr_oid)
    di = ctx.oid_to_idx.get(card_oid)
    f1 = float(np.dot(ctx.normed_emb[ci].astype(np.float32),
                       ctx.normed_emb[di].astype(np.float32))) if ci is not None and di is not None else 0.0

    # F2-F5: causal features
    ev_out = cmdr.cmdr_out_events.get(card_oid, set())
    ev_in = cmdr.cmdr_in_events.get(card_oid, set())

    # F7: strategy overlap
    c_strats = ctx.card_strats.get(card_oid, set())

    # F8: strategy cosine
    csv = ctx.strat_vector(card_oid)
    strat_cos = 0.0
    if cmdr.cmdr_strat_vec is not None and csv is not None:
        d = float(np.dot(cmdr.cmdr_strat_vec, csv))
        nc = float(np.linalg.norm(cmdr.cmdr_strat_vec))
        nd = float(np.linalg.norm(csv))
        strat_cos = d / (nc * nd) if nc > 0 and nd > 0 else 0.0

    # F9: oracle similarity
    ct = ctx.tfidf_vector(card_oid)
    oracle_sim = float(np.dot(cmdr.cmdr_tfidf, ct)) if cmdr.cmdr_tfidf is not None and ct is not None else 0.0

    # F10: phase match
    cp = ctx.card_phase_order.get(card_oid, set())
    phase_m = 0.0
    if cmdr.cmdr_phases and cp:
        for p1 in cmdr.cmdr_phases:
            for p2 in cp:
                phase_m = max(phase_m, max(0.0, 1.0 - abs(p1 - p2) * 2.0))

    # F12: tribal match
    tribal = 0.0
    if cmdr.cmdr_subtypes and "creature" in tl.lower() and "\u2014" in tl:
        try:
            card_sub = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
            if cmdr.cmdr_subtypes & card_sub:
                tribal = 1.0
        except (IndexError, AttributeError):
            pass

    # F20: deck exact edge ratio
    n_exact = cmdr.deck_exact_counts.get(card_oid, 0)
    n_broad = cmdr.deck_broad_counts.get(card_oid, 0)
    deck_exact_ratio = n_exact / (n_exact + n_broad) if (n_exact + n_broad) > 0 else 0.0

    # F22: causal_composite — combined causal signal, denser than individual features
    causal_str = out_s + in_s
    event_div = float(len(ev_out | ev_in))
    exact_edge = 1.0 if card_oid in cmdr.cmdr_exact else 0.0
    causal_composite = min(causal_str * (1.0 + event_div) * (1.0 + exact_edge), 20.0)

    # F23: card_hub_score — total unique causal neighbors (connectedness)
    hub = 0.0
    if ctx._has_edge_index and di is not None:
        n_out = len(ctx._adj_out.get(di, []))
        n_in = len(ctx._adj_in.get(di, []))
        hub = float(min(n_out + n_in, 500)) / 100.0  # scaled 0-5
    elif di is not None:
        # Approximate from deck_edge_count when no index
        hub = float(min(cmdr.deck_edge_counts.get(card_oid, 0), 20)) / 4.0

    # F24: deck_exact_edge_count — absolute count of exact-precision deck connections
    deck_exact_abs = float(min(n_exact, 20))

    # F25: text_mentions_cmdr_type — card oracle mentions commander's creature type
    # Captures "Whenever you tap a Goblin" for Krenko, "each Vampire" for Edgar, etc.
    card_oracle = ctx._card_oracle.get(card_oid, "")
    cmdr_type_mentions = 0.0
    if cmdr.cmdr_subtypes and card_oracle:
        for subtype in cmdr.cmdr_subtypes:
            if subtype in card_oracle:
                cmdr_type_mentions += 1.0

    # F26: cmdr_text_mentions_card_type — commander oracle mentions card's types
    # Captures Sram's "Aura, Equipment, or Vehicle" for equipment/aura cards
    cmdr_oracle = ctx._card_oracle.get(cmdr.cmdr_oid, "")
    card_type_in_cmdr = 0.0
    if cmdr_oracle and "\u2014" in tl:
        try:
            card_subs = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
            for sub in card_subs:
                if len(sub) >= 3 and sub in cmdr_oracle:
                    card_type_in_cmdr += 1.0
        except (IndexError, AttributeError):
            pass
    # Also check card type categories mentioned in commander text
    for ctype in ["creature", "artifact", "enchantment", "instant", "sorcery",
                  "equipment", "aura", "vehicle", "planeswalker"]:
        if ctype in tl.lower() and ctype in cmdr_oracle:
            card_type_in_cmdr += 0.5

    # F27: shared_keyword_count — count of specific MTG keywords shared
    # between commander and card oracle texts
    shared_kw = 0.0
    if card_oracle and cmdr_oracle:
        for kw in ["token", "counter", "sacrifice", "draw", "damage", "life",
                   "graveyard", "exile", "destroy", "discard", "mill",
                   "flying", "trample", "haste", "deathtouch", "lifelink",
                   "enter", "die", "attack", "block", "tap", "untap",
                   "mana", "cast", "copy", "proliferate", "transform"]:
            if kw in card_oracle and kw in cmdr_oracle:
                shared_kw += 1.0

    # F28: cmdr_keyword_match — how many commander-specific keywords appear in card text
    cmdr_kw_match = 0.0
    if cmdr.cmdr_keywords and card_oracle:
        card_words = set(re.findall(r"[a-z]{3,}", card_oracle))
        cmdr_kw_match = float(len(cmdr.cmdr_keywords & card_words))

    # F29: anti_tribal_text — card has tribal restrictions that conflict with deck
    # Catches: "non-Human", "whenever you cast a Knight spell" in a Human deck
    anti_tribal = 0.0
    if cmdr.cmdr_subtypes and card_oracle:
        for subtype in cmdr.cmdr_subtypes:
            if f"non-{subtype}" in card_oracle or f"non{subtype}" in card_oracle:
                anti_tribal = 1.0
                break
        # Check if card's trigger condition requires a DIFFERENT type
        # e.g., "whenever you cast a Knight spell" in a Human deck
        trigger_patterns = re.findall(
            r"whenever you cast (?:a|an) (\w+) spell", card_oracle)
        trigger_patterns += re.findall(
            r"whenever (?:a|an|another) (\w+) (?:enters|dies|attacks)", card_oracle)
        for trigger_type in trigger_patterns:
            trigger_type = trigger_type.lower()
            # If trigger requires a specific creature type and it's NOT our type
            if (len(trigger_type) > 2 and trigger_type not in cmdr.cmdr_subtypes
                    and trigger_type not in {"creature", "permanent", "nontoken",
                                             "token", "artifact", "enchantment",
                                             "land", "spell", "card"}):
                anti_tribal = 1.0
                break

    # F30: mechanic_match — does card produce the commander's SPECIFIC mechanic?
    # Distinguishes "+1/+1 counter" from "gets +1/+1 until end of turn"
    mech_match = 0.0
    if cmdr_oracle and card_oracle:
        # Extract key mechanic phrases from commander text (plain string match)
        cmdr_mechs = set()
        for phrase in ["+1/+1 counter", "token", "draw a card", "lose life",
                       "gain life", "deals damage", "mill", "exile",
                       "destroy", "sacrifice", "discard", "proliferate",
                       "enters the battlefield", "dies", "graveyard",
                       "counter target"]:
            if phrase in cmdr_oracle:
                cmdr_mechs.add(phrase)
        if cmdr_mechs:
            for phrase in cmdr_mechs:
                if phrase in card_oracle:
                    mech_match += 1.0

    # F31: forge_mech_synergy — does this card PRODUCE what the commander CONSUMES?
    # Captures ALL mechanical interactions at once via dense vector dot product
    mech_fwd = 0.0  # card produces → commander consumes
    mech_rev = 0.0  # commander produces → card consumes
    card_prod = ctx._mech_produces.get(card_oid)
    card_cons = ctx._mech_consumes.get(card_oid)
    if cmdr.cmdr_consumes is not None and card_prod is not None:
        mech_fwd = float(np.dot(cmdr.cmdr_consumes, card_prod))
    if cmdr.cmdr_produces is not None and card_cons is not None:
        mech_rev = float(np.dot(cmdr.cmdr_produces, card_cons))

    return [
        f0,                                              # F0 tower_forge
        f1,                                              # F1 embedding_cosine
        min(out_s, 10.0),                                # F2 causal_cmdr_to_card
        min(in_s, 10.0),                                 # F3 causal_card_to_cmdr
        1.0 if (out_s > 0 and in_s > 0) else 0.0,       # F4 causal_bidirectional
        float(len(ev_out | ev_in)),                      # F5 causal_event_diversity
        float(min(cmdr.deck_edge_counts.get(card_oid, 0), 20)),  # F6 deck_edge_count
        float(len(cmdr.cmdr_strats & c_strats)),         # F7 strategy_overlap
        strat_cos,                                       # F8 strategy_cosine
        oracle_sim,                                      # F9 oracle_similarity
        phase_m,                                         # F10 phase_match
        1.0 if cp else 0.0,                              # F11 has_phase_trigger
        tribal,                                          # F12 tribal_match
        1.0 if "Creature" in tl else 0.0,                # F13 type_creature
        1.0 if ("Instant" in tl or "Sorcery" in tl) else 0.0,  # F14
        1.0 if "Artifact" in tl else 0.0,                # F15
        1.0 if "Enchantment" in tl else 0.0,             # F16
        1.0 if "Land" in tl else 0.0,                    # F17
        1.0 if "Planeswalker" in tl else 0.0,            # F18
        float(card_cmc),                                 # F19 cmc
        deck_exact_ratio,                                # F20 deck_exact_edge_ratio
        1.0 if card_oid in cmdr.cmdr_exact else 0.0,     # F21 cmdr_exact_edge
        causal_composite,                                # F22 causal_composite
        hub,                                             # F23 card_hub_score
        deck_exact_abs,                                  # F24 deck_exact_count
        cmdr_type_mentions,                              # F25 text_mentions_cmdr_type
        card_type_in_cmdr,                               # F26 cmdr_text_mentions_card_type
        shared_kw,                                       # F27 shared_keyword_count
        cmdr_kw_match,                                   # F28 cmdr_keyword_match
        anti_tribal,                                     # F29 anti_tribal_text
        mech_match,                                      # F30 mechanic_match
        mech_fwd,                                        # F31 forge_mech_synergy_fwd
        mech_rev,                                        # F32 forge_mech_synergy_rev
    ]
