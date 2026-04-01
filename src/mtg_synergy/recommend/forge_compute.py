"""Per-commander feature context and batch/single-card feature computation.

Extracted from forge_features.py. Contains CmdrFeatureContext and all
compute_*() / _compute_*() functions for the 93-feature GBM vector.
"""
import logging
import sqlite3

import numpy as np

from mtg_synergy.recommend.forge_features import (
    ForgeFeatureContext,
    _decode_events,
    _EVENT_EXPR_COLUMN,
    _EVENT_EXPR_JSON,
    _EDHREC_FREE,
)
from mtg_synergy.recommend.mechanics_vectors import _concept_idx

_log = logging.getLogger(__name__)


class CmdrFeatureContext:
    """Per-commander pre-loaded data for feature computation."""

    def __init__(self, ctx: ForgeFeatureContext, cmdr_oid: str, deck_oids: set):
        self.cmdr_oid = cmdr_oid

        # Commander vectors
        self.cmdr_strats = ctx.card_strats.get(cmdr_oid, set())
        self.cmdr_strat_vec = ctx.strat_vector(cmdr_oid)
        self.cmdr_ability_vec = ctx._ability_vectors.get(cmdr_oid)
        self.cmdr_phases = ctx.card_phase_order.get(cmdr_oid, set())

        # Commander subtypes for tribal matching (from pre-cached type_lines)
        from mtg_synergy.config import extract_subtypes
        self.cmdr_subtypes = extract_subtypes(ctx._type_lines.get(cmdr_oid, ""))

        # Commander mechanics vectors
        self.cmdr_produces = ctx._mech_produces.get(cmdr_oid)
        self.cmdr_consumes = ctx._mech_consumes.get(cmdr_oid)

        # Commander functional fingerprint
        self.cmdr_func = ctx._func_fingerprints.get(cmdr_oid)

        # Commander deck tags (Forge's deck-building AI signals)
        self.cmdr_has = ctx._deck_has.get(cmdr_oid, set())
        self.cmdr_hints = ctx._deck_hints.get(cmdr_oid, set())
        self.cmdr_needs = ctx._deck_needs.get(cmdr_oid, set())

        # Commander zones and profile for new features F33-F39
        self.cmdr_zones = ctx._card_zones.get(cmdr_oid, set())
        self.cmdr_profile = ctx._forge_profiles.get(cmdr_oid, {
            'verbs': set(), 'triggers': set(), 'keywords': set(),
            'counter_types': set(), 'targets': set(), 'ability_types': set(),
            'trigger_filters': set(), 'required_subtypes': set(),
            'granted_keywords': set(), 'conditions': set(),
            'duration': set(), 'combat_damage': False,
            'effect_zones': set(), 'scales_with': set(),
            'grants_types': set(), 'damage_amount': None,
            'cards_drawn': None, 'life_amount': None,
            'is_secondary': False, 'gain_control': False,
        })

        # Pre-compute compound values used by compute_card_features (F27)
        self.cmdr_mechs = (self.cmdr_profile.get('verbs', set()) |
                           self.cmdr_profile.get('triggers', set()) |
                           self.cmdr_profile.get('keywords', set()))

        # ── Commander per-category mech slices (for vectorized sub-product features) ──
        self.cmdr_cat_produces = []  # list of 1D arrays, one per category
        self.cmdr_cat_consumes = []
        for cat_dims in ctx._mech_categories:
            cat_len = len(cat_dims)
            p_slice = np.zeros(cat_len, dtype=np.float32)
            c_slice = np.zeros(cat_len, dtype=np.float32)
            if self.cmdr_produces is not None:
                for j, d in enumerate(cat_dims):
                    if d < len(self.cmdr_produces):
                        p_slice[j] = self.cmdr_produces[d]
            if self.cmdr_consumes is not None:
                for j, d in enumerate(cat_dims):
                    if d < len(self.cmdr_consumes):
                        c_slice[j] = self.cmdr_consumes[d]
            self.cmdr_cat_produces.append(p_slice)
            self.cmdr_cat_consumes.append(c_slice)

        # ── Commander verb demand: which trigger_modes does this commander respond to? ──
        cmdr_triggers = self.cmdr_profile.get('triggers', set())
        n_bits = len(ctx._demand_trigger_modes)
        self.cmdr_verb_demand_mask = np.zeros(n_bits, dtype=np.float32)
        for tm in cmdr_triggers:
            bit = ctx._demand_tm_to_bit.get(tm)
            if bit is not None:
                self.cmdr_verb_demand_mask[bit] = 1.0
        # Also check trigger_demand from the raw forge data (covers triggers
        # that don't appear in the profile 'triggers' set but do appear as trigger_mode)
        for tm in ctx._card_trigger_demand.get(cmdr_oid, set()):
            bit = ctx._demand_tm_to_bit.get(tm)
            if bit is not None:
                self.cmdr_verb_demand_mask[bit] = 1.0
        self.cmdr_has_verb_demand = bool(self.cmdr_verb_demand_mask.any())

        # ── Commander type demand: what card types does this commander's triggers want? ──
        raw_demand = ctx._card_type_demand.get(cmdr_oid, {})
        self.cmdr_type_demand = np.zeros(7, dtype=np.float32)
        if raw_demand:
            for j, t in enumerate(ctx._demand_type_names):
                self.cmdr_type_demand[j] = raw_demand.get(t, 0.0)
            # Normalize to max 1.0
            max_val = self.cmdr_type_demand.max()
            if max_val > 0:
                self.cmdr_type_demand /= max_val
        self.cmdr_has_type_demand = bool(self.cmdr_type_demand.any())

        # Tribal depth data: commander's creature-type interests from multiple sources
        # Combines trigger_filters, token subtypes, deck hints, and type_line subtypes
        cmdr_tribal_filters = set()
        generic = {"card", "creature", "permanent", "nontoken", "token",
                   "artifact", "enchantment", "land", "spell", "self", "other", "any"}
        for tf in self.cmdr_profile.get('trigger_filters', set()):
            if tf not in generic:
                cmdr_tribal_filters.add(tf)
        # Add token subtypes (Krenko creates Goblins → wants Goblins)
        cmdr_token_subs = ctx._token_subtypes.get(cmdr_oid, set())
        cmdr_tribal_filters |= cmdr_token_subs
        # Add Type$ hints from deck tags (e.g., hints Type$Goblin)
        for tag in self.cmdr_hints | self.cmdr_needs:
            if tag.startswith('Type$'):
                sub = tag[5:].lower()
                if sub not in generic and len(sub) > 2:
                    cmdr_tribal_filters.add(sub)
        # Fallback: if no specific tribal signals, use type_line subtypes
        if not cmdr_tribal_filters:
            cmdr_tribal_filters = self.cmdr_subtypes.copy()
        self.cmdr_tribal_filters = cmdr_tribal_filters

        if ctx._has_edge_index:
            self._init_from_index(ctx, cmdr_oid, deck_oids)
        else:
            self._init_from_db(ctx, ctx.conn, ctx.oid_to_idx, cmdr_oid, deck_oids)

    def _init_from_index(self, ctx, cmdr_oid, deck_oids):
        """Fast path: use pre-loaded edge index for deck edges.

        Commander strength/event dicts use in-memory agg arrays when available
        (training mode, preload_strength=True), otherwise fall back to SQL
        (inference mode, saves ~5-6 GB memory).
        """
        self.cmdr_out = {}
        self.cmdr_in = {}
        self.cmdr_out_events = {}
        self.cmdr_in_events = {}

        if ctx._agg_strength_out:
            # Training mode: in-memory aggregated dicts available
            cmdr_idx = ctx.oid_to_idx.get(cmdr_oid)
            idx_to_oid = ctx._idx_to_oid

            if cmdr_idx is not None:
                str_dict = ctx._agg_strength_out.get(cmdr_idx, {})
                evt_dict = ctx._agg_events_out.get(cmdr_idx, {})
                for tgt_idx, s in str_dict.items():
                    oid = idx_to_oid.get(tgt_idx)
                    if oid:
                        self.cmdr_out[oid] = s
                        mask = evt_dict.get(tgt_idx, 0)
                        self.cmdr_out_events[oid] = _decode_events(mask, ctx._bit_to_event)

                str_dict = ctx._agg_strength_in.get(cmdr_idx, {})
                evt_dict = ctx._agg_events_in.get(cmdr_idx, {})
                for src_idx, s in str_dict.items():
                    oid = idx_to_oid.get(src_idx)
                    if oid:
                        self.cmdr_in[oid] = s
                        mask = evt_dict.get(src_idx, 0)
                        self.cmdr_in_events[oid] = _decode_events(mask, ctx._bit_to_event)
        else:
            # Inference mode: agg dicts not built; use SQL for commander edges only
            self._init_cmdr_edges_from_db(ctx)

        self._init_cmdr_exact_and_deck_edges(ctx, cmdr_oid, deck_oids)

    def _init_cmdr_edges_from_db(self, ctx):
        """SQL fallback for commander strength/event edges (inference mode)."""
        conn = ctx.conn
        event_expr = _EVENT_EXPR_COLUMN if ctx._has_event_col else _EVENT_EXPR_JSON
        try:
            for row in conn.execute(
                f"SELECT target_id, SUM(strength), GROUP_CONCAT(DISTINCT {event_expr}) "
                "FROM interaction_edges WHERE source_id = ? GROUP BY target_id",
                (self.cmdr_oid,)):
                self.cmdr_out[row[0]] = row[1]
                self.cmdr_out_events[row[0]] = set(row[2].split(",")) if row[2] else set()
            for row in conn.execute(
                f"SELECT source_id, SUM(strength), GROUP_CONCAT(DISTINCT {event_expr}) "
                "FROM interaction_edges WHERE target_id = ? GROUP BY source_id",
                (self.cmdr_oid,)):
                self.cmdr_in[row[0]] = row[1]
                self.cmdr_in_events[row[0]] = set(row[2].split(",")) if row[2] else set()
        except sqlite3.OperationalError as e:
            _log.warning("Commander edge query failed for %s: %s", self.cmdr_oid, e)

    def _init_cmdr_exact_and_deck_edges(self, ctx, cmdr_oid, deck_oids):
        """Compute commander exact edges and deck edge counts from in-memory index."""
        oid_to_idx = ctx.oid_to_idx
        idx_to_oid = ctx._idx_to_oid
        cmdr_idx = oid_to_idx.get(cmdr_oid)

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

        # ── 2-hop graph features: commander → intermediary → candidate ──
        # Count how many of the commander's direct causal neighbors also connect
        # to each candidate. Available during both training and inference.
        self.cmdr_2hop_counts = {}
        if ctx._has_edge_index and cmdr_idx is not None:
            # Get commander's direct out-neighbors (indices)
            cmdr_out_indices = set()
            if ctx._agg_strength_out:
                cmdr_out_indices = set(ctx._agg_strength_out.get(cmdr_idx, {}).keys())
            else:
                for oid in self.cmdr_out:
                    idx = oid_to_idx.get(oid)
                    if idx is not None:
                        cmdr_out_indices.add(idx)

            if cmdr_out_indices:
                n_cards = ctx._n_cards_idx
                hop2_counts = np.zeros(n_cards, dtype=np.int32)
                for x_idx in cmdr_out_indices:
                    out_neighbors = ctx._adj_out.get(x_idx)
                    if out_neighbors is not None:
                        hop2_counts[out_neighbors] += 1
                nonzero = np.nonzero(hop2_counts)[0]
                for i in nonzero:
                    if i == cmdr_idx:
                        continue
                    oid = idx_to_oid.get(int(i))
                    if oid:
                        self.cmdr_2hop_counts[oid] = int(hop2_counts[i])

        # Zone interaction flags
        self.cmdr_zone_graveyard = self.cmdr_oid in ctx._zone_graveyard
        self.cmdr_zone_exile = self.cmdr_oid in ctx._zone_exile

    def _init_from_db(self, ctx, conn, oid_to_idx, cmdr_oid, deck_oids):
        """Original DB query path (used for inference with small datasets)."""
        event_expr = _EVENT_EXPR_COLUMN if ctx._has_event_col else _EVENT_EXPR_JSON
        # Causal edges from/to commander
        self.cmdr_out = {}
        self.cmdr_in = {}
        self.cmdr_out_events = {}
        self.cmdr_in_events = {}
        try:
            for row in conn.execute(
                f"SELECT target_id, SUM(strength), GROUP_CONCAT(DISTINCT {event_expr}) "
                "FROM interaction_edges WHERE source_id = ? GROUP BY target_id", (cmdr_oid,)):
                self.cmdr_out[row[0]] = row[1]
                self.cmdr_out_events[row[0]] = set(row[2].split(",")) if row[2] else set()
            for row in conn.execute(
                f"SELECT source_id, SUM(strength), GROUP_CONCAT(DISTINCT {event_expr}) "
                "FROM interaction_edges WHERE target_id = ? GROUP BY source_id", (cmdr_oid,)):
                self.cmdr_in[row[0]] = row[1]
                self.cmdr_in_events[row[0]] = set(row[2].split(",")) if row[2] else set()
        except sqlite3.OperationalError as e:
            _log.warning("Commander edge query failed for %s: %s", cmdr_oid, e)

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
        except sqlite3.OperationalError as e:
            _log.warning("Commander exact edge query failed for %s: %s", cmdr_oid, e)

        # 2-hop counts (DB path — skip, too expensive for SQL)
        self.cmdr_2hop_counts = {}

        # Zone interaction flags
        self.cmdr_zone_graveyard = self.cmdr_oid in ctx._zone_graveyard
        self.cmdr_zone_exile = self.cmdr_oid in ctx._zone_exile


