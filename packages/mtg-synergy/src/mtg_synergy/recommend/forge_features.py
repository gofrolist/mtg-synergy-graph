"""Shared forge feature computation for training and inference.

89-feature GBM vector used by both train_fusion_model.py and scoring.py.
"""
import logging
import os
import re
import sqlite3
import time

import numpy as np

_log = logging.getLogger(__name__)

_EMPTY_INT32 = np.array([], dtype=np.int32)
_EMPTY_FS = frozenset()

# All forge profile field names (must match _process_forge_ability_row template)
_PROFILE_SET_FIELDS = (
    'verbs', 'triggers', 'keywords', 'counter_types', 'targets',
    'ability_types', 'trigger_filters', 'required_subtypes',
    'granted_keywords', 'conditions', 'duration', 'effect_zones',
    'excluded_subtypes', 'counter_trigger_themes', 'opponent_only_events',
    'granted_ability_names', 'granted_triggers', 'changes_type',
    'cost_types', 'raw_trigger_filters',
)
_PROFILE_BOOL_FIELDS = (
    'combat_damage', 'is_secondary', 'gain_control', 'produces_mana',
    'grants_abilities', 'token_amount_variable', 'counters_on_lands',
    'has_p1p1', 'grants_all_creature_types', 'pump_is_variable',
)
_PROFILE_ALL_SLOTS = (
    _PROFILE_SET_FIELDS + _PROFILE_BOOL_FIELDS
    + ('damage_amount', 'cards_drawn', 'life_amount')  # optional str
    + ('affected_self_count', 'affected_opp_count', 'max_pump_power')  # int
    + ('affected_scope_ratio',)  # float
)


class ForgeProfile:
    """Memory-efficient forge profile using __slots__.

    Replaces per-card dicts (~832 bytes + 58 MiB key strings for 31K cards)
    with a compact object (~320 bytes, no key storage).
    Provides dict-like .get() for backward compatibility.
    """
    __slots__ = _PROFILE_ALL_SLOTS

    def get(self, key: str, default=None):
        """Dict-compatible accessor."""
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return key in self.__slots__

    def items(self):
        """Iterate (key, value) pairs like dict.items()."""
        for k in self.__slots__:
            yield k, getattr(self, k, None)

    def keys(self):
        return iter(self.__slots__)

    @classmethod
    def from_dict(cls, d: dict, fs_cache: dict) -> 'ForgeProfile':
        """Build from a mutable profile dict, deduplicating frozensets."""
        p = cls()
        for k in _PROFILE_SET_FIELDS:
            v = d.get(k)
            if v:
                fs = frozenset(v) if isinstance(v, set) else v
                fs = fs_cache.setdefault(fs, fs)
            else:
                fs = _EMPTY_FS
            setattr(p, k, fs)
        for k in _PROFILE_BOOL_FIELDS:
            setattr(p, k, d.get(k, False))
        for k in ('damage_amount', 'cards_drawn', 'life_amount'):
            setattr(p, k, d.get(k))
        for k in ('affected_self_count', 'affected_opp_count', 'max_pump_power'):
            setattr(p, k, d.get(k, 0))
        p.affected_scope_ratio = d.get('affected_scope_ratio', 0.5)
        return p


