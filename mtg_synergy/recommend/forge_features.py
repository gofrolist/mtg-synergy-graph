"""Shared forge feature computation for training and inference.

Extracts the 22-feature forge GBM feature vector computation into a
single module used by both train_fusion_model.py and scoring.py.
"""
import math
import re
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

    def __init__(self, conn, normed_emb, oid_to_idx):
        self.conn = conn
        self.normed_emb = normed_emb
        self.oid_to_idx = oid_to_idx

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
        conn = ctx.conn
        self.cmdr_oid = cmdr_oid

        # Commander vectors
        self.cmdr_strats = ctx.card_strats.get(cmdr_oid, set())
        self.cmdr_strat_vec = ctx.strat_vector(cmdr_oid)
        self.cmdr_tfidf = ctx.tfidf_vector(cmdr_oid)
        self.cmdr_phases = ctx.card_phase_order.get(cmdr_oid, set())

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
                # Precision breakdown
                for row in conn.execute(
                    f"SELECT target_id, json_extract(detail, '$.filter_precision'), COUNT(*) "
                    f"FROM interaction_edges WHERE source_id IN ({ph}) "
                    f"GROUP BY target_id, json_extract(detail, '$.filter_precision')", chunk):
                    if row[1] == "exact":
                        self.deck_exact_counts[row[0]] = self.deck_exact_counts.get(row[0], 0) + row[2]
                    else:
                        self.deck_broad_counts[row[0]] = self.deck_broad_counts.get(row[0], 0) + row[2]
                for row in conn.execute(
                    f"SELECT source_id, json_extract(detail, '$.filter_precision'), COUNT(*) "
                    f"FROM interaction_edges WHERE target_id IN ({ph}) "
                    f"GROUP BY source_id, json_extract(detail, '$.filter_precision')", chunk):
                    if row[1] == "exact":
                        self.deck_exact_counts[row[0]] = self.deck_exact_counts.get(row[0], 0) + row[2]
                    else:
                        self.deck_broad_counts[row[0]] = self.deck_broad_counts.get(row[0], 0) + row[2]

        # Commander exact edges
        self.cmdr_exact = set()
        try:
            for row in conn.execute(
                "SELECT target_id FROM interaction_edges WHERE source_id = ? "
                "AND json_extract(detail, '$.filter_precision') = 'exact'", (cmdr_oid,)):
                self.cmdr_exact.add(row[0])
            for row in conn.execute(
                "SELECT source_id FROM interaction_edges WHERE target_id = ? "
                "AND json_extract(detail, '$.filter_precision') = 'exact'", (cmdr_oid,)):
                self.cmdr_exact.add(row[0])
        except Exception:
            pass

        # Commander subtypes for tribal matching
        self.cmdr_subtypes = set()


def compute_card_features(card_oid: str, card_type_line: str, card_cmc: float,
                          tower_prob: float,
                          ctx: ForgeFeatureContext, cmdr: CmdrFeatureContext) -> list:
    """Compute the 22-feature vector for a single (commander, card) pair.

    Returns a list of 22 floats matching FORGE_FEATURE_NAMES order.
    """
    out_s = cmdr.cmdr_out.get(card_oid, 0.0)
    in_s = cmdr.cmdr_in.get(card_oid, 0.0)
    tl = card_type_line

    # F0: tower_forge
    f0 = tower_prob

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
    ]