def _batch_gather_cmdr_arrays(ctx, cmdr):
    """Convert commander edge dicts to dense arrays for vectorized indexing."""
    oid_to_idx = ctx.oid_to_idx
    n_total = len(oid_to_idx)
    cmdr_out_arr = np.zeros(n_total, dtype=np.float32)
    cmdr_in_arr = np.zeros(n_total, dtype=np.float32)
    deck_edge_arr = np.zeros(n_total, dtype=np.float32)
    deck_exact_arr = np.zeros(n_total, dtype=np.float32)
    deck_broad_arr = np.zeros(n_total, dtype=np.float32)
    hop2_arr = np.zeros(n_total, dtype=np.float32)

    for oid, val in cmdr.cmdr_out.items():
        i = oid_to_idx.get(oid)
        if i is not None:
            cmdr_out_arr[i] = val
    for oid, val in cmdr.cmdr_in.items():
        i = oid_to_idx.get(oid)
        if i is not None:
            cmdr_in_arr[i] = val
    for oid, val in cmdr.deck_edge_counts.items():
        i = oid_to_idx.get(oid)
        if i is not None:
            deck_edge_arr[i] = val
    for oid, val in cmdr.deck_exact_counts.items():
        i = oid_to_idx.get(oid)
        if i is not None:
            deck_exact_arr[i] = val
    for oid, val in cmdr.deck_broad_counts.items():
        i = oid_to_idx.get(oid)
        if i is not None:
            deck_broad_arr[i] = val
    for oid, val in cmdr.cmdr_2hop_counts.items():
        i = oid_to_idx.get(oid)
        if i is not None:
            hop2_arr[i] = val

    # Fancy-index to get per-card values (use 0 index for invalid, will be masked)

    return cmdr_out_arr, cmdr_in_arr, deck_edge_arr, deck_exact_arr, deck_broad_arr, hop2_arr