class CSRIndex:
    """Memory-efficient adjacency index using CSR (Compressed Sparse Row) format.

    Stores the same data as dict[int, np.ndarray[int32]] but without Python
    dict/object overhead (~100 MiB savings for 30K+ keys, 18M values).
    Lookups use binary search on the sorted keys array: O(log n) where n=keys.
    """
    __slots__ = ('_keys', '_offsets', '_values')

    def __init__(self, keys: np.ndarray, offsets: np.ndarray, values: np.ndarray):
        self._keys = keys        # int32, sorted
        self._offsets = offsets   # int64, len = len(keys) + 1
        self._values = values    # int32

    def get(self, key: int, default=None) -> np.ndarray | None:
        """Look up neighbors for *key*. Returns np.ndarray view or *default*.

        Note: returns a view into the underlying values array (no copy).
        Callers must not mutate the returned array.
        """
        idx = np.searchsorted(self._keys, key)
        if idx < len(self._keys) and int(self._keys[idx]) == key:
            start, end = int(self._offsets[idx]), int(self._offsets[idx + 1])
            return self._values[start:end]
        return default

    def __contains__(self, key: int) -> bool:
        idx = np.searchsorted(self._keys, key)
        return idx < len(self._keys) and int(self._keys[idx]) == key

    def __len__(self) -> int:
        return len(self._keys)

    def total_values(self) -> int:
        """Total number of stored neighbor entries."""
        return len(self._values)

    def neighbor_count(self, key: int) -> int:
        """Return number of neighbors for *key* (0 if absent)."""
        idx = np.searchsorted(self._keys, key)
        if idx < len(self._keys) and int(self._keys[idx]) == key:
            return int(self._offsets[idx + 1]) - int(self._offsets[idx])
        return 0

    def to_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return (keys, offsets, values) arrays for serialization."""
        return self._keys, self._offsets, self._values

    @classmethod
    def empty(cls) -> 'CSRIndex':
        return cls(_EMPTY_INT32, np.array([0], dtype=np.int64), _EMPTY_INT32)

    @classmethod
    def from_dict(cls, adj: dict) -> 'CSRIndex':
        """Build from dict[int, np.ndarray]."""
        if not adj:
            return cls.empty()
        sorted_keys = np.array(sorted(adj.keys()), dtype=np.int32)
        lengths = np.array([len(adj[int(k)]) for k in sorted_keys], dtype=np.int64)
        offsets = np.zeros(len(sorted_keys) + 1, dtype=np.int64)
        np.cumsum(lengths, out=offsets[1:])
        total = int(offsets[-1])
        values = np.empty(total, dtype=np.int32)
        for i, k in enumerate(sorted_keys):
            start, end = int(offsets[i]), int(offsets[i + 1])
            values[start:end] = adj[int(k)]
        return cls(sorted_keys, offsets, values)

    @classmethod
    def from_csr_arrays(cls, keys: np.ndarray, offsets: np.ndarray,
                        values: np.ndarray) -> 'CSRIndex':
        """Build from pre-existing CSR arrays (e.g. loaded from npz cache)."""
        return cls(keys, offsets, values)


# Set EDHREC_FREE=1 to disable edhrec_deck_pct feature (pure Forge-native mode)
_EDHREC_FREE = os.environ.get("EDHREC_FREE", "") == "1"

# ── Pre-compiled regex patterns for hot-path methods ──
# Used in _process_forge_ability_row / _extract_subtypes_from_raw_line /
# _extract_keywords_and_conditions / _extract_counters_and_special /
# _build_func_fingerprints.  Avoids ~2.87M re._compile calls per run.

# ── Forge engine vocabulary normalization maps ──────────────────────────
# These two dicts normalize Forge engine event/trigger names to the canonical
# verb vocabulary used by features (mech_cosine, shared_verb_count,
# cmdr_verb_concentration, mech_density, auto-derived deck tags).
#
# They are NOT per-card rules — each entry is a stable engine-name → verb
# mapping that applies to every card using that event/trigger. Treat them as
# vocabulary, not as gameplay logic.
#
# MAINTENANCE CONTRACT: when Forge ships a new TriggerType or replacement
# Event$ value (see data/forge/forge-game/src/main/java/forge/game/trigger/
# TriggerType.java and replacement/ReplacementType.java), add an entry here
# if the new event has a meaningful canonical verb. Missing entries fail
# silently — the event simply does not contribute verb-based feature signal.

# R: replacement Event$ → canonical verb for non-opponent R: abilities.
# Lets cards like Doubling Season, Anointed Procession, Chatterfang surface
# their substitute effect verb in mech_cosine + the auto-derived deck tags.
_R_EVENT_TO_VERB = {
    "CreateToken": "Token", "AddCounter": "PutCounter",
    "Mill": "Mill", "DamageDone": "DealDamage", "Draw": "Draw",
    "GainLife": "GainLife", "LoseLife": "LoseLife",
}

# T: trigger mode (past-tense engine name) → canonical present-tense verb.
# Used by _load_deck_tags to auto-derive Ability$<verb> hints from a card's
# trigger set. Excludes ChangesZone/ChangesZoneAll (too generic — nearly all
# triggered cards would get the same hint).
_TRIGGER_STEM_TO_VERB = {
    "Sacrificed": "Sacrifice", "TokenCreated": "Token",
    "DamageDone": "DealDamage", "DamageDoneOnce": "DealDamage",
    "Drawn": "Draw", "CounterAdded": "PutCounter",
    "CounterAddedOnce": "PutCounter", "LifeGained": "GainLife",
    "LifeLost": "LoseLife", "Milled": "Mill",
    "Discarded": "Discard", "SpellCast": "SpellCast",
    "Taps": "Tap", "Untaps": "Untap",
}

# Generic type tokens that should NOT be promoted to Type$ deck hints
# (too broad to be meaningful tribal/thematic signal).
_GENERIC_TRIGGER_FILTER_TYPES = frozenset({
    "card", "creature", "permanent", "nontoken", "token",
    "artifact", "enchantment", "land", "spell", "self",
    "other", "any",
})

# Fixed field patterns (field$\s*value)
_RE_EVENT = re.compile(r'Event\$\s*(\S+)')
_RE_DURATION = re.compile(r'Duration\$\s*(\S+)')
_RE_CHANGE_TYPE = re.compile(r'ChangeType\$\s*(\S+)')
_RE_NUM_DMG = re.compile(r'NumDmg\$\s*(\S+)')
_RE_NUM_CARDS = re.compile(r'NumCards\$\s*(\S+)')
_RE_LIFE_AMOUNT = re.compile(r'LifeAmount\$\s*(\S+)')
_RE_PRODUCED = re.compile(r'Produced\$\s*(\S+)')
_RE_ADD_ABILITY = re.compile(r'AddAbility\$\s*(\S+)')
_RE_ADD_TRIGGER = re.compile(r'AddTrigger\$\s*(\S+)')
_RE_TOKEN_AMOUNT = re.compile(r'TokenAmount\$\s*(\S+)')
_RE_VALID_TGTS = re.compile(r'ValidTgts\$\s*(\S+)')
_RE_ORIGIN = re.compile(r'Origin\$\s*(\S+)')
_RE_DESTINATION = re.compile(r'Destination\$\s*(\S+)')
_RE_REPLACE_WITH = re.compile(r'ReplaceWith\$\s*(\S+)')

# Phase 1: combat trigger filter regexes — values extend up to next ' | ' or EOL.
# Unlike most field regexes (which use \S+), these may legitimately be plain
# tokens like `Creature`, so a non-greedy match to the next `|` boundary is used.
_RE_VALID_ATTACKER = re.compile(r'ValidAttacker\$\s*([^|]+?)(?:\s*\||$)')
_RE_VALID_BLOCKER = re.compile(r'ValidBlocker\$\s*([^|]+?)(?:\s*\||$)')

# Cost subtype patterns
_RE_TAP_X_TYPE = re.compile(r'tapXType<\d+/(\w+)')
_RE_SAC_TYPE = re.compile(r'Sac<\d+/(\w+)')

# Description non-X exclusion
_RE_NON_CREATURE = re.compile(r'non-(\w+)\s+creature', re.IGNORECASE)

# Loop-variable field patterns: pre-compiled per field name
# _extract_subtypes_from_raw_line: field$\s*(\S+)
_RE_SUBTYPE_FIELDS = {
    field: re.compile(rf'{field}\$\s*(\S+)')
    for field in ('ValidCards', 'ValidCard', 'ValidTgts',
                  'ValidAttackers', 'Affected', 'AddsCounters')
}
# _extract_subtypes_from_raw_line: desc_field$\s*(.+?)(?:\||$)
_RE_DESC_FIELDS = {
    field: re.compile(rf'{field}\$\s*(.+?)(?:\||$)')
    for field in ('TriggerDescription', 'Description', 'SpellDescription')
}
# _extract_keywords_and_conditions: kw_field$\s*([^|]+)
_RE_KEYWORD_FIELDS = {
    field: re.compile(rf'{field}\$\s*([^|]+)')
    for field in ('AddKeyword', 'KW', 'PumpKeywords', 'Keywords')
}
# _extract_keywords_and_conditions: cond_field$\s*(\S+)
_RE_CONDITION_FIELDS = {
    field: re.compile(rf'{field}\$\s*(\S+)')
    for field in ('IsPresent', 'ConditionPresent')
}
# _extract_keywords_and_conditions: zone_field$\s*(\S+)
_RE_ZONE_FIELDS = {
    field: re.compile(rf'{field}\$\s*(\S+)')
    for field in ('ActiveZones', 'EffectZone', 'AffectedZone')
}
# _extract_counters_and_special: pump_field$\s*([+-]?\w+)
_RE_PUMP_FIELDS = {
    field: re.compile(rf'{field}\$\s*([+-]?\w+)')
    for field in ('NumAtt', 'AddPower', 'NumDef', 'AddToughness')
}
# _build_func_fingerprints: targets field$\s*(\S+)
_RE_FP_TARGET_FIELDS = {
    field: re.compile(rf'{field}\$\s*(\S+)')
    for field in ('ValidTgts', 'Affected')
}

# ── Promoted in-function constants ──
_SELF_SCOPES = frozenset({'youctrl', 'self', 'equippedby', 'enchantedby',
                           'pairedwith', 'iscommander'})
_GENERIC_TYPES = frozenset({
    "card", "creature", "permanent", "nontoken", "token",
    "artifact", "enchantment", "land", "spell",
    "self", "other", "any",
    "youctrl", "oppctrl", "strictlyother",
})

# Known-safe SQL fragments for column access (avoids f-string injection).
# Only these values are ever interpolated into SQL — validated at each use site.
_EVENT_EXPR_COLUMN = "event"
_EVENT_EXPR_JSON = "json_extract(detail, '$.event')"
_VALID_EVENT_EXPRS = frozenset({_EVENT_EXPR_COLUMN, _EVENT_EXPR_JSON})

_PREC_EXPR_COLUMN = "filter_precision"
_PREC_EXPR_JSON = "json_extract(detail, '$.filter_precision')"
_VALID_PREC_EXPRS = frozenset({_PREC_EXPR_COLUMN, _PREC_EXPR_JSON})


def _decode_events(mask, bit_to_event):
    """Decode a uint32 bitmask to a set of event name strings."""
    result = set()
    for bit, name in bit_to_event.items():
        if mask & (1 << bit):
            result.add(name)
    return result


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

    def __init__(self, conn, preload_edges=False, preload_strength=False,
                 card_provider=None, artifact_dir=None):
        self.conn = conn
        self._has_edge_index = False
        self._bit_to_event = {}
        self._preload_strength = preload_strength
        # Store artifact dir for edge cache paths
        if artifact_dir is not None:
            self._artifact_dir = str(artifact_dir)
        else:
            from mtg_synergy.config import ARTIFACT_DIR
            self._artifact_dir = str(ARTIFACT_DIR)

        # Default to SqliteCardProvider if none provided
        if card_provider is None:
            from mtg_synergy.protocol import SqliteCardProvider
            card_provider = SqliteCardProvider(conn)
        self._card_provider = card_provider

        # Check if interaction_edges table exists and has event column
        _edge_cols = []
        try:
            _edge_cols = [r[1] for r in conn.execute("PRAGMA table_info(interaction_edges)")]
        except sqlite3.OperationalError:
            pass
        self._has_edges_table = bool(_edge_cols)
        self._has_event_col = "event" in _edge_cols

        self._load_card_data(conn)
        self._load_forge_profiles(conn)  # populates self._raw_abilities
        # Compact profiles early (before heavy allocations) to reduce peak RSS.
        # Replaces mutable sets with deduplicated frozensets, saves ~60 MiB.
        self._compact_forge_profiles()

        # Trigger filter IDF weights: log(N / freq) for each trigger_filter string
        import math
        tf_counts = {}
        for p in self._forge_profiles.values():
            for tf in p.get('raw_trigger_filters', frozenset()):
                tf_counts[tf] = tf_counts.get(tf, 0) + 1
        n_cards_tf = len(self._forge_profiles)
        self._trigger_filter_idf = {
            tf: math.log(n_cards_tf / count)
            for tf, count in tf_counts.items()
        }

        # Note: _load_functional_fingerprints does its own DB query (independent
        # of self._raw_abilities). Must stay BEFORE del self._raw_abilities below.
        self._load_functional_fingerprints(conn)
        self._load_deck_tags(conn)
        self._load_ability_vectors(conn)
        self._enrich_deck_tags_from_tokens()
        self._build_deck_has_providers()
        self._load_edhrec_stats(conn)

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

        self._load_verb_demand_data(conn)

        # Forge mechanics vectors: encode each card's full mechanical profile
        # Pass pre-loaded abilities to avoid redundant forge_abilities DB scan
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        self._mech_produces, self._mech_consumes, self._mech_dim, _, self._mech_category_dims = \
            build_mechanics_vectors(conn, preloaded_abilities=self._raw_abilities,
                                   type_lines=self._type_lines)
        # Free raw abilities list after mechanics vectors are built (~5MB)
        del self._raw_abilities

        # ── Pre-encode per-card arrays for batch feature computation ──
        self._build_card_arrays()

        if preload_edges:
            self._build_edge_index(conn)

    def _compact_forge_profiles(self):
        """Compact _forge_profiles: replace dicts with __slots__ ForgeProfile objects.

        Saves ~130 MiB by eliminating per-dict overhead (832 bytes → ~320 bytes)
        and key string storage (58 MiB for 31K × 30 keys).
        Builds a new dict to avoid peak RSS spike from overlapping old+new.
        """
        fs_cache = {_EMPTY_FS: _EMPTY_FS}
        new_profiles = {}
        for oid, p in self._forge_profiles.items():
            new_profiles[oid] = ForgeProfile.from_dict(p, fs_cache)
        self._forge_profiles = new_profiles
        _log.info("Compacted forge profiles: %d cards, %d unique frozensets cached",
                   len(self._forge_profiles), len(fs_cache))

    def _load_card_data(self, conn):
        """Load card index, type lines, strategies, and phase data."""
        # Load type_lines from CardProvider (replaces cards table queries)
        self._type_lines = self._card_provider.get_type_lines()

        # Build oid_to_idx from all known oracle_ids (sorted for deterministic ordering)
        self.oid_to_idx = {oid: i for i, oid in enumerate(sorted(self._type_lines))}

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

    def _load_forge_profiles(self, conn):
        """Load forge ability profiles and raw abilities from DB."""
        # Forge ability profiles: per-card structured data from forge_abilities
        # Replaces oracle text regex matching for features F25-F30
        self._forge_profiles = {}
        self._verb_counts = {}  # oracle_id → Counter of verb occurrences (for concentration)
        # Also collect raw abilities for build_mechanics_vectors (avoids redundant DB scan)
        # Output format consumed by mechanics_vectors.py:
        #   (oid, verb, trig_mode, trig_filter, cost, kw, token_script,
        #    counter, raw_line, amount, trigger_origin, trigger_destination, defined)
        self._raw_abilities = []
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.trigger_filter, "
            "fa.cost, fa.keyword, fa.token_script, fa.counter_type, fa.raw_line, "
            "fa.amount, fa.trigger_origin, fa.trigger_destination, "
            "fa.target, fa.ability_type, fa.defined "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            # row[0..11] + row[14] (defined) → 13-element tuple for mechanics_vectors
            self._raw_abilities.append(row[:12] + (row[14],))
            self._process_forge_ability_row(row)

        # Post-process derived fields
        for p in self._forge_profiles.values():
            # Derive grants_abilities boolean from detailed set
            p['grants_abilities'] = bool(p['granted_ability_names'])
            # Affected$ scope ratio: fraction of effects targeting self vs opponents
            a_total = p['affected_self_count'] + p['affected_opp_count']
            p['affected_scope_ratio'] = (p['affected_self_count'] / a_total
                                         if a_total > 0 else 0.5)

    def _process_forge_ability_row(self, row):
        """Process a single forge_abilities row into the profile dict."""
        # Named references for profile building (SELECT order matches tuple order)
        oid = row[0]
        verb = row[1]
        trig_mode = row[2]
        trig_filter = row[3]
        cost = row[4]
        keyword = row[5]
        # row[6] = token_script (used by _raw_abilities only)
        counter_type = row[7]
        raw_line_val = row[8]
        # row[9] = amount, row[10] = trigger_origin, row[11] = trigger_destination
        target = row[12]
        ability_type = row[13]
        defined = row[14]

        p = self._forge_profiles.setdefault(oid, {
            'verbs': set(), 'triggers': set(), 'keywords': set(),
            'counter_types': set(), 'targets': set(), 'ability_types': set(),
            'trigger_filters': set(), 'required_subtypes': set(),
            'granted_keywords': set(), 'conditions': set(),
            'duration': set(), 'combat_damage': False,
            'effect_zones': set(),
            'damage_amount': None,
            'cards_drawn': None, 'life_amount': None,
            'is_secondary': False, 'gain_control': False,
            'produces_mana': False, 'grants_abilities': False,
            'token_amount_variable': False,
            'excluded_subtypes': set(),
            'counters_on_lands': False,
            'counter_trigger_themes': set(), 'has_p1p1': False,
            'opponent_only_events': set(),
            # New fields: Affected$ scope, AddAbility$ detail, ChangeType$,
            # pump magnitude, AddTrigger$
            'affected_self_count': 0, 'affected_opp_count': 0,
            'granted_ability_names': set(), 'granted_triggers': set(),
            'changes_type': set(), 'grants_all_creature_types': False,
            'max_pump_power': 0, 'pump_is_variable': False,
            'cost_types': set(),  # sacrifice, tap, discard, exile, paylife
            'raw_trigger_filters': set(),  # full trigger_filter strings for IDF
        })
        if verb:
            p['verbs'].add(verb)
            # Track verb occurrence counts for concentration feature
            vc = self._verb_counts.setdefault(oid, {})
            vc[verb] = vc.get(verb, 0) + 1
        if trig_mode: p['triggers'].add(trig_mode)
        if keyword: p['keywords'].add(keyword)
        if counter_type:
            p['counter_types'].add(counter_type)
            if counter_type == 'P1P1':
                p['has_p1p1'] = True
        # Also check raw_line for P1P1 references (replacement effects, etbCounter)
        if not p['has_p1p1'] and raw_line_val and 'P1P1' in raw_line_val:
            p['has_p1p1'] = True
        if target:
            for t in target.split(","):
                main = t.split(".")[0].strip()
                if main:
                    p['targets'].add(main)
        if ability_type: p['ability_types'].add(ability_type)
        # Track opponent-only replacement events (e.g., Bruvac's Mill)
        # and extract effective verbs from non-opponent R: abilities
        if ability_type == 'R' and raw_line_val:
            m = _RE_EVENT.search(raw_line_val)
            if m:
                is_opp_only = ('ValidPlayer$ Player.Opponent' in raw_line_val
                               or 'ValidPlayer$ Opponent' in raw_line_val)
                if is_opp_only:
                    p['opponent_only_events'].add(m.group(1))
                else:
                    # Non-opponent R: effects → add effective verb so they appear
                    # in verb_alignment, shared_verb, verb_concentration, deck tags.
                    # E.g., Chatterfang R:Event$ CreateToken → verb=Token
                    eff_verb = _R_EVENT_TO_VERB.get(m.group(1))
                    if eff_verb:
                        p['verbs'].add(eff_verb)
                        vc = self._verb_counts.setdefault(oid, {})
                        vc[eff_verb] = vc.get(eff_verb, 0) + 1
        if trig_filter:
            if trig_filter != "Card.Self":
                p['raw_trigger_filters'].add(trig_filter)
            p['trigger_filters'] |= ForgeFeatureContext._derive_coarse_filter_types(trig_filter)
        # Phase 1: combat trigger filters (ValidAttacker$ / ValidBlocker$)
        # The trigger_filter column is populated from ValidCard$; combat
        # triggers use ValidAttacker$/ValidBlocker$ instead, which would
        # otherwise be invisible to raw_trigger_filters / trigger_specificity.
        if ability_type == 'T' and raw_line_val:
            for combat_filter in ForgeFeatureContext._parse_combat_trigger_filters(raw_line_val):
                p['raw_trigger_filters'].add(combat_filter)
                p['trigger_filters'] |= ForgeFeatureContext._derive_coarse_filter_types(combat_filter)
        # Extract cost types for cost-effect alignment (flows into cost_feeds_cmdr F94)
        cost_str = cost or ""
        p['cost_types'] |= ForgeFeatureContext._parse_cost_types(cost_str)
        # Extract subtype requirements from cost, defined, and raw_line fields
        defined_str = defined or ""
        raw_line = raw_line_val or ""
        # Cost subtypes: tapXType<1/Cleric>, Sac<1/Human>
        for m in _RE_TAP_X_TYPE.findall(cost_str):
            if m not in ("CARDNAME",):
                p['required_subtypes'].add(m.lower())
        for m in _RE_SAC_TYPE.findall(cost_str):
            if m not in ("CARDNAME",) and m[0].isupper():
                p['required_subtypes'].add(m.lower())
        # Defined$ with subtype filter (e.g., TriggeredCardLKICopy.Spider)
        if "." in defined_str:
            for part in defined_str.split(","):
                if "." in part:
                    subtype = part.split(".")[-1].strip()
                    if subtype and subtype[0].isupper() and len(subtype) > 2:
                        p['required_subtypes'].add(subtype.lower())
        # Effect target subtypes from ValidCards$, Affected$, ValidTgts$,
        # ValidCard$, ValidAttackers$, AddsCounters$ in raw_line
        # e.g., ValidCards$ Creature.Orc → effect only benefits Orcs
        # e.g., Affected$ Creature.nonHuman → EXCLUDES Humans
        self._extract_subtypes_from_raw_line(p, raw_line, _GENERIC_TYPES)
        self._extract_keywords_and_conditions(p, verb, raw_line)
        self._extract_counters_and_special(p, row, raw_line)

    @staticmethod
    def _parse_combat_trigger_filters(raw_line: str | None) -> set[str]:
        """Extract ValidAttacker$/ValidBlocker$ filter values from a raw_line.

        Combat triggers (Attacks, Blocks, AttackerBlockedByCreature, etc.)
        filter the trigger source via ValidAttacker$/ValidBlocker$ rather than
        ValidCard$. Returns the full filter strings (e.g., 'Creature.Vampire',
        'Creature.YouCtrl+Dragon') for downstream use by raw_trigger_filters
        / trigger_specificity. Skips the 'Card.Self' sentinel, mirroring the
        existing ValidCard$ handling. Returns empty set for None / empty input.
        """
        if not raw_line:
            return set()
        out: set[str] = set()
        for rx in (_RE_VALID_ATTACKER, _RE_VALID_BLOCKER):
            for m in rx.finditer(raw_line):
                val = m.group(1).strip()
                if val and val != "Card.Self":
                    out.add(val)
        return out

    @staticmethod
    def _derive_coarse_filter_types(filter_string: str | None) -> set[str]:
        """Extract lowercase coarse type tokens from a comma-separated filter value.

        Used by both the existing ValidCard$ trig_filter path and the Phase 1
        ValidAttacker$/ValidBlocker$ combat-filter path so the two paths cannot
        drift. Iterates ALL comma-separated parts (multi-type filters like
        'Creature,Artifact' produce {'creature', 'artifact'}). Skips the
        'Card' sentinel and any token that does not start with an uppercase
        letter (heuristic to filter out qualifiers like 'nonHuman').
        """
        if not filter_string:
            return set()
        out: set[str] = set()
        for part in filter_string.split(","):
            main = part.split(".")[0].strip()
            if main and main != "Card" and main[0].isupper():
                out.add(main.lower())
        return out

    @staticmethod
    def _parse_cost_types(cost_str: str | None) -> set[str]:
        """Tokenize a Forge cost string into mechanical cost categories.

        Returns a set of category labels consumed by cost_feeds_cmdr (F94).
        Categories are intentionally generic (no per-card rules).

        Preserves the prior 5-category substring semantics exactly:
        sacrifice, tap, discard, exile, paylife.

        Phase 1 adds 4 new categories:
          - subcounter: counter-removal costs (P1P1, CHARGE, OIL, ..., loyalty-minus)
          - exilegrave: graveyard-exile costs (ALWAYS additive — ExileFromGrave
                        strings also set 'exile' via the base substring match,
                        preserving the pre-Phase-1 'exile' count bit-for-bit
                        so the trained model's baseline is not shifted)
          - taptype:    typed tap costs (tapXType<N/Subtype>, convoke/improvise style)
          - return:     bounce-to-hand costs (Return<N/Target> — '<' anchor
                        to avoid matching ReturnToHand/ReturnFromGrave verbs)
        """
        if not cost_str:
            return set()
        out: set[str] = set()
        # --- Existing 5 categories (preserve exact semantics) ---
        if "Sac" in cost_str:
            out.add('sacrifice')
        if "T" in cost_str.split() or cost_str == "T":
            out.add('tap')
        if "Discard" in cost_str:
            out.add('discard')
        if "Exile" in cost_str:
            out.add('exile')
        if "PayLife" in cost_str:
            out.add('paylife')
        # --- Phase 1 new categories ---
        if "SubCounter" in cost_str:
            out.add('subcounter')
        if "ExileFromGrave" in cost_str:
            # Additive: exile is already set above via the 'Exile' substring;
            # this only adds the more specific 'exilegrave' tag.
            out.add('exilegrave')
        if "tapXType" in cost_str:
            out.add('taptype')
        if "Return<" in cost_str:
            out.add('return')
        return out

    @staticmethod
    def _extract_subtypes_from_raw_line(p, raw_line, _generic):
        """Extract required/excluded subtypes and Affected$ scope from raw_line."""
        for field in ('ValidCards', 'ValidCard', 'ValidTgts',
                      'ValidAttackers', 'Affected', 'AddsCounters'):
            m = _RE_SUBTYPE_FIELDS[field].search(raw_line)
            if not m:
                continue
            full_val = m.group(1)
            # Affected$ scope: track who benefits (YouCtrl vs OppCtrl)
            if field == 'Affected':
                segs_lower = full_val.lower().replace('+', '.').split('.')
                if any(s in _SELF_SCOPES for s in segs_lower):
                    p['affected_self_count'] += 1
                elif 'oppctrl' in segs_lower:
                    p['affected_opp_count'] += 1
                # Bare scope (e.g., Affected$ Creature) = symmetric, don't count
            for part in full_val.split(","):
                for seg in part.split("."):
                    seg = seg.split("+")[0].strip()
                    if not seg or len(seg) <= 2:
                        continue
                    # Detect non-X exclusions (e.g., nonHuman, nonGoblin)
                    if seg.startswith("non") and len(seg) > 3 and seg[3].isupper():
                        excluded = seg[3:].lower()
                        if excluded not in _generic:
                            p['excluded_subtypes'].add(excluded)
                    elif seg[0].isupper() and seg.lower() not in _generic:
                        p['required_subtypes'].add(seg.lower())
        # Also extract non-X from TriggerDescription$ and Description$
        # Catches sub-ability targets like "non-Human creature"
        for desc_field in ('TriggerDescription', 'Description', 'SpellDescription'):
            dm = _RE_DESC_FIELDS[desc_field].search(raw_line)
            if dm:
                for nm in _RE_NON_CREATURE.finditer(dm.group(1)):
                    excl = nm.group(1).lower()
                    if excl not in _generic and len(excl) > 2:
                        p['excluded_subtypes'].add(excl)

    @staticmethod
    def _extract_keywords_and_conditions(p, verb, raw_line):
        """Extract granted keywords, conditions, duration, zones, scaling, and types."""
        # --- Granted keywords: AddKeyword$, KW$, PumpKeywords$, Keywords$ ---
        for kw_field in ('AddKeyword', 'KW', 'PumpKeywords', 'Keywords'):
            m = _RE_KEYWORD_FIELDS[kw_field].search(raw_line)
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
            m = _RE_CONDITION_FIELDS[cond_field].search(raw_line)
            if m:
                for part in m.group(1).split(","):
                    main = part.split(".")[0].split("+")[0].strip()
                    if main and main[0].isupper() and len(main) > 2:
                        p['conditions'].add(main.lower())
        # --- Duration$ ---
        m = _RE_DURATION.search(raw_line)
        if m:
            p['duration'].add(m.group(1).lower())
        elif verb in ('Pump', 'PumpAll') and 'Duration$' not in raw_line:
            p['duration'].add('temporary')
        # --- CombatDamage$ True ---
        if 'CombatDamage$ True' in raw_line:
            p['combat_damage'] = True
        # --- Effect zones: ActiveZones$, EffectZone$, AffectedZone$ ---
        for zone_field in ('ActiveZones', 'EffectZone', 'AffectedZone'):
            m = _RE_ZONE_FIELDS[zone_field].search(raw_line)
            if m:
                for z in m.group(1).split(","):
                    z = z.strip()
                    if z:
                        p['effect_zones'].add(z.lower())
        # --- ChangeType$: type changes for tribal synergy ---
        ct_m = _RE_CHANGE_TYPE.search(raw_line)
        if ct_m:
            ct_val = ct_m.group(1)
            if ct_val == 'AllCreatureTypes':
                p['grants_all_creature_types'] = True
            else:
                for t in ct_val.split(","):
                    t = t.split(".")[0].strip()
                    if t and t[0].isupper() and len(t) > 2:
                        p['changes_type'].add(t.lower())

    @staticmethod
    def _extract_counters_and_special(p, row, raw_line):
        """Extract damage/draw/life amounts, mana, counters, and special flags."""
        verb = row[1]
        # --- Damage amount: NumDmg$ ---
        m = _RE_NUM_DMG.search(raw_line)
        if m:
            val = m.group(1)
            # Keep the latest (most relevant) if multiple abilities
            if p['damage_amount'] is None or val == 'X':
                p['damage_amount'] = val
        # --- Cards drawn: NumCards$ ---
        m = _RE_NUM_CARDS.search(raw_line)
        if m:
            val = m.group(1)
            if p['cards_drawn'] is None or val == 'X':
                p['cards_drawn'] = val
        # --- Life amount: LifeAmount$ ---
        m = _RE_LIFE_AMOUNT.search(raw_line)
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
        # --- Mana production: Produced$ W/U/B/R/G/C/Any/Combo ---
        m = _RE_PRODUCED.search(raw_line)
        if m:
            p['produces_mana'] = True
        # --- Grants abilities: AddAbility$ (extract detail, not just boolean) ---
        ab_m = _RE_ADD_ABILITY.search(raw_line)
        if ab_m:
            p['granted_ability_names'].add(ab_m.group(1))
        # --- Grants triggers: AddTrigger$ ---
        trig_m = _RE_ADD_TRIGGER.search(raw_line)
        if trig_m:
            p['granted_triggers'].add(trig_m.group(1))
        # --- Pump magnitude: NumAtt$/AddPower$/NumDef$/AddToughness$ ---
        for pump_field in ('NumAtt', 'AddPower', 'NumDef', 'AddToughness'):
            pm = _RE_PUMP_FIELDS[pump_field].search(raw_line)
            if pm:
                pval = pm.group(1)
                if pval in ('X', 'Y', 'AffectedX'):
                    p['pump_is_variable'] = True
                else:
                    try:
                        mag = abs(int(pval.lstrip('+')))
                        p['max_pump_power'] = max(p['max_pump_power'], mag)
                    except ValueError:
                        pass
        # --- Token amount variable: TokenAmount$ X ---
        m = _RE_TOKEN_AMOUNT.search(raw_line)
        if m and m.group(1) in ('X', 'Y'):
            p['token_amount_variable'] = True
        # --- Static anthem: Continuous + AddPower (not actual counters) ---
        # --- Counters on lands: PutCounter targeting lands, or Earthbend ---
        if verb in ('PutCounter', 'PutCounterAll'):
            vtgt = _RE_VALID_TGTS.search(raw_line)
            if vtgt and 'Land' in vtgt.group(1) and 'Creature' not in vtgt.group(1):
                p['counters_on_lands'] = True
        if verb == 'Earthbend':
            p['counters_on_lands'] = True
        # --- Counter trigger themes: what triggers this card's counter placement ---
        if verb in ('PutCounter', 'PutCounterAll') and row[2]:
            trig = row[2]
            if trig == 'LifeGained':
                p['counter_trigger_themes'].add('lifegain')
            elif trig == 'Sacrificed':
                p['counter_trigger_themes'].add('sacrifice')
            elif trig == 'Discarded':
                p['counter_trigger_themes'].add('discard')
            elif trig in ('SpellCast', 'SpellCopy'):
                p['counter_trigger_themes'].add('spellcast')

    def _load_deck_tags(self, conn):
        """Load Forge deck-building AI tags (has/hints/needs)."""
        # Forge deck tags: Forge's own deck-building AI signals
        # has = what abilities/themes a card provides
        # hints = what the card wants in the deck
        # needs = what the card requires to function
        self._deck_has = {}    # oracle_id -> set of tags
        self._deck_hints = {}  # oracle_id -> set of tags
        self._deck_needs = {}  # oracle_id -> set of tags
        for row in conn.execute(
            "SELECT fnm.oracle_id, fdt.tag_type, fdt.tag "
            "FROM forge_deck_tags fdt "
            "JOIN forge_name_map fnm ON fnm.forge_name = fdt.card_name"
        ):
            oid, tag_type, tag = row
            # Normalize compound tags: "Ability$Token|Sacrifice" → {"Ability$Token", "Ability$Sacrifice"}
            # Also keep the raw compound for exact matching
            tags = set()
            parts = tag.split("|")
            tags.add(tag)
            # Split compound tags like "Type$Goblin|Warrior" into individual tags
            if "|" in tag:
                prefix = ""
                for part in parts:
                    if "$" in part:
                        prefix = part.split("$")[0] + "$"
                        tags.add(part)
                    elif prefix:
                        tags.add(prefix + part)
            if tag_type == "has":
                self._deck_has.setdefault(oid, set()).update(tags)
            elif tag_type == "hints":
                self._deck_hints.setdefault(oid, set()).update(tags)
            elif tag_type == "needs":
                self._deck_needs.setdefault(oid, set()).update(tags)

        # Auto-derive deck tags from forge profiles for cards missing them.
        # verb → has (card provides this ability), trigger → hints (card wants this event).
        # Uses verb/trigger names directly as tags (e.g., Ability$Sacrifice, Ability$Token).
        # Trigger normalization map _TRIGGER_STEM_TO_VERB is module-level — see top
        # of file for the maintenance contract.
        #
        # The reverse index self._deck_has_providers is built once in
        # _build_deck_has_providers() AFTER _enrich_deck_tags_from_tokens() has
        # added token Type$ tags. Do not build it here — that would force a
        # second incremental update later and create a hidden ordering bug if
        # the call sequence in __init__ ever changes.
        for oid, profile in self._forge_profiles.items():
            for verb in profile.get('verbs', set()):
                tag = f"Ability${verb}"
                self._deck_has.setdefault(oid, set()).add(tag)
            for trig in profile.get('triggers', set()):
                stem = _TRIGGER_STEM_TO_VERB.get(trig)
                if stem:
                    tag = f"Ability${stem}"
                    self._deck_hints.setdefault(oid, set()).add(tag)
            # Type$ tags from trigger_filters (card triggers on specific types)
            for tf in profile.get('trigger_filters', set()):
                if tf not in _GENERIC_TRIGGER_FILTER_TYPES and len(tf) > 2:
                    self._deck_hints.setdefault(oid, set()).add(f"Type${tf.title()}")

    def _enrich_deck_tags_from_tokens(self):
        """Add Type$ has-tags from token subtypes (runs after _load_ability_vectors)."""
        for oid, subs in self._token_subtypes.items():
            for sub in subs:
                self._deck_has.setdefault(oid, set()).add(f"Type${sub.title()}")

    def _build_deck_has_providers(self):
        """Build the tag → providers reverse index from the final _deck_has state.

        Must be called AFTER _load_deck_tags() and _enrich_deck_tags_from_tokens()
        have populated _deck_has. Single-pass construction so the index is always
        consistent with _deck_has — no incremental updates that could rot.
        """
        self._deck_has_providers = {}
        for oid, tags in self._deck_has.items():
            for tag in tags:
                self._deck_has_providers.setdefault(tag, set()).add(oid)

    def _load_ability_vectors(self, conn):
        """Load ability vectors, trigger zones, ability counts, token/zone stats."""
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
        if self._n_abilities > 65535:
            raise ValueError(
                f"Ability vocab ({self._n_abilities}) exceeds uint16 range; "
                "update sparse vector dtype to uint32"
            )

        # Pre-compute normalized ability vectors per card (sparse format).
        # 99.7% sparse (avg 2.6 nonzero per 754-dim vector), so sparse saves ~90 MiB.
        # Each card stores (indices: uint16, values: float32) arrays.
        self._ability_vectors = {}
        for oid, p in self._forge_profiles.items():
            indices = []
            for a in p['verbs']:
                idx = self._ability_idx.get(a)
                if idx is not None:
                    indices.append(idx)
            for a in p['triggers']:
                idx = self._ability_idx.get(a)
                if idx is not None:
                    indices.append(idx)
            for a in p['keywords']:
                idx = self._ability_idx.get(a)
                if idx is not None:
                    indices.append(idx)
            if indices:
                idx_arr = np.array(sorted(set(indices)), dtype=np.uint16)
                n = len(idx_arr)
                norm = np.sqrt(float(n))  # binary vector: norm = sqrt(count)
                vals = np.full(n, 1.0 / norm, dtype=np.float32)
                self._ability_vectors[oid] = (idx_arr, vals)

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

        # Pre-load total ability counts per card (for F63)
        self._total_ability_counts = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, COUNT(*) FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "GROUP BY fnm.oracle_id"
        ):
            self._total_ability_counts[row[0]] = row[1]

        # Pre-load triggered ability counts per card (for F64)
        self._triggered_counts = {}
        for row in conn.execute(
            "SELECT fnm.oracle_id, COUNT(*) FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.ability_type = 'T' "
            "GROUP BY fnm.oracle_id"
        ):
            self._triggered_counts[row[0]] = row[1]

        # Pre-load token stats per card (for F65, F66) + token subtypes for penalties
        self._token_max_pt = {}      # oid -> max P+T across all token scripts
        self._token_max_kw = {}      # oid -> max keyword count across all token scripts
        self._token_subtypes = {}    # oid -> set of creature subtypes from tokens
        _kw_skip = {"sac", "draw"}   # not real keywords in token scripts
        _token_kw_skip = {"flying", "haste", "trample", "vigilance", "deathtouch",
                          "lifelink", "menace", "reach", "defender", "sac", "draw",
                          "unblockable", "first", "strike", "double", "indestructible",
                          "hexproof", "shroud", "wither", "persist"}
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.token_script FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.token_script IS NOT NULL"
        ):
            oid, ts = row
            for raw_ts in ts.split(","):
                parts = raw_ts.strip().lower().split("_")
                if len(parts) >= 3:
                    try:
                        pw = int(parts[1]) if parts[1] not in ("x", "a") else 0
                        th = int(parts[2]) if parts[2] not in ("x", "a") else 0
                        pt = pw + th
                        self._token_max_pt[oid] = max(self._token_max_pt.get(oid, 0), pt)
                    except ValueError:
                        pass
                    if len(parts) > 3:
                        kws = [tok for tok in parts[3:] if tok and tok not in _kw_skip and len(tok) > 1]
                        kw_count = len(kws)
                        self._token_max_kw[oid] = max(self._token_max_kw.get(oid, 0), kw_count)
                        # Extract token creature subtypes (not keywords)
                        for tok in parts[3:]:
                            if tok and tok not in _token_kw_skip and len(tok) > 1:
                                self._token_subtypes.setdefault(oid, set()).add(tok)

        # Pre-load zone interaction flags per card (for F67, F68)
        self._zone_graveyard = set()
        self._zone_exile = set()
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.trigger_origin, fa.trigger_destination "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_origin IS NOT NULL OR fa.trigger_destination IS NOT NULL"
        ):
            oid, origin, dest = row
            if origin and "Graveyard" in origin:
                self._zone_graveyard.add(oid)
            if dest and "Graveyard" in dest:
                self._zone_graveyard.add(oid)
            if origin and "Exile" in origin:
                self._zone_exile.add(oid)
            if dest and "Exile" in dest:
                self._zone_exile.add(oid)
        # Also mark cards with graveyard/exile-related verbs
        for oid, p in self._forge_profiles.items():
            if p['verbs'] & {"Mill", "Sacrifice", "Destroy", "DestroyAll", "Discard"}:
                self._zone_graveyard.add(oid)
            if "exile" in p.get('effect_zones', set()):
                self._zone_exile.add(oid)

    def _load_verb_demand_data(self, conn):
        """Load trigger_mode→verb mapping for general commander demand features.

        Builds two data structures:
        1. Per-card verb supply: set of verbs each card performs (including keywords
           like Scry/Surveil that appear as verbs in forge_abilities)
        2. Per-card type demand: which card types each card's triggers care about
           (extracted from trigger_filter field)

        These enable two general features:
        - verb_demand_match: commander triggers on X → card performs X
        - type_demand_match: commander wants type X → card IS type X
        """
        # ── Reverse mapping: trigger_mode → set of verbs that produce that event ──
        # Built from the existing _verb_triggers (verb→triggers) by inverting it,
        # plus direct trigger_mode→verb pairs (Scry trigger → Scry verb, etc.)
        self._trigger_to_verbs = {}
        for verb, triggers in self._verb_triggers.items():
            for tm in triggers:
                self._trigger_to_verbs.setdefault(tm, set()).add(verb)
        # Direct trigger_mode→verb mappings (trigger name = verb name)
        for direct in ('Scry', 'Surveil', 'Mill', 'Discard', 'Sacrifice',
                       'Proliferate', 'Explore'):
            self._trigger_to_verbs.setdefault(direct, set()).add(direct)
        # Additional mappings for less obvious trigger→verb pairs
        self._trigger_to_verbs.setdefault('LifeGained', set()).add('GainLife')
        self._trigger_to_verbs.setdefault('LifeLost', set()).add('LoseLife')
        self._trigger_to_verbs.setdefault('LifeLost', set()).add('DealDamage')
        self._trigger_to_verbs.setdefault('Cycled', set()).add('Cycling')

        # ── Per-card verb supply: what verbs/keywords each card has ──
        self._card_verb_supply = {}  # oid → set of verbs
        for oid, p in self._forge_profiles.items():
            verbs = set(p.get('verbs', set()))
            # Also count keywords that are also verbs (Scry, Surveil, Cycling, etc.)
            for kw in p.get('keywords', set()):
                if kw in ('Scry', 'Surveil', 'Cycling', 'Explore', 'Proliferate',
                          'Mill', 'Connive', 'Investigate', 'Foretell'):
                    verbs.add(kw)
            if verbs:
                self._card_verb_supply[oid] = verbs

        # Also pick up verb signals from forge_abilities for cards with
        # scry/surveil as effect verbs (not just keywords)
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id, fa.verb FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.verb IN ('Scry', 'Surveil', 'Mill', 'Proliferate', "
            "'Explore', 'Investigate', 'Connive')"
        ):
            oid, verb = row
            if oid in self.oid_to_idx:
                self._card_verb_supply.setdefault(oid, set()).add(verb)

        # ── Per-card trigger demand: what trigger_modes each card responds to ──
        # (non-self triggers only — we want "when SOMETHING ELSE does X")
        self._card_trigger_demand = {}  # oid → set of trigger_modes
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id, fa.trigger_mode FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_mode IS NOT NULL AND fa.trigger_mode != '' "
            "AND (fa.trigger_filter IS NULL OR fa.trigger_filter NOT IN ('Card.Self')) "
            "AND fa.trigger_filter NOT LIKE 'Card.Self+%'"
        ):
            oid, tm = row
            if oid in self.oid_to_idx:
                self._card_trigger_demand.setdefault(oid, set()).add(tm)

        # ── Per-card type demand: what card types does trigger_filter mention? ──
        # E.g., trigger_filter="Instant,Sorcery" → demands Instant, Sorcery
        self._card_type_demand = {}  # oid → dict {type_name: weight}
        _TYPE_NAMES = ('Creature', 'Instant', 'Sorcery', 'Enchantment',
                       'Artifact', 'Planeswalker', 'Land')
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id, fa.trigger_filter, fa.raw_line "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE (fa.trigger_filter IS NOT NULL AND fa.trigger_filter != '' "
            "AND fa.trigger_filter NOT IN ('Card.Self') "
            "AND fa.trigger_filter NOT LIKE 'Card.Self+%') "
            "OR fa.raw_line LIKE '%Affected$ %'"
        ):
            oid, tf, raw = row
            if oid not in self.oid_to_idx:
                continue
            demand = self._card_type_demand.setdefault(oid, {})
            # From trigger_filter
            if tf and tf not in ('Card.Self',) and not tf.startswith('Card.Self+'):
                for t in _TYPE_NAMES:
                    if t in tf:
                        demand[t] = demand.get(t, 0.0) + 1.0
                # "Permanent" = all permanent types
                if 'Permanent' in tf:
                    for t in ('Creature', 'Artifact', 'Enchantment', 'Planeswalker'):
                        demand[t] = demand.get(t, 0.0) + 0.5
            # From Affected$ in raw_line
            if raw:
                for t in _TYPE_NAMES:
                    if f'Affected$ {t}' in raw:
                        demand[t] = demand.get(t, 0.0) + 0.8

        # Also add type demand from deck tags hints/needs
        for oid, tags in self._deck_hints.items():
            for tag in tags:
                if tag.startswith('Type$'):
                    demand = self._card_type_demand.setdefault(oid, {})
                    for part in tag[5:].split('|'):
                        for t in _TYPE_NAMES:
                            if t == part:
                                demand[t] = demand.get(t, 0.0) + 0.6
        for oid, tags in self._deck_needs.items():
            for tag in tags:
                if tag.startswith('Type$'):
                    demand = self._card_type_demand.setdefault(oid, {})
                    for part in tag[5:].split('|'):
                        for t in _TYPE_NAMES:
                            if t == part:
                                demand[t] = demand.get(t, 0.0) + 0.6

        _log.info("Verb demand: %d cards with verb supply, %d with trigger demand, "
                   "%d with type demand", len(self._card_verb_supply),
                   len(self._card_trigger_demand), len(self._card_type_demand))

    def _load_edhrec_stats(self, conn):
        """Load EDHREC popularity stats and card quality proxies."""
        # ── Card quality / popularity signals ──
        # EDHREC deck frequency: fraction of EDHREC commanders that include each card
        self._edhrec_deck_pct = {}
        try:
            total_cmdrs = conn.execute(
                "SELECT COUNT(DISTINCT commander_slug) FROM edhrec_card_synergy"
            ).fetchone()[0]
            if total_cmdrs > 0:
                # Build name→oracle_id lookup from CardProvider
                name_to_oid = self._card_provider.get_name_to_oid()
                # Count commanders per card_name
                card_cmdr_counts = {}
                for row in conn.execute(
                    "SELECT card_name, COUNT(DISTINCT commander_slug) "
                    "FROM edhrec_card_synergy GROUP BY card_name"
                ):
                    oid = name_to_oid.get(row[0].lower())
                    if oid:
                        card_cmdr_counts[oid] = card_cmdr_counts.get(oid, 0) + row[1]
                for oid, count in card_cmdr_counts.items():
                    self._edhrec_deck_pct[oid] = float(count) / total_cmdrs
        except sqlite3.OperationalError as e:
            _log.warning("edhrec_deck_pct load failed (table may not exist): %s", e)

        # Cards with Forge data (have any abilities parsed)
        self._has_forge_data = set(self._forge_profiles.keys())

        # Forge-native card quality proxies (EDHREC-independent noise suppression)
        # forge_ability_richness: total distinct mechanical components per card
        self._forge_richness = {}
        for oid, p in self._forge_profiles.items():
            richness = (len(p['verbs']) + len(p['triggers']) + len(p['keywords']) +
                        len(p['counter_types']) + len(p['targets']))
            self._forge_richness[oid] = float(min(richness, 15))

        # deck_tag_count: how many deck tags (has+hints+needs) Forge's AI assigned
        self._deck_tag_count = {}
        for oid in set(list(self._deck_has.keys()) + list(self._deck_hints.keys()) +
                       list(self._deck_needs.keys())):
            count = (len(self._deck_has.get(oid, set())) +
                     len(self._deck_hints.get(oid, set())) +
                     len(self._deck_needs.get(oid, set())))
            self._deck_tag_count[oid] = float(min(count, 10))

    def _load_functional_fingerprints(self, conn):
        """Build functional fingerprint vectors from Forge ability data."""
        # ── Functional fingerprints: semantic vectors for what each card does ──
        # 33 dimensions across 4 sub-vectors: produces, requires, amplifies, targets
        # Dot products between commander and card fingerprints capture synergy
        # without hand-coded penalties.
        self._func_fingerprints = self._build_func_fingerprints(conn)

    def _build_card_arrays(self):
        """Pre-encode per-card data as dense numpy arrays for vectorized features.

        Arrays are indexed by oid_to_idx[oid]. Used by compute_batch_features()
        for ~10x speedup over per-card dict lookups.
        """
        n = len(self.oid_to_idx)
        idx_to_oid = {v: k for k, v in self.oid_to_idx.items()}

        # ── Type flags from type_lines ──
        self._arr_type_creature = np.zeros(n, dtype=np.float32)
        self._arr_type_instant_sorcery = np.zeros(n, dtype=np.float32)
        self._arr_type_artifact = np.zeros(n, dtype=np.float32)
        self._arr_type_enchantment = np.zeros(n, dtype=np.float32)
        self._arr_type_land = np.zeros(n, dtype=np.float32)
        self._arr_type_planeswalker = np.zeros(n, dtype=np.float32)
        self._arr_cmc = np.zeros(n, dtype=np.float32)
        # Parse card subtypes for tribal matching
        self._arr_card_subtypes = [set() for _ in range(n)]  # list of sets

        for oid, tl in self._type_lines.items():
            i = self.oid_to_idx.get(oid)
            if i is None:
                continue
            if "Creature" in tl:
                self._arr_type_creature[i] = 1.0
            if "Instant" in tl or "Sorcery" in tl:
                self._arr_type_instant_sorcery[i] = 1.0
            if "Artifact" in tl:
                self._arr_type_artifact[i] = 1.0
            if "Enchantment" in tl:
                self._arr_type_enchantment[i] = 1.0
            if "Land" in tl:
                self._arr_type_land[i] = 1.0
            if "Planeswalker" in tl:
                self._arr_type_planeswalker[i] = 1.0
            if "\u2014" in tl:
                try:
                    self._arr_card_subtypes[i] = {
                        s.lower() for s in tl.split("\u2014")[1].strip().split()
                    }
                except (IndexError, AttributeError):
                    pass

        # CMC from cards table (populated externally before batch call)
        # Will be filled by compute_batch_features caller

        # ── Ability counts and boolean flags ──
        self._arr_activated_count = np.zeros(n, dtype=np.float32)
        self._arr_total_abilities = np.zeros(n, dtype=np.float32)
        self._arr_triggered_count = np.zeros(n, dtype=np.float32)
        self._arr_token_pt = np.zeros(n, dtype=np.float32)
        self._arr_token_kw = np.zeros(n, dtype=np.float32)
        self._arr_forge_richness = np.zeros(n, dtype=np.float32)
        self._arr_in_forge = np.zeros(n, dtype=np.float32)
        self._arr_deck_tag_count = np.zeros(n, dtype=np.float32)
        self._arr_edhrec_pct = np.zeros(n, dtype=np.float32)
        self._arr_mech_nonzero = np.zeros(n, dtype=np.float32)

        # ── Profile-derived boolean flags ──
        self._arr_combat_damage = np.zeros(n, dtype=np.float32)
        self._arr_gain_control = np.zeros(n, dtype=np.float32)
        self._arr_produces_mana = np.zeros(n, dtype=np.float32)
        self._arr_token_amt_var = np.zeros(n, dtype=np.float32)
        self._arr_has_p1p1 = np.zeros(n, dtype=np.float32)
        self._arr_has_T = np.zeros(n, dtype=np.float32)
        self._arr_has_A = np.zeros(n, dtype=np.float32)
        self._arr_has_phase = np.zeros(n, dtype=np.float32)
        self._arr_forge_depth = np.zeros(n, dtype=np.float32)
        self._arr_damage_scales = np.zeros(n, dtype=np.float32)
        self._arr_draw_scales = np.zeros(n, dtype=np.float32)
        self._arr_life_scales = np.zeros(n, dtype=np.float32)
        self._arr_granted_kw_count = np.zeros(n, dtype=np.float32)
        self._arr_condition_count = np.zeros(n, dtype=np.float32)
        self._arr_n_counter_verbs = np.zeros(n, dtype=np.float32)
        self._arr_n_pump_verbs = np.zeros(n, dtype=np.float32)
        self._arr_has_any_counter_verb = np.zeros(n, dtype=np.float32)
        self._arr_zone_gy = np.zeros(n, dtype=np.float32)
        self._arr_zone_ex = np.zeros(n, dtype=np.float32)
        # ── New field arrays: Affected$ scope, pump magnitude, type changes ──
        self._arr_affected_scope = np.zeros(n, dtype=np.float32)
        self._arr_pump_magnitude = np.zeros(n, dtype=np.float32)
        self._arr_pump_variable = np.zeros(n, dtype=np.float32)
        self._arr_changes_type = [set() for _ in range(n)]

        # Single merged loop over all cards (was 3 separate loops)
        _COUNTER_VERBS = frozenset({'PutCounter', 'PutCounterAll'})
        _PUMP_VERBS = frozenset({'Pump', 'PumpAll'})
        _ANY_COUNTER_VERBS = frozenset({'PutCounter', 'PutCounterAll', 'Proliferate', 'MoveCounter'})
        _XY = frozenset({'X', 'Y'})
        _empty_set = set()
        _empty_prof = {}
        for oid, i in self.oid_to_idx.items():
            # Ability counts
            self._arr_activated_count[i] = min(self._activated_counts.get(oid, 0), 5)
            self._arr_total_abilities[i] = min(self._total_ability_counts.get(oid, 0), 15)
            self._arr_triggered_count[i] = min(self._triggered_counts.get(oid, 0), 10)
            self._arr_token_pt[i] = min(self._token_max_pt.get(oid, 0), 20)
            self._arr_token_kw[i] = min(self._token_max_kw.get(oid, 0), 5)
            self._arr_forge_richness[i] = self._forge_richness.get(oid, 0.0)
            self._arr_in_forge[i] = 1.0 if oid in self._has_forge_data else 0.0
            self._arr_deck_tag_count[i] = self._deck_tag_count.get(oid, 0.0)
            if not _EDHREC_FREE:
                self._arr_edhrec_pct[i] = self._edhrec_deck_pct.get(oid, 0.0)
            pv = self._mech_produces.get(oid)
            cv = self._mech_consumes.get(oid)
            nz = 0
            if pv is not None:
                nz += int(np.count_nonzero(pv))
            if cv is not None:
                nz += int(np.count_nonzero(cv))
            self._arr_mech_nonzero[i] = min(nz, 50)

            # Profile-derived flags (single dict lookup)
            p = self._forge_profiles.get(oid, _empty_prof)
            if p:
                self._arr_combat_damage[i] = 1.0 if p.get('combat_damage', False) else 0.0
                self._arr_gain_control[i] = 1.0 if p.get('gain_control', False) else 0.0
                self._arr_produces_mana[i] = 1.0 if p.get('produces_mana', False) else 0.0
                self._arr_token_amt_var[i] = 1.0 if p.get('token_amount_variable', False) else 0.0
                self._arr_has_p1p1[i] = 1.0 if p.get('has_p1p1', False) else 0.0
                atypes = p.get('ability_types', _empty_set)
                self._arr_has_T[i] = 1.0 if 'T' in atypes else 0.0
                self._arr_has_A[i] = 1.0 if 'A' in atypes else 0.0
                self._arr_has_phase[i] = 1.0 if self.card_phase_order.get(oid) else 0.0
                verbs = p.get('verbs', _empty_set)
                triggers = p.get('triggers', _empty_set)
                keywords = p.get('keywords', _empty_set)
                counter_types = p.get('counter_types', _empty_set)
                depth = len(verbs) + len(triggers) + len(keywords) + len(counter_types)
                self._arr_forge_depth[i] = min(depth, 10.0)
                self._arr_damage_scales[i] = 1.0 if p.get('damage_amount') in _XY else 0.0
                self._arr_draw_scales[i] = 1.0 if p.get('cards_drawn') in _XY else 0.0
                self._arr_life_scales[i] = 1.0 if p.get('life_amount') in _XY else 0.0
                self._arr_granted_kw_count[i] = min(len(p.get('granted_keywords', _empty_set)), 5)
                self._arr_condition_count[i] = min(len(p.get('conditions', _empty_set)), 5)
                self._arr_n_counter_verbs[i] = sum(1 for v in verbs if v in _COUNTER_VERBS)
                self._arr_n_pump_verbs[i] = sum(1 for v in verbs if v in _PUMP_VERBS)
                self._arr_has_any_counter_verb[i] = 1.0 if verbs & _ANY_COUNTER_VERBS else 0.0
                self._arr_zone_gy[i] = 1.0 if oid in self._zone_graveyard else 0.0
                self._arr_zone_ex[i] = 1.0 if oid in self._zone_exile else 0.0
                # Affected$ scope, pump magnitude, type changes
                self._arr_affected_scope[i] = p.get('affected_scope_ratio', 0.5)
                self._arr_pump_magnitude[i] = min(p.get('max_pump_power', 0), 15)
                self._arr_pump_variable[i] = 1.0 if p.get('pump_is_variable', False) else 0.0
                ct = p.get('changes_type', _empty_set)
                if p.get('grants_all_creature_types', False):
                    ct = ct | {'_all_'}
                if ct:
                    self._arr_changes_type[i] = ct

        # ── Verb demand arrays: bitmask of which trigger_modes each card satisfies ──
        # Ordered list of trigger_modes with clear verb mappings
        self._demand_trigger_modes = [
            'Scry', 'Surveil', 'Sacrificed', 'LifeGained', 'DamageDone',
            'Drawn', 'Discarded', 'TokenCreated', 'CounterAdded', 'Milled',
            'LifeLost', 'Proliferate', 'DamageDoneOnce', 'Cycled', 'SpellCast',
            'ChangesZone', 'Attacks',
        ]
        self._demand_tm_to_bit = {tm: i for i, tm in enumerate(self._demand_trigger_modes)}
        n_bits = len(self._demand_trigger_modes)

        # Per-card: which trigger_modes does this card's verbs satisfy?
        self._arr_verb_supply_mask = np.zeros((n, n_bits), dtype=np.float32)
        for oid, i in self.oid_to_idx.items():
            card_verbs = self._card_verb_supply.get(oid, set())
            if not card_verbs:
                continue
            for bit_idx, tm in enumerate(self._demand_trigger_modes):
                satisfying_verbs = self._trigger_to_verbs.get(tm, set())
                if card_verbs & satisfying_verbs:
                    self._arr_verb_supply_mask[i, bit_idx] = 1.0

        # Per-card type supply: 7-dim binary vector [Creature, Instant, Sorcery, ...]
        self._demand_type_names = ('Creature', 'Instant', 'Sorcery', 'Enchantment',
                                    'Artifact', 'Planeswalker', 'Land')
        self._arr_type_supply = np.zeros((n, 7), dtype=np.float32)
        # Instant and Sorcery are combined in _arr_type_instant_sorcery, split them
        for oid, i in self.oid_to_idx.items():
            tl = self._type_lines.get(oid, "")
            self._arr_type_supply[i, 0] = self._arr_type_creature[i]
            if 'Instant' in tl:
                self._arr_type_supply[i, 1] = 1.0
            if 'Sorcery' in tl:
                self._arr_type_supply[i, 2] = 1.0
            self._arr_type_supply[i, 3] = self._arr_type_enchantment[i]
            self._arr_type_supply[i, 4] = self._arr_type_artifact[i]
            self._arr_type_supply[i, 5] = self._arr_type_planeswalker[i]
            self._arr_type_supply[i, 6] = self._arr_type_land[i]

        # Hub scores (computed from edge index, filled after edge loading)
        self._arr_hub_score = np.zeros(n, dtype=np.float32)
        self._arr_hub_raw = np.zeros(n, dtype=np.float32)

        # ── Per-category mech slice arrays for vectorized sub-product features ──
        # Category dimensions auto-derived from mechanics_vectors vocabulary
        cd = self._mech_category_dims
        self._MECH_BOARD_DIMS = cd.get("board", [])
        self._MECH_RESOURCE_DIMS = cd.get("resource", [])
        self._MECH_DISRUPTION_DIMS = cd.get("disruption", [])
        self._MECH_TEMPO_DIMS = cd.get("tempo", [])
        self._MECH_UTILITY_DIMS = cd.get("utility", [])
        self._MECH_ZONES_DIMS = cd.get("zones", [])
        self._MECH_THEMES_DIMS = cd.get("themes", [])
        self._MECH_TRIBAL_DIMS = cd.get("tribal", [])
        self._mech_categories = [
            self._MECH_BOARD_DIMS,
            self._MECH_RESOURCE_DIMS,
            self._MECH_DISRUPTION_DIMS,
            self._MECH_TEMPO_DIMS,
            self._MECH_UTILITY_DIMS,
            self._MECH_ZONES_DIMS,
            self._MECH_THEMES_DIMS,
            self._MECH_TRIBAL_DIMS,
        ]
        # Pre-build per-card per-category produces/consumes arrays
        # For each category: (n, len(cat_dims)) arrays
        self._mech_cat_produces = []  # list of (n, cat_len) arrays
        self._mech_cat_consumes = []  # list of (n, cat_len) arrays
        for cat_dims in self._mech_categories:
            cat_len = len(cat_dims)
            prod_arr = np.zeros((n, cat_len), dtype=np.float32)
            cons_arr = np.zeros((n, cat_len), dtype=np.float32)
            for oid, i in self.oid_to_idx.items():
                p = self._mech_produces.get(oid)
                c = self._mech_consumes.get(oid)
                if p is not None:
                    for j, d in enumerate(cat_dims):
                        if d < len(p):
                            prod_arr[i, j] = p[d]
                if c is not None:
                    for j, d in enumerate(cat_dims):
                        if d < len(c):
                            cons_arr[i, j] = c[d]
            self._mech_cat_produces.append(prod_arr)
            self._mech_cat_consumes.append(cons_arr)

    def _build_edge_index(self, conn):
        """Pre-load edge adjacency + strength + events into memory.

        Tries adj cache first (skips loading 271 MiB raw edge arrays).
        Falls back to edge_index_cache.npz, then DB scan.
        """
        _log.info("Building in-memory edge index...")
        t0 = time.time()

        # Get edge_count from DB if available (for cache validation).
        # In inference-only mode (synergy.db without interaction_edges),
        # edge_count stays None and we trust the cache unconditionally.
        edge_count = None
        try:
            edge_count = conn.execute(
                "SELECT MAX(rowid) FROM interaction_edges"
            ).fetchone()[0] or 0
        except sqlite3.OperationalError:
            pass  # no interaction_edges table (inference-only synergy.db)
        card_count = len(self.oid_to_idx)

        # Try adj cache first — avoids loading raw edge arrays entirely (~271 MiB savings)
        adj_cache_path = os.path.join(self._artifact_dir, "edge_adj_cache.npz")
        adj_loaded = self._load_adj_cache(adj_cache_path, edge_count, card_count, t0)

        if adj_loaded:
            # _load_adj_cache sets _bit_to_event if event_names is in cache
            if not self._bit_to_event:
                # Legacy adj cache without event_names — load from raw cache
                self._load_event_names_from_raw_cache(edge_count, card_count)
        else:
            # Need raw edge arrays to build adj dicts
            src, tgt, exact, strength, event_ids, event_names = (
                self._load_raw_edges(conn, edge_count, card_count, t0)
            )
            self._bit_to_event = {i: str(name) for i, name in enumerate(event_names)}
            self._build_adj_from_arrays(src, tgt, exact, strength, event_ids)
            self._save_adj_cache(adj_cache_path, edge_count, card_count, event_names)
            del src, tgt, exact, strength, event_ids

        self._idx_to_oid = {v: k for k, v in self.oid_to_idx.items()}
        self._n_cards_idx = len(self.oid_to_idx)
        self._has_edge_index = True

        elapsed = time.time() - t0
        n_unique = self._adj_out.total_values()
        _log.info("Edge index built: %.1fs, %s unique outgoing pairs", elapsed, f"{n_unique:,}")

    def _load_raw_edges(self, conn, edge_count, card_count, t0):
        """Load raw edge arrays from npz cache or DB."""
        cache_path = os.path.join(self._artifact_dir, "edge_index_cache.npz")
        if os.path.exists(cache_path):
            try:
                cached = np.load(cache_path, allow_pickle=False)
                cache_valid = (
                    'strength' in cached and 'event_ids' in cached
                    and 'event_names' in cached
                )
                if cache_valid and edge_count is not None:
                    cache_valid = (
                        int(cached['edge_count']) == edge_count
                        and int(cached['card_count']) == card_count
                    )
                if cache_valid:
                    _log.info("Loaded %s edges from cache (%.1fs)",
                              f"{len(cached['src']):,}", time.time() - t0)
                    return (cached['src'], cached['tgt'], cached['exact'],
                            cached['strength'], cached['event_ids'], cached['event_names'])
            except (ValueError, KeyError, OSError) as e:
                _log.warning("Edge index cache load failed, rebuilding: %s", e)

        if edge_count is None:
            raise RuntimeError(
                "Edge cache not found and interaction_edges table not available. "
                "Run export_inference_db.py to regenerate edge caches."
            )
        src, tgt, exact, strength, event_ids, event_names = (
            self._load_edges_from_db(conn, edge_count, t0)
        )
        try:
            np.savez(cache_path, src=src, tgt=tgt, exact=exact,
                     strength=strength, event_ids=event_ids,
                     event_names=event_names,
                     edge_count=np.array(edge_count), card_count=np.array(card_count))
            _log.info("Cached to %s", cache_path)
        except (OSError, ValueError) as e:
            _log.warning("Cache write failed: %s", e)
        return src, tgt, exact, strength, event_ids, event_names

    def _load_event_names_from_raw_cache(self, edge_count, card_count):
        """Load only event_names from edge_index_cache.npz (legacy adj cache compat)."""
        cache_path = os.path.join(self._artifact_dir, "edge_index_cache.npz")
        if os.path.exists(cache_path):
            try:
                cached = np.load(cache_path, allow_pickle=False)
                if 'event_names' in cached:
                    cache_valid = True
                    if edge_count is not None:
                        cache_valid = (
                            int(cached['edge_count']) == edge_count
                            and int(cached['card_count']) == card_count
                        )
                    if cache_valid:
                        self._bit_to_event = {
                            i: str(name) for i, name in enumerate(cached['event_names'])
                        }
                        return
            except (ValueError, KeyError, OSError):
                pass
        _log.warning("Could not load event_names from raw edge cache")

    def _load_edges_from_db(self, conn, edge_count, t0):
        """Load raw edge arrays from the database."""
        event_name_list = sorted(
            r[0] for r in conn.execute(
                "SELECT DISTINCT event FROM interaction_edges WHERE event IS NOT NULL"
            ) if r[0]
        )
        event_to_id = {name: i for i, name in enumerate(event_name_list)}
        event_names = np.array(event_name_list, dtype='U64')

        has_col = any(
            r[1] == "filter_precision"
            for r in conn.execute("PRAGMA table_info(interaction_edges)")
        )
        prec_expr = _PREC_EXPR_COLUMN if has_col else _PREC_EXPR_JSON
        event_expr = _EVENT_EXPR_COLUMN if self._has_event_col else _EVENT_EXPR_JSON
        if event_expr not in _VALID_EVENT_EXPRS:
            raise ValueError(f"Unexpected SQL fragment: {event_expr!r}")
        if prec_expr not in _VALID_PREC_EXPRS:
            raise ValueError(f"Unexpected SQL fragment: {prec_expr!r}")
        src = np.empty(edge_count, dtype=np.int32)
        tgt = np.empty(edge_count, dtype=np.int32)
        exact = np.empty(edge_count, dtype=np.bool_)
        strength = np.empty(edge_count, dtype=np.float32)
        event_ids = np.empty(edge_count, dtype=np.uint8)
        n = 0
        for row in conn.execute(
            f"SELECT source_id, target_id, {prec_expr}, strength, {event_expr} "
            "FROM interaction_edges"
        ):
            s = self.oid_to_idx.get(row[0])
            t = self.oid_to_idx.get(row[1])
            if s is None or t is None:
                continue
            src[n] = s
            tgt[n] = t
            exact[n] = row[2] == "exact"
            strength[n] = float(row[3]) if row[3] is not None else 0.0
            event_ids[n] = event_to_id.get(row[4], 255) if row[4] else 255
            n += 1
        src = src[:n]
        tgt = tgt[:n]
        exact = exact[:n]
        strength = strength[:n]
        event_ids = event_ids[:n]
        _log.info("Loaded %s edges from DB (%.1fs)", f"{n:,}", time.time()-t0)
        return src, tgt, exact, strength, event_ids, event_names

    def _load_adj_cache(self, adj_cache_path, edge_count, card_count, t0):
        """Try loading adjacency dicts from CSR-style npz cache. Returns True if loaded."""
        if not os.path.exists(adj_cache_path):
            return False
        try:
            cached = np.load(adj_cache_path, allow_pickle=False)
            # Validate counts against DB when available
            if edge_count is not None:
                if (int(cached['edge_count']) != edge_count
                        or int(cached['card_count']) != card_count
                        or bool(cached['has_strength']) != self._preload_strength):
                    return False
            elif bool(cached['has_strength']) != self._preload_strength:
                return False
            self._adj_out = CSRIndex.from_csr_arrays(
                cached['adj_out_keys'], cached['adj_out_offsets'], cached['adj_out_values'])
            self._adj_in = CSRIndex.from_csr_arrays(
                cached['adj_in_keys'], cached['adj_in_offsets'], cached['adj_in_values'])
            self._exact_out = CSRIndex.from_csr_arrays(
                cached['exact_out_keys'], cached['exact_out_offsets'], cached['exact_out_values'])
            self._exact_in = CSRIndex.from_csr_arrays(
                cached['exact_in_keys'], cached['exact_in_offsets'], cached['exact_in_values'])
            self._arr_hub_raw = cached['hub_raw']
            self._arr_hub_score = cached['hub_score']
            # PageRank computed live from CSR (fast, ~1s)
            self._arr_pagerank = self._compute_pagerank(len(self.oid_to_idx))
            self._agg_strength_out = {}
            self._agg_events_out = {}
            self._agg_strength_in = {}
            self._agg_events_in = {}
            if self._preload_strength and 'agg_str_out_keys' in cached:
                self._agg_strength_out, self._agg_events_out = self._load_agg_from_cache(
                    cached, 'agg_str_out')
                self._agg_strength_in, self._agg_events_in = self._load_agg_from_cache(
                    cached, 'agg_str_in')
            # Load event_names if present (saves loading 271 MiB raw edge cache)
            if 'event_names' in cached:
                self._bit_to_event = {
                    i: str(name) for i, name in enumerate(cached['event_names'])
                }
            _log.info("Loaded adjacency dicts from cache (%.1fs)", time.time()-t0)
            return True
        except (ValueError, KeyError, OSError) as e:
            _log.warning("Adjacency cache load failed, rebuilding: %s", e)
            return False

    @staticmethod
    def _load_agg_from_cache(cached, prefix):
        """Restore aggregated strength + event dicts from cache arrays."""
        keys = cached[f'{prefix}_keys']
        offsets = cached[f'{prefix}_offsets']
        val_keys = cached[f'{prefix}_val_keys']
        val_strengths = cached[f'{prefix}_val_strengths']
        val_events = cached[f'{prefix}_val_events']
        agg_strength = {}
        agg_events = {}
        for i, k in enumerate(keys):
            start, end = int(offsets[i]), int(offsets[i + 1])
            k_int = int(k)
            str_dict = {}
            evt_dict = {}
            for j in range(start, end):
                v = int(val_keys[j])
                str_dict[v] = float(val_strengths[j])
                evt_dict[v] = int(val_events[j])
            agg_strength[k_int] = str_dict
            agg_events[k_int] = evt_dict
        return agg_strength, agg_events

    @staticmethod
    def _serialize_agg_dicts(agg_strength, agg_events):
        """Convert aggregated strength + event nested dicts to flat arrays for npz."""
        if not agg_strength:
            empty_i = np.array([], dtype=np.int32)
            empty_o = np.array([0], dtype=np.int64)
            empty_f = np.array([], dtype=np.float32)
            empty_u = np.array([], dtype=np.uint32)
            return empty_i, empty_o, empty_i, empty_f, empty_u
        sorted_keys = np.array(sorted(agg_strength.keys()), dtype=np.int32)
        total = sum(len(agg_strength[int(k)]) for k in sorted_keys)
        offsets = np.zeros(len(sorted_keys) + 1, dtype=np.int64)
        val_keys = np.empty(total, dtype=np.int32)
        val_strengths = np.empty(total, dtype=np.float32)
        val_events = np.empty(total, dtype=np.uint32)
        pos = 0
        for i, k in enumerate(sorted_keys):
            k_int = int(k)
            str_dict = agg_strength[k_int]
            evt_dict = agg_events.get(k_int, {})
            for v, s in str_dict.items():
                val_keys[pos] = v
                val_strengths[pos] = s
                val_events[pos] = evt_dict.get(v, 0)
                pos += 1
            offsets[i + 1] = pos
        return sorted_keys, offsets, val_keys, val_strengths, val_events

    def _save_adj_cache(self, adj_cache_path, edge_count, card_count, event_names=None):
        """Save adjacency CSR indices to npz cache."""
        try:
            ao_k, ao_o, ao_v = self._adj_out.to_arrays()
            ai_k, ai_o, ai_v = self._adj_in.to_arrays()
            eo_k, eo_o, eo_v = self._exact_out.to_arrays()
            ei_k, ei_o, ei_v = self._exact_in.to_arrays()
            save_dict = {
                'edge_count': np.array(edge_count),
                'card_count': np.array(card_count),
                'has_strength': np.array(self._preload_strength),
                'adj_out_keys': ao_k, 'adj_out_offsets': ao_o, 'adj_out_values': ao_v,
                'adj_in_keys': ai_k, 'adj_in_offsets': ai_o, 'adj_in_values': ai_v,
                'exact_out_keys': eo_k, 'exact_out_offsets': eo_o, 'exact_out_values': eo_v,
                'exact_in_keys': ei_k, 'exact_in_offsets': ei_o, 'exact_in_values': ei_v,
                'hub_raw': self._arr_hub_raw,
                'hub_score': self._arr_hub_score,
            }
            if event_names is not None:
                save_dict['event_names'] = event_names
            if self._preload_strength:
                so_k, so_o, so_vk, so_vs, so_ve = self._serialize_agg_dicts(
                    self._agg_strength_out, self._agg_events_out)
                si_k, si_o, si_vk, si_vs, si_ve = self._serialize_agg_dicts(
                    self._agg_strength_in, self._agg_events_in)
                save_dict.update({
                    'agg_str_out_keys': so_k, 'agg_str_out_offsets': so_o,
                    'agg_str_out_val_keys': so_vk, 'agg_str_out_val_strengths': so_vs,
                    'agg_str_out_val_events': so_ve,
                    'agg_str_in_keys': si_k, 'agg_str_in_offsets': si_o,
                    'agg_str_in_val_keys': si_vk, 'agg_str_in_val_strengths': si_vs,
                    'agg_str_in_val_events': si_ve,
                })
            np.savez(adj_cache_path, **save_dict)
            _log.info("Cached adjacency dicts to %s", adj_cache_path)
        except (OSError, ValueError) as e:
            _log.warning("Adjacency cache write failed: %s", e)

    def _build_adj_from_arrays(self, src, tgt, exact, strength, event_ids):
        """Build CSRIndex adjacency from raw edge arrays."""
        adj_out_dict = self._build_adj_arrays(src, tgt)
        adj_in_dict = self._build_adj_arrays(tgt, src)
        self._adj_out = CSRIndex.from_dict(adj_out_dict)
        self._adj_in = CSRIndex.from_dict(adj_in_dict)

        # Exact-precision adjacency
        if exact.any():
            self._exact_out = CSRIndex.from_dict(self._build_adj_arrays(src[exact], tgt[exact]))
            self._exact_in = CSRIndex.from_dict(self._build_adj_arrays(tgt[exact], src[exact]))
        else:
            self._exact_out = CSRIndex.empty()
            self._exact_in = CSRIndex.empty()

        # Aggregated strength + event dicts for commander edge lookups (no SQL needed).
        # Only built for training (preload_strength=True); inference uses SQL fallback.
        if self._preload_strength:
            self._agg_strength_out, self._agg_events_out = self._build_agg_arrays(
                src, tgt, strength, event_ids)
            self._agg_strength_in, self._agg_events_in = self._build_agg_arrays(
                tgt, src, strength, event_ids)
        else:
            self._agg_strength_out = {}
            self._agg_events_out = {}
            self._agg_strength_in = {}
            self._agg_events_in = {}

        # Build hub score arrays
        n = len(self.oid_to_idx)
        for i in range(n):
            n_out = self._adj_out.neighbor_count(i)
            n_in = self._adj_in.neighbor_count(i)
            raw = float(n_out + n_in)
            self._arr_hub_raw[i] = raw
            self._arr_hub_score[i] = np.log2(1.0 + min(raw, 500))

        # PageRank on the causal graph (20 iterations, damping=0.85)
        self._arr_pagerank = self._compute_pagerank(n)

    def _compute_pagerank(self, n, damping=0.85, n_iter=20):
        """Compute PageRank on the outgoing adjacency graph."""
        pr = np.ones(n, dtype=np.float32) / n
        out_keys, out_offsets, out_vals = self._adj_out.to_arrays()
        # Precompute out-degree for each node
        out_degree = np.zeros(n, dtype=np.float32)
        for ki in range(len(out_keys)):
            k = int(out_keys[ki])
            out_degree[k] = float(out_offsets[ki + 1] - out_offsets[ki])
        for _ in range(n_iter):
            new_pr = np.full(n, (1.0 - damping) / n, dtype=np.float32)
            # For each node with outgoing edges, distribute PR to neighbors
            for ki in range(len(out_keys)):
                k = int(out_keys[ki])
                start, end = int(out_offsets[ki]), int(out_offsets[ki + 1])
                if end > start and out_degree[k] > 0:
                    contrib = damping * pr[k] / out_degree[k]
                    np.add.at(new_pr, out_vals[start:end], contrib)
            pr = new_pr
        # Log-transform for feature scale
        return np.log2(1.0 + pr * n)

    @staticmethod
    def _build_adj_arrays(keys, values):
        """Build adjacency dict: key_idx -> sorted numpy array of unique value indices."""
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

    @staticmethod
    def _accumulate_group(sv, ss, se, start, end):
        """Accumulate strength sums and event bitmasks for a key group."""
        str_dict = {}
        evt_dict = {}
        for j in range(start, end):
            v = int(sv[j])
            str_dict[v] = str_dict.get(v, 0.0) + float(ss[j])
            eid = int(se[j])
            if eid < 32:
                evt_dict[v] = evt_dict.get(v, 0) | (1 << eid)
        return str_dict, evt_dict

    @staticmethod
    def _build_agg_arrays(keys, values, strengths, event_ids):
        """Build aggregated strength + event dicts per (key, value) pair.

        Returns:
            agg_strength: {key_idx: {val_idx: sum_of_strength}}
            agg_events:   {key_idx: {val_idx: uint32_bitmask_of_events}}
        """
        if len(keys) == 0:
            return {}, {}
        order = np.argsort(keys)
        sk = keys[order]
        sv = values[order]
        ss = strengths[order]
        se = event_ids[order]

        agg_strength = {}
        agg_events = {}

        changes = np.concatenate([[0], np.where(sk[1:] != sk[:-1])[0] + 1, [len(sk)]])
        for i in range(len(changes) - 1):
            start, end = int(changes[i]), int(changes[i + 1])
            k = int(sk[start])
            str_dict, evt_dict = ForgeFeatureContext._accumulate_group(sv, ss, se, start, end)
            agg_strength[k] = str_dict
            agg_events[k] = evt_dict

        return agg_strength, agg_events

    # ── Functional fingerprint dimension layout ──
    # produces (12): tokens, p1p1_counters, other_counters, mana, cards_drawn,
    #   damage, lifegain, removal, reanimate, pump_buff, proliferate, move_counter
    # requires (11): creature_etb, creature_death, spell_cast, combat,
    #   damage_dealt, lifegain_trigger, discard, cycling, sacrifice, landfall, counter_placed
    # amplifies (4): counter_doubler, token_doubler, damage_doubler, lifegain_doubler
    # targets (6): creatures, self_only, lands, players, artifacts, any_permanent
    _FUNC_DIM = 33
    _FUNC_PRODUCES_SLICE = slice(0, 12)
    _FUNC_REQUIRES_SLICE = slice(12, 23)
    _FUNC_AMPLIFIES_SLICE = slice(23, 27)
    _FUNC_TARGETS_SLICE = slice(27, 33)

    def _build_func_fingerprints(self, conn):
        """Build functional fingerprint vectors from Forge ability data.

        Each card gets a 33-dim vector encoding what it does semantically:
        produces, requires (triggers), amplifies (doublers), targets.
        """
        fingerprints = {}

        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.trigger_filter, "
            "fa.raw_line, fa.counter_type "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            oid, verb, trig_mode, trig_filter, raw_line, counter_type = row
            raw_line = raw_line or ""

            if oid not in fingerprints:
                fingerprints[oid] = np.zeros(self._FUNC_DIM, dtype=np.float32)
            fp = fingerprints[oid]

            # ── PRODUCES (dims 0-11) ──
            if verb == 'Token':
                fp[0] = 1.0  # tokens
            if verb in ('PutCounter', 'PutCounterAll'):
                if counter_type == 'P1P1' or 'P1P1' in raw_line:
                    fp[1] = 1.0  # p1p1_counters
                else:
                    fp[2] = 1.0  # other_counters
            if verb == 'Mana':
                fp[3] = 1.0  # mana
            if verb in ('Draw', 'Dig'):
                fp[4] = 1.0  # cards_drawn
            if verb in ('DealDamage', 'DamageAll'):
                fp[5] = 1.0  # damage
            if verb == 'GainLife':
                fp[6] = 1.0  # lifegain
            if verb in ('Destroy', 'DestroyAll', 'Sacrifice'):
                fp[7] = 1.0  # removal
            if verb == 'ChangeZone':
                # Reanimate: moves from graveyard to battlefield
                orig = _RE_ORIGIN.search(raw_line)
                dest = _RE_DESTINATION.search(raw_line)
                if (orig and 'Graveyard' in orig.group(1) and
                        dest and 'Battlefield' in dest.group(1)):
                    fp[8] = 1.0  # reanimate
            if verb in ('Pump', 'PumpAll') or (verb == 'Continuous' and 'AddPower$' in raw_line):
                fp[9] = 1.0  # pump_buff
            if verb == 'Proliferate':
                fp[10] = 1.0  # proliferate
            if verb == 'MoveCounter':
                fp[11] = 1.0  # move_counter

            # ── REQUIRES / triggers (dims 12-22) ──
            if trig_mode == 'ChangesZone':
                dest = _RE_DESTINATION.search(raw_line)
                orig = _RE_ORIGIN.search(raw_line)
                filt = trig_filter or ''
                if dest and 'Battlefield' in dest.group(1):
                    if 'Creature' in filt or 'Human' in filt or (not filt and 'Land' not in filt):
                        fp[12] = 1.0  # creature_etb
                    if 'Land' in filt:
                        fp[21] = 1.0  # landfall
                if dest and 'Graveyard' in dest.group(1):
                    fp[13] = 1.0  # creature_death
                if orig and 'Battlefield' in orig.group(1) and dest and 'Graveyard' in dest.group(1):
                    fp[13] = 1.0  # creature_death (dies)
            if trig_mode in ('SpellCast', 'SpellCopy'):
                fp[14] = 1.0  # spell_cast
            if trig_mode in ('Attacks', 'Blocks', 'AttackerBlocked',
                             'DeclareAttackers', 'DeclareBlockers'):
                fp[15] = 1.0  # combat
            if trig_mode == 'DamageDone':
                fp[16] = 1.0  # damage_dealt
            if trig_mode == 'LifeGained':
                fp[17] = 1.0  # lifegain_trigger
            if trig_mode == 'Discarded':
                fp[18] = 1.0  # discard
            if trig_mode == 'Cycled':
                fp[19] = 1.0  # cycling
            if trig_mode == 'Sacrificed':
                fp[20] = 1.0  # sacrifice
            if trig_mode in ('CounterAdded', 'CounterAddedOnce'):
                fp[22] = 1.0  # counter_placed

            # ── AMPLIFIES / replacement effects (dims 23-26) ──
            event_m = _RE_EVENT.search(raw_line)
            repl_m = _RE_REPLACE_WITH.search(raw_line)
            if event_m and repl_m:
                event = event_m.group(1)
                repl = repl_m.group(1)
                if event == 'AddCounter' and ('Double' in repl or 'OneMore' in repl):
                    fp[23] = 1.0  # counter_doubler
                if event == 'CreateToken' and 'Double' in repl:
                    fp[24] = 1.0  # token_doubler
                if event == 'DamageDone' and 'Twice' in repl:
                    fp[25] = 1.0  # damage_doubler
                if event == 'GainLife' and 'Double' in repl:
                    fp[26] = 1.0  # lifegain_doubler

            # ── TARGETS (dims 27-32) ──
            for field in ('ValidTgts', 'Affected'):
                tgt_m = _RE_FP_TARGET_FIELDS[field].search(raw_line)
                if tgt_m:
                    tgt = tgt_m.group(1)
                    if tgt.startswith('Card.Self') or tgt == 'Card.Self':
                        fp[28] = 1.0  # self_only
                    elif 'Creature' in tgt:
                        fp[27] = 1.0  # targets_creatures
                    if 'Land' in tgt and 'Creature' not in tgt:
                        fp[29] = 1.0  # targets_lands
                    if tgt.startswith('Player') or tgt.startswith('Opponent'):
                        fp[30] = 1.0  # targets_players
                    if 'Artifact' in tgt:
                        fp[31] = 1.0  # targets_artifacts
                    if tgt in ('Any', 'Permanent') or tgt.startswith('Permanent'):
                        fp[32] = 1.0  # targets_any

        return fingerprints



# Re-export compute functions for backward compatibility
from mtg_synergy.recommend.forge_compute import (  # noqa: E402
    CmdrFeatureContext,
    compute_batch_features,
)
