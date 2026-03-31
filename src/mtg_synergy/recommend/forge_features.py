"""Shared forge feature computation for training and inference.

Extracts the 105-feature forge GBM feature vector computation into a
single module used by both train_fusion_model.py and scoring.py.

No oracle-text embeddings or tower model — pure Forge-native features.
"""
import logging
import os
import re
import sqlite3
import time

import numpy as np

_log = logging.getLogger(__name__)

# Set EDHREC_FREE=1 to disable edhrec_deck_pct feature (pure Forge-native mode)
_EDHREC_FREE = os.environ.get("EDHREC_FREE", "") == "1"

from mtg_synergy.recommend.mechanics_vectors import _concept_idx

# Known-safe SQL fragments for event column access (avoids f-string injection)
_EVENT_EXPR_COLUMN = "event"
_EVENT_EXPR_JSON = "json_extract(detail, '$.event')"


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

    def __init__(self, conn, preload_edges=False, preload_strength=False):
        self.conn = conn
        self._has_edge_index = False
        self._preload_strength = preload_strength

        # Check for materialized event column (speeds up commander edge queries)
        self._has_event_col = any(
            r[1] == "event"
            for r in conn.execute("PRAGMA table_info(interaction_edges)")
        )

        self._load_card_data(conn)
        self._load_forge_profiles(conn)  # populates self._raw_abilities
        # Note: _load_functional_fingerprints does its own DB query (independent
        # of self._raw_abilities). Must stay BEFORE del self._raw_abilities below.
        self._load_functional_fingerprints(conn)
        self._load_deck_tags(conn)
        self._load_ability_vectors(conn)
        self._load_theme_sets(conn)
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

        # Forge mechanics vectors: encode each card's full mechanical profile
        # Pass pre-loaded abilities to avoid redundant forge_abilities DB scan
        from mtg_synergy.recommend.mechanics_vectors import build_mechanics_vectors
        self._mech_produces, self._mech_consumes, self._mech_dim, _ = \
            build_mechanics_vectors(conn, preloaded_abilities=self._raw_abilities)
        # Free raw abilities list after mechanics vectors are built (~5MB)
        del self._raw_abilities

        # ── Pre-encode per-card arrays for batch feature computation ──
        self._build_card_arrays()

        if preload_edges:
            self._build_edge_index(conn)

    def _load_card_data(self, conn):
        """Load card index, type lines, strategies, and phase data."""
        # Build oid_to_idx from cards table (replaces embedding-based index)
        self.oid_to_idx = {}
        for i, (oid,) in enumerate(
            conn.execute("SELECT DISTINCT oracle_id FROM cards")
        ):
            self.oid_to_idx[oid] = i

        # Pre-cache type_lines for CmdrFeatureContext (avoids per-commander SQL)
        self._type_lines = {}
        for oid, tl in conn.execute("SELECT oracle_id, type_line FROM cards"):
            if tl:
                self._type_lines[oid] = tl

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

    def _load_forge_profiles(self, conn):
        """Load forge ability profiles and raw abilities from DB."""
        # Forge ability profiles: per-card structured data from forge_abilities
        # Replaces oracle text regex matching for features F25-F30
        self._forge_profiles = {}
        # Also collect raw abilities for build_mechanics_vectors (avoids redundant DB scan)
        # Output format consumed by mechanics_vectors.py:
        #   (oid, verb, trig_mode, trig_filter, cost, kw, token_script,
        #    counter, raw_line, amount, trigger_origin, trigger_destination)
        self._raw_abilities = []
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.trigger_filter, "
            "fa.cost, fa.keyword, fa.token_script, fa.counter_type, fa.raw_line, "
            "fa.amount, fa.trigger_origin, fa.trigger_destination, "
            "fa.target, fa.ability_type, fa.defined "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            # row[0..11] match the output tuple directly; row[12..14] used only for profiles below
            self._raw_abilities.append(row[:12])
            self._process_forge_ability_row(row)

        # Post-process: static anthem is only meaningful if card has NO PutCounter
        for p in self._forge_profiles.values():
            if p['has_static_anthem'] and ('PutCounter' in p['verbs'] or 'PutCounterAll' in p['verbs']):
                p['has_static_anthem'] = False  # card also places real counters, not just anthem

        # Pre-compute card-level mechanical unions (avoid redundant set ops in inner loop)
        self._card_mechs = {}
        for oid, p in self._forge_profiles.items():
            mechs = p['verbs'] | p['triggers'] | p['keywords']
            if mechs:
                self._card_mechs[oid] = mechs

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
            'effect_zones': set(), 'scales_with': set(),
            'grants_types': set(), 'damage_amount': None,
            'cards_drawn': None, 'life_amount': None,
            'is_secondary': False, 'gain_control': False,
            'produces_mana': False, 'mana_colors': set(),
            'counter_num_variable': False, 'grants_abilities': False,
            'token_amount_variable': False,
            'excluded_subtypes': set(),
            'has_static_anthem': False, 'counters_on_lands': False,
            'counter_trigger_themes': set(), 'has_p1p1': False,
        })
        if verb: p['verbs'].add(verb)
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
        if trig_filter:
            for part in trig_filter.split(","):
                main = part.split(".")[0].strip()
                if main and main != "Card" and main[0].isupper():
                    p['trigger_filters'].add(main.lower())
        # Extract subtype requirements from cost, defined, and raw_line fields
        cost_str = cost or ""
        defined_str = defined or ""
        raw_line = raw_line_val or ""
        # Cost subtypes: tapXType<1/Cleric>, Sac<1/Human>
        for m in re.findall(r'tapXType<\d+/(\w+)', cost_str):
            if m not in ("CARDNAME",):
                p['required_subtypes'].add(m.lower())
        for m in re.findall(r'Sac<\d+/(\w+)', cost_str):
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
        _generic = {"card", "creature", "permanent", "self", "other",
                    "youctrl", "oppctrl", "strictlyother", "token", "nontoken"}
        self._extract_subtypes_from_raw_line(p, raw_line, _generic)
        self._extract_keywords_and_conditions(p, verb, raw_line)
        self._extract_counters_and_special(p, row, raw_line)

    @staticmethod
    def _extract_subtypes_from_raw_line(p, raw_line, _generic):
        """Extract required/excluded subtypes from raw_line fields."""
        for field in ('ValidCards', 'ValidCard', 'ValidTgts',
                      'ValidAttackers', 'Affected', 'AddsCounters'):
            m = re.search(rf'{field}\$\s*(\S+)', raw_line)
            if not m:
                continue
            for part in m.group(1).split(","):
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
            dm = re.search(rf'{desc_field}\$\s*(.+?)(?:\||$)', raw_line)
            if dm:
                for nm in re.finditer(r'non-(\w+)\s+creature', dm.group(1), re.IGNORECASE):
                    excl = nm.group(1).lower()
                    if excl not in _generic and len(excl) > 2:
                        p['excluded_subtypes'].add(excl)

    @staticmethod
    def _extract_keywords_and_conditions(p, verb, raw_line):
        """Extract granted keywords, conditions, duration, zones, scaling, and types."""
        # --- Granted keywords: AddKeyword$, KW$, PumpKeywords$, Keywords$ ---
        for kw_field in ('AddKeyword', 'KW', 'PumpKeywords', 'Keywords'):
            m = re.search(rf'{kw_field}\$\s*([^|]+)', raw_line)
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
            m = re.search(rf'{cond_field}\$\s*(\S+)', raw_line)
            if m:
                for part in m.group(1).split(","):
                    main = part.split(".")[0].split("+")[0].strip()
                    if main and main[0].isupper() and len(main) > 2:
                        p['conditions'].add(main.lower())
        # --- Duration$ ---
        m = re.search(r'Duration\$\s*(\S+)', raw_line)
        if m:
            p['duration'].add(m.group(1).lower())
        elif verb in ('Pump', 'PumpAll') and 'Duration$' not in raw_line:
            # Pump without Duration → temporary buff
            p['duration'].add('temporary')
        # --- CombatDamage$ True ---
        if 'CombatDamage$ True' in raw_line:
            p['combat_damage'] = True
        # --- Effect zones: ActiveZones$, EffectZone$, AffectedZone$ ---
        for zone_field in ('ActiveZones', 'EffectZone', 'AffectedZone'):
            m = re.search(rf'{zone_field}\$\s*(\S+)', raw_line)
            if m:
                for z in m.group(1).split(","):
                    z = z.strip()
                    if z:
                        p['effect_zones'].add(z.lower())
        # --- Scales with: SetPower$ X or AddPower$ X ---
        for pw_field in ('SetPower', 'AddPower'):
            m = re.search(rf'{pw_field}\$\s*(\S+)', raw_line)
            if m and m.group(1) in ('X', 'Y'):
                p['scales_with'].add('variable_pt')
                # Try to extract what it scales with from Description$
                desc_m = re.search(r'Description\$\s*(.+?)(?:\||$)', raw_line)
                if desc_m:
                    desc = desc_m.group(1).lower()
                    if 'for each' in desc or 'equal to' in desc:
                        p['scales_with'].add('count_based')
        # --- Grants types: Types$, AddType$ ---
        for type_field in ('Types', 'AddType'):
            m = re.search(rf'{type_field}\$\s*(\S+)', raw_line)
            if m:
                for t in m.group(1).split(","):
                    t = t.strip()
                    if t and t[0].isupper():
                        p['grants_types'].add(t.lower())

    @staticmethod
    def _extract_counters_and_special(p, row, raw_line):
        """Extract damage/draw/life amounts, mana, counters, and special flags."""
        verb = row[1]
        # --- Damage amount: NumDmg$ ---
        m = re.search(r'NumDmg\$\s*(\S+)', raw_line)
        if m:
            val = m.group(1)
            # Keep the latest (most relevant) if multiple abilities
            if p['damage_amount'] is None or val == 'X':
                p['damage_amount'] = val
        # --- Cards drawn: NumCards$ ---
        m = re.search(r'NumCards\$\s*(\S+)', raw_line)
        if m:
            val = m.group(1)
            if p['cards_drawn'] is None or val == 'X':
                p['cards_drawn'] = val
        # --- Life amount: LifeAmount$ ---
        m = re.search(r'LifeAmount\$\s*(\S+)', raw_line)
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
        m = re.search(r'Produced\$\s*(\S+)', raw_line)
        if m:
            p['produces_mana'] = True
            prod = m.group(1)
            for c in ('W', 'U', 'B', 'R', 'G'):
                if c in prod:
                    p['mana_colors'].add(c)
            if prod in ('Any', 'Combo', 'Chosen'):
                p['mana_colors'].update({'W', 'U', 'B', 'R', 'G'})
        # --- Counter quantity variable: CounterNum$ X/Y ---
        m = re.search(r'CounterNum\$\s*(\S+)', raw_line)
        if m and m.group(1) in ('X', 'Y', 'All', 'Any'):
            p['counter_num_variable'] = True
        # --- Grants abilities: AddAbility$ ---
        if 'AddAbility$' in raw_line:
            p['grants_abilities'] = True
        # --- Token amount variable: TokenAmount$ X ---
        m = re.search(r'TokenAmount\$\s*(\S+)', raw_line)
        if m and m.group(1) in ('X', 'Y'):
            p['token_amount_variable'] = True
        # --- Static anthem: Continuous + AddPower (not actual counters) ---
        if verb == 'Continuous' and 'AddPower$' in raw_line:
            p['has_static_anthem'] = True
        # --- Counters on lands: PutCounter targeting lands, or Earthbend ---
        if verb in ('PutCounter', 'PutCounterAll'):
            vtgt = re.search(r'ValidTgts\$\s*(\S+)', raw_line)
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

        # Reverse index: tag → set of oids that "has" it (for needs_rarity feature)
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

    def _load_theme_sets(self, conn):
        """Load theme detection sets for equipment, defender, enchantress, ETB."""
        # ── Theme detection sets: identify cards by archetype relevance ──
        # Equipment theme: cards that ARE equipment or CARE about equipment
        self._equipment_cards = set()    # oids of Equipment type cards
        self._equipment_payoffs = set()  # oids that trigger on/care about equipment
        self._defender_cards = set()     # oids with Defender keyword
        self._enchantress_payoffs = set()  # oids that trigger on enchantment ETBs
        self._etb_doublers = set()       # oids with Panharmonicon-class effects

        # Equipment: cards with Equip keyword or Type$Equipment in deck_has
        for oid, p in self._forge_profiles.items():
            if 'Equip' in p['keywords']:
                self._equipment_cards.add(oid)
            if p['verbs'] & {'Attach'}:
                self._equipment_payoffs.add(oid)
            if 'Defender' in p['keywords']:
                self._defender_cards.add(oid)
            if 'CanAttackDefender' in p['verbs']:
                self._defender_cards.add(oid)
            # Check conditions for equipment references (e.g., Balan "IsPresent$ Equipment")
            if 'equipment' in p.get('conditions', set()):
                self._equipment_payoffs.add(oid)

        # Also detect equipment payoffs from raw_line patterns
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id, fa.raw_line FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.raw_line LIKE '%Equipment%'"
        ):
            oid, raw = row
            if raw and re.search(
                r'(IsPresent|RepeatCards|Condition|ValidCards?|Affected)\$[^|]*Equipment', raw
            ):
                self._equipment_payoffs.add(oid)

        # ETB doublers: cards with Panharmonicon verb
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.verb = 'Panharmonicon'"
        ):
            self._etb_doublers.add(row[0])

        # Equipment payoffs from deck tags: cards that hints/needs Type$Equipment
        # Enchantress payoffs: cards that hints/needs Type$Enchantment (and trigger on it)
        for oid, tags in self._deck_hints.items():
            for tag in tags:
                if 'Type$Equipment' in tag:
                    self._equipment_payoffs.add(oid)
                if 'Type$Enchantment' in tag:
                    self._enchantress_payoffs.add(oid)
        for oid, tags in self._deck_needs.items():
            for tag in tags:
                if 'Type$Equipment' in tag:
                    self._equipment_payoffs.add(oid)
                if 'Type$Enchantment' in tag:
                    self._enchantress_payoffs.add(oid)
                if 'Keyword$Defender' in tag:
                    self._defender_cards.add(oid)
        # Also mark equipment cards via deck_has (cards that provide equipment theme)
        for oid, tags in self._deck_has.items():
            for tag in tags:
                if 'Type$Equipment' in tag:
                    self._equipment_cards.add(oid)

        # Enchantress payoffs from triggers: cards that trigger on SpellCast with
        # enchantment filter (e.g., Setessan Champion, Eidolon of Blossoms)
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_mode = 'SpellCast' AND fa.trigger_filter LIKE '%Enchantment%'"
        ):
            self._enchantress_payoffs.add(row[0])
        # Also cards with ChangesZone trigger + Enchantment filter (constellation)
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.trigger_mode IN ('ChangesZone', 'ChangesZoneAll') "
            "AND fa.trigger_filter LIKE '%Enchantment%'"
        ):
            self._enchantress_payoffs.add(row[0])

        # Commander theme detection helpers: which deck tags indicate themes
        self._equipment_theme_tags = {'Type$Equipment', 'Ability$Equip'}
        self._defender_theme_tags = {'Keyword$Defender'}
        self._enchantress_theme_tags = {'Type$Enchantment', 'Type$Aura'}

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
                # Build name→oracle_id lookup (fast Python join instead of slow SQL LOWER)
                name_to_oid = {}
                for row in conn.execute("SELECT oracle_id, name FROM cards"):
                    name_to_oid[row[1].lower()] = row[0]
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
        self._arr_strat_count = np.zeros(n, dtype=np.float32)

        for oid, i in self.oid_to_idx.items():
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
            self._arr_strat_count[i] = min(len(self.card_strats.get(oid, set())), 5)

        # ── Profile-derived boolean flags ──
        self._arr_combat_damage = np.zeros(n, dtype=np.float32)
        self._arr_scales_with = np.zeros(n, dtype=np.float32)
        self._arr_is_secondary = np.zeros(n, dtype=np.float32)
        self._arr_gain_control = np.zeros(n, dtype=np.float32)
        self._arr_produces_mana = np.zeros(n, dtype=np.float32)
        self._arr_counter_num_var = np.zeros(n, dtype=np.float32)
        self._arr_grants_abilities = np.zeros(n, dtype=np.float32)
        self._arr_token_amt_var = np.zeros(n, dtype=np.float32)
        self._arr_has_static_anthem = np.zeros(n, dtype=np.float32)
        self._arr_counters_on_lands = np.zeros(n, dtype=np.float32)
        self._arr_has_p1p1 = np.zeros(n, dtype=np.float32)
        self._arr_dur_permanent = np.zeros(n, dtype=np.float32)
        self._arr_dur_temporary = np.zeros(n, dtype=np.float32)
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
        # Sets needed for batch computation (kept as lists of sets)
        self._arr_zone_gy = np.zeros(n, dtype=np.float32)
        self._arr_zone_ex = np.zeros(n, dtype=np.float32)
        self._arr_is_etb_doubler = np.zeros(n, dtype=np.float32)
        self._arr_is_equipment = np.zeros(n, dtype=np.float32)
        self._arr_is_equip_payoff = np.zeros(n, dtype=np.float32)
        self._arr_is_ench_payoff = np.zeros(n, dtype=np.float32)
        self._arr_is_defender = np.zeros(n, dtype=np.float32)
        # Pure enchantment (not creature) flag for enchantress
        self._arr_pure_enchantment = np.zeros(n, dtype=np.float32)

        for oid, i in self.oid_to_idx.items():
            p = self._forge_profiles.get(oid, {})
            self._arr_combat_damage[i] = 1.0 if p.get('combat_damage', False) else 0.0
            self._arr_scales_with[i] = 1.0 if p.get('scales_with', set()) else 0.0
            self._arr_is_secondary[i] = 1.0 if p.get('is_secondary', False) else 0.0
            self._arr_gain_control[i] = 1.0 if p.get('gain_control', False) else 0.0
            self._arr_produces_mana[i] = 1.0 if p.get('produces_mana', False) else 0.0
            self._arr_counter_num_var[i] = 1.0 if p.get('counter_num_variable', False) else 0.0
            self._arr_grants_abilities[i] = 1.0 if p.get('grants_abilities', False) else 0.0
            self._arr_token_amt_var[i] = 1.0 if p.get('token_amount_variable', False) else 0.0
            self._arr_has_static_anthem[i] = 1.0 if p.get('has_static_anthem', False) else 0.0
            self._arr_counters_on_lands[i] = 1.0 if p.get('counters_on_lands', False) else 0.0
            self._arr_has_p1p1[i] = 1.0 if p.get('has_p1p1', False) else 0.0
            dur = p.get('duration', set())
            self._arr_dur_permanent[i] = 1.0 if 'permanent' in dur else 0.0
            self._arr_dur_temporary[i] = 1.0 if 'temporary' in dur else 0.0
            atypes = p.get('ability_types', set())
            self._arr_has_T[i] = 1.0 if 'T' in atypes else 0.0
            self._arr_has_A[i] = 1.0 if 'A' in atypes else 0.0
            self._arr_has_phase[i] = 1.0 if self.card_phase_order.get(oid) else 0.0
            depth = (len(p.get('verbs', set())) + len(p.get('triggers', set())) +
                     len(p.get('keywords', set())) + len(p.get('counter_types', set())))
            self._arr_forge_depth[i] = min(depth, 10.0)
            self._arr_damage_scales[i] = 1.0 if p.get('damage_amount') in ('X', 'Y') else 0.0
            self._arr_draw_scales[i] = 1.0 if p.get('cards_drawn') in ('X', 'Y') else 0.0
            self._arr_life_scales[i] = 1.0 if p.get('life_amount') in ('X', 'Y') else 0.0
            self._arr_granted_kw_count[i] = min(len(p.get('granted_keywords', set())), 5)
            self._arr_condition_count[i] = min(len(p.get('conditions', set())), 5)
            verbs = p.get('verbs', set())
            self._arr_n_counter_verbs[i] = sum(1 for v in verbs if v in ('PutCounter', 'PutCounterAll'))
            self._arr_n_pump_verbs[i] = sum(1 for v in verbs if v in ('Pump', 'PumpAll'))
            # Broader counter verb check for F78 (includes Proliferate, MoveCounter)
            self._arr_has_any_counter_verb[i] = 1.0 if verbs & {'PutCounter', 'PutCounterAll', 'Proliferate', 'MoveCounter'} else 0.0
            self._arr_zone_gy[i] = 1.0 if oid in self._zone_graveyard else 0.0
            self._arr_zone_ex[i] = 1.0 if oid in self._zone_exile else 0.0
            self._arr_is_etb_doubler[i] = 1.0 if oid in self._etb_doublers else 0.0
            self._arr_is_equipment[i] = 1.0 if oid in self._equipment_cards else 0.0
            self._arr_is_equip_payoff[i] = 1.0 if oid in self._equipment_payoffs else 0.0
            self._arr_is_ench_payoff[i] = 1.0 if oid in self._enchantress_payoffs else 0.0
            self._arr_is_defender[i] = 1.0 if oid in self._defender_cards else 0.0
            tl = self._type_lines.get(oid, "")
            if "Enchantment" in tl and "Creature" not in tl:
                self._arr_pure_enchantment[i] = 0.5
            if "Wall" in tl:
                self._arr_is_defender[i] = 1.0

        # Hub scores (computed from edge index, filled after edge loading)
        self._arr_hub_score = np.zeros(n, dtype=np.float32)
        self._arr_hub_raw = np.zeros(n, dtype=np.float32)
        self._hub_scores_built = False

    def _build_edge_index(self, conn):
        """Pre-load edge adjacency + strength + events into memory.

        Caches raw numpy arrays to data/edge_index_cache.npz (~2s reload vs ~40s DB scan).
        Cache key: interaction_edges row count + card count.
        Stores: src, tgt, exact, strength (float32), event_ids (uint8), event_names.
        """
        from mtg_synergy.config import DATA_DIR

        _log.info("Building in-memory edge index...")
        t0 = time.time()

        cache_path = os.path.join(DATA_DIR, "edge_index_cache.npz")
        edge_count = conn.execute("SELECT COUNT(*) FROM interaction_edges").fetchone()[0]
        card_count = len(self.oid_to_idx)

        # Try loading from cache (numpy arrays only)
        src = tgt = exact = strength = event_ids = event_names = None
        if os.path.exists(cache_path):
            try:
                cached = np.load(cache_path)
                if (int(cached['edge_count']) == edge_count and
                    int(cached['card_count']) == card_count and
                    'strength' in cached and 'event_ids' in cached
                    and 'event_names' in cached):
                    src = cached['src']
                    tgt = cached['tgt']
                    exact = cached['exact']
                    strength = cached['strength']
                    event_ids = cached['event_ids']
                    event_names = cached['event_names']
                    _log.info("Loaded %s edges from cache (%.1fs)", f"{len(src):,}", time.time()-t0)
            except (ValueError, KeyError, OSError) as e:
                _log.warning("Edge index cache load failed, rebuilding: %s", e)

        if src is None:
            src, tgt, exact, strength, event_ids, event_names = (
                self._load_edges_from_db(conn, edge_count, t0)
            )
            try:
                np.savez(cache_path, src=src, tgt=tgt, exact=exact,
                         strength=strength, event_ids=event_ids,
                         event_names=event_names,
                         edge_count=np.array(edge_count), card_count=np.array(card_count))
                _log.info("Cached to %s", cache_path)
            except Exception as e:
                _log.warning("Cache write failed: %s", e)

        # Build event lookup dicts
        self._event_names = list(event_names)
        self._bit_to_event = {i: str(name) for i, name in enumerate(event_names)}

        # Try loading pre-built adjacency dicts from CSR-style npz cache.
        adj_cache_path = os.path.join(DATA_DIR, "edge_adj_cache.npz")
        adj_loaded = self._load_adj_cache(adj_cache_path, edge_count, card_count, t0)

        if not adj_loaded:
            self._build_adj_from_arrays(src, tgt, exact, strength, event_ids)
            self._save_adj_cache(adj_cache_path, edge_count, card_count)

        del src, tgt, exact, strength, event_ids

        self._idx_to_oid = {v: k for k, v in self.oid_to_idx.items()}
        self._n_cards_idx = len(self.oid_to_idx)
        self._has_edge_index = True

        elapsed = time.time() - t0
        n_unique = sum(len(v) for v in self._adj_out.values())
        _log.info("Edge index built: %.1fs, %s unique outgoing pairs", elapsed, f"{n_unique:,}")

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
        prec_expr = "filter_precision" if has_col else "json_extract(detail, '$.filter_precision')"
        event_expr = _EVENT_EXPR_COLUMN if self._has_event_col else _EVENT_EXPR_JSON
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

    @staticmethod
    def _adj_dict_to_csr(adj):
        """Convert adjacency dict {int: np.array(int32)} to CSR-like arrays."""
        if not adj:
            return np.array([], dtype=np.int32), np.array([0], dtype=np.int64), np.array([], dtype=np.int32)
        sorted_keys = np.array(sorted(adj.keys()), dtype=np.int32)
        lengths = np.array([len(adj[int(k)]) for k in sorted_keys], dtype=np.int64)
        offsets = np.zeros(len(sorted_keys) + 1, dtype=np.int64)
        np.cumsum(lengths, out=offsets[1:])
        total = int(offsets[-1])
        values = np.empty(total, dtype=np.int32)
        for i, k in enumerate(sorted_keys):
            start, end = int(offsets[i]), int(offsets[i + 1])
            values[start:end] = adj[int(k)]
        return sorted_keys, offsets, values

    @staticmethod
    def _csr_to_adj_dict(keys, offsets, values):
        """Restore adjacency dict from CSR-like arrays."""
        result = {}
        for i, k in enumerate(keys):
            start, end = int(offsets[i]), int(offsets[i + 1])
            result[int(k)] = values[start:end].copy()
        return result

    def _load_adj_cache(self, adj_cache_path, edge_count, card_count, t0):
        """Try loading adjacency dicts from CSR-style npz cache. Returns True if loaded."""
        if not os.path.exists(adj_cache_path):
            return False
        try:
            cached = np.load(adj_cache_path, allow_pickle=False)
            if (int(cached['edge_count']) != edge_count
                    or int(cached['card_count']) != card_count
                    or bool(cached['has_strength']) != self._preload_strength):
                return False
            self._adj_out = self._csr_to_adj_dict(
                cached['adj_out_keys'], cached['adj_out_offsets'], cached['adj_out_values'])
            self._adj_in = self._csr_to_adj_dict(
                cached['adj_in_keys'], cached['adj_in_offsets'], cached['adj_in_values'])
            self._exact_out = self._csr_to_adj_dict(
                cached['exact_out_keys'], cached['exact_out_offsets'], cached['exact_out_values'])
            self._exact_in = self._csr_to_adj_dict(
                cached['exact_in_keys'], cached['exact_in_offsets'], cached['exact_in_values'])
            self._arr_hub_raw = cached['hub_raw']
            self._arr_hub_score = cached['hub_score']
            self._hub_scores_built = True
            self._agg_strength_out = {}
            self._agg_events_out = {}
            self._agg_strength_in = {}
            self._agg_events_in = {}
            if self._preload_strength and 'agg_str_out_keys' in cached:
                self._agg_strength_out, self._agg_events_out = self._load_agg_from_cache(
                    cached, 'agg_str_out')
                self._agg_strength_in, self._agg_events_in = self._load_agg_from_cache(
                    cached, 'agg_str_in')
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

    def _save_adj_cache(self, adj_cache_path, edge_count, card_count):
        """Save adjacency dicts to CSR-style npz cache."""
        try:
            ao_k, ao_o, ao_v = self._adj_dict_to_csr(self._adj_out)
            ai_k, ai_o, ai_v = self._adj_dict_to_csr(self._adj_in)
            eo_k, eo_o, eo_v = self._adj_dict_to_csr(self._exact_out)
            ei_k, ei_o, ei_v = self._adj_dict_to_csr(self._exact_in)
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
        except Exception as e:
            _log.warning("Adjacency cache write failed: %s", e)

    def _build_adj_from_arrays(self, src, tgt, exact, strength, event_ids):
        """Build adjacency dicts from raw edge arrays."""
        self._adj_out = self._build_adj_arrays(src, tgt)
        self._adj_in = self._build_adj_arrays(tgt, src)

        # Exact-precision adjacency
        if exact.any():
            self._exact_out = self._build_adj_arrays(src[exact], tgt[exact])
            self._exact_in = self._build_adj_arrays(tgt[exact], src[exact])
        else:
            self._exact_out = {}
            self._exact_in = {}

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
            n_out = len(self._adj_out.get(i, []))
            n_in = len(self._adj_in.get(i, []))
            raw = float(n_out + n_in)
            self._arr_hub_raw[i] = raw
            self._arr_hub_score[i] = np.log2(1.0 + min(raw, 500))
        self._hub_scores_built = True

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
        import re as _re

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
                orig = re.search(r'Origin\$\s*(\S+)', raw_line)
                dest = re.search(r'Destination\$\s*(\S+)', raw_line)
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
                dest = re.search(r'Destination\$\s*(\S+)', raw_line)
                orig = re.search(r'Origin\$\s*(\S+)', raw_line)
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
            event_m = re.search(r'Event\$\s*(\S+)', raw_line)
            repl_m = re.search(r'ReplaceWith\$\s*(\S+)', raw_line)
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
                tgt_m = re.search(rf'{field}\$\s*(\S+)', raw_line)
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

        # ── Commander theme flags ──
        # Equipment theme: commander hints/needs equipment, or has Equip/Attach verbs,
        # or is detected as equipment payoff from raw_line patterns
        cmdr_tags = self.cmdr_has | self.cmdr_hints | self.cmdr_needs
        self.cmdr_equipment_theme = (
            bool(cmdr_tags & ctx._equipment_theme_tags) or
            bool(self.cmdr_profile.get('verbs', set()) & {'Equip', 'Attach'}) or
            cmdr_oid in ctx._equipment_payoffs
        )
        # Defender theme: commander needs/hints Keyword$Defender or has CanAttackDefender
        self.cmdr_defender_theme = (
            bool(cmdr_tags & ctx._defender_theme_tags) or
            'CanAttackDefender' in self.cmdr_profile.get('verbs', set()) or
            'Defender' in self.cmdr_profile.get('trigger_filters', set())
        )
        # Enchantress theme: commander hints/needs enchantments or triggers on enchantment ETBs
        self.cmdr_enchantress_theme = (
            bool(cmdr_tags & ctx._enchantress_theme_tags) or
            cmdr_oid in ctx._enchantress_payoffs or
            'Enchant' in self.cmdr_profile.get('verbs', set())
        )
        # ETB density: how many ETB-producing verbs the commander has
        # Token, ChangeZone (to battlefield), Animate, Manifest, etc.
        etb_verbs = self.cmdr_profile.get('verbs', set()) & {
            'Token', 'ChangeZone', 'ChangeZoneAll', 'Animate', 'Manifest', 'Flicker',
        }
        etb_triggers = self.cmdr_profile.get('triggers', set()) & {
            'ChangesZone', 'ChangesZoneAll',
        }
        self.cmdr_etb_density = float(len(etb_verbs) + len(etb_triggers))
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