def _batch_vectorized_features(X, N, card_oids, card_cmcs, safe_idx, valid, ctx, cmdr, cmdr_arrays):
    """Compute all vectorized array-indexed features (F0-F72 excluding per-card loop)."""
    cmdr_out_arr, cmdr_in_arr, deck_edge_arr, deck_exact_arr, deck_broad_arr, hop2_arr = cmdr_arrays

    out_s = cmdr_out_arr[safe_idx]
    in_s = cmdr_in_arr[safe_idx]
    deck_edges = deck_edge_arr[safe_idx]
    deck_exact = deck_exact_arr[safe_idx]
    deck_broad = deck_broad_arr[safe_idx]
    hop2_raw = hop2_arr[safe_idx]
    hub_score = ctx._arr_hub_score[safe_idx]
    hub_raw = ctx._arr_hub_raw[safe_idx]

    # Mask invalid cards
    out_s = np.where(valid, out_s, 0.0)
    in_s = np.where(valid, in_s, 0.0)

    # ── F0-F2: Causal features ──
    X[:, 0] = np.minimum(out_s, 10.0)                                 # causal_cmdr_to_card
    X[:, 1] = np.minimum(in_s, 10.0)                                  # causal_card_to_cmdr
    X[:, 2] = np.log2(1.0 + np.minimum(deck_edges, 50))               # deck_edge_count

    # ── F3-F4: Strategy and ability cosine (set in per-card loop below) ──
    # F3: strategy_cosine — set in loop
    # F4: forge_ability_cosine — set in loop

    # ── F5-F6: Phase features ──
    # F5: phase_match — set in loop
    X[:, 6] = ctx._arr_has_phase[safe_idx]                             # has_phase_trigger

    # ── F7: tribal_match — set in loop ──

    # ── F8: cmc ──
    X[:, 8] = card_cmcs                                                # cmc

    # ── F9-F12: Edge precision features ──
    total_prec = deck_exact + deck_broad
    safe_prec = np.where(total_prec > 0, total_prec, 1.0)
    X[:, 9] = np.where(total_prec > 0, deck_exact / safe_prec, 0.0)   # deck_exact_edge_ratio

    causal_str = out_s + in_s
    X[:, 10] = np.minimum(causal_str, 20.0)                           # causal_composite
    X[:, 11] = hub_score                                               # card_hub_score
    X[:, 12] = np.minimum(deck_exact, 20.0)                           # deck_exact_count

    # ── F13-F14: forge_type_synergy, cmdr_forge_type_match — set in loop ──

    # ── F15: forge_ability_depth ──
    X[:, 15] = ctx._arr_forge_depth[safe_idx]                          # forge_ability_depth

    # ── F16: forge_anti_tribal — set in loop ──

    # ── F17: forge_verb_alignment — set in loop ──

    # ── F18: counter_type_match — set in loop ──

    # ── F19-F20: Ability type flags ──
    X[:, 19] = ctx._arr_has_T[safe_idx]                                # ability_type_ratio_T
    X[:, 20] = ctx._arr_has_A[safe_idx]                                # ability_type_ratio_A

    # ── F21-F22: zone_alignment, target_alignment — set in loop ──

    # ── F23: forge_keyword_synergy — set in loop ──

    # ── F24: activated_ability_count ──
    X[:, 24] = ctx._arr_activated_count[safe_idx]                      # activated_ability_count

    # ── F25-F26: Duration flags ──
    X[:, 25] = ctx._arr_dur_permanent[safe_idx]                        # is_permanent_effect
    X[:, 26] = ctx._arr_dur_temporary[safe_idx]                        # is_temporary_effect

    # ── F27: duration_match — set in loop ──

    # ── F28: combat_damage_flag ──
    X[:, 28] = ctx._arr_combat_damage[safe_idx]                        # combat_damage_flag

    # ── F29: effect_zone_match — set in loop ──

    # ── F30: scales_with_board ──
    X[:, 30] = ctx._arr_scales_with[safe_idx]                          # scales_with_board

    # ── F31-F32: is_secondary_trigger, gain_control ──
    X[:, 31] = ctx._arr_is_secondary[safe_idx]                         # is_secondary_trigger
    X[:, 32] = ctx._arr_gain_control[safe_idx]                         # gain_control

    # ── F33-F34: granted_keyword_count, condition_count ──
    X[:, 33] = ctx._arr_granted_kw_count[safe_idx]                     # granted_keyword_count
    X[:, 34] = ctx._arr_condition_count[safe_idx]                      # condition_count

    # ── F35-F39: Deck tag overlaps — set in loop ──

    # ── F40-F42: Scaling flags ──
    X[:, 40] = ctx._arr_damage_scales[safe_idx]                        # damage_scales
    X[:, 41] = ctx._arr_draw_scales[safe_idx]                          # draw_scales
    X[:, 42] = ctx._arr_life_scales[safe_idx]                          # life_scales

    # ── F43-F45: Boolean flags ──
    X[:, 43] = ctx._arr_produces_mana[safe_idx]                        # produces_mana
    # F44 (granted_ability_match) computed in per-card loop (needs commander context)
    X[:, 45] = ctx._arr_token_amt_var[safe_idx]                        # token_amount_variable

    # ── F46-F49: Ability counts and token stats ──
    X[:, 46] = ctx._arr_total_abilities[safe_idx]                      # total_ability_count
    X[:, 47] = ctx._arr_triggered_count[safe_idx]                      # triggered_ability_count
    X[:, 48] = ctx._arr_token_pt[safe_idx]                             # token_power_toughness
    X[:, 49] = ctx._arr_token_kw[safe_idx]                             # token_keyword_count

    # ── F50: zone_graveyard_interact ──
    X[:, 50] = ctx._arr_zone_gy[safe_idx] * (1.0 if cmdr.cmdr_zone_graveyard else 0.0)

    # ── F51: ability_density ──
    raw_counts_uncapped = np.array([ctx._total_ability_counts.get(oid, 0) for oid in card_oids], dtype=np.float32)
    safe_cmc = np.maximum(card_cmcs, 1.0)
    X[:, 51] = np.minimum(np.where(raw_counts_uncapped > 0, raw_counts_uncapped / safe_cmc, 0.0), 5.0)

    # ── F52-F54: cmdr_needs_to_card_has, card_needs_satisfied, needs_rarity — set in loop ──

    # ── F55: put_counter_ratio ──
    n_counter = ctx._arr_n_counter_verbs[safe_idx]
    n_pump = ctx._arr_n_pump_verbs[safe_idx]
    n_buff = n_counter + n_pump
    safe_buff = np.where(n_buff > 0, n_buff, 1.0)
    X[:, 55] = np.where(n_buff > 0, n_counter / safe_buff, 0.5)       # put_counter_ratio

    # ── F56-F57: Counter interaction features ──
    cmdr_has_p1p1 = 'P1P1' in cmdr.cmdr_profile.get('counter_types', set())
    if cmdr_has_p1p1:
        X[:, 56] = np.where(n_counter > 0, 1.0, 0.0)                 # cmdr_counter_x_put_counter
        has_p1p1 = ctx._arr_has_p1p1[safe_idx]
        has_any_cv = ctx._arr_has_any_counter_verb[safe_idx]
        X[:, 57] = np.where((has_p1p1 == 0) & (has_any_cv == 0), 1.0, 0.0)  # cmdr_p1p1_card_no_counters

    # ── F58-F61: Functional fingerprint features — set in loop ──

    # ── F62-F63: 2-hop features ──
    X[:, 62] = np.log2(1.0 + np.minimum(hop2_raw, 200))               # cmdr_2hop_count
    safe_hub = np.where(hub_raw > 0, hub_raw, 1.0)
    X[:, 63] = np.minimum(np.where(hop2_raw > 0, hop2_raw / safe_hub, 0.0), 1.0)  # cmdr_2hop_ratio

    # ── F64-F67: Card quality features ──
    X[:, 64] = ctx._arr_forge_richness[safe_idx]                       # forge_ability_richness
    X[:, 65] = ctx._arr_strat_count[safe_idx]                          # card_strategy_count
    X[:, 66] = ctx._arr_deck_tag_count[safe_idx]                       # deck_tag_count
    X[:, 67] = ctx._arr_edhrec_pct[safe_idx]                           # edhrec_deck_pct

    # ── F68-F70: Tribal depth — set in loop ──

    # ── F71-F72: verb_demand_match, type_demand_match ──
    if cmdr.cmdr_has_verb_demand:
        X[:, 71] = ctx._arr_verb_supply_mask[safe_idx] @ cmdr.cmdr_verb_demand_mask
    if cmdr.cmdr_has_type_demand:
        X[:, 72] = ctx._arr_type_supply[safe_idx] @ cmdr.cmdr_type_demand


