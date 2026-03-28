"""Shared forge feature computation for training and inference.

Extracts the 38-feature forge GBM feature vector computation into a
single module used by both train_fusion_model.py and scoring.py.

No oracle-text embeddings or tower model — pure Forge-native features.
"""
import time

import numpy as np

from mtg_synergy.recommend.mechanics_vectors import _concept_idx


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

    def __init__(self, conn, preload_edges=False):
        self.conn = conn
        self._has_edge_index = False

        # Check for materialized event column (speeds up commander edge queries)
        self._has_event_col = any(
            r[1] == "event"
            for r in conn.execute("PRAGMA table_info(interaction_edges)")
        )

        # Build oid_to_idx from cards table (replaces embedding-based index)
        self.oid_to_idx = {}
        for i, (oid,) in enumerate(
            conn.execute("SELECT DISTINCT oracle_id FROM cards")
        ):
            self.oid_to_idx[oid] = i

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

        # Forge ability profiles: per-card structured data from forge_abilities
        # Replaces oracle text regex matching for features F25-F30
        import re as _re
        self._forge_profiles = {}
        # Also collect raw abilities for build_mechanics_vectors (avoids redundant DB scan)
        # Format: (oid, verb, trig_mode, trig_filter, cost, kw, token_script, counter, raw_line, amount)
        self._raw_abilities = []
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.keyword, "
            "fa.counter_type, fa.target, fa.ability_type, fa.trigger_filter, "
            "fa.cost, fa.defined, fa.raw_line, fa.token_script, fa.amount "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            # indices: 0=oid, 1=verb, 2=trig_mode, 3=trig_filter, 4=cost, 5=kw, 6=token_script, 7=counter, 8=raw_line, 9=amount
            self._raw_abilities.append((row[0], row[1], row[2], row[7], row[8], row[3], row[11], row[4], row[10], row[12]))
            oid = row[0]
            p = self._forge_profiles.setdefault(oid, {
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
            if row[1]: p['verbs'].add(row[1])
            if row[2]: p['triggers'].add(row[2])
            if row[3]: p['keywords'].add(row[3])
            if row[4]: p['counter_types'].add(row[4])
            if row[5]:
                for t in row[5].split(","):
                    main = t.split(".")[0].strip()
                    if main:
                        p['targets'].add(main)
            if row[6]: p['ability_types'].add(row[6])
            if row[7]:
                for part in row[7].split(","):
                    main = part.split(".")[0].strip()
                    if main and main != "Card" and main[0].isupper():
                        p['trigger_filters'].add(main.lower())
            # Extract subtype requirements from cost, defined, and raw_line fields
            cost_str = row[8] or ""
            defined_str = row[9] or ""
            raw_line = row[10] or ""
            # Cost subtypes: tapXType<1/Cleric>, Sac<1/Human>
            for m in _re.findall(r'tapXType<\d+/(\w+)', cost_str):
                if m not in ("CARDNAME",):
                    p['required_subtypes'].add(m.lower())
            for m in _re.findall(r'Sac<\d+/(\w+)', cost_str):
                if m not in ("CARDNAME",) and m[0].isupper():
                    p['required_subtypes'].add(m.lower())
            # Defined$ with subtype filter (e.g., TriggeredCardLKICopy.Spider)
            if "." in defined_str:
                for part in defined_str.split(","):
                    if "." in part:
                        subtype = part.split(".")[-1].strip()
                        if subtype and subtype[0].isupper() and len(subtype) > 2:
                            p['required_subtypes'].add(subtype.lower())
            # Effect target subtypes from ValidCards$ and Affected$ in raw_line
            # e.g., ValidCards$ Creature.Orc → effect only benefits Orcs
            # e.g., Affected$ Card.Human → static effect only applies to Humans
            _generic = {"card", "creature", "permanent", "self", "other",
                        "youctrl", "oppctrl", "strictlyother", "token", "nontoken"}
            for field in ('ValidCards', 'Affected'):
                m = _re.search(rf'{field}\$\s*(\S+)', raw_line)
                if m:
                    for part in m.group(1).split(","):
                        for seg in part.split("."):
                            seg = seg.split("+")[0].strip()
                            if (seg and seg[0].isupper() and len(seg) > 2
                                    and seg.lower() not in _generic):
                                p['required_subtypes'].add(seg.lower())
            # --- Granted keywords: AddKeyword$, KW$, PumpKeywords$, Keywords$ ---
            for kw_field in ('AddKeyword', 'KW', 'PumpKeywords', 'Keywords'):
                m = _re.search(rf'{kw_field}\$\s*([^|]+)', raw_line)
                if m:
                    for kw in m.group(1).split("&"):
                        kw = kw.strip()
                        # Skip HIDDEN prefixed entries and empty
                        if kw and not kw.startswith("HIDDEN"):
                            # Strip modifiers like :CardManaCost:Spell.Creature
                            kw = kw.split(":")[0].strip()
                            if kw:
                                p['granted_keywords'].add(kw.lower())
            # --- Conditions: IsPresent$, ConditionPresent$ ---
            for cond_field in ('IsPresent', 'ConditionPresent'):
                m = _re.search(rf'{cond_field}\$\s*(\S+)', raw_line)
                if m:
                    for part in m.group(1).split(","):
                        main = part.split(".")[0].split("+")[0].strip()
                        if main and main[0].isupper() and len(main) > 2:
                            p['conditions'].add(main.lower())
            # --- Duration$ ---
            m = _re.search(r'Duration\$\s*(\S+)', raw_line)
            if m:
                p['duration'].add(m.group(1).lower())
            elif row[1] in ('Pump', 'PumpAll') and 'Duration$' not in raw_line:
                # Pump without Duration → temporary buff
                p['duration'].add('temporary')
            # --- CombatDamage$ True ---
            if 'CombatDamage$ True' in raw_line:
                p['combat_damage'] = True
            # --- Effect zones: ActiveZones$, EffectZone$, AffectedZone$ ---
            for zone_field in ('ActiveZones', 'EffectZone', 'AffectedZone'):
                m = _re.search(rf'{zone_field}\$\s*(\S+)', raw_line)
                if m:
                    for z in m.group(1).split(","):
                        z = z.strip()
                        if z:
                            p['effect_zones'].add(z.lower())
            # --- Scales with: SetPower$ X or AddPower$ X ---
            for pw_field in ('SetPower', 'AddPower'):
                m = _re.search(rf'{pw_field}\$\s*(\S+)', raw_line)
                if m and m.group(1) in ('X', 'Y'):
                    p['scales_with'].add('variable_pt')
                    # Try to extract what it scales with from Description$
                    desc_m = _re.search(r'Description\$\s*(.+?)(?:\||$)', raw_line)
                    if desc_m:
                        desc = desc_m.group(1).lower()
                        if 'for each' in desc or 'equal to' in desc:
                            p['scales_with'].add('count_based')
            # --- Grants types: Types$, AddType$ ---
            for type_field in ('Types', 'AddType'):
                m = _re.search(rf'{type_field}\$\s*(\S+)', raw_line)
                if m:
                    for t in m.group(1).split(","):
                        t = t.strip()
                        if t and t[0].isupper():
                            p['grants_types'].add(t.lower())
            # --- Damage amount: NumDmg$ ---
            m = _re.search(r'NumDmg\$\s*(\S+)', raw_line)
            if m:
                val = m.group(1)
                # Keep the latest (most relevant) if multiple abilities
                if p['damage_amount'] is None or val == 'X':
                    p['damage_amount'] = val
            # --- Cards drawn: NumCards$ ---
            m = _re.search(r'NumCards\$\s*(\S+)', raw_line)
            if m:
                val = m.group(1)
                if p['cards_drawn'] is None or val == 'X':
                    p['cards_drawn'] = val
            # --- Life amount: LifeAmount$ ---
            m = _re.search(r'LifeAmount\$\s*(\S+)', raw_line)
            if m:
                val = m.group(1)
                if p['life_amount'] is None or val == 'X':
                    p['life_amount'] = val
            # --- Secondary ability ---
            if 'Secondary$ True' in raw_line:
                p['is_secondary'] = True
            # --- Gain control ---
            if 'GainControl$ True' in raw_line:
                p['gain_control'] = True

        # Pre-compute card-level mechanical unions (avoid redundant set ops in inner loop)
        self._card_mechs = {}
        for oid, p in self._forge_profiles.items():
            mechs = p['verbs'] | p['triggers'] | p['keywords']
            if mechs:
                self._card_mechs[oid] = mechs

        # Forge ability vectors: binary encoding of verbs+triggers+keywords for cosine similarity
        # Replaces oracle text TF-IDF (F9) with mechanical similarity
        all_abilities = set()
        for p in self._forge_profiles.values():
            all_abilities.update(p['verbs'])
            all_abilities.update(p['triggers'])
            all_abilities.update(p['keywords'])
        self._ability_vocab = sorted(all_abilities)
        self._ability_idx = {a: i for i, a in enumerate(self._ability_vocab)}
        self._n_abilities = len(self._ability_vocab)

        # Pre-compute normalized ability vectors per card
        self._ability_vectors = {}
        for oid, p in self._forge_profiles.items():
            v = np.zeros(self._n_abilities, dtype=np.float32)
            for a in p['verbs']:
                idx = self._ability_idx.get(a)
                if idx is not None: v[idx] = 1.0
            for a in p['triggers']:
                idx = self._ability_idx.get(a)
                if idx is not None: v[idx] = 1.0
            for a in p['keywords']:
                idx = self._ability_idx.get(a)
                if idx is not None: v[idx] = 1.0
            norm = np.linalg.norm(v)
            if norm > 0:
                self._ability_vectors[oid] = v / norm

        # Pre-load trigger zones per card (for F36)
        self._card_zones = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.trigger_zones FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_zones IS NOT NULL"
        ):
            oid, zones = row
            zset = self._card_zones.setdefault(oid, set())
            for z in zones.split(","):
                z = z.strip()
                if z:
                    zset.add(z)

        # Pre-load activated ability counts per card (for F39)
        self._activated_counts = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, COUNT(*) FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.ability_type = 'A' "
            "GROUP BY fnm.oracle_id"
        ):
            self._activated_counts[row[0]] = row[1]

        # Verb→trigger alignment mapping for F30
        self._verb_triggers = {
            "Token": {"ChangesZone", "ChangesZoneAll", "TokenCreated"},
            "ChangeZone": {"ChangesZone", "ChangesZoneAll"},
            "ChangeZoneAll": {"ChangesZone", "ChangesZoneAll"},
            "DealDamage": {"DamageDone", "DamageDoneOnce", "LifeLost"},
            "DamageAll": {"DamageDone", "DamageDoneOnce"},
            "Draw": {"Drawn"},
            "Dig": {"Drawn"},
            "PutCounter": {"CounterAdded", "CounterAddedOnce"},
            "Proliferate": {"CounterAdded", "CounterAddedOnce"},
            "GainLife": {"LifeGained"},
            "LoseLife": {"LifeLost"},
            "Destroy": {"ChangesZone"},
            "DestroyAll": {"ChangesZone"},
            "Sacrifice": {"Sacrificed", "ChangesZone"},
            "Discard": {"Discarded"},
            "Mill": {"Milled", "ChangesZone"},
            "Tap": {"Taps", "TapsForMana"},
            "Untap": {"Untaps"},
            "Counter": {"SpellCast"},
            "Mana": {"TapsForMana"},
        }

        # Forge mechanics vectors: encode each card's full mechanical profile
        # Pass pre-loaded abilities to avoid redundant forge_abilities DB scan
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        self._mech_produces, self._mech_consumes, self._mech_dim, _ = \
            build_mechanics_vectors(conn, preloaded_abilities=self._raw_abilities)
        # Free raw abilities list after mechanics vectors are built (~5MB)
        del self._raw_abilities

        if preload_edges:
            self._build_edge_index(conn)

    def _build_edge_index(self, conn):
        """Pre-load edge adjacency into memory for fast deck_edge_count computation.

        Caches raw numpy arrays to data/edge_index_cache.npz (~2s reload vs ~40s DB scan).
        Cache key: interaction_edges row count + card count.
        """
        import os
        from mtg_synergy.config import DATA_DIR

        print("  Building in-memory edge index...", flush=True)
        t0 = time.time()

        cache_path = os.path.join(DATA_DIR, "edge_index_cache.npz")
        edge_count = conn.execute("SELECT COUNT(*) FROM interaction_edges").fetchone()[0]
        card_count = len(self.oid_to_idx)

        # Try loading from cache (numpy arrays only, no pickle)
        src = tgt = exact = None
        if os.path.exists(cache_path):
            try:
                cached = np.load(cache_path)
                if (int(cached['edge_count']) == edge_count and
                    int(cached['card_count']) == card_count):
                    src = cached['src']
                    tgt = cached['tgt']
                    exact = cached['exact']
                    print(f"    Loaded {len(src):,} edges from cache ({time.time()-t0:.1f}s)")
            except Exception:
                pass

        if src is None:
            # Pre-allocate numpy arrays to avoid ~450MB Python list overhead.
            # edge_count is an upper bound (some edges may have unmapped oids).
            has_col = any(
                r[1] == "filter_precision"
                for r in conn.execute("PRAGMA table_info(interaction_edges)")
            )
            prec_expr = "filter_precision" if has_col else "json_extract(detail, '$.filter_precision')"
            src = np.empty(edge_count, dtype=np.int32)
            tgt = np.empty(edge_count, dtype=np.int32)
            exact = np.empty(edge_count, dtype=np.bool_)
            n = 0
            for row in conn.execute(
                f"SELECT source_id, target_id, {prec_expr} FROM interaction_edges"
            ):
                s = self.oid_to_idx.get(row[0])
                t = self.oid_to_idx.get(row[1])
                if s is not None and t is not None:
                    src[n] = s
                    tgt[n] = t
                    exact[n] = row[2] == "exact"
                    n += 1
            # Trim to actual count
            src = src[:n]
            tgt = tgt[:n]
            exact = exact[:n]
            print(f"    Loaded {n:,} edges from DB ({time.time()-t0:.1f}s)")
            try:
                np.savez(cache_path, src=src, tgt=tgt, exact=exact,
                         edge_count=np.array(edge_count), card_count=np.array(card_count))
                print(f"    Cached to {cache_path}")
            except Exception as e:
                print(f"    Cache write failed: {e}")

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