def compute_batch_features(card_oids, card_cmcs, ctx, cmdr):
    """Vectorized batch feature computation for all cards of one commander.

    Returns (N, 105) float32 numpy array. ~10x faster than per-card loop
    by replacing dict lookups with numpy array indexing.

    Args:
        card_oids: list of card oracle_id strings
        card_cmcs: numpy array of float32 CMC values (same order as card_oids)
        ctx: ForgeFeatureContext with pre-built card arrays
        cmdr: CmdrFeatureContext for the current commander
    """
    N = len(card_oids)
    X = np.zeros((N, 105), dtype=np.float32)
    oid_to_idx = ctx.oid_to_idx

    # Convert card_oids to ctx indices for array lookup
    card_indices = np.array([oid_to_idx.get(oid, -1) for oid in card_oids], dtype=np.int32)
    valid = card_indices >= 0

    # ── Gather commander arrays (convert dicts to arrays once) ──
    n_total = len(oid_to_idx)
    cmdr_out_arr = np.zeros(n_total, dtype=np.float32)
    cmdr_in_arr = np.zeros(n_total, dtype=np.float32)
    deck_edge_arr = np.zeros(n_total, dtype=np.float32)
    deck_exact_arr = np.zeros(n_total, dtype=np.float32)
    deck_broad_arr = np.zeros(n_total, dtype=np.float32)
    hop2_arr = np.zeros(n_total, dtype=np.float32)
    cmdr_exact_arr = np.zeros(n_total, dtype=np.float32)

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
    for oid in cmdr.cmdr_exact:
        i = oid_to_idx.get(oid)
        if i is not None:
            cmdr_exact_arr[i] = 1.0

    # Fancy-index to get per-card values (use 0 index for invalid, will be masked)
    safe_idx = np.where(valid, card_indices, 0)

    # ── Gather values via array indexing ──
    out_s = cmdr_out_arr[safe_idx]
    in_s = cmdr_in_arr[safe_idx]
    deck_edges = deck_edge_arr[safe_idx]
    deck_exact = deck_exact_arr[safe_idx]
    deck_broad = deck_broad_arr[safe_idx]
    hop2_raw = hop2_arr[safe_idx]
    exact_edge = cmdr_exact_arr[safe_idx]
    hub_score = ctx._arr_hub_score[safe_idx]
    hub_raw = ctx._arr_hub_raw[safe_idx]

    # Mask invalid cards
    out_s = np.where(valid, out_s, 0.0)
    in_s = np.where(valid, in_s, 0.0)

    # ── F0-F4: Causal features ──
    X[:, 0] = np.minimum(out_s, 10.0)  # causal_cmdr_to_card
    X[:, 1] = np.minimum(in_s, 10.0)   # causal_card_to_cmdr
    X[:, 2] = np.where((out_s > 0) & (in_s > 0), 1.0, 0.0)  # causal_bidirectional
    # F3 causal_event_diversity — requires set union, leave as 0 (was always 0 in practice)
    X[:, 4] = np.log2(1.0 + np.minimum(deck_edges, 50))  # deck_edge_count

    # ── F11-F17: Type flags and CMC ──
    X[:, 11] = ctx._arr_type_creature[safe_idx]
    X[:, 12] = ctx._arr_type_instant_sorcery[safe_idx]
    X[:, 13] = ctx._arr_type_artifact[safe_idx]
    X[:, 14] = ctx._arr_type_enchantment[safe_idx]
    X[:, 15] = ctx._arr_type_land[safe_idx]
    X[:, 16] = ctx._arr_type_planeswalker[safe_idx]
    X[:, 17] = card_cmcs  # cmc

    # ── F18-F22: Edge precision features ──
    total_prec = deck_exact + deck_broad
    safe_prec = np.where(total_prec > 0, total_prec, 1.0)
    X[:, 18] = np.where(total_prec > 0, deck_exact / safe_prec, 0.0)  # deck_exact_edge_ratio
    X[:, 19] = exact_edge  # cmdr_exact_edge
    # F20: causal_composite
    event_div = np.zeros(N, dtype=np.float32)  # simplified: skip event diversity
    causal_str = out_s + in_s
    X[:, 20] = np.minimum(causal_str * (1.0 + event_div) * (1.0 + exact_edge), 20.0)
    X[:, 21] = hub_score  # card_hub_score
    X[:, 22] = np.minimum(deck_exact, 20.0)  # deck_exact_count

    # ── F26: forge_ability_depth ──
    X[:, 26] = ctx._arr_forge_depth[safe_idx]

    # ── F32-F33: Ability type flags ──
    X[:, 32] = ctx._arr_has_T[safe_idx]
    X[:, 33] = ctx._arr_has_A[safe_idx]

    # ── F37: activated_ability_count ──
    X[:, 37] = ctx._arr_activated_count[safe_idx]

    # ── F40-F41: Duration flags ──
    X[:, 40] = ctx._arr_dur_permanent[safe_idx]
    X[:, 41] = ctx._arr_dur_temporary[safe_idx]

    # ── F43: combat_damage_flag ──
    X[:, 43] = ctx._arr_combat_damage[safe_idx]

    # ── F45: scales_with_board ──
    X[:, 45] = ctx._arr_scales_with[safe_idx]

    # ── F47-F48: is_secondary, gain_control ──
    X[:, 47] = ctx._arr_is_secondary[safe_idx]
    X[:, 48] = ctx._arr_gain_control[safe_idx]

    # ── F49-F50: granted_keyword_count, condition_count ──
    X[:, 49] = ctx._arr_granted_kw_count[safe_idx]
    X[:, 50] = ctx._arr_condition_count[safe_idx]

    # ── F56-F62: Scaling and boolean flags ──
    X[:, 56] = ctx._arr_damage_scales[safe_idx]
    X[:, 57] = ctx._arr_draw_scales[safe_idx]
    X[:, 58] = ctx._arr_life_scales[safe_idx]
    X[:, 59] = ctx._arr_produces_mana[safe_idx]
    X[:, 60] = ctx._arr_counter_num_var[safe_idx]
    X[:, 61] = ctx._arr_grants_abilities[safe_idx]
    X[:, 62] = ctx._arr_token_amt_var[safe_idx]

    # ── F63-F66: Ability counts and token stats ──
    X[:, 63] = ctx._arr_total_abilities[safe_idx]
    X[:, 64] = ctx._arr_triggered_count[safe_idx]
    X[:, 65] = ctx._arr_token_pt[safe_idx]
    X[:, 66] = ctx._arr_token_kw[safe_idx]

    # ── F67-F68: Zone interaction (both card AND commander must interact) ──
    X[:, 67] = ctx._arr_zone_gy[safe_idx] * (1.0 if cmdr.cmdr_zone_graveyard else 0.0)
    X[:, 68] = ctx._arr_zone_ex[safe_idx] * (1.0 if cmdr.cmdr_zone_exile else 0.0)

    # ── F69: ability_density (abilities / cmc) ──
    raw_counts = ctx._arr_total_abilities[safe_idx]  # already capped at 15
    raw_counts_uncapped = np.array([ctx._total_ability_counts.get(oid, 0) for oid in card_oids], dtype=np.float32)
    safe_cmc = np.maximum(card_cmcs, 1.0)
    X[:, 69] = np.minimum(np.where(raw_counts_uncapped > 0, raw_counts_uncapped / safe_cmc, 0.0), 5.0)

    # ── F74: put_counter_ratio ──
    n_counter = ctx._arr_n_counter_verbs[safe_idx]
    n_pump = ctx._arr_n_pump_verbs[safe_idx]
    n_buff = n_counter + n_pump
    safe_buff = np.where(n_buff > 0, n_buff, 1.0)
    X[:, 74] = np.where(n_buff > 0, n_counter / safe_buff, 0.5)

    # ── F77: counters_on_lands ──
    X[:, 77] = ctx._arr_counters_on_lands[safe_idx]

    # ── F83-F84: 2-hop features ──
    X[:, 83] = np.log2(1.0 + np.minimum(hop2_raw, 200))
    safe_hub = np.where(hub_raw > 0, hub_raw, 1.0)
    X[:, 84] = np.minimum(np.where(hop2_raw > 0, hop2_raw / safe_hub, 0.0), 1.0)

    # ── F85-F87: Card quality features ──
    X[:, 85] = ctx._arr_forge_richness[safe_idx]
    X[:, 86] = ctx._arr_in_forge[safe_idx]
    X[:, 87] = ctx._arr_strat_count[safe_idx]
    X[:, 88] = ctx._arr_deck_tag_count[safe_idx]  # deck_tag_count → F88 in array
    X[:, 89] = ctx._arr_edhrec_pct[safe_idx]

    # ── F9: has_phase_trigger ──
    X[:, 9] = ctx._arr_has_phase[safe_idx]

    # ── Commander-constant theme features ──
    cmdr_equip = 1.0 if cmdr.cmdr_equipment_theme else 0.0
    cmdr_ench = 1.0 if cmdr.cmdr_enchantress_theme else 0.0
    cmdr_def = 1.0 if cmdr.cmdr_defender_theme else 0.0
    cmdr_etb = min(cmdr.cmdr_etb_density, 5.0)
    cmdr_has_p1p1 = 'P1P1' in cmdr.cmdr_profile.get('counter_types', set())

    X[:, 90] = cmdr_equip  # cmdr_equipment_theme
    card_equip = np.maximum(ctx._arr_is_equipment[safe_idx], ctx._arr_is_equip_payoff[safe_idx])
    X[:, 91] = card_equip  # card_equipment_payoff
    X[:, 92] = cmdr_equip * card_equip  # equipment_theme_match
    X[:, 93] = cmdr_ench  # cmdr_enchantress_theme
    card_ench_payoff = ctx._arr_is_ench_payoff[safe_idx]
    card_ench_pure = ctx._arr_pure_enchantment[safe_idx]
    card_ench = np.maximum(card_ench_payoff, card_ench_pure)
    X[:, 94] = card_ench  # card_enchantress_payoff
    X[:, 95] = cmdr_ench * card_ench  # enchantress_theme_match
    X[:, 96] = cmdr_def  # cmdr_defender_theme
    X[:, 97] = ctx._arr_is_defender[safe_idx]  # card_has_defender
    X[:, 98] = cmdr_def * X[:, 97]  # defender_theme_match
    X[:, 99] = ctx._arr_is_etb_doubler[safe_idx]  # card_is_etb_doubler
    X[:, 100] = cmdr_etb  # cmdr_etb_density
    X[:, 101] = X[:, 99] * cmdr_etb  # etb_doubler_match

    # ── Counter/anthem interaction features (need cmdr P1P1 flag) ──
    if cmdr_has_p1p1:
        X[:, 73] = ctx._arr_dur_temporary[safe_idx] * (1.0 - ctx._arr_dur_permanent[safe_idx])  # temp_buff_counter_cmdr
        X[:, 75] = np.where(n_counter > 0, 1.0, 0.0)  # cmdr_counter_x_put_counter
        X[:, 76] = ctx._arr_has_static_anthem[safe_idx]  # static_anthem_counter_cmdr
        # F78: cmdr_p1p1_card_no_counters
        has_p1p1 = ctx._arr_has_p1p1[safe_idx]
        has_any_cv = ctx._arr_has_any_counter_verb[safe_idx]
        X[:, 78] = np.where((has_p1p1 == 0) & (has_any_cv == 0), 1.0, 0.0)

    # ── Per-card features requiring set operations (loop, but fast) ──
    # These features need per-card profile access. Process in a tight loop.
    cmdr_strats = cmdr.cmdr_strats
    cmdr_strat_vec = cmdr.cmdr_strat_vec
    cmdr_ability_vec = cmdr.cmdr_ability_vec
    cmdr_subtypes = cmdr.cmdr_subtypes
    cmdr_mechs = cmdr.cmdr_mechs
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
    cmdr_conds = cmdr.cmdr_profile.get('conditions', set())
    cmdr_ezones = cmdr.cmdr_profile.get('effect_zones', set())
    cmdr_granted = cmdr.cmdr_profile.get('granted_keywords', set())
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

    # Keyword-filter mapping for F36
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

        c_strats = ctx.card_strats.get(oid, set())
        card_profile = ctx._forge_profiles.get(oid, {})

        # F5: strategy_overlap
        X[row_i, 5] = float(len(cmdr_strats & c_strats))

        # F6: strategy_cosine
        csv = ctx.strat_vector(oid)
        if cmdr_strat_vec is not None and csv is not None:
            d = float(np.dot(cmdr_strat_vec, csv))
            nc = float(np.linalg.norm(cmdr_strat_vec))
            nd = float(np.linalg.norm(csv))
            X[row_i, 6] = d / (nc * nd) if nc > 0 and nd > 0 else 0.0

        # F7: forge_ability_cosine
        card_av = ctx._ability_vectors.get(oid)
        if cmdr_ability_vec is not None and card_av is not None:
            X[row_i, 7] = float(np.dot(cmdr_ability_vec, card_av))

        # F8: phase_match
        cp = ctx.card_phase_order.get(oid, set())
        if cmdr.cmdr_phases and cp:
            best = 0.0
            for p1 in cmdr.cmdr_phases:
                for p2 in cp:
                    best = max(best, max(0.0, 1.0 - abs(p1 - p2) * 2.0))
            X[row_i, 8] = best

        # F10: tribal_match
        if cmdr_subtypes:
            card_subs = ctx._arr_card_subtypes[ci]
            if card_subs and (cmdr_subtypes & card_subs):
                X[row_i, 10] = 1.0

        # F23: forge_type_synergy
        card_trigger_types = card_profile.get('trigger_filters', set())
        card_targets = card_profile.get('targets', set())
        if cmdr_subtypes:
            fts = 0.0
            for sub in cmdr_subtypes:
                if sub in card_trigger_types:
                    fts += 1.0
                if sub.title() in card_targets:
                    fts += 0.5
            X[row_i, 23] = fts

        # F24: cmdr_forge_type_match
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
        X[row_i, 24] = ctm

        # F25: shared_forge_mechanics
        card_mechs = ctx._card_mechs.get(oid, set())
        X[row_i, 25] = float(len(cmdr_mechs & card_mechs))

        # F27: forge_anti_tribal
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
            X[row_i, 27] = anti

        # F28: forge_verb_alignment
        card_verbs = card_profile.get('verbs', set())
        card_trigs = card_profile.get('triggers', set())
        va = 0.0
        for v in card_verbs:
            va += len(ctx._verb_triggers.get(v, set()) & cmdr_trigs)
        for v in cmdr_verbs:
            va += len(ctx._verb_triggers.get(v, set()) & card_trigs)
        X[row_i, 28] = va

        # F29-F30: mech_fwd/rev
        card_prod = ctx._mech_produces.get(oid)
        card_cons = ctx._mech_consumes.get(oid)
        if cmdr_consumes is not None and card_prod is not None:
            X[row_i, 29] = float(np.dot(cmdr_consumes, card_prod))
        if cmdr_produces is not None and card_cons is not None:
            X[row_i, 30] = float(np.dot(cmdr_produces, card_cons))

        # F31: counter_type_match
        card_counters = card_profile.get('counter_types', set())
        if cmdr_counters and card_counters:
            X[row_i, 31] = float(len(cmdr_counters & card_counters))

        # F34: zone_alignment
        card_zones = ctx._card_zones.get(oid, set())
        if cmdr_zones and card_zones:
            X[row_i, 34] = float(len(cmdr_zones & card_zones))

        # F35: target_alignment
        card_tgts = card_profile.get('targets', set())
        if cmdr_prod_types and card_tgts:
            X[row_i, 35] = float(len(cmdr_prod_types & card_tgts))

        # F36: forge_keyword_synergy
        card_kws = card_profile.get('keywords', set())
        kw_syn = 0.0
        for kw in card_kws:
            ff = kw_to_filter.get(kw, kw.lower().replace(" ", ""))
            if any(ff in f for f in cmdr_filter_kws):
                kw_syn += 1.0
        if cmdr_makes_creatures:
            kw_syn += float(len(card_kws & combat_kws)) * 0.3
        X[row_i, 36] = kw_syn

        # F38: granted_keyword_synergy
        card_granted = card_profile.get('granted_keywords', set())
        if card_granted:
            gks = 0.0
            for gk in card_granted:
                if gk in cmdr_filter_kws:
                    gks += 1.0
            gks += float(len(card_granted & cmdr_granted)) * 0.5
            X[row_i, 38] = gks

        # F39: shared_conditions
        card_conds = card_profile.get('conditions', set())
        if cmdr_conds and card_conds:
            X[row_i, 39] = float(len(card_conds & cmdr_conds))

        # F42: duration_match
        card_dur = card_profile.get('duration', set())
        if cmdr_dur and card_dur:
            X[row_i, 42] = float(len(card_dur & cmdr_dur))

        # F44: effect_zone_match
        card_ezones = card_profile.get('effect_zones', set())
        if cmdr_ezones and card_ezones:
            X[row_i, 44] = float(len(card_ezones & cmdr_ezones))

        # F46: grants_types_match
        card_gtypes = card_profile.get('grants_types', set())
        if cmdr_subtypes and card_gtypes:
            X[row_i, 46] = sum(1.0 for gt in card_gtypes if gt in cmdr_subtypes)

        # F51-F55: Deck tag overlaps
        card_has = ctx._deck_has.get(oid, set())
        card_hints = ctx._deck_hints.get(oid, set())
        card_needs = ctx._deck_needs.get(oid, set())
        X[row_i, 51] = float(len(cmdr.cmdr_hints & card_has))
        X[row_i, 52] = float(len(cmdr.cmdr_has & card_hints))
        X[row_i, 53] = float(len(cmdr.cmdr_has & card_needs))
        X[row_i, 54] = float(len(cmdr.cmdr_has & card_has))
        X[row_i, 55] = float(len(cmdr.cmdr_hints & card_hints))

        # F70: cmdr_needs_to_card_has
        X[row_i, 70] = float(len(cmdr.cmdr_needs & card_has))

        # F71: card_needs_satisfied
        if card_needs:
            met = len(card_needs & (cmdr.cmdr_has | cmdr.cmdr_hints))
            X[row_i, 71] = float(met) / float(len(card_needs))

        # F72: needs_rarity
        if card_needs:
            nr = 0.0
            for need_tag in card_needs:
                np_ = len(ctx._deck_has_providers.get(need_tag, set()))
                if np_ > 0:
                    nr += 1.0 / min(np_, 100)
            X[row_i, 72] = min(nr, 5.0)

        # F79-F82: Functional fingerprint features
        card_func = ctx._func_fingerprints.get(oid)
        if card_func is not None and cmdr_func is not None:
            X[row_i, 79] = float(np.dot(cmdr_prod_projected, card_func[A]))
            for req_dim, prod_dim in _req_to_prod.items():
                X[row_i, 80] += cmdr_func[R.start + req_dim] * card_func[P.start + prod_dim]
                X[row_i, 81] += card_func[R.start + req_dim] * cmdr_func[P.start + prod_dim]
            norm_c = np.linalg.norm(cmdr_func)
            norm_d = np.linalg.norm(card_func)
            if norm_c > 0 and norm_d > 0:
                X[row_i, 82] = float(np.dot(cmdr_func, card_func) / (norm_c * norm_d))

        # F95-F97: Tribal depth
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
        X[row_i, 102] = tribal_lord

        tribal_member = 0.0
        if cmdr_tribal_filters:
            card_subs = ctx._arr_card_subtypes[ci]
            if card_subs and (cmdr_tribal_filters & card_subs):
                tribal_member = 1.0
        X[row_i, 103] = tribal_member

        tribal_depth = X[row_i, 10] + tribal_lord + tribal_member
        card_tok_subs = ctx._token_subtypes.get(oid, set())
        if cmdr_subtypes and (card_tok_subs & cmdr_subtypes):
            tribal_depth += 1.0
        X[row_i, 104] = tribal_depth

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
    if ctx._has_edge_index and di is not None:
        n_out = len(ctx._adj_out.get(di, []))
        n_in = len(ctx._adj_in.get(di, []))
        hub = np.log2(1.0 + min(n_out + n_in, 500))
    elif di is not None:
        hub = np.log2(1.0 + min(cmdr.deck_edge_counts.get(card_oid, 0), 20))

    deck_exact_abs = float(min(n_exact, 20))

    hop2_raw = cmdr.cmdr_2hop_counts.get(card_oid, 0)
    cmdr_2hop = np.log2(1.0 + min(hop2_raw, 200))

    hub_raw = 0.0
    if ctx._has_edge_index and di is not None:
        n_out = len(ctx._adj_out.get(di, []))
        n_in = len(ctx._adj_in.get(di, []))
        hub_raw = float(n_out + n_in)
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

    # F7: forge_ability_cosine
    card_ability_vec = ctx._ability_vectors.get(card_oid)
    forge_ability_cos = float(np.dot(cmdr.cmdr_ability_vec, card_ability_vec)) \
        if cmdr.cmdr_ability_vec is not None and card_ability_vec is not None else 0.0

    # F8: phase match
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

    shared_forge = float(len(cmdr.cmdr_mechs & ctx._card_mechs.get(card_oid, set())))

    card_depth = float(len(card_verbs) + len(card_trigs) +
                       len(card_profile.get('keywords', set())) +
                       len(card_profile.get('counter_types', set())))
    forge_depth = min(card_depth, 10.0)

    verb_align = 0.0
    for v in card_verbs:
        verb_align += len(ctx._verb_triggers.get(v, set()) & cmdr_trigs)
    for v in cmdr_verbs:
        verb_align += len(ctx._verb_triggers.get(v, set()) & card_trigs)

    mech_fwd = 0.0
    mech_rev = 0.0
    card_prod = ctx._mech_produces.get(card_oid)
    card_cons = ctx._mech_consumes.get(card_oid)
    if cmdr.cmdr_consumes is not None and card_prod is not None:
        mech_fwd = float(np.dot(cmdr.cmdr_consumes, card_prod))
    if cmdr.cmdr_produces is not None and card_cons is not None:
        mech_rev = float(np.dot(cmdr.cmdr_produces, card_cons))

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

    card_granted = card_profile.get('granted_keywords', set())
    cmdr_granted = cmdr.cmdr_profile.get('granted_keywords', set())
    granted_kw_syn = 0.0
    if card_granted:
        for gk in card_granted:
            if gk in cmdr_filter_kws:
                granted_kw_syn += 1.0
        granted_kw_syn += float(len(card_granted & cmdr_granted)) * 0.5

    card_conds = card_profile.get('conditions', set())
    cmdr_conds = cmdr.cmdr_profile.get('conditions', set())
    shared_conds = float(len(card_conds & cmdr_conds)) if card_conds and cmdr_conds else 0.0

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

    card_gtypes = card_profile.get('grants_types', set())
    gtypes_match = 0.0
    if cmdr.cmdr_subtypes and card_gtypes:
        for gt in card_gtypes:
            if gt in cmdr.cmdr_subtypes:
                gtypes_match += 1.0

    is_secondary = 1.0 if card_profile.get('is_secondary', False) else 0.0
    gain_ctrl = 1.0 if card_profile.get('gain_control', False) else 0.0
    granted_kw_count = float(min(len(card_granted), 5))
    condition_count = float(min(len(card_conds), 5))

    dmg_amt = card_profile.get('damage_amount')
    damage_scales = 1.0 if dmg_amt in ('X', 'Y') else 0.0
    draw_amt = card_profile.get('cards_drawn')
    draw_scales = 1.0 if draw_amt in ('X', 'Y') else 0.0
    life_amt = card_profile.get('life_amount')
    life_scales = 1.0 if life_amt in ('X', 'Y') else 0.0
    produces_mana = 1.0 if card_profile.get('produces_mana', False) else 0.0
    counter_num_var = 1.0 if card_profile.get('counter_num_variable', False) else 0.0
    grants_abilities = 1.0 if card_profile.get('grants_abilities', False) else 0.0
    token_amt_var = 1.0 if card_profile.get('token_amount_variable', False) else 0.0

    total_abilities = float(min(ctx._total_ability_counts.get(card_oid, 0), 15))
    triggered_count = float(min(ctx._triggered_counts.get(card_oid, 0), 10))
    token_pt = float(min(ctx._token_max_pt.get(card_oid, 0), 20))
    token_kw = float(min(ctx._token_max_kw.get(card_oid, 0), 5))

    zone_gy = 1.0 if (card_oid in ctx._zone_graveyard and cmdr.cmdr_zone_graveyard) else 0.0
    zone_ex = 1.0 if (card_oid in ctx._zone_exile and cmdr.cmdr_zone_exile) else 0.0

    raw_count = ctx._total_ability_counts.get(card_oid, 0)
    ability_dens = float(raw_count) / max(card_cmc, 1.0) if raw_count > 0 else 0.0
    ability_dens = min(ability_dens, 5.0)

    # Counter interaction features
    n_counter = sum(1 for v in card_verbs if v in ('PutCounter', 'PutCounterAll'))
    n_pump = sum(1 for v in card_verbs if v in ('Pump', 'PumpAll'))
    n_buff = n_counter + n_pump
    put_counter_ratio = float(n_counter) / n_buff if n_buff > 0 else 0.5

    cmdr_counter_x_put = 1.0 if ('P1P1' in cmdr_counters and n_counter > 0) else 0.0
    temp_counter_clash = 1.0 if ('P1P1' in cmdr_counters and
                                  'temporary' in card_dur and
                                  'permanent' not in card_dur) else 0.0
    static_anthem_clash = 1.0 if ('P1P1' in cmdr_counters and
                                   card_profile.get('has_static_anthem', False)) else 0.0
    counters_on_lands = 1.0 if card_profile.get('counters_on_lands', False) else 0.0
    card_has_p1p1 = card_profile.get('has_p1p1', False)
    card_counter_verbs = card_verbs & {'PutCounter', 'PutCounterAll', 'Proliferate', 'MoveCounter'}
    no_counter_for_cmdr = 1.0 if ('P1P1' in cmdr_counters and
                                   not card_has_p1p1 and
                                   not card_counter_verbs) else 0.0

    return (strat_cos, forge_ability_cos, phase_m, cp,
            shared_forge, forge_depth, verb_align, mech_fwd, mech_rev,
            counter_match, ratio_T, ratio_A, zone_align, target_align,
            kw_syn, activated_count, granted_kw_syn, shared_conds,
            is_permanent, is_temporary, duration_match, combat_dmg,
            ezone_match, scales, gtypes_match, is_secondary, gain_ctrl,
            granted_kw_count, condition_count,
            damage_scales, draw_scales, life_scales, produces_mana,
            counter_num_var, grants_abilities, token_amt_var,
            total_abilities, triggered_count, token_pt, token_kw,
            zone_gy, zone_ex, ability_dens,
            temp_counter_clash, put_counter_ratio, cmdr_counter_x_put,
            static_anthem_clash, counters_on_lands, no_counter_for_cmdr)


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