def _batch_per_card_loop(X, N, card_oids, card_indices, safe_idx, ctx, cmdr):
    """Compute per-card features requiring set operations (strategy, profile, tribal, fingerprints)."""
    cmdr_strat_vec = cmdr.cmdr_strat_vec
    cmdr_ability_vec = cmdr.cmdr_ability_vec
    cmdr_subtypes = cmdr.cmdr_subtypes
    cmdr_produces = cmdr.cmdr_produces
    cmdr_consumes = cmdr.cmdr_consumes
    cmdr_func = cmdr.cmdr_func
    cmdr_profile = ctx._forge_profiles.get(cmdr.cmdr_oid, {})
    cmdr_trigs = cmdr_profile.get('triggers', set())
    cmdr_verbs = cmdr_profile.get('verbs', set())
    cmdr_counters = cmdr.cmdr_profile.get('counter_types', set())
    cmdr_filter_kws = cmdr.cmdr_profile.get('trigger_filters', set())
    cmdr_trigger_types = cmdr_profile.get('trigger_filters', set())
    cmdr_targets = cmdr_profile.get('targets', set())
    cmdr_zones = cmdr.cmdr_zones
    cmdr_dur = cmdr.cmdr_profile.get('duration', set())
    cmdr_ezones = cmdr.cmdr_profile.get('effect_zones', set())
    cmdr_tribal_filters = cmdr.cmdr_tribal_filters

    # Concept-based target alignment setup
    cmdr_prod_types = set()
    if cmdr_produces is not None:
        for concept, target_type in [("creature_enters", "Creature"),
                                      ("artifact_enters", "Artifact"),
                                      ("enchantment_enters", "Enchantment"),
                                      ("token_created", "Creature"),
                                      ("counter_added", "Creature")]:
            idx = _concept_idx.get(concept)
            if idx is not None and cmdr_produces[idx] > 0:
                cmdr_prod_types.add(target_type)

    # Keyword-filter mapping for F23
    kw_to_filter = {
        "Flying": "flying", "Trample": "trample", "Haste": "haste",
        "Deathtouch": "deathtouch", "Lifelink": "lifelink", "Menace": "menace",
        "First Strike": "firststrike", "Double Strike": "doublestrike",
        "Hexproof": "hexproof", "Indestructible": "indestructible",
        "Vigilance": "vigilance", "Reach": "reach",
    }
    creature_idx_concept = _concept_idx.get("creature_enters")
    cmdr_makes_creatures = (cmdr_produces is not None and creature_idx_concept is not None
                            and cmdr_produces[creature_idx_concept] > 0)
    combat_kws = {"Flying", "Trample", "Haste", "Menace", "Double Strike", "First Strike"}

    # Functional fingerprint slices
    P = ForgeFeatureContext._FUNC_PRODUCES_SLICE
    R = ForgeFeatureContext._FUNC_REQUIRES_SLICE
    A = ForgeFeatureContext._FUNC_AMPLIFIES_SLICE
    _amp_to_prod = [1, 0, 5, 6]
    _req_to_prod = {0: 0, 4: 5, 5: 6, 8: 7, 10: 1}
    cmdr_prod_projected = None
    if cmdr_func is not None:
        cmdr_prod_projected = np.array([cmdr_func[P.start + i] for i in _amp_to_prod])

    generic_types = {"card", "creature", "permanent", "nontoken",
                     "token", "artifact", "enchantment", "land",
                     "spell", "self", "other", "any"}

    for row_i in range(N):
        oid = card_oids[row_i]
        ci = card_indices[row_i]
        if ci < 0:
            continue

        card_profile = ctx._forge_profiles.get(oid, {})

        # F3: strategy_cosine
        csv = ctx.strat_vector(oid)
        if cmdr_strat_vec is not None and csv is not None:
            d = float(np.dot(cmdr_strat_vec, csv))
            nc = float(np.linalg.norm(cmdr_strat_vec))
            nd = float(np.linalg.norm(csv))
            X[row_i, 3] = d / (nc * nd) if nc > 0 and nd > 0 else 0.0

        # F4: forge_ability_cosine
        card_av = ctx._ability_vectors.get(oid)
        if cmdr_ability_vec is not None and card_av is not None:
            X[row_i, 4] = float(np.dot(cmdr_ability_vec, card_av))

        # F5: phase_match
        cp = ctx.card_phase_order.get(oid, set())
        if cmdr.cmdr_phases and cp:
            best = 0.0
            for p1 in cmdr.cmdr_phases:
                for p2 in cp:
                    best = max(best, max(0.0, 1.0 - abs(p1 - p2) * 2.0))
            X[row_i, 5] = best

        # F7: tribal_match
        if cmdr_subtypes:
            card_subs = ctx._arr_card_subtypes[ci]
            if card_subs and (cmdr_subtypes & card_subs):
                X[row_i, 7] = 1.0

        # F13: forge_type_synergy
        card_trigger_types = card_profile.get('trigger_filters', set())
        card_targets = card_profile.get('targets', set())
        if cmdr_subtypes:
            fts = 0.0
            for sub in cmdr_subtypes:
                if sub in card_trigger_types:
                    fts += 1.0
                if sub.title() in card_targets:
                    fts += 0.5
            X[row_i, 13] = fts

        # F14: cmdr_forge_type_match
        tl = ctx._type_lines.get(oid, "")
        ctm = 0.0
        card_subs_lower = ctx._arr_card_subtypes[ci]
        for sub in card_subs_lower:
            if sub in cmdr_trigger_types:
                ctm += 1.0
            if sub.title() in cmdr_targets:
                ctm += 0.5
        for ctype in ("Creature", "Artifact", "Enchantment", "Instant", "Sorcery",
                       "Equipment", "Aura", "Vehicle", "Planeswalker"):
            if ctype in tl and ctype.lower() in cmdr_trigger_types:
                ctm += 0.5
        X[row_i, 14] = ctm

        # F16: forge_anti_tribal
        if cmdr_subtypes:
            anti = 0.0
            for tf in card_trigger_types:
                if tf not in generic_types and tf not in cmdr_subtypes:
                    anti = 1.0
                    break
            if anti == 0.0:
                for rs in card_profile.get('required_subtypes', set()):
                    if rs not in generic_types and rs not in cmdr_subtypes:
                        anti = 1.0
                        break
            if anti == 0.0:
                for es in card_profile.get('excluded_subtypes', set()):
                    if es in cmdr_subtypes:
                        anti = 1.0
                        break
            X[row_i, 16] = anti

        # F17: forge_verb_alignment
        card_verbs = card_profile.get('verbs', set())
        card_trigs = card_profile.get('triggers', set())
        va = 0.0
        for v in card_verbs:
            va += len(ctx._verb_triggers.get(v, set()) & cmdr_trigs)
        for v in cmdr_verbs:
            va += len(ctx._verb_triggers.get(v, set()) & card_trigs)
        X[row_i, 17] = va

        # F18: counter_type_match
        card_counters = card_profile.get('counter_types', set())
        if cmdr_counters and card_counters:
            X[row_i, 18] = float(len(cmdr_counters & card_counters))

        # F21: zone_alignment
        card_zones = ctx._card_zones.get(oid, set())
        if cmdr_zones and card_zones:
            X[row_i, 21] = float(len(cmdr_zones & card_zones))

        # F22: target_alignment
        card_tgts = card_profile.get('targets', set())
        if cmdr_prod_types and card_tgts:
            X[row_i, 22] = float(len(cmdr_prod_types & card_tgts))

        # F23: forge_keyword_synergy
        card_kws = card_profile.get('keywords', set())
        kw_syn = 0.0
        for kw in card_kws:
            ff = kw_to_filter.get(kw, kw.lower().replace(" ", ""))
            if any(ff in f for f in cmdr_filter_kws):
                kw_syn += 1.0
        if cmdr_makes_creatures:
            kw_syn += float(len(card_kws & combat_kws)) * 0.3
        X[row_i, 23] = kw_syn

        # F27: duration_match
        card_dur = card_profile.get('duration', set())
        if cmdr_dur and card_dur:
            X[row_i, 27] = float(len(card_dur & cmdr_dur))

        # F29: effect_zone_match
        card_ezones = card_profile.get('effect_zones', set())
        if cmdr_ezones and card_ezones:
            X[row_i, 29] = float(len(card_ezones & cmdr_ezones))

        # F35-F39: Deck tag overlaps
        card_has = ctx._deck_has.get(oid, set())
        card_hints = ctx._deck_hints.get(oid, set())
        card_needs = ctx._deck_needs.get(oid, set())
        X[row_i, 35] = float(len(cmdr.cmdr_hints & card_has))         # deck_hints_to_has
        X[row_i, 36] = float(len(cmdr.cmdr_has & card_hints))         # deck_has_to_hints
        X[row_i, 37] = float(len(cmdr.cmdr_has & card_needs))         # deck_needs_to_has
        X[row_i, 38] = float(len(cmdr.cmdr_has & card_has))           # deck_has_overlap
        X[row_i, 39] = float(len(cmdr.cmdr_hints & card_hints))       # deck_hints_overlap

        # F52: cmdr_needs_to_card_has
        X[row_i, 52] = float(len(cmdr.cmdr_needs & card_has))

        # F53: card_needs_satisfied
        if card_needs:
            met = len(card_needs & (cmdr.cmdr_has | cmdr.cmdr_hints))
            X[row_i, 53] = float(met) / float(len(card_needs))

        # F54: needs_rarity
        if card_needs:
            nr = 0.0
            for need_tag in card_needs:
                n_providers = len(ctx._deck_has_providers.get(need_tag, set()))
                if n_providers > 0:
                    nr += 1.0 / min(n_providers, 100)
            X[row_i, 54] = min(nr, 5.0)

        # F58-F61: Functional fingerprint features
        card_func = ctx._func_fingerprints.get(oid)
        if card_func is not None and cmdr_func is not None:
            X[row_i, 58] = float(np.dot(cmdr_prod_projected, card_func[A]))
            for req_dim, prod_dim in _req_to_prod.items():
                X[row_i, 59] += cmdr_func[R.start + req_dim] * card_func[P.start + prod_dim]
                X[row_i, 60] += card_func[R.start + req_dim] * cmdr_func[P.start + prod_dim]
            norm_c = np.linalg.norm(cmdr_func)
            norm_d = np.linalg.norm(card_func)
            if norm_c > 0 and norm_d > 0:
                X[row_i, 61] = float(np.dot(cmdr_func, card_func) / (norm_c * norm_d))

        # F68-F70: Tribal depth
        tribal_lord = 0.0
        if cmdr_subtypes:
            buff_verbs = card_verbs & {'Pump', 'PumpAll', 'PutCounter', 'PutCounterAll', 'Continuous'}
            if buff_verbs:
                card_tf = card_profile.get('trigger_filters', set())
                card_req = card_profile.get('required_subtypes', set())
                for sub in cmdr_subtypes:
                    if sub in card_tf or sub in card_req:
                        tribal_lord = 1.0
                        break
        X[row_i, 68] = tribal_lord                                     # tribal_lord_for_cmdr

        tribal_member = 0.0
        if cmdr_tribal_filters:
            card_subs = ctx._arr_card_subtypes[ci]
            if card_subs and (cmdr_tribal_filters & card_subs):
                tribal_member = 1.0
        X[row_i, 69] = tribal_member                                   # tribal_member_of_cmdr

        tribal_depth = X[row_i, 7] + tribal_lord + tribal_member
        card_tok_subs = ctx._token_subtypes.get(oid, set())
        if cmdr_subtypes and (card_tok_subs & cmdr_subtypes):
            tribal_depth += 1.0
        X[row_i, 70] = tribal_depth                                    # tribal_synergy_depth

        # F44: granted_ability_match — overlap of granted abilities/triggers
        # with commander's verbs/triggers (replaces boolean grants_abilities)
        granted = (card_profile.get('granted_ability_names', set())
                   | card_profile.get('granted_triggers', set()))
        if granted:
            cmdr_mechs_for_grant = cmdr_verbs | cmdr_trigs
            match = float(len(granted & cmdr_mechs_for_grant))
            X[row_i, 44] = min(match, 5.0)

        # F92: type_change_tribal_match — card changes types to match commander
        changes = ctx._arr_changes_type[ci]
        if changes:
            if '_all_' in changes:
                X[row_i, 92] = 1.0 if cmdr_subtypes else 0.0
            elif cmdr_subtypes and (changes & cmdr_subtypes):
                X[row_i, 92] = 1.0