class CmdrFeatureContext:
    """Per-commander pre-loaded data for feature computation."""

    def __init__(self, ctx: ForgeFeatureContext, cmdr_oid: str, deck_oids: set,
                 preloaded_cmdr_edges=None):
        self.cmdr_oid = cmdr_oid

        # Commander vectors
        self.cmdr_strats = ctx.card_strats.get(cmdr_oid, set())
        self.cmdr_strat_vec = ctx.strat_vector(cmdr_oid)
        self.cmdr_ability_vec = ctx._ability_vectors.get(cmdr_oid)
        self.cmdr_phases = ctx.card_phase_order.get(cmdr_oid, set())

        # Commander subtypes for tribal matching
        self.cmdr_subtypes = set()

        # Commander mechanics vectors
        self.cmdr_produces = ctx._mech_produces.get(cmdr_oid)
        self.cmdr_consumes = ctx._mech_consumes.get(cmdr_oid)

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

        if preloaded_cmdr_edges is not None:
            if not ctx._has_edge_index:
                raise ValueError("preloaded_cmdr_edges requires preload_edges=True")
            self.cmdr_out, self.cmdr_in, self.cmdr_out_events, self.cmdr_in_events = preloaded_cmdr_edges
            self._init_cmdr_exact_and_deck_edges(ctx, cmdr_oid, deck_oids)
        elif ctx._has_edge_index:
            self._init_from_index(ctx, cmdr_oid, deck_oids)
        else:
            self._init_from_db(ctx, ctx.conn, ctx.oid_to_idx, cmdr_oid, deck_oids)

    def _init_from_index(self, ctx, cmdr_oid, deck_oids):
        """Fast path: use pre-loaded edge index for deck edges, DB for commander edges."""
        conn = ctx.conn
        event_expr = "event" if ctx._has_event_col else "json_extract(detail, '$.event')"

        # Commander strength + events: indexed DB queries
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
        except Exception:
            pass

        self._init_cmdr_exact_and_deck_edges(ctx, cmdr_oid, deck_oids)

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

    def _init_from_db(self, ctx, conn, oid_to_idx, cmdr_oid, deck_oids):
        """Original DB query path (used for inference with small datasets)."""
        event_expr = "event" if ctx._has_event_col else "json_extract(detail, '$.event')"
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
                          ctx: ForgeFeatureContext, cmdr: CmdrFeatureContext) -> list:
    """Compute the 51-feature vector for a single (commander, card) pair.

    Returns a list of 51 floats matching FORGE_FEATURE_NAMES order.
    Pure Forge-native features — no tower model or oracle-text embeddings.
    """
    out_s = cmdr.cmdr_out.get(card_oid, 0.0)
    in_s = cmdr.cmdr_in.get(card_oid, 0.0)
    tl = card_type_line

    # Card index for hub score lookup
    di = ctx.oid_to_idx.get(card_oid)

    # F0-F3: causal features
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

    # F9: forge_ability_cosine — cosine similarity of Forge ability vectors
    # (verbs + triggers + keywords). Captures "cards that DO similar mechanical
    # things" instead of "cards that SAY similar things" (old oracle TF-IDF).
    card_ability_vec = ctx._ability_vectors.get(card_oid)
    forge_ability_cos = float(np.dot(cmdr.cmdr_ability_vec, card_ability_vec)) \
        if cmdr.cmdr_ability_vec is not None and card_ability_vec is not None else 0.0

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

    # F25: forge_type_synergy — card's Forge trigger_filter or target references
    # commander's creature subtypes. Replaces oracle text substring matching.
    card_profile = ctx._forge_profiles.get(card_oid, {})
    card_trigger_types = card_profile.get('trigger_filters', set())
    card_targets = card_profile.get('targets', set())
    forge_type_syn = 0.0
    if cmdr.cmdr_subtypes:
        for subtype in cmdr.cmdr_subtypes:
            if subtype in card_trigger_types:
                forge_type_syn += 1.0
            if subtype.title() in card_targets:
                forge_type_syn += 0.5

    # F26: cmdr_forge_type_match — commander's Forge trigger_filter or target
    # references card's subtypes.
    cmdr_profile = ctx._forge_profiles.get(cmdr.cmdr_oid, {})
    cmdr_trigger_types = cmdr_profile.get('trigger_filters', set())
    cmdr_targets = cmdr_profile.get('targets', set())
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

    # F27: shared_forge_mechanics — count of shared Forge verbs, trigger_modes,
    # and keywords between commander and card.
    # Uses pre-computed unions from ctx._card_mechs and cmdr.cmdr_mechs
    cmdr_mechs = cmdr.cmdr_mechs
    card_mechs = ctx._card_mechs.get(card_oid, set())
    shared_forge = float(len(cmdr_mechs & card_mechs))

    # F28: forge_ability_depth — total distinct mechanical components
    # (verbs + triggers + keywords + counter_types). Replaces oracle text
    # keyword overlap with Forge ability richness.
    card_depth = float(len(card_profile.get('verbs', set())) +
                       len(card_profile.get('triggers', set())) +
                       len(card_profile.get('keywords', set())) +
                       len(card_profile.get('counter_types', set())))
    forge_depth = min(card_depth, 10.0)  # cap at 10

    # F29: forge_anti_tribal — card's abilities require a creature subtype
    # that conflicts with the commander's type.
    # Sources: trigger_filter (what triggers on), required_subtypes (costs, conditionals)
    # e.g., Master Apothecary requires tapping Clerics → anti-tribal for non-Cleric commanders
    # e.g., Aunt May puts counters on Spiders only → anti-tribal for non-Spider commanders
    anti_tribal = 0.0
    if cmdr.cmdr_subtypes:
        generic_types = {"card", "creature", "permanent", "nontoken",
                        "token", "artifact", "enchantment", "land",
                        "spell", "self", "other", "any"}
        # Check trigger_filters
        for tf in card_trigger_types:
            if tf not in generic_types and tf not in cmdr.cmdr_subtypes:
                anti_tribal = 1.0
                break
        # Check required_subtypes (from cost/defined fields)
        if anti_tribal == 0.0:
            req_subs = card_profile.get('required_subtypes', set())
            for rs in req_subs:
                if rs not in generic_types and rs not in cmdr.cmdr_subtypes:
                    anti_tribal = 1.0
                    break

    # F30: forge_verb_alignment — card's verbs → cmdr triggers + cmdr verbs → card triggers
    verb_align = 0.0
    card_verbs = card_profile.get('verbs', set())
    card_trigs = card_profile.get('triggers', set())
    cmdr_trigs = cmdr_profile.get('triggers', set())
    cmdr_verbs = cmdr_profile.get('verbs', set())
    for v in card_verbs:
        matching_trigs = ctx._verb_triggers.get(v, set())
        verb_align += len(matching_trigs & cmdr_trigs)
    for v in cmdr_verbs:
        matching_trigs = ctx._verb_triggers.get(v, set())
        verb_align += len(matching_trigs & card_trigs)

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

    # F33: counter_type_match — card uses same counter type as commander
    cmdr_counters = cmdr.cmdr_profile.get('counter_types', set())
    card_counters = card_profile.get('counter_types', set())
    counter_match = float(len(cmdr_counters & card_counters)) if cmdr_counters and card_counters else 0.0

    # F34: ability_type_ratio_T — card has Triggered abilities
    card_atypes = card_profile.get('ability_types', set())
    ratio_T = 1.0 if 'T' in card_atypes else 0.0

    # F35: ability_type_ratio_A — card has Activated abilities
    ratio_A = 1.0 if 'A' in card_atypes else 0.0

    # F36: zone_alignment — shared trigger zones between card and commander
    card_zones = ctx._card_zones.get(card_oid, set())
    zone_align = float(len(cmdr.cmdr_zones & card_zones)) if cmdr.cmdr_zones and card_zones else 0.0

    # F37: target_alignment — card targets what the commander produces
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

    # F38: forge_keyword_synergy — card keywords that synergize with cmdr's mechanics
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

    # F39: activated_ability_count
    activated_count = float(min(ctx._activated_counts.get(card_oid, 0), 5))

    # ── New features from comprehensive raw_line extraction ──

    # F38: granted_keyword_synergy — card grants keywords that match commander's
    # trigger_filters or existing keywords. "Grants flying" + "flying matters" cmdr
    card_granted = card_profile.get('granted_keywords', set())
    cmdr_granted = cmdr.cmdr_profile.get('granted_keywords', set())
    granted_kw_syn = 0.0
    if card_granted:
        # Card grants keywords the commander cares about (in trigger_filters)
        for gk in card_granted:
            if gk in cmdr_filter_kws:
                granted_kw_syn += 1.0
        # Card grants keywords the commander also grants → redundancy signal
        granted_kw_syn += float(len(card_granted & cmdr_granted)) * 0.5

    # F39: shared_conditions — card and commander have overlapping conditions
    # Both need "a black creature" to function → they want the same board state
    card_conds = card_profile.get('conditions', set())
    cmdr_conds = cmdr.cmdr_profile.get('conditions', set())
    shared_conds = float(len(card_conds & cmdr_conds)) if card_conds and cmdr_conds else 0.0

    # F40: is_permanent_effect — card produces permanent effects (counters) vs temporary (pump)
    # Kyler wants permanent +1/+1 counters, not "until end of turn" pumps
    card_dur = card_profile.get('duration', set())
    is_permanent = 1.0 if 'permanent' in card_dur else 0.0

    # F41: is_temporary_effect — card effects are temporary (until EOT)
    is_temporary = 1.0 if 'temporary' in card_dur else 0.0

    # F42: duration_match — card and commander share duration type
    cmdr_dur = cmdr.cmdr_profile.get('duration', set())
    duration_match = float(len(card_dur & cmdr_dur)) if card_dur and cmdr_dur else 0.0

    # F43: combat_damage_flag — card has combat damage triggers (voltron signal)
    combat_dmg = 1.0 if card_profile.get('combat_damage', False) else 0.0

    # F44: effect_zone_match — card works from zones the commander cares about
    # Cards working from graveyard + reanimator commander = synergy
    card_ezones = card_profile.get('effect_zones', set())
    cmdr_ezones = cmdr.cmdr_profile.get('effect_zones', set())
    ezone_match = float(len(card_ezones & cmdr_ezones)) if card_ezones and cmdr_ezones else 0.0

    # F45: scales_with_board — card P/T or effect scales with game state (X/Y)
    # Tribal count, devotion, etc. — these are often synergy multipliers
    scales = 1.0 if card_profile.get('scales_with', set()) else 0.0

    # F46: grants_types_match — card creates/grants creature types matching commander's
    card_gtypes = card_profile.get('grants_types', set())
    gtypes_match = 0.0
    if cmdr.cmdr_subtypes and card_gtypes:
        for gt in card_gtypes:
            if gt in cmdr.cmdr_subtypes:
                gtypes_match += 1.0

    # F47: is_secondary_trigger — card triggers on multiple events (ETB + attack)
    # More triggers = more value in synergy-heavy decks
    is_secondary = 1.0 if card_profile.get('is_secondary', False) else 0.0

    # F48: gain_control — card steals permanents
    gain_ctrl = 1.0 if card_profile.get('gain_control', False) else 0.0

    # F49: granted_keyword_count — how many keywords this card grants total
    granted_kw_count = float(min(len(card_granted), 5))

    # F50: condition_count — how many conditions this card requires
    # More conditions = more restrictive = harder to use
    condition_count = float(min(len(card_conds), 5))

    return [
        min(out_s, 10.0),                                # F0 causal_cmdr_to_card
        min(in_s, 10.0),                                 # F1 causal_card_to_cmdr
        1.0 if (out_s > 0 and in_s > 0) else 0.0,       # F2 causal_bidirectional
        float(len(ev_out | ev_in)),                      # F3 causal_event_diversity
        float(min(cmdr.deck_edge_counts.get(card_oid, 0), 20)),  # F4 deck_edge_count
        float(len(cmdr.cmdr_strats & c_strats)),         # F5 strategy_overlap
        strat_cos,                                       # F6 strategy_cosine
        forge_ability_cos,                               # F7 forge_ability_cosine
        phase_m,                                         # F8 phase_match
        1.0 if cp else 0.0,                              # F9 has_phase_trigger
        tribal,                                          # F10 tribal_match
        1.0 if "Creature" in tl else 0.0,                # F11 type_creature
        1.0 if ("Instant" in tl or "Sorcery" in tl) else 0.0,  # F12
        1.0 if "Artifact" in tl else 0.0,                # F13
        1.0 if "Enchantment" in tl else 0.0,             # F14
        1.0 if "Land" in tl else 0.0,                    # F15
        1.0 if "Planeswalker" in tl else 0.0,            # F16
        float(card_cmc),                                 # F17 cmc
        deck_exact_ratio,                                # F18 deck_exact_edge_ratio
        1.0 if card_oid in cmdr.cmdr_exact else 0.0,     # F19 cmdr_exact_edge
        causal_composite,                                # F20 causal_composite
        hub,                                             # F21 card_hub_score
        deck_exact_abs,                                  # F22 deck_exact_count
        forge_type_syn,                                  # F23 forge_type_synergy
        cmdr_type_match,                                 # F24 cmdr_forge_type_match
        shared_forge,                                    # F25 shared_forge_mechanics
        forge_depth,                                     # F26 forge_ability_depth
        anti_tribal,                                     # F27 forge_anti_tribal
        verb_align,                                      # F28 forge_verb_alignment
        mech_fwd,                                        # F29 forge_mech_synergy_fwd
        mech_rev,                                        # F30 forge_mech_synergy_rev
        counter_match,                                   # F31 counter_type_match
        ratio_T,                                         # F32 ability_type_ratio_T
        ratio_A,                                         # F33 ability_type_ratio_A
        zone_align,                                      # F34 zone_alignment
        target_align,                                    # F35 target_alignment
        kw_syn,                                          # F36 forge_keyword_synergy
        activated_count,                                 # F37 activated_ability_count
        granted_kw_syn,                                  # F38 granted_keyword_synergy
        shared_conds,                                    # F39 shared_conditions
        is_permanent,                                    # F40 is_permanent_effect
        is_temporary,                                    # F41 is_temporary_effect
        duration_match,                                  # F42 duration_match
        combat_dmg,                                      # F43 combat_damage_flag
        ezone_match,                                     # F44 effect_zone_match
        scales,                                          # F45 scales_with_board
        gtypes_match,                                    # F46 grants_types_match
        is_secondary,                                    # F47 is_secondary_trigger
        gain_ctrl,                                       # F48 gain_control
        granted_kw_count,                                # F49 granted_keyword_count
        condition_count,                                 # F50 condition_count
    ]