def _compute_theme_features(card_oid, card_type_line, ctx, cmdr):
    """Compute theme-related features (equipment, enchantress, defender, ETB)."""
    tl = card_type_line
    cmdr_equip = 1.0 if cmdr.cmdr_equipment_theme else 0.0
    card_equip = 1.0 if (card_oid in ctx._equipment_cards or
                          card_oid in ctx._equipment_payoffs) else 0.0
    equip_match = cmdr_equip * card_equip

    cmdr_ench = 1.0 if cmdr.cmdr_enchantress_theme else 0.0
    card_ench = 0.0
    if card_oid in ctx._enchantress_payoffs:
        card_ench = 1.0
    elif "Enchantment" in tl and "Creature" not in tl:
        card_ench = 0.5
    ench_match = cmdr_ench * card_ench

    cmdr_def = 1.0 if cmdr.cmdr_defender_theme else 0.0
    card_def = 1.0 if (card_oid in ctx._defender_cards or "Wall" in tl) else 0.0
    def_match = cmdr_def * card_def

    card_etb_dbl = 1.0 if card_oid in ctx._etb_doublers else 0.0
    cmdr_etb = min(cmdr.cmdr_etb_density, 5.0)
    etb_match = card_etb_dbl * cmdr_etb

    return (cmdr_equip, card_equip, equip_match,
            cmdr_ench, card_ench, ench_match,
            cmdr_def, card_def, def_match,
            card_etb_dbl, cmdr_etb, etb_match)


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
    """Compute the 105-feature vector for a single (commander, card) pair.

    Returns a list of 103 floats matching FORGE_FEATURE_NAMES order.
    Pure Forge-native features — no tower model or oracle-text embeddings.
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
     shared_forge, forge_depth, verb_align, mech_fwd, mech_rev,
     counter_match, ratio_T, ratio_A, zone_align, target_align,
     kw_syn, activated_count, granted_kw_syn, shared_conds,
     is_permanent, is_temporary, duration_match, combat_dmg,
     ezone_match, scales, gtypes_match, is_secondary, gain_ctrl,
     granted_kw_count, condition_count,
     damage_scales, draw_scales, life_scales, produces_mana,
     counter_num_var, grants_abilities, token_amt_var,
     total_abilities, triggered_count, token_pt, token_kw,
     zone_gy, zone_ex, ability_dens,
     temp_counter_clash, put_counter_ratio, cmdr_counter_x_put,
     static_anthem_clash, counters_on_lands, no_counter_for_cmdr
     ) = _compute_forge_profile_features(card_oid, card_cmc, card_profile, ctx, cmdr)

    (hints_to_has, has_to_hints, needs_to_has, has_overlap,
     hints_overlap, cmdr_needs_to_has, card_needs_met, needs_rarity
     ) = _compute_deck_tag_features(card_oid, ctx, cmdr)

    (cmdr_equip, card_equip, equip_match,
     cmdr_ench, card_ench, ench_match,
     cmdr_def, card_def, def_match,
     card_etb_dbl, cmdr_etb, etb_match
     ) = _compute_theme_features(card_oid, tl, ctx, cmdr)

    (func_produces_amp, func_requires_prod, func_card_req_cmdr, func_full_cosine
     ) = _compute_fingerprint_features(card_oid, ctx, cmdr)

    forge_richness = ctx._forge_richness.get(card_oid, 0.0)
    in_forge = 1.0 if card_oid in ctx._has_forge_data else 0.0
    strat_count = float(min(len(c_strats), 5))
    deck_tags = ctx._deck_tag_count.get(card_oid, 0.0)
    edhrec_pct = 0.0 if _EDHREC_FREE else ctx._edhrec_deck_pct.get(card_oid, 0.0)

    ev_out = cmdr.cmdr_out_events.get(card_oid, set())
    ev_in = cmdr.cmdr_in_events.get(card_oid, set())

    return [
        min(out_s, 10.0),                                # F0 causal_cmdr_to_card
        min(in_s, 10.0),                                 # F1 causal_card_to_cmdr
        1.0 if (out_s > 0 and in_s > 0) else 0.0,       # F2 causal_bidirectional
        float(len(ev_out | ev_in)),                      # F3 causal_event_diversity
        np.log2(1.0 + min(cmdr.deck_edge_counts.get(card_oid, 0), 50)),  # F4 deck_edge_count
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
        hints_to_has,                                    # F51 deck_hints_to_has
        has_to_hints,                                    # F52 deck_has_to_hints
        needs_to_has,                                    # F53 deck_needs_to_has
        has_overlap,                                     # F54 deck_has_overlap
        hints_overlap,                                   # F55 deck_hints_overlap
        damage_scales,                                   # F56 damage_scales
        draw_scales,                                     # F57 draw_scales
        life_scales,                                     # F58 life_scales
        produces_mana,                                   # F59 produces_mana
        counter_num_var,                                 # F60 counter_num_variable
        grants_abilities,                                # F61 grants_abilities
        token_amt_var,                                   # F62 token_amount_variable
        total_abilities,                                 # F63 total_ability_count
        triggered_count,                                 # F64 triggered_ability_count
        token_pt,                                        # F65 token_power_toughness
        token_kw,                                        # F66 token_keyword_count
        zone_gy,                                         # F67 zone_graveyard_interact
        zone_ex,                                         # F68 zone_exile_interact
        ability_dens,                                    # F69 ability_density
        cmdr_needs_to_has,                               # F70 cmdr_needs_to_card_has
        card_needs_met,                                  # F71 card_needs_satisfied
        needs_rarity,                                    # F72 needs_rarity
        temp_counter_clash,                              # F73 temp_buff_counter_cmdr
        put_counter_ratio,                               # F74 put_counter_ratio
        cmdr_counter_x_put,                              # F75 cmdr_counter_x_put_counter
        static_anthem_clash,                             # F76 static_anthem_counter_cmdr
        counters_on_lands,                               # F77 counters_on_lands
        no_counter_for_cmdr,                             # F78 cmdr_p1p1_card_no_counters
        func_produces_amp,                               # F79 func_produces_amplifies
        func_requires_prod,                              # F80 func_requires_produces
        func_card_req_cmdr,                              # F81 func_card_requires_cmdr
        func_full_cosine,                                # F82 func_full_cosine
        # ── 2-hop graph features ──
        cmdr_2hop,                                       # F83 cmdr_2hop_count
        cmdr_2hop_ratio,                                 # F84 cmdr_2hop_ratio
        # ── Card quality / noise suppression ──
        forge_richness,                                  # F85 forge_ability_richness
        in_forge,                                        # F84 card_in_forge
        strat_count,                                     # F85 card_strategy_count
        deck_tags,                                       # F86 deck_tag_count
        edhrec_pct,                                      # F87 edhrec_deck_pct
        # ── Theme-based features ──
        cmdr_equip,                                      # F88 cmdr_equipment_theme
        card_equip,                                      # F84 card_equipment_payoff
        equip_match,                                     # F85 equipment_theme_match
        cmdr_ench,                                       # F86 cmdr_enchantress_theme
        card_ench,                                       # F87 card_enchantress_payoff
        ench_match,                                      # F88 enchantress_theme_match
        cmdr_def,                                        # F89 cmdr_defender_theme
        card_def,                                        # F90 card_has_defender
        def_match,                                       # F91 defender_theme_match
        card_etb_dbl,                                    # F92 card_is_etb_doubler
        cmdr_etb,                                        # F93 cmdr_etb_density
        etb_match,                                       # F94 etb_doubler_match
        tribal_lord,                                     # F95 tribal_lord_for_cmdr
        tribal_member,                                   # F96 tribal_member_of_cmdr
        tribal_depth,                                    # F97 tribal_synergy_depth
    ]