def _batch_mech_subproducts(X, safe_idx, ctx, cmdr):
    """Compute F73-F88: per-category mech sub-product features (vectorized)."""

    # ── F73-F88: Per-category mech sub-product features (vectorized) ──
    # 8 categories × 2 directions (fwd + rev) = 16 features
    # fwd = cmdr_consumes[cat] · card_produces[cat]
    # rev = cmdr_produces[cat] · card_consumes[cat]
    n_cats = len(ctx._mech_categories)
    for cat_i in range(n_cats):
        cmdr_cons_slice = cmdr.cmdr_cat_consumes[cat_i]
        cmdr_prod_slice = cmdr.cmdr_cat_produces[cat_i]
        card_prod_slice = ctx._mech_cat_produces[cat_i][safe_idx]  # (N, cat_len)
        card_cons_slice = ctx._mech_cat_consumes[cat_i][safe_idx]  # (N, cat_len)
        col_fwd = 73 + cat_i * 2
        col_rev = 74 + cat_i * 2
        if cmdr_cons_slice.any():
            X[:, col_fwd] = card_prod_slice @ cmdr_cons_slice
        if cmdr_prod_slice.any():
            X[:, col_rev] = card_cons_slice @ cmdr_prod_slice


def compute_batch_features(card_oids, card_cmcs, ctx, cmdr):
    """Vectorized batch feature computation for all cards of one commander.

    Returns (N, 93) float32 numpy array. ~10x faster than per-card loop
    by replacing dict lookups with numpy array indexing.

    Args:
        card_oids: list of card oracle_id strings
        card_cmcs: numpy array of float32 CMC values (same order as card_oids)
        ctx: ForgeFeatureContext with pre-built card arrays
        cmdr: CmdrFeatureContext for the current commander
    """
    N = len(card_oids)
    X = np.zeros((N, 93), dtype=np.float32)

    # Convert card_oids to ctx indices for array lookup
    card_indices = np.array([ctx.oid_to_idx.get(oid, -1) for oid in card_oids], dtype=np.int32)
    valid = card_indices >= 0
    safe_idx = np.where(valid, card_indices, 0)

    cmdr_arrays = _batch_gather_cmdr_arrays(ctx, cmdr)
    _batch_vectorized_features(X, N, card_oids, card_cmcs, safe_idx, valid, ctx, cmdr, cmdr_arrays)
    _batch_per_card_loop(X, N, card_oids, card_indices, safe_idx, ctx, cmdr)
    _batch_mech_subproducts(X, safe_idx, ctx, cmdr)

    # ── F89-F91: New field features (vectorized) ──
    X[:, 89] = ctx._arr_affected_scope[safe_idx]       # affected_scope_ratio
    X[:, 90] = ctx._arr_pump_magnitude[safe_idx]        # pump_magnitude
    X[:, 91] = ctx._arr_pump_variable[safe_idx]         # pump_is_variable
    return X

def _compute_causal_features(card_oid, card_cmc, ctx, cmdr):
    """Compute causal scores, deck edges, hub score, 2-hop features."""
    out_s = cmdr.cmdr_out.get(card_oid, 0.0)
    in_s = cmdr.cmdr_in.get(card_oid, 0.0)
    di = ctx.oid_to_idx.get(card_oid)

    ev_out = cmdr.cmdr_out_events.get(card_oid, set())
    ev_in = cmdr.cmdr_in_events.get(card_oid, set())

    n_exact = cmdr.deck_exact_counts.get(card_oid, 0)
    n_broad = cmdr.deck_broad_counts.get(card_oid, 0)
    deck_exact_ratio = n_exact / (n_exact + n_broad) if (n_exact + n_broad) > 0 else 0.0

    causal_str = out_s + in_s
    event_div = float(len(ev_out | ev_in))
    exact_edge = 1.0 if card_oid in cmdr.cmdr_exact else 0.0
    causal_composite = min(causal_str * (1.0 + event_div) * (1.0 + exact_edge), 20.0)

    hub = 0.0
    hub_raw = 0.0
    if ctx._has_edge_index and di is not None:
        n_out = len(ctx._adj_out.get(di, []))
        n_in = len(ctx._adj_in.get(di, []))
        hub_raw = float(n_out + n_in)
        hub = np.log2(1.0 + min(n_out + n_in, 500))
    elif di is not None:
        hub = np.log2(1.0 + min(cmdr.deck_edge_counts.get(card_oid, 0), 20))

    deck_exact_abs = float(min(n_exact, 20))

    hop2_raw = cmdr.cmdr_2hop_counts.get(card_oid, 0)
    cmdr_2hop = np.log2(1.0 + min(hop2_raw, 200))
    cmdr_2hop_ratio = float(hop2_raw) / max(hub_raw, 1.0) if hop2_raw > 0 else 0.0
    cmdr_2hop_ratio = min(cmdr_2hop_ratio, 1.0)

    return (out_s, in_s, event_div, deck_exact_ratio, exact_edge,
            causal_composite, hub, deck_exact_abs, cmdr_2hop, cmdr_2hop_ratio)


