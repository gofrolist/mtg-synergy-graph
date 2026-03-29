"""Shared forge feature computation for training and inference.

Extracts the 98-feature forge GBM feature vector computation into a
single module used by both train_fusion_model.py and scoring.py.

No oracle-text embeddings or tower model — pure Forge-native features.
"""
import time

import numpy as np

from mtg_synergy.recommend.mechanics_vectors import _concept_idx


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
        # Format: (oid, verb, trig_mode, trig_filter, cost, kw, token_script, counter, raw_line, amount, trigger_origin, trigger_destination)
        self._raw_abilities = []
        for row in conn.execute(
            "SELECT fnm.oracle_id, fa.verb, fa.trigger_mode, fa.keyword, "
            "fa.counter_type, fa.target, fa.ability_type, fa.trigger_filter, "
            "fa.cost, fa.defined, fa.raw_line, fa.token_script, fa.amount, "
            "fa.trigger_origin, fa.trigger_destination "
            "FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name"
        ):
            # indices: 0=oid, 1=verb, 2=trig_mode, 3=trig_filter, 4=cost, 5=kw, 6=token_script, 7=counter, 8=raw_line, 9=amount, 10=trigger_origin, 11=trigger_destination
            self._raw_abilities.append((row[0], row[1], row[2], row[7], row[8], row[3], row[11], row[4], row[10], row[12], row[13], row[14]))
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
                'produces_mana': False, 'mana_colors': set(),
                'counter_num_variable': False, 'grants_abilities': False,
                'token_amount_variable': False,
                'excluded_subtypes': set(),
                'has_static_anthem': False, 'counters_on_lands': False,
                'counter_trigger_themes': set(), 'has_p1p1': False,
            })
            if row[1]: p['verbs'].add(row[1])
            if row[2]: p['triggers'].add(row[2])
            if row[3]: p['keywords'].add(row[3])
            if row[4]:
                p['counter_types'].add(row[4])
                if row[4] == 'P1P1':
                    p['has_p1p1'] = True
            # Also check raw_line for P1P1 references (replacement effects, etbCounter)
            if not p['has_p1p1'] and row[10] and 'P1P1' in row[10]:
                p['has_p1p1'] = True
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
            # Effect target subtypes from ValidCards$, Affected$, ValidTgts$,
            # ValidCard$, ValidAttackers$, AddsCounters$ in raw_line
            # e.g., ValidCards$ Creature.Orc → effect only benefits Orcs
            # e.g., Affected$ Creature.nonHuman → EXCLUDES Humans
            _generic = {"card", "creature", "permanent", "self", "other",
                        "youctrl", "oppctrl", "strictlyother", "token", "nontoken"}
            for field in ('ValidCards', 'ValidCard', 'ValidTgts',
                          'ValidAttackers', 'Affected', 'AddsCounters'):
                m = _re.search(rf'{field}\$\s*(\S+)', raw_line)
                if m:
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
                            elif (seg[0].isupper()
                                    and seg.lower() not in _generic):
                                p['required_subtypes'].add(seg.lower())
            # Also extract non-X from TriggerDescription$ and Description$
            # Catches sub-ability targets like "non-Human creature"
            for desc_field in ('TriggerDescription', 'Description', 'SpellDescription'):
                dm = _re.search(rf'{desc_field}\$\s*(.+?)(?:\||$)', raw_line)
                if dm:
                    for nm in _re.finditer(r'non-(\w+)\s+creature', dm.group(1), _re.IGNORECASE):
                        excl = nm.group(1).lower()
                        if excl not in _generic and len(excl) > 2:
                            p['excluded_subtypes'].add(excl)
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
            # --- Mana production: Produced$ W/U/B/R/G/C/Any/Combo ---
            m = _re.search(r'Produced\$\s*(\S+)', raw_line)
            if m:
                p['produces_mana'] = True
                prod = m.group(1)
                for c in ('W', 'U', 'B', 'R', 'G'):
                    if c in prod:
                        p['mana_colors'].add(c)
                if prod in ('Any', 'Combo', 'Chosen'):
                    p['mana_colors'].update({'W', 'U', 'B', 'R', 'G'})
            # --- Counter quantity variable: CounterNum$ X/Y ---
            m = _re.search(r'CounterNum\$\s*(\S+)', raw_line)
            if m and m.group(1) in ('X', 'Y', 'All', 'Any'):
                p['counter_num_variable'] = True
            # --- Grants abilities: AddAbility$ ---
            if 'AddAbility$' in raw_line:
                p['grants_abilities'] = True
            # --- Token amount variable: TokenAmount$ X ---
            m = _re.search(r'TokenAmount\$\s*(\S+)', raw_line)
            if m and m.group(1) in ('X', 'Y'):
                p['token_amount_variable'] = True
            # --- Static anthem: Continuous + AddPower (not actual counters) ---
            if row[1] == 'Continuous' and 'AddPower$' in raw_line:
                p['has_static_anthem'] = True
            # --- Counters on lands: PutCounter targeting lands, or Earthbend ---
            if row[1] in ('PutCounter', 'PutCounterAll'):
                vtgt = _re.search(r'ValidTgts\$\s*(\S+)', raw_line)
                if vtgt and 'Land' in vtgt.group(1) and 'Creature' not in vtgt.group(1):
                    p['counters_on_lands'] = True
            if row[1] == 'Earthbend':
                p['counters_on_lands'] = True
            # --- Counter trigger themes: what triggers this card's counter placement ---
            if row[1] in ('PutCounter', 'PutCounterAll') and row[2]:
                trig = row[2]
                if trig == 'LifeGained':
                    p['counter_trigger_themes'].add('lifegain')
                elif trig == 'Sacrificed':
                    p['counter_trigger_themes'].add('sacrifice')
                elif trig == 'Discarded':
                    p['counter_trigger_themes'].add('discard')
                elif trig in ('SpellCast', 'SpellCopy'):
                    p['counter_trigger_themes'].add('spellcast')

        # Post-process: static anthem is only meaningful if card has NO PutCounter
        for p in self._forge_profiles.values():
            if p['has_static_anthem'] and ('PutCounter' in p['verbs'] or 'PutCounterAll' in p['verbs']):
                p['has_static_anthem'] = False  # card also places real counters, not just anthem

        # ── Functional fingerprints: semantic vectors for what each card does ──
        # 33 dimensions across 4 sub-vectors: produces, requires, amplifies, targets
        # Dot products between commander and card fingerprints capture synergy
        # without hand-coded penalties.
        self._func_fingerprints = self._build_func_fingerprints(conn)

        # Pre-compute card-level mechanical unions (avoid redundant set ops in inner loop)
        self._card_mechs = {}
        for oid, p in self._forge_profiles.items():
            mechs = p['verbs'] | p['triggers'] | p['keywords']
            if mechs:
                self._card_mechs[oid] = mechs

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
        import re as _re_equip
        for row in conn.execute(
            "SELECT DISTINCT fnm.oracle_id, fa.raw_line FROM forge_abilities fa "
            "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
            "WHERE fa.raw_line LIKE '%Equipment%'"
        ):
            oid, raw = row
            if raw and _re_equip.search(
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
        """Pre-load edge adjacency + strength + events into memory.

        Caches raw numpy arrays to data/edge_index_cache.npz (~2s reload vs ~40s DB scan).
        Cache key: interaction_edges row count + card count.
        Stores: src, tgt, exact, strength (float32), event_ids (uint8), event_names.
        """
        import os
        from mtg_synergy.config import DATA_DIR

        print("  Building in-memory edge index...", flush=True)
        t0 = time.time()

        cache_path = os.path.join(DATA_DIR, "edge_index_cache.npz")
        edge_count = conn.execute("SELECT COUNT(*) FROM interaction_edges").fetchone()[0]
        card_count = len(self.oid_to_idx)

        # Try loading from cache (numpy arrays only, no pickle)
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
                    print(f"    Loaded {len(src):,} edges from cache ({time.time()-t0:.1f}s)")
            except Exception:
                pass

        if src is None:
            # Build event encoding from DB
            event_name_list = sorted(
                r[0] for r in conn.execute(
                    "SELECT DISTINCT event FROM interaction_edges WHERE event IS NOT NULL"
                ) if r[0]
            )
            event_to_id = {name: i for i, name in enumerate(event_name_list)}
            event_names = np.array(event_name_list, dtype='U64')

            # Pre-allocate numpy arrays to avoid ~450MB Python list overhead.
            # edge_count is an upper bound (some edges may have unmapped oids).
            has_col = any(
                r[1] == "filter_precision"
                for r in conn.execute("PRAGMA table_info(interaction_edges)")
            )
            prec_expr = "filter_precision" if has_col else "json_extract(detail, '$.filter_precision')"
            event_expr = "event" if self._has_event_col else "json_extract(detail, '$.event')"
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
                if s is not None and t is not None:
                    src[n] = s
                    tgt[n] = t
                    exact[n] = row[2] == "exact"
                    strength[n] = float(row[3]) if row[3] is not None else 0.0
                    event_ids[n] = event_to_id.get(row[4], 255) if row[4] else 255
                    n += 1
            # Trim to actual count
            src = src[:n]
            tgt = tgt[:n]
            exact = exact[:n]
            strength = strength[:n]
            event_ids = event_ids[:n]
            print(f"    Loaded {n:,} edges from DB ({time.time()-t0:.1f}s)")
            try:
                np.savez(cache_path, src=src, tgt=tgt, exact=exact,
                         strength=strength, event_ids=event_ids,
                         event_names=event_names,
                         edge_count=np.array(edge_count), card_count=np.array(card_count))
                print(f"    Cached to {cache_path}")
            except Exception as e:
                print(f"    Cache write failed: {e}")

        # Build event lookup dicts
        self._event_names = list(event_names)
        self._bit_to_event = {i: str(name) for i, name in enumerate(event_names)}

        # Build outgoing/incoming adjacency: card_idx -> numpy array of unique neighbors
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

        del src, tgt, exact, strength, event_ids

        self._idx_to_oid = {v: k for k, v in self.oid_to_idx.items()}
        self._n_cards_idx = len(self.oid_to_idx)
        self._has_edge_index = True

        elapsed = time.time() - t0
        n_unique = sum(len(v) for v in self._adj_out.values())
        print(f"    Edge index built: {elapsed:.1f}s, {n_unique:,} unique outgoing pairs")

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
            str_dict = {}
            evt_dict = {}
            for j in range(start, end):
                v = int(sv[j])
                str_dict[v] = str_dict.get(v, 0.0) + float(ss[j])
                eid = int(se[j])
                if eid < 32:
                    evt_dict[v] = evt_dict.get(v, 0) | (1 << eid)
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
                orig = _re.search(r'Origin\$\s*(\S+)', raw_line)
                dest = _re.search(r'Destination\$\s*(\S+)', raw_line)
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
                dest = _re.search(r'Destination\$\s*(\S+)', raw_line)
                orig = _re.search(r'Origin\$\s*(\S+)', raw_line)
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
            event_m = _re.search(r'Event\$\s*(\S+)', raw_line)
            repl_m = _re.search(r'ReplaceWith\$\s*(\S+)', raw_line)
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
                tgt_m = _re.search(rf'{field}\$\s*(\S+)', raw_line)
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

        # Commander subtypes for tribal matching (populated from cards table)
        self.cmdr_subtypes = set()
        cmdr_type_row = ctx.conn.execute(
            "SELECT type_line FROM cards WHERE oracle_id = ?", (cmdr_oid,)
        ).fetchone()
        if cmdr_type_row and cmdr_type_row[0] and "\u2014" in cmdr_type_row[0]:
            try:
                self.cmdr_subtypes = {
                    s.lower() for s in cmdr_type_row[0].split("\u2014")[1].strip().split()
                }
            except (IndexError, AttributeError):
                pass

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
        event_expr = "event" if ctx._has_event_col else "json_extract(detail, '$.event')"
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
        except Exception:
            pass

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

        # Zone interaction flags
        self.cmdr_zone_graveyard = self.cmdr_oid in ctx._zone_graveyard
        self.cmdr_zone_exile = self.cmdr_oid in ctx._zone_exile

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

        # Zone interaction flags
        self.cmdr_zone_graveyard = self.cmdr_oid in ctx._zone_graveyard
        self.cmdr_zone_exile = self.cmdr_oid in ctx._zone_exile


def compute_card_features(card_oid: str, card_type_line: str, card_cmc: float,
                          ctx: ForgeFeatureContext, cmdr: CmdrFeatureContext) -> list:
    """Compute the 98-feature vector for a single (commander, card) pair.

    Returns a list of 98 floats matching FORGE_FEATURE_NAMES order.
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
    # Log-scaled to reduce older-card bias (Sol Ring 500 neighbors vs new card 50)
    hub = 0.0
    if ctx._has_edge_index and di is not None:
        n_out = len(ctx._adj_out.get(di, []))
        n_in = len(ctx._adj_in.get(di, []))
        hub = np.log2(1.0 + min(n_out + n_in, 500))
    elif di is not None:
        hub = np.log2(1.0 + min(cmdr.deck_edge_counts.get(card_oid, 0), 20))

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
        # Check excluded_subtypes — card explicitly excludes commander's type
        # e.g., Hornbash Mentor targets nonHuman → anti-tribal for Human commanders
        if anti_tribal == 0.0:
            excl_subs = card_profile.get('excluded_subtypes', set())
            for es in excl_subs:
                if es in cmdr.cmdr_subtypes:
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

    # ── Forge deck tags: Forge's deck-building AI signals ──

    card_has = ctx._deck_has.get(card_oid, set())
    card_hints = ctx._deck_hints.get(card_oid, set())
    card_needs = ctx._deck_needs.get(card_oid, set())

    # F51: deck_hints_to_has — commander hints X, card has X
    # e.g., Krenko hints Type$Goblin, Chancellor of the Forge has Type$Goblin → strong match
    hints_to_has = float(len(cmdr.cmdr_hints & card_has))

    # F52: deck_has_to_hints — card hints X, commander has X
    # e.g., Card hints Ability$Token, Krenko has Ability$Token → card wants this commander
    has_to_hints = float(len(cmdr.cmdr_has & card_hints))

    # F53: deck_needs_to_has — card needs X, commander has X
    # e.g., Card needs Ability$Counters, Atraxa has Ability$Proliferate → card functions here
    needs_to_has = float(len(cmdr.cmdr_has & card_needs))

    # F54: deck_has_overlap — shared has tags (theme alignment)
    has_overlap = float(len(cmdr.cmdr_has & card_has))

    # F55: deck_hints_overlap — both want the same deck themes
    hints_overlap = float(len(cmdr.cmdr_hints & card_hints))

    # F71: cmdr_needs_to_card_has — commander NEEDS X, card HAS X
    # e.g., Atraxa needs Ability$Counters, card has Ability$Counters → card provides what cmdr requires
    cmdr_needs_to_has = float(len(cmdr.cmdr_needs & card_has))

    # F72: card_needs_satisfied — how many of card's needs are met by commander's has+hints?
    # Card needs Type$Goblin, commander hints Type$Goblin → card will work in this deck
    card_needs_met = 0.0
    if card_needs:
        met = len(card_needs & (cmdr.cmdr_has | cmdr.cmdr_hints))
        card_needs_met = float(met) / float(len(card_needs))  # fraction satisfied (0-1)

    # F73: needs_rarity — how rare/specific are the card's needs?
    # Needing Type$Snow (20 providers) is much harder than Type$Creature (38 providers)
    # Inverse of how many cards provide each needed tag → higher = harder requirement
    needs_rarity = 0.0
    if card_needs:
        for need_tag in card_needs:
            n_providers = len(ctx._deck_has_providers.get(need_tag, set()))
            if n_providers > 0:
                needs_rarity += 1.0 / min(n_providers, 100)  # cap denominator
        needs_rarity = min(needs_rarity, 5.0)

    # ── Numeric effect scaling features ──

    # F56: damage_scales — card's damage amount is variable (X/Y)
    dmg_amt = card_profile.get('damage_amount')
    damage_scales = 1.0 if dmg_amt in ('X', 'Y') else 0.0

    # F57: draw_scales — card's draw amount is variable (X/Y)
    draw_amt = card_profile.get('cards_drawn')
    draw_scales = 1.0 if draw_amt in ('X', 'Y') else 0.0

    # F58: life_scales — card's life amount is variable (X/Y)
    life_amt = card_profile.get('life_amount')
    life_scales = 1.0 if life_amt in ('X', 'Y') else 0.0

    # F59: produces_mana — card produces mana (mana rock/dork signal)
    produces_mana = 1.0 if card_profile.get('produces_mana', False) else 0.0

    # F60: counter_num_variable — card places X/Y counters (scales with commander)
    counter_num_var = 1.0 if card_profile.get('counter_num_variable', False) else 0.0

    # F61: grants_abilities — card grants abilities to other permanents
    grants_abilities = 1.0 if card_profile.get('grants_abilities', False) else 0.0

    # F62: token_amount_variable — card creates X tokens (scales with game state)
    token_amt_var = 1.0 if card_profile.get('token_amount_variable', False) else 0.0

    # ── New features: ability counts, token complexity, zone interaction ──

    # F63: total_ability_count
    total_abilities = float(min(ctx._total_ability_counts.get(card_oid, 0), 15))

    # F64: triggered_ability_count
    triggered_count = float(min(ctx._triggered_counts.get(card_oid, 0), 10))

    # F65: token_power_toughness
    token_pt = float(min(ctx._token_max_pt.get(card_oid, 0), 20))

    # F66: token_keyword_count
    token_kw = float(min(ctx._token_max_kw.get(card_oid, 0), 5))

    # F67: zone_graveyard_interact
    zone_gy = 1.0 if (card_oid in ctx._zone_graveyard and cmdr.cmdr_zone_graveyard) else 0.0

    # F68: zone_exile_interact
    zone_ex = 1.0 if (card_oid in ctx._zone_exile and cmdr.cmdr_zone_exile) else 0.0

    # F69: ability_density
    raw_count = ctx._total_ability_counts.get(card_oid, 0)
    ability_dens = float(raw_count) / max(card_cmc, 1.0) if raw_count > 0 else 0.0
    ability_dens = min(ability_dens, 5.0)

    # ── Interaction features: cross anti-synergy with positive signals ──

    # F73: temp_buff_counter_cmdr — card gives temporary buffs but commander wants
    # permanent +1/+1 counters. E.g., Dawnhart Disciple gives "until EOT" pump
    # but Kyler wants permanent counters.
    cmdr_counters = cmdr.cmdr_profile.get('counter_types', set())
    card_dur = card_profile.get('duration', set())
    temp_counter_clash = 1.0 if ('P1P1' in cmdr_counters and
                                  'temporary' in card_dur and
                                  'permanent' not in card_dur) else 0.0

    # F76: put_counter_ratio — fraction of card's buff verbs that place permanent
    # counters (PutCounter/PutCounterAll) vs temporary pumps (Pump/PumpAll).
    # 1.0 = all counter-based, 0.0 = all pump-based, 0.5 = no buff verbs.
    card_verbs = card_profile.get('verbs', set())
    n_counter = sum(1 for v in card_verbs if v in ('PutCounter', 'PutCounterAll'))
    n_pump = sum(1 for v in card_verbs if v in ('Pump', 'PumpAll'))
    n_buff = n_counter + n_pump
    put_counter_ratio = float(n_counter) / n_buff if n_buff > 0 else 0.5

    # F75: cmdr_counter_x_put_counter — commander uses +1/+1 counters AND card
    # places counters (not just pumps). Captures Kyler + Hardened Scales type synergy.
    cmdr_counter_x_put = 1.0 if ('P1P1' in cmdr_counters and n_counter > 0) else 0.0

    # F76: static_anthem_counter_cmdr — card is a static anthem (Continuous+AddPower,
    # no PutCounter) but commander uses +1/+1 counters. Anthems can't be proliferated
    # or doubled — they're worse than actual counters for counter commanders.
    static_anthem_clash = 1.0 if ('P1P1' in cmdr_counters and
                                   card_profile.get('has_static_anthem', False)) else 0.0

    # F77: counters_on_lands — card places counters on lands (Earthbend, PutCounter
    # targeting lands). These counters don't benefit creature-based counter strategies.
    counters_on_lands = 1.0 if card_profile.get('counters_on_lands', False) else 0.0

    # F78: cmdr_p1p1_card_no_counters — commander uses +1/+1 counters but card has
    # NO interaction with P1P1 counters at all (no PutCounter, no P1P1 reference).
    card_has_p1p1 = card_profile.get('has_p1p1', False)
    card_counter_verbs = card_verbs & {'PutCounter', 'PutCounterAll', 'Proliferate', 'MoveCounter'}
    no_counter_for_cmdr = 1.0 if ('P1P1' in cmdr_counters and
                                   not card_has_p1p1 and
                                   not card_counter_verbs) else 0.0

    # ── Functional fingerprint dot-product features ──
    # These capture semantic synergy: "cmdr produces counters + card amplifies counters"
    card_func = ctx._func_fingerprints.get(card_oid)
    cmdr_func = cmdr.cmdr_func

    func_produces_amp = 0.0   # F79: cmdr produces X, card amplifies X
    func_requires_prod = 0.0  # F80: cmdr requires X trigger, card produces X
    func_card_req_cmdr = 0.0  # F81: card requires X trigger, cmdr produces X
    func_full_cosine = 0.0    # F82: overall functional similarity

    # ── Theme-based features (F83-F97) ──

    # Equipment theme features (F83-F85)
    # F83: cmdr_equipment_theme — commander wants equipment
    cmdr_equip = 1.0 if cmdr.cmdr_equipment_theme else 0.0
    # F84: card_equipment_payoff — card is equipment or cares about equipment
    card_equip = 1.0 if (card_oid in ctx._equipment_cards or
                          card_oid in ctx._equipment_payoffs) else 0.0
    # F85: equipment_theme_match — both align on equipment
    equip_match = cmdr_equip * card_equip

    # Enchantress theme features (F86-F88)
    # F86: cmdr_enchantress_theme — commander wants enchantments
    cmdr_ench = 1.0 if cmdr.cmdr_enchantress_theme else 0.0
    # F87: card_enchantress_payoff — card triggers on or cares about enchantments
    card_ench = 0.0
    if card_oid in ctx._enchantress_payoffs:
        card_ench = 1.0
    elif "Enchantment" in tl and "Creature" not in tl:
        # Pure enchantment (not enchantment creature) — relevant for enchantress
        card_ench = 0.5
    # F88: enchantress_theme_match
    ench_match = cmdr_ench * card_ench

    # Defender theme features (F89-F91)
    # F89: cmdr_defender_theme — commander cares about defenders/walls
    cmdr_def = 1.0 if cmdr.cmdr_defender_theme else 0.0
    # F90: card_has_defender — card has defender or is a Wall
    card_def = 1.0 if (card_oid in ctx._defender_cards or
                        "Wall" in tl) else 0.0
    # F91: defender_theme_match
    def_match = cmdr_def * card_def

    # ETB doubler features (F92-F94)
    # F92: card_is_etb_doubler — Panharmonicon-class card
    card_etb_dbl = 1.0 if card_oid in ctx._etb_doublers else 0.0
    # F93: cmdr_etb_density — how many ETB-related verbs/triggers commander has
    cmdr_etb = min(cmdr.cmdr_etb_density, 5.0)
    # F94: etb_doubler_match — doubler × commander ETB density
    etb_match = card_etb_dbl * cmdr_etb

    # Tribal depth features (F95-F97)
    # F95: tribal_lord_for_cmdr — card gives bonuses to commander's creature type
    # Check if card's trigger_filters or targets include commander's subtypes AND
    # card has buff verbs (Pump/PumpAll/PutCounter/Continuous)
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
    # F96: tribal_member_of_cmdr — card IS the creature type the commander filters on
    tribal_member = 0.0
    if cmdr.cmdr_tribal_filters and "Creature" in tl and "\u2014" in tl:
        try:
            card_subs = {s.lower() for s in tl.split("\u2014")[1].strip().split()}
            if cmdr.cmdr_tribal_filters & card_subs:
                tribal_member = 1.0
        except (IndexError, AttributeError):
            pass
    # F97: tribal_synergy_depth — combined tribal signal:
    #   subtype match + lord bonus + token type match + trigger filter match
    tribal_depth = tribal + tribal_lord + tribal_member
    if cmdr.cmdr_subtypes:
        # Token subtype matching
        card_tok_subs = ctx._token_subtypes.get(card_oid, set())
        if card_tok_subs & cmdr.cmdr_subtypes:
            tribal_depth += 1.0

    if card_func is not None and cmdr_func is not None:
        P = ForgeFeatureContext._FUNC_PRODUCES_SLICE
        R = ForgeFeatureContext._FUNC_REQUIRES_SLICE
        A = ForgeFeatureContext._FUNC_AMPLIFIES_SLICE

        # Commander produces → card amplifies: project produces dims to amplifies dims
        # amplifies[0]=counter_doubler ↔ produces[1]=p1p1_counters
        # amplifies[1]=token_doubler ↔ produces[0]=tokens
        # amplifies[2]=damage_doubler ↔ produces[5]=damage
        # amplifies[3]=lifegain_doubler ↔ produces[6]=lifegain
        _amp_to_prod = [1, 0, 5, 6]  # maps amplifies dim → produces dim
        cmdr_prod_projected = np.array([cmdr_func[P.start + i] for i in _amp_to_prod])
        func_produces_amp = float(np.dot(cmdr_prod_projected, card_func[A]))

        # Requires ↔ Produces: both are 11/12 dims but different semantics
        # Use element-wise min and sum: if cmdr requires creature_etb AND card produces tokens → match
        # Map: requires[0]=creature_etb ↔ produces[0]=tokens (tokens ETB)
        #       requires[4]=damage_dealt ↔ produces[5]=damage
        #       requires[5]=lifegain_trigger ↔ produces[6]=lifegain
        #       requires[8]=sacrifice ↔ produces[7]=removal (sacrifice is removal)
        #       requires[10]=counter_placed ↔ produces[1]=p1p1_counters
        _req_to_prod = {0: 0, 4: 5, 5: 6, 8: 7, 10: 1}  # req_dim → prod_dim
        for req_dim, prod_dim in _req_to_prod.items():
            func_requires_prod += cmdr_func[R.start + req_dim] * card_func[P.start + prod_dim]
            func_card_req_cmdr += card_func[R.start + req_dim] * cmdr_func[P.start + prod_dim]

        # Full cosine similarity across all 33 dimensions
        norm_c = np.linalg.norm(cmdr_func)
        norm_d = np.linalg.norm(card_func)
        if norm_c > 0 and norm_d > 0:
            func_full_cosine = float(np.dot(cmdr_func, card_func) / (norm_c * norm_d))

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
        # ── Theme-based features ──
        cmdr_equip,                                      # F83 cmdr_equipment_theme
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