def _compute_tribal_features(card_oid, card_type_line, card_profile, ctx, cmdr):
    """Compute tribal/subtype features."""
    tl = card_type_line
    tribal = 0.0
    if cmdr.cmdr_subtypes and "creature" in tl.lower() and "\u2014" in tl:
        try:
            card_sub = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
            if cmdr.cmdr_subtypes & card_sub:
                tribal = 1.0
        except (IndexError, AttributeError):
            pass

    cmdr_profile = ctx._forge_profiles.get(cmdr.cmdr_oid, {})
    cmdr_trigger_types = cmdr_profile.get('trigger_filters', set())
    cmdr_targets = cmdr_profile.get('targets', set())
    card_trigger_types = card_profile.get('trigger_filters', set())
    card_targets = card_profile.get('targets', set())

    forge_type_syn = 0.0
    if cmdr.cmdr_subtypes:
        for subtype in cmdr.cmdr_subtypes:
            if subtype in card_trigger_types:
                forge_type_syn += 1.0
            if subtype.title() in card_targets:
                forge_type_syn += 0.5

    cmdr_type_match = 0.0
    if "\u2014" in tl:
        try:
            card_subs = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
            for sub in card_subs:
                if sub in cmdr_trigger_types:
                    cmdr_type_match += 1.0
                if sub.title() in cmdr_targets:
                    cmdr_type_match += 0.5
        except (IndexError, AttributeError):
            pass
    for ctype in ["Creature", "Artifact", "Enchantment", "Instant", "Sorcery",
                  "Equipment", "Aura", "Vehicle", "Planeswalker"]:
        if ctype in tl and ctype.lower() in cmdr_trigger_types:
            cmdr_type_match += 0.5

    anti_tribal = 0.0
    if cmdr.cmdr_subtypes:
        generic_types = {"card", "creature", "permanent", "nontoken",
                        "token", "artifact", "enchantment", "land",
                        "spell", "self", "other", "any"}
        for tf in card_trigger_types:
            if tf not in generic_types and tf not in cmdr.cmdr_subtypes:
                anti_tribal = 1.0
                break
        if anti_tribal == 0.0:
            req_subs = card_profile.get('required_subtypes', set())
            for rs in req_subs:
                if rs not in generic_types and rs not in cmdr.cmdr_subtypes:
                    anti_tribal = 1.0
                    break
        if anti_tribal == 0.0:
            excl_subs = card_profile.get('excluded_subtypes', set())
            for es in excl_subs:
                if es in cmdr.cmdr_subtypes:
                    anti_tribal = 1.0
                    break

    tribal_lord = 0.0
    if cmdr.cmdr_subtypes:
        buff_verbs = card_profile.get('verbs', set()) & {
            'Pump', 'PumpAll', 'PutCounter', 'PutCounterAll', 'Continuous',
        }
        if buff_verbs:
            card_tf = card_profile.get('trigger_filters', set())
            card_req = card_profile.get('required_subtypes', set())
            for sub in cmdr.cmdr_subtypes:
                if sub in card_tf or sub in card_req:
                    tribal_lord = 1.0
                    break

    tribal_member = 0.0
    if cmdr.cmdr_tribal_filters and "Creature" in tl and "\u2014" in tl:
        try:
            card_subs = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
            if cmdr.cmdr_tribal_filters & card_subs:
                tribal_member = 1.0
        except (IndexError, AttributeError):
            pass

    tribal_depth = tribal + tribal_lord + tribal_member
    if cmdr.cmdr_subtypes:
        card_tok_subs = ctx._token_subtypes.get(card_oid, set())
        if card_tok_subs & cmdr.cmdr_subtypes:
            tribal_depth += 1.0

    return (tribal, forge_type_syn, cmdr_type_match, anti_tribal,
            tribal_lord, tribal_member, tribal_depth)


def _compute_forge_profile_features(card_oid, card_cmc, card_profile, ctx, cmdr):
    """Compute forge ability profile features."""
    cmdr_profile = ctx._forge_profiles.get(cmdr.cmdr_oid, {})
    card_verbs = card_profile.get('verbs', set())
    card_trigs = card_profile.get('triggers', set())
    cmdr_trigs = cmdr_profile.get('triggers', set())
    cmdr_verbs = cmdr_profile.get('verbs', set())

    # forge_ability_cosine
    card_ability_vec = ctx._ability_vectors.get(card_oid)
    forge_ability_cos = float(np.dot(cmdr.cmdr_ability_vec, card_ability_vec)) \
        if cmdr.cmdr_ability_vec is not None and card_ability_vec is not None else 0.0

    # phase match
    cp = ctx.card_phase_order.get(card_oid, set())
    phase_m = 0.0
    if cmdr.cmdr_phases and cp:
        for p1 in cmdr.cmdr_phases:
            for p2 in cp:
                phase_m = max(phase_m, max(0.0, 1.0 - abs(p1 - p2) * 2.0))

    # strategy cosine
    csv = ctx.strat_vector(card_oid)
    strat_cos = 0.0
    if cmdr.cmdr_strat_vec is not None and csv is not None:
        d = float(np.dot(cmdr.cmdr_strat_vec, csv))
        nc = float(np.linalg.norm(cmdr.cmdr_strat_vec))
        nd = float(np.linalg.norm(csv))
        strat_cos = d / (nc * nd) if nc > 0 and nd > 0 else 0.0

    card_depth = float(len(card_verbs) + len(card_trigs) +
                       len(card_profile.get('keywords', set())) +
                       len(card_profile.get('counter_types', set())))
    forge_depth = min(card_depth, 10.0)

    verb_align = 0.0
    for v in card_verbs:
        verb_align += len(ctx._verb_triggers.get(v, set()) & cmdr_trigs)
    for v in cmdr_verbs:
        verb_align += len(ctx._verb_triggers.get(v, set()) & card_trigs)

    cmdr_counters = cmdr.cmdr_profile.get('counter_types', set())
    card_counters = card_profile.get('counter_types', set())
    counter_match = float(len(cmdr_counters & card_counters)) if cmdr_counters and card_counters else 0.0

    card_atypes = card_profile.get('ability_types', set())
    ratio_T = 1.0 if 'T' in card_atypes else 0.0
    ratio_A = 1.0 if 'A' in card_atypes else 0.0

    card_zones = ctx._card_zones.get(card_oid, set())
    zone_align = float(len(cmdr.cmdr_zones & card_zones)) if cmdr.cmdr_zones and card_zones else 0.0

    cmdr_prod_types = set()
    if cmdr.cmdr_produces is not None:
        for concept, target_type in [("creature_enters", "Creature"),
                                      ("artifact_enters", "Artifact"),
                                      ("enchantment_enters", "Enchantment"),
                                      ("token_created", "Creature"),
                                      ("counter_added", "Creature")]:
            idx = _concept_idx.get(concept)
            if idx is not None and cmdr.cmdr_produces[idx] > 0:
                cmdr_prod_types.add(target_type)
    card_tgts = card_profile.get('targets', set())
    target_align = float(len(cmdr_prod_types & card_tgts)) if cmdr_prod_types and card_tgts else 0.0

    card_kws = card_profile.get('keywords', set())
    cmdr_filter_kws = cmdr.cmdr_profile.get('trigger_filters', set())
    kw_syn = 0.0
    kw_to_filter = {
        "Flying": "flying", "Trample": "trample", "Haste": "haste",
        "Deathtouch": "deathtouch", "Lifelink": "lifelink", "Menace": "menace",
        "First Strike": "firststrike", "Double Strike": "doublestrike",
        "Hexproof": "hexproof", "Indestructible": "indestructible",
        "Vigilance": "vigilance", "Reach": "reach",
    }
    for kw in card_kws:
        filter_form = kw_to_filter.get(kw, kw.lower().replace(" ", ""))
        if any(filter_form in f for f in cmdr_filter_kws):
            kw_syn += 1.0
    if cmdr.cmdr_produces is not None:
        creature_idx = _concept_idx.get("creature_enters")
        if creature_idx is not None and cmdr.cmdr_produces[creature_idx] > 0:
            combat_kws = {"Flying", "Trample", "Haste", "Menace", "Double Strike", "First Strike"}
            kw_syn += float(len(card_kws & combat_kws)) * 0.3

    activated_count = float(min(ctx._activated_counts.get(card_oid, 0), 5))

    card_dur = card_profile.get('duration', set())
    is_permanent = 1.0 if 'permanent' in card_dur else 0.0
    is_temporary = 1.0 if 'temporary' in card_dur else 0.0
    cmdr_dur = cmdr.cmdr_profile.get('duration', set())
    duration_match = float(len(card_dur & cmdr_dur)) if card_dur and cmdr_dur else 0.0

    combat_dmg = 1.0 if card_profile.get('combat_damage', False) else 0.0
    card_ezones = card_profile.get('effect_zones', set())
    cmdr_ezones = cmdr.cmdr_profile.get('effect_zones', set())
    ezone_match = float(len(card_ezones & cmdr_ezones)) if card_ezones and cmdr_ezones else 0.0
    scales = 1.0 if card_profile.get('scales_with', set()) else 0.0

    is_secondary = 1.0 if card_profile.get('is_secondary', False) else 0.0
    gain_ctrl = 1.0 if card_profile.get('gain_control', False) else 0.0
    card_granted = card_profile.get('granted_keywords', set())
    card_conds = card_profile.get('conditions', set())
    granted_kw_count = float(min(len(card_granted), 5))
    condition_count = float(min(len(card_conds), 5))

    dmg_amt = card_profile.get('damage_amount')
    damage_scales = 1.0 if dmg_amt in ('X', 'Y') else 0.0
    draw_amt = card_profile.get('cards_drawn')
    draw_scales = 1.0 if draw_amt in ('X', 'Y') else 0.0
    life_amt = card_profile.get('life_amount')
    life_scales = 1.0 if life_amt in ('X', 'Y') else 0.0
    produces_mana = 1.0 if card_profile.get('produces_mana', False) else 0.0
    # granted_ability_match: overlap of granted abilities/triggers with commander
    _granted = (card_profile.get('granted_ability_names', set())
                | card_profile.get('granted_triggers', set()))
    cmdr_mechs_for_grant = cmdr.cmdr_profile.get('verbs', set()) | cmdr.cmdr_profile.get('triggers', set())
    granted_ability_match = min(float(len(_granted & cmdr_mechs_for_grant)), 5.0) if _granted else 0.0
    token_amt_var = 1.0 if card_profile.get('token_amount_variable', False) else 0.0

    total_abilities = float(min(ctx._total_ability_counts.get(card_oid, 0), 15))
    triggered_count = float(min(ctx._triggered_counts.get(card_oid, 0), 10))
    token_pt = float(min(ctx._token_max_pt.get(card_oid, 0), 20))
    token_kw = float(min(ctx._token_max_kw.get(card_oid, 0), 5))

    zone_gy = 1.0 if (card_oid in ctx._zone_graveyard and cmdr.cmdr_zone_graveyard) else 0.0

    raw_count = ctx._total_ability_counts.get(card_oid, 0)
    ability_dens = float(raw_count) / max(card_cmc, 1.0) if raw_count > 0 else 0.0
    ability_dens = min(ability_dens, 5.0)

    # Counter interaction features
    n_counter = sum(1 for v in card_verbs if v in ('PutCounter', 'PutCounterAll'))
    n_pump = sum(1 for v in card_verbs if v in ('Pump', 'PumpAll'))
    n_buff = n_counter + n_pump
    put_counter_ratio = float(n_counter) / n_buff if n_buff > 0 else 0.5

    cmdr_counter_x_put = 1.0 if ('P1P1' in cmdr_counters and n_counter > 0) else 0.0
    card_has_p1p1 = card_profile.get('has_p1p1', False)
    card_counter_verbs = card_verbs & {'PutCounter', 'PutCounterAll', 'Proliferate', 'MoveCounter'}
    no_counter_for_cmdr = 1.0 if ('P1P1' in cmdr_counters and
                                   not card_has_p1p1 and
                                   not card_counter_verbs) else 0.0

    # Per-category mech sub-products
    card_prod = ctx._mech_produces.get(card_oid)
    card_cons = ctx._mech_consumes.get(card_oid)
    mech_cat_fwd = []  # 8 values
    mech_cat_rev = []  # 8 values
    for cat_dims in ctx._mech_categories:
        fwd = 0.0
        rev = 0.0
        if cmdr.cmdr_consumes is not None and card_prod is not None:
            for d in cat_dims:
                if d < len(cmdr.cmdr_consumes) and d < len(card_prod):
                    fwd += cmdr.cmdr_consumes[d] * card_prod[d]
        if cmdr.cmdr_produces is not None and card_cons is not None:
            for d in cat_dims:
                if d < len(cmdr.cmdr_produces) and d < len(card_cons):
                    rev += cmdr.cmdr_produces[d] * card_cons[d]
        mech_cat_fwd.append(fwd)
        mech_cat_rev.append(rev)

    return (strat_cos, forge_ability_cos, phase_m, cp,
            forge_depth, verb_align,
            counter_match, ratio_T, ratio_A, zone_align, target_align,
            kw_syn, activated_count,
            is_permanent, is_temporary, duration_match, combat_dmg,
            ezone_match, scales, is_secondary, gain_ctrl,
            granted_kw_count, condition_count,
            damage_scales, draw_scales, life_scales, produces_mana,
            granted_ability_match, token_amt_var,
            total_abilities, triggered_count, token_pt, token_kw,
            zone_gy, ability_dens,
            put_counter_ratio, cmdr_counter_x_put,
            no_counter_for_cmdr, mech_cat_fwd, mech_cat_rev,
            card_profile.get('affected_scope_ratio', 0.5),
            min(card_profile.get('max_pump_power', 0), 15),
            1.0 if card_profile.get('pump_is_variable', False) else 0.0,
            _type_change_tribal(card_profile, cmdr.cmdr_subtypes))


def _type_change_tribal(card_profile, cmdr_subtypes):
    """Compute type_change_tribal_match for a single card."""
    changes = card_profile.get('changes_type', set())
    if card_profile.get('grants_all_creature_types', False):
        return 1.0 if cmdr_subtypes else 0.0
    if changes and cmdr_subtypes and (changes & cmdr_subtypes):
        return 1.0
    return 0.0


def _compute_deck_tag_features(card_oid, ctx, cmdr):
    """Compute deck tag overlap features."""
    card_has = ctx._deck_has.get(card_oid, set())
    card_hints = ctx._deck_hints.get(card_oid, set())
    card_needs = ctx._deck_needs.get(card_oid, set())

    hints_to_has = float(len(cmdr.cmdr_hints & card_has))
    has_to_hints = float(len(cmdr.cmdr_has & card_hints))
    needs_to_has = float(len(cmdr.cmdr_has & card_needs))
    has_overlap = float(len(cmdr.cmdr_has & card_has))
    hints_overlap = float(len(cmdr.cmdr_hints & card_hints))
    cmdr_needs_to_has = float(len(cmdr.cmdr_needs & card_has))

    card_needs_met = 0.0
    if card_needs:
        met = len(card_needs & (cmdr.cmdr_has | cmdr.cmdr_hints))
        card_needs_met = float(met) / float(len(card_needs))

    needs_rarity = 0.0
    if card_needs:
        for need_tag in card_needs:
            n_providers = len(ctx._deck_has_providers.get(need_tag, set()))
            if n_providers > 0:
                needs_rarity += 1.0 / min(n_providers, 100)
        needs_rarity = min(needs_rarity, 5.0)

    return (hints_to_has, has_to_hints, needs_to_has, has_overlap,
            hints_overlap, cmdr_needs_to_has, card_needs_met, needs_rarity)


def _verb_demand_match(card_oid, ctx, cmdr):
    """Compute verb demand match: commander triggers on X -> card performs X."""
    if not cmdr.cmdr_has_verb_demand:
        return 0.0
    ci = ctx.oid_to_idx.get(card_oid)
    if ci is None:
        return 0.0
    return float(ctx._arr_verb_supply_mask[ci] @ cmdr.cmdr_verb_demand_mask)


def _type_demand_match(card_oid, ctx, cmdr):
    """Compute type demand match: commander wants type X -> card IS type X."""
    if not cmdr.cmdr_has_type_demand:
        return 0.0
    ci = ctx.oid_to_idx.get(card_oid)
    if ci is None:
        return 0.0
    return float(ctx._arr_type_supply[ci] @ cmdr.cmdr_type_demand)


def _compute_fingerprint_features(card_oid, ctx, cmdr):
    """Compute functional fingerprint dot product features."""
    card_func = ctx._func_fingerprints.get(card_oid)
    cmdr_func = cmdr.cmdr_func

    func_produces_amp = 0.0
    func_requires_prod = 0.0
    func_card_req_cmdr = 0.0
    func_full_cosine = 0.0

    if card_func is not None and cmdr_func is not None:
        P = ForgeFeatureContext._FUNC_PRODUCES_SLICE
        R = ForgeFeatureContext._FUNC_REQUIRES_SLICE
        A = ForgeFeatureContext._FUNC_AMPLIFIES_SLICE

        _amp_to_prod = [1, 0, 5, 6]
        cmdr_prod_projected = np.array([cmdr_func[P.start + i] for i in _amp_to_prod])
        func_produces_amp = float(np.dot(cmdr_prod_projected, card_func[A]))

        _req_to_prod = {0: 0, 4: 5, 5: 6, 8: 7, 10: 1}
        for req_dim, prod_dim in _req_to_prod.items():
            func_requires_prod += cmdr_func[R.start + req_dim] * card_func[P.start + prod_dim]
            func_card_req_cmdr += card_func[R.start + req_dim] * cmdr_func[P.start + prod_dim]

        norm_c = np.linalg.norm(cmdr_func)
        norm_d = np.linalg.norm(card_func)
        if norm_c > 0 and norm_d > 0:
            func_full_cosine = float(np.dot(cmdr_func, card_func) / (norm_c * norm_d))

    return func_produces_amp, func_requires_prod, func_card_req_cmdr, func_full_cosine


def compute_card_features(card_oid: str, card_type_line: str, card_cmc: float,
                          ctx: ForgeFeatureContext, cmdr: CmdrFeatureContext) -> list:
    """Compute the 93-feature vector for a single (commander, card) pair.

    Returns a list of 93 floats matching FORGE_FEATURE_NAMES order.
    """
    tl = card_type_line
    card_profile = ctx._forge_profiles.get(card_oid, {})
    c_strats = ctx.card_strats.get(card_oid, set())

    (out_s, in_s, event_div, deck_exact_ratio, exact_edge,
     causal_composite, hub, deck_exact_abs, cmdr_2hop, cmdr_2hop_ratio
     ) = _compute_causal_features(card_oid, card_cmc, ctx, cmdr)

    (tribal, forge_type_syn, cmdr_type_match, anti_tribal,
     tribal_lord, tribal_member, tribal_depth
     ) = _compute_tribal_features(card_oid, tl, card_profile, ctx, cmdr)

    (strat_cos, forge_ability_cos, phase_m, cp,
     forge_depth, verb_align,
     counter_match, ratio_T, ratio_A, zone_align, target_align,
     kw_syn, activated_count,
     is_permanent, is_temporary, duration_match, combat_dmg,
     ezone_match, scales, is_secondary, gain_ctrl,
     granted_kw_count, condition_count,
     damage_scales, draw_scales, life_scales, produces_mana,
     granted_ability_match, token_amt_var,
     total_abilities, triggered_count, token_pt, token_kw,
     zone_gy, ability_dens,
     put_counter_ratio, cmdr_counter_x_put,
     no_counter_for_cmdr, mech_cat_fwd, mech_cat_rev,
     affected_scope, pump_mag, pump_var, type_change_tribal
     ) = _compute_forge_profile_features(card_oid, card_cmc, card_profile, ctx, cmdr)

    (hints_to_has, has_to_hints, needs_to_has, has_overlap,
     hints_overlap, cmdr_needs_to_has, card_needs_met, needs_rarity
     ) = _compute_deck_tag_features(card_oid, ctx, cmdr)

    (func_produces_amp, func_requires_prod, func_card_req_cmdr, func_full_cosine
     ) = _compute_fingerprint_features(card_oid, ctx, cmdr)

    forge_richness = ctx._forge_richness.get(card_oid, 0.0)
    strat_count = float(min(len(c_strats), 5))
    deck_tags = ctx._deck_tag_count.get(card_oid, 0.0)
    edhrec_pct = 0.0 if _EDHREC_FREE else ctx._edhrec_deck_pct.get(card_oid, 0.0)

    return [
        min(out_s, 10.0),                                # F0 causal_cmdr_to_card
        min(in_s, 10.0),                                 # F1 causal_card_to_cmdr
        np.log2(1.0 + min(cmdr.deck_edge_counts.get(card_oid, 0), 50)),  # F2 deck_edge_count
        strat_cos,                                       # F3 strategy_cosine
        forge_ability_cos,                               # F4 forge_ability_cosine
        phase_m,                                         # F5 phase_match
        1.0 if cp else 0.0,                              # F6 has_phase_trigger
        tribal,                                          # F7 tribal_match
        float(card_cmc),                                 # F8 cmc
        deck_exact_ratio,                                # F9 deck_exact_edge_ratio
        causal_composite,                                # F10 causal_composite
        hub,                                             # F11 card_hub_score
        deck_exact_abs,                                  # F12 deck_exact_count
        forge_type_syn,                                  # F13 forge_type_synergy
        cmdr_type_match,                                 # F14 cmdr_forge_type_match
        forge_depth,                                     # F15 forge_ability_depth
        anti_tribal,                                     # F16 forge_anti_tribal
        verb_align,                                      # F17 forge_verb_alignment
        counter_match,                                   # F18 counter_type_match
        ratio_T,                                         # F19 ability_type_ratio_T
        ratio_A,                                         # F20 ability_type_ratio_A
        zone_align,                                      # F21 zone_alignment
        target_align,                                    # F22 target_alignment
        kw_syn,                                          # F23 forge_keyword_synergy
        activated_count,                                 # F24 activated_ability_count
        is_permanent,                                    # F25 is_permanent_effect
        is_temporary,                                    # F26 is_temporary_effect
        duration_match,                                  # F27 duration_match
        combat_dmg,                                      # F28 combat_damage_flag
        ezone_match,                                     # F29 effect_zone_match
        scales,                                          # F30 scales_with_board
        is_secondary,                                    # F31 is_secondary_trigger
        gain_ctrl,                                       # F32 gain_control
        granted_kw_count,                                # F33 granted_keyword_count
        condition_count,                                 # F34 condition_count
        hints_to_has,                                    # F35 deck_hints_to_has
        has_to_hints,                                    # F36 deck_has_to_hints
        needs_to_has,                                    # F37 deck_needs_to_has
        has_overlap,                                     # F38 deck_has_overlap
        hints_overlap,                                   # F39 deck_hints_overlap
        damage_scales,                                   # F40 damage_scales
        draw_scales,                                     # F41 draw_scales
        life_scales,                                     # F42 life_scales
        produces_mana,                                   # F43 produces_mana
        granted_ability_match,                           # F44 granted_ability_match
        token_amt_var,                                   # F45 token_amount_variable
        total_abilities,                                 # F46 total_ability_count
        triggered_count,                                 # F47 triggered_ability_count
        token_pt,                                        # F48 token_power_toughness
        token_kw,                                        # F49 token_keyword_count
        zone_gy,                                         # F50 zone_graveyard_interact
        ability_dens,                                    # F51 ability_density
        cmdr_needs_to_has,                               # F52 cmdr_needs_to_card_has
        card_needs_met,                                  # F53 card_needs_satisfied
        needs_rarity,                                    # F54 needs_rarity
        put_counter_ratio,                               # F55 put_counter_ratio
        cmdr_counter_x_put,                              # F56 cmdr_counter_x_put_counter
        no_counter_for_cmdr,                             # F57 cmdr_p1p1_card_no_counters
        func_produces_amp,                               # F58 func_produces_amplifies
        func_requires_prod,                              # F59 func_requires_produces
        func_card_req_cmdr,                              # F60 func_card_requires_cmdr
        func_full_cosine,                                # F61 func_full_cosine
        cmdr_2hop,                                       # F62 cmdr_2hop_count
        cmdr_2hop_ratio,                                 # F63 cmdr_2hop_ratio
        forge_richness,                                  # F64 forge_ability_richness
        strat_count,                                     # F65 card_strategy_count
        deck_tags,                                       # F66 deck_tag_count
        edhrec_pct,                                      # F67 edhrec_deck_pct
        tribal_lord,                                     # F68 tribal_lord_for_cmdr
        tribal_member,                                   # F69 tribal_member_of_cmdr
        tribal_depth,                                    # F70 tribal_synergy_depth
        _verb_demand_match(card_oid, ctx, cmdr),         # F71 verb_demand_match
        _type_demand_match(card_oid, ctx, cmdr),         # F72 type_demand_match
        mech_cat_fwd[0],                                 # F73 mech_board_fwd
        mech_cat_rev[0],                                 # F74 mech_board_rev
        mech_cat_fwd[1],                                 # F75 mech_resource_fwd
        mech_cat_rev[1],                                 # F76 mech_resource_rev
        mech_cat_fwd[2],                                 # F77 mech_disruption_fwd
        mech_cat_rev[2],                                 # F78 mech_disruption_rev
        mech_cat_fwd[3],                                 # F79 mech_tempo_fwd
        mech_cat_rev[3],                                 # F80 mech_tempo_rev
        mech_cat_fwd[4],                                 # F81 mech_utility_fwd
        mech_cat_rev[4],                                 # F82 mech_utility_rev
        mech_cat_fwd[5],                                 # F83 mech_zones_fwd
        mech_cat_rev[5],                                 # F84 mech_zones_rev
        mech_cat_fwd[6],                                 # F85 mech_themes_fwd
        mech_cat_rev[6],                                 # F86 mech_themes_rev
        mech_cat_fwd[7],                                 # F87 mech_tribal_fwd
        mech_cat_rev[7],                                 # F88 mech_tribal_rev
        affected_scope,                                  # F89 affected_scope_ratio
        pump_mag,                                        # F90 pump_magnitude
        pump_var,                                        # F91 pump_is_variable
        type_change_tribal,                              # F92 type_change_tribal_match
    ]
