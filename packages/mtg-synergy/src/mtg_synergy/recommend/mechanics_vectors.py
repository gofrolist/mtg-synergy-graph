"""Build dense mechanics vectors from Forge structured ability data.

Each card gets PRODUCE and CONSUME vectors in a SHARED concept space.
Concepts are auto-derived from Forge DSL fields as structured event tuples:
  (event_class, type_qualifier, from_zone, to_zone)

Verbs and triggers that describe the same game event map to the SAME tuple,
so the dot product of one card's produces and another's consumes captures synergy
without hand-coded concept lists.

Plus subtype dimensions shared between production (token types, card types)
and consumption (trigger filters, sacrifice costs).
"""
import re
import numpy as np
from collections import Counter

# Pre-compiled regex patterns
_RE_AFFECTED = re.compile(r'Affected\$\s*(\S+)')
_RE_EVENT = re.compile(r'Event\$\s*(\S+)')
_RE_ADDTRIGGER = re.compile(r'AddTrigger\$\s*(\S+)')
_RE_DESTINATION = re.compile(r'Destination\$\s*(\S+)')
_RE_ORIGIN = re.compile(r'Origin\$\s*(\S+)')
_RE_SAC_COST = re.compile(r"Sac<\d+/([^/>]+)", re.IGNORECASE)
_RE_FOR_EACH = re.compile(r"for each (\w+)")
_RE_TAP_COST = re.compile(r"tap<\d+/([^/>]+)")
_RE_CHANGE_TYPE = re.compile(r'ChangeType\$\s*(\S+)')
_RE_DISCARD_COST = re.compile(r"Discard<\d+/([^/>]+)", re.IGNORECASE)
_RE_TRIGGER_PREFIX = re.compile(r'^Trigger|^Trig')

# ── Canonical event class mappings ──
# Verbs and triggers that describe the same game event share the same event_class.
# This is the ONLY manual mapping — everything else is auto-derived from DSL fields.

_VERB_EVENT_CLASS = {
    "Token":         "enters",
    "ChangeZone":    "zone_change",
    "ChangeZoneAll": "zone_change",
    "DealDamage":    "damage",
    "DamageAll":     "damage",
    "Draw":          "draw",
    "Dig":           "draw",
    "PutCounter":    "counter_add",
    "PutCounterAll": "counter_add",
    "GainLife":      "life_gain",
    "LoseLife":      "life_lose",
    "Destroy":       "destroy",
    "DestroyAll":    "destroy",
    "Sacrifice":     "sacrifice",
    "Discard":       "discard",
    "Mill":          "mill",
    "Tap":           "tap",
    "Untap":         "untap",
    "Pump":          "pump",
    "PumpAll":       "pump",
    "Mana":          "mana",
    "Counter":       "counter_spell",
    "Animate":       "enters",
    "Attach":        "attach",
    "Equip":         "equip",
    "CopyPermanent": "enters",
    "Fight":         "damage",
    "Proliferate":   "counter_add",
    "RemoveCounter": "counter_remove",
    "MoveCounter":   "counter_add",
    "Panharmonicon": "etb_doubled",
}

_TRIGGER_EVENT_CLASS = {
    "ChangesZone":     "zone_change",
    "ChangesZoneAll":  "zone_change",
    "DamageDone":      "damage",
    "DamageDoneOnce":  "damage",
    "DamageAll":       "damage",
    "Drawn":           "draw",
    "CounterAdded":    "counter_add",
    "CounterAddedOnce": "counter_add",
    "LifeGained":      "life_gain",
    "LifeLost":        "life_lose",
    "Destroyed":       "destroy",
    "Sacrificed":      "sacrifice",
    "Discarded":       "discard",
    "Milled":          "mill",
    "SpellCast":       "spell_cast",
    "SpellCopy":       "spell_cast",
    "Attacks":         "attacks",
    "AttackersDeclared": "attacks",
    "Blocks":          "blocks",
    "BecomesTarget":   "target",
    "Taps":            "tap",
    "Untaps":          "untap",
    "TokenCreated":    "token_created",
    "CounterRemoved":  "counter_remove",
    "Phase":           "phase",
    "TurnFaceUp":      None,  # no synergy signal
    "Cycled":          "discard",
}

# R: replacement Event$ types → equivalent verb for event class lookup
_REPLACEMENT_EVENT_TO_VERB = {
    "Mill": "Mill", "DamageDone": "DealDamage", "GainLife": "GainLife",
    "Draw": "Draw", "LoseLife": "LoseLife",
}

# AddTrigger$ SVar name → trigger mode for event class lookup
_ADDTRIGGER_MAP = {
    'Attack': 'Attacks', 'AttackTrig': 'Attacks',
    'Attacks': 'Attacks', 'AttackersDeclared': 'AttackersDeclared',
    'ETB': 'ChangesZone', 'Death': 'ChangesZone',
    'Dies': 'ChangesZone', 'DiesTrig': 'ChangesZone',
    'SpellCast': 'SpellCast', 'DamageDone': 'DamageDone',
    'DamageDoneOnce': 'DamageDoneOnce',
    'Drawn': 'Drawn', 'DrawnCard': 'Drawn',
    'LifeGained': 'LifeGained', 'LifeLost': 'LifeLost',
    'Phase': 'Phase', 'Upkeep': 'Phase',
    'Sacrificed': 'Sacrificed', 'Discarded': 'Discarded',
    'Milled': 'Milled', 'Taps': 'Taps', 'Untaps': 'Untaps',
    'CounterAdded': 'CounterAdded', 'TokenCreated': 'TokenCreated',
}

# ── Category assignment by event class ──
# Maps event_class → mech category for the 8-category dot product features.
_EVENT_CATEGORY = {
    "enters":        "board",
    "zone_change":   "board",  # base; zone-qualified tuples go to "zones"
    "destroy":       "board",
    "sacrifice":     "board",
    "token_created": "board",
    "damage":        "resource",
    "draw":          "resource",
    "counter_add":   "resource",
    "counter_remove": "resource",
    "life_gain":     "resource",
    "life_lose":     "resource",
    "discard":       "disruption",
    "mill":          "disruption",
    "target":        "disruption",
    "counter_spell": "disruption",
    "spell_cast":    "tempo",
    "attacks":       "tempo",
    "blocks":        "tempo",
    "tap":           "utility",
    "untap":         "utility",
    "pump":          "utility",
    "mana":          "utility",
    "equip":         "themes",
    "attach":        "themes",
    "etb_doubled":   "themes",
    "phase":         "utility",
}

# Known card types for trigger_filter parsing
_CARD_TYPES = frozenset({"card", "creature", "artifact", "enchantment",
                         "instant", "sorcery", "permanent", "land",
                         "planeswalker", "spell", "tribal", "battle"})

# Zones to normalize
_ZONE_NORMALIZE = {
    "battlefield": "bf", "graveyard": "gy", "exile": "ex",
    "hand": "hand", "library": "lib", "command": None, "any": None,
}


def _normalize_zone(z: str | None) -> str | None:
    """Normalize a zone string to a short canonical form, or None if generic."""
    if not z:
        return None
    zl = z.lower().split(".")[0].split(",")[0]
    return _ZONE_NORMALIZE.get(zl)


def _extract_type_qualifier(trigger_filter: str | None, raw_line: str,
                            token_script: str | None,
                            exec_change_type: str | None) -> str | None:
    """Extract the primary type qualifier from DSL fields.

    Returns lowercase type string (e.g., 'land', 'creature', 'artifact') or None.
    """
    # From Execute$ SVar ChangeType (highest priority — actual effect type)
    if exec_change_type:
        base = exec_change_type.split(".")[0].lower()
        if base in _CARD_TYPES and base not in ("card", "permanent", "spell"):
            return base

    # From trigger_filter (e.g., "Land.YouCtrl" → "land")
    if trigger_filter:
        first_part = trigger_filter.split(",")[0].split(".")[0].strip().lower()
        if first_part in _CARD_TYPES and first_part not in ("card", "permanent", "spell"):
            return first_part

    # From raw_line ChangeType$ (direct ability)
    if raw_line:
        m = _RE_CHANGE_TYPE.search(raw_line)
        if m:
            base = m.group(1).split(".")[0].lower()
            if base in _CARD_TYPES and base not in ("card", "permanent", "spell"):
                return base
        # From ValidTgts$ (e.g., "ValidTgts$ Land")
        if "ValidTgts$ Land" in raw_line and "nonLand" not in raw_line:
            return "land"

    # From token_script (e.g., "g_5_3_elemental" — always creatures)
    if token_script:
        return "creature"

    return None


def _is_opponent_only(defined: str | None, raw_line: str) -> bool:
    """Check if an ability targets only opponents (not self/any player)."""
    d = (defined or "").lower()
    if "opponent" in d and "you" not in d:
        return True
    if d in ("triggeredplayer", "triggeredtarget"):
        if ("ValidPlayer$ Player.Opponent" in raw_line
                or "ValidPlayer$ Opponent" in raw_line):
            return True
    if not defined and raw_line.startswith("R:"):
        if ("ValidPlayer$ Player.Opponent" in raw_line
                or "ValidPlayer$ Opponent" in raw_line):
            return True
    if "Affected$" in raw_line:
        m = _RE_AFFECTED.search(raw_line)
        if m:
            val = m.group(1).lower()
            if ".oppctrl" in val and ".youctrl" not in val:
                return True
    return False


def _normalize_addtrigger(name):
    """Normalize AddTrigger$ SVar names to trigger mode keys."""
    n = _RE_TRIGGER_PREFIX.sub('', name)
    return _ADDTRIGGER_MAP.get(n, n if n in _TRIGGER_EVENT_CLASS else None)


def _collect_subtypes(abilities):
    """Count subtypes from trigger_filter and token_script, return top 80."""
    subtype_counts = Counter()
    kw_skip = {"flying", "haste", "trample", "vigilance", "deathtouch",
               "lifelink", "menace", "reach", "defender", "first",
               "strike", "double", "indestructible", "hexproof",
               "sac", "unblockable", "shroud", "wither", "persist"}

    for ab in abilities:
        trig_filter, token_script = ab[3], ab[6]
        if trig_filter:
            for part in trig_filter.split(","):
                main = part.split(".")[0].strip()
                if main and main[0].isupper() and main.lower() not in _CARD_TYPES:
                    subtype_counts[main.lower()] += 1
        if token_script:
            parts = token_script.lower().split("_")
            if len(parts) >= 4:
                for p in parts[3:]:
                    if p and p not in kw_skip and len(p) > 1:
                        subtype_counts[p] += 1

    # Sort by count descending, then name ascending for deterministic tie-breaking
    return [st for st, _ in sorted(subtype_counts.most_common(80),
                                    key=lambda x: (-x[1], x[0]))]


def _parse_exec_section(raw_line: str):
    """Parse |EXEC| section from raw_line. Returns (change_type, origin, dest)."""
    if "|EXEC|" not in raw_line:
        return None, None, None
    exec_section = raw_line[raw_line.find("|EXEC|") + 6:]
    ct, orig, dest = None, None, None
    m = _RE_CHANGE_TYPE.search(exec_section)
    if m:
        ct = m.group(1)
    m = _RE_ORIGIN.search(exec_section)
    if m:
        orig = m.group(1)
    m = _RE_DESTINATION.search(exec_section)
    if m:
        dest = m.group(1)
    return ct, orig, dest


def _extract_tuples_from_ability(ab):
    """Extract event tuples from a single ability row for vocabulary building.

    Returns set of (event_class, type_qualifier, from_zone, to_zone) tuples.
    """
    tuples = set()
    verb, trig_mode, trig_filter = ab[1], ab[2], ab[3]
    keyword = ab[5]
    token_script = ab[6]
    raw_line = ab[8] or ""
    trig_origin = ab[10] if len(ab) > 10 else None
    trig_dest = ab[11] if len(ab) > 11 else None

    exec_ct, exec_orig, exec_dest = _parse_exec_section(raw_line)
    type_qual = _extract_type_qualifier(trig_filter, raw_line, token_script, exec_ct)

    # Produces tuples from verb
    is_replacement = raw_line.startswith("R:")
    effective_verb = verb
    if is_replacement:
        m = _RE_EVENT.search(raw_line)
        effective_verb = _REPLACEMENT_EVENT_TO_VERB.get(m.group(1)) if m else None

    if effective_verb and effective_verb in _VERB_EVENT_CLASS:
        ec = _VERB_EVENT_CLASS[effective_verb]
        if ec:
            from_z = _normalize_zone(exec_orig or trig_origin)
            to_z = _normalize_zone(exec_dest or trig_dest)
            if ec == "zone_change" and to_z:
                tuples.add((ec, type_qual, from_z, to_z))
                tuples.add((ec, None, from_z, to_z))
                if type_qual:
                    tuples.add((ec, type_qual, None, to_z))
            else:
                tuples.add((ec, None, None, None))
                if type_qual:
                    tuples.add((ec, type_qual, None, None))

    # Special produces
    if "AdjustLandPlays$" in raw_line:
        tuples.add(("zone_change", "land", None, "bf"))
    if "MayPlay$" in raw_line and "Affected$" in raw_line:
        affected = raw_line.split("Affected$")[1].split("|")[0]
        if "Land" in affected and "nonLand" not in affected:
            tuples.add(("zone_change", "land", None, "bf"))

    # Consumes tuples from trigger_mode
    if trig_mode and trig_mode in _TRIGGER_EVENT_CLASS:
        ec = _TRIGGER_EVENT_CLASS[trig_mode]
        if ec:
            from_z = _normalize_zone(trig_origin)
            to_z = _normalize_zone(trig_dest)
            if not to_z:
                m = _RE_DESTINATION.search(raw_line)
                if m:
                    to_z = _normalize_zone(m.group(1))
            if not from_z:
                m = _RE_ORIGIN.search(raw_line)
                if m:
                    from_z = _normalize_zone(m.group(1))
            if ec == "zone_change" and to_z:
                tuples.add((ec, type_qual, from_z, to_z))
                tuples.add((ec, None, from_z, to_z))
                if type_qual:
                    tuples.add((ec, type_qual, None, to_z))
            else:
                tuples.add((ec, None, None, None))
                if type_qual:
                    tuples.add((ec, type_qual, None, None))

    if keyword == 'Equip':
        tuples.add(("equip", None, None, None))
    if keyword == 'Defender':
        tuples.add(("defender", None, None, None))

    return tuples


def _build_concept_vocabulary(abilities):
    """Scan abilities and build concept vocabulary from extracted event tuples.

    Returns:
        tuple_to_idx: dict[tuple → int]
        n_concepts: total concept dimensions
        category_dims: dict[category_name → list[int]]
    """
    tuples_seen = set()
    for ab in abilities:
        tuples_seen |= _extract_tuples_from_ability(ab)

    # Always-present tuples (type-based produces, themes, secondary events)
    tuples_seen |= {
        ("spell_cast", None, None, None),
        ("attacks", None, None, None),
        ("available", "creature", None, None),
        ("available", "artifact", None, None),
        ("etb_doubled", None, None, None),
        ("defender", None, None, None),
        ("token_created", None, None, None),
        ("life_lose", None, None, None),
    }

    # Sort deterministically for stable indices
    sorted_tuples = sorted(tuples_seen, key=lambda t: (
        t[0] or "", t[1] or "", t[2] or "", t[3] or ""))
    tuple_to_idx = {t: i for i, t in enumerate(sorted_tuples)}
    n_concepts = len(sorted_tuples)

    # Build category assignments
    cat_names = ["board", "resource", "disruption", "tempo",
                 "utility", "zones", "themes", "tribal"]
    category_dims = {c: [] for c in cat_names}
    for t, idx in tuple_to_idx.items():
        ec = t[0]
        from_z, to_z = t[2], t[3]
        if ec == "zone_change" and (from_z or to_z):
            category_dims["zones"].append(idx)
        elif ec in ("equip", "attach", "etb_doubled", "defender"):
            category_dims["themes"].append(idx)
        elif ec == "available":
            category_dims["board"].append(idx)
        else:
            cat = _EVENT_CATEGORY.get(ec, "utility")
            category_dims[cat].append(idx)

    return tuple_to_idx, n_concepts, category_dims


def build_mechanics_vectors(conn, preloaded_abilities=None, type_lines=None,
                            quiet=False):
    """Build dense mechanics vectors for all cards with Forge data.

    Concepts are auto-derived from Forge DSL fields as structured event tuples.
    Both verbs and triggers map to the same tuple space, so dot products
    between produces and consumes capture synergy automatically.

    Args:
        conn: SQLite connection
        type_lines: optional dict[oracle_id → type_line] from CardProvider.
        preloaded_abilities: optional list of ability tuples.

    Returns:
        produces: dict[oracle_id → numpy float32 vector]
        consumes: dict[oracle_id → numpy float32 vector]
        dim: total vector dimension
        subtype_idx: dict[subtype → dimension index]
        category_dims: dict[category_name → list[int]] (for mech_* features)
    """
    if preloaded_abilities is not None:
        abilities = preloaded_abilities
    else:
        forge_to_oid = {}
        for row in conn.execute("SELECT forge_name, oracle_id FROM forge_name_map"):
            forge_to_oid[row[0]] = row[1]

        abilities = []
        for row in conn.execute(
            "SELECT card_name, verb, trigger_mode, trigger_filter, cost, "
            "keyword, token_script, counter_type, raw_line, amount, "
            "trigger_origin, trigger_destination, defined "
            "FROM forge_abilities"
        ):
            oid = forge_to_oid.get(row[0])
            if oid:
                abilities.append((oid, row[1], row[2], row[3], row[4],
                                  row[5], row[6], row[7], row[8], row[9],
                                  row[10], row[11], row[12]))

    # Build vocabulary and subtypes
    tuple_to_idx, n_concepts, category_dims = _build_concept_vocabulary(abilities)
    top_subtypes = _collect_subtypes(abilities)
    subtype_idx = {st: n_concepts + i for i, st in enumerate(top_subtypes)}
    dim = n_concepts + len(top_subtypes)

    # Add tribal dims to category_dims
    category_dims["tribal"] = list(range(n_concepts, dim))

    produces = {}  # oid → vector
    consumes = {}  # oid → vector

    def _ensure_p(oid):
        if oid not in produces:
            produces[oid] = np.zeros(dim, dtype=np.float32)
        return produces[oid]

    def _ensure_c(oid):
        if oid not in consumes:
            consumes[oid] = np.zeros(dim, dtype=np.float32)
        return consumes[oid]

    def _add_tuple(vec, t, weight=1.0):
        """Add event tuple to vector if it exists in vocabulary."""
        idx = tuple_to_idx.get(t)
        if idx is not None:
            vec[idx] += weight

    for ab in abilities:
        oid = ab[0]
        verb, trig_mode, trig_filter = ab[1], ab[2], ab[3]
        keyword = ab[5]
        token_script = ab[6]
        raw_line = ab[8] or ""
        trig_origin = ab[10] if len(ab) > 10 else None
        trig_dest = ab[11] if len(ab) > 11 else None
        defined = ab[12] if len(ab) > 12 else None

        exec_ct, exec_orig, exec_dest = _parse_exec_section(raw_line)
        type_qual = _extract_type_qualifier(trig_filter, raw_line, token_script, exec_ct)

        # ── PRODUCES: effect verb → event tuples ──
        is_replacement = raw_line.startswith("R:")
        effective_verb = verb
        if is_replacement:
            m = _RE_EVENT.search(raw_line)
            if m:
                effective_verb = _REPLACEMENT_EVENT_TO_VERB.get(m.group(1))
                if ("Prevent$ True" in raw_line
                        or "PreventionEffect$ True" in raw_line
                        or "Layer$ CantHappen" in raw_line):
                    effective_verb = None
                else:
                    # Replacement effects CONSUME the event they modify
                    # (Bruvac wants mill to happen, even if he targets opponents)
                    r_verb = _REPLACEMENT_EVENT_TO_VERB.get(m.group(1))
                    if r_verb and r_verb in _VERB_EVENT_CLASS:
                        r_ec = _VERB_EVENT_CLASS[r_verb]
                        if r_ec:
                            c = _ensure_c(oid)
                            _add_tuple(c, (r_ec, None, None, None))
            else:
                effective_verb = None

        if effective_verb and effective_verb in _VERB_EVENT_CLASS:
            ec = _VERB_EVENT_CLASS[effective_verb]
            if ec:
                p = _ensure_p(oid)
                from_z = _normalize_zone(exec_orig or trig_origin)
                to_z = _normalize_zone(exec_dest or trig_dest)
                if ec == "zone_change" and to_z:
                    _add_tuple(p, (ec, type_qual, from_z, to_z))
                    _add_tuple(p, (ec, None, from_z, to_z))
                    if type_qual:
                        _add_tuple(p, (ec, type_qual, None, to_z))
                else:
                    _add_tuple(p, (ec, None, None, None))
                    if type_qual:
                        _add_tuple(p, (ec, type_qual, None, None))

        # Secondary produces: some verbs produce multiple event types
        if effective_verb == "Token":
            p = _ensure_p(oid)
            _add_tuple(p, ("token_created", None, None, None))
            _add_tuple(p, ("available", "creature", None, None))
        if effective_verb in ("Destroy", "DestroyAll", "Sacrifice"):
            p = _ensure_p(oid)
            _add_tuple(p, ("zone_change", None, None, "gy"))
        if effective_verb in ("DealDamage", "DamageAll"):
            p = _ensure_p(oid)
            _add_tuple(p, ("life_lose", None, None, None))

        # Keyword-based produces
        if keyword == 'Equip':
            p = _ensure_p(oid)
            _add_tuple(p, ("equip", None, None, None))
        if keyword == 'Defender':
            p = _ensure_p(oid)
            _add_tuple(p, ("defender", None, None, None))

        # AdjustLandPlays → produces land entering battlefield
        if "AdjustLandPlays$" in raw_line:
            p = _ensure_p(oid)
            _add_tuple(p, ("zone_change", "land", None, "bf"))

        # MayPlay lands → produces land entering battlefield
        if "MayPlay$" in raw_line and "Affected$" in raw_line:
            affected = raw_line.split("Affected$")[1].split("|")[0]
            if "Land" in affected and "nonLand" not in affected:
                p = _ensure_p(oid)
                _add_tuple(p, ("zone_change", "land", None, "bf"))

        # RepeatSubAbility$ — extract verb from sub-ability name (e.g., DBMill → Mill)
        if raw_line and 'RepeatSubAbility$' in raw_line:
            rsm = raw_line.split('RepeatSubAbility$')[1].split('|')[0].strip()
            # DB prefix convention: DBMill, DBDraw, DBDamage, etc.
            sub_verb = rsm.replace('DB', '') if rsm.startswith('DB') else rsm
            if sub_verb in _VERB_EVENT_CLASS:
                sec = _VERB_EVENT_CLASS[sub_verb]
                if sec:
                    p = _ensure_p(oid)
                    _add_tuple(p, (sec, None, None, None))

        # Panharmonicon-class effects
        if verb == 'Panharmonicon':
            p = _ensure_p(oid)
            _add_tuple(p, ("etb_doubled", None, None, None))

        # AddTrigger$ — granting triggers produces those events (weaker)
        if raw_line and 'AddTrigger$' in raw_line:
            at_m = _RE_ADDTRIGGER.search(raw_line)
            if at_m:
                trig_name = _normalize_addtrigger(at_m.group(1))
                if trig_name and trig_name in _TRIGGER_EVENT_CLASS:
                    ec = _TRIGGER_EVENT_CLASS[trig_name]
                    if ec:
                        p = _ensure_p(oid)
                        _add_tuple(p, (ec, None, None, None), weight=0.5)

        # Token with subtype → produces that subtype
        if token_script:
            parts = token_script.lower().split("_")
            if len(parts) >= 4:
                p = _ensure_p(oid)
                for part in parts[3:]:
                    if part in subtype_idx:
                        p[subtype_idx[part]] += 1.0

        # ── CONSUMES: trigger mode → event tuples ──
        if trig_mode and trig_mode in _TRIGGER_EVENT_CLASS:
            ec = _TRIGGER_EVENT_CLASS[trig_mode]
            if ec:
                c = _ensure_c(oid)
                from_z = _normalize_zone(trig_origin)
                to_z = _normalize_zone(trig_dest)
                if not to_z:
                    m = _RE_DESTINATION.search(raw_line)
                    if m:
                        to_z = _normalize_zone(m.group(1))
                if not from_z:
                    m = _RE_ORIGIN.search(raw_line)
                    if m:
                        from_z = _normalize_zone(m.group(1))
                if ec == "zone_change" and to_z:
                    _add_tuple(c, (ec, type_qual, from_z, to_z))
                    _add_tuple(c, (ec, None, from_z, to_z))
                    if type_qual:
                        _add_tuple(c, (ec, type_qual, None, to_z))
                else:
                    _add_tuple(c, (ec, None, None, None))
                    if type_qual:
                        _add_tuple(c, (ec, type_qual, None, None))

        # ETB-doubled consumes: ETB triggers benefit from Panharmonicon
        if trig_mode in ('ChangesZone', 'ChangesZoneAll'):
            etb_dest = trig_dest or ""
            if not etb_dest:
                m = _RE_DESTINATION.search(raw_line)
                if m:
                    etb_dest = m.group(1)
            if 'Battlefield' in etb_dest:
                c = _ensure_c(oid)
                _add_tuple(c, ("etb_doubled", None, None, None))

        # CanAttackDefender → consumes defenders
        if verb == 'CanAttackDefender':
            c = _ensure_c(oid)
            _add_tuple(c, ("defender", None, None, None))

        # Equipment trigger filter → consumes equip
        if trig_filter and 'Equipment' in trig_filter:
            c = _ensure_c(oid)
            _add_tuple(c, ("equip", None, None, None))

        # Trigger filter subtypes → consumes those subtypes
        if trig_filter:
            c = _ensure_c(oid)
            for part in trig_filter.split(","):
                main = part.split(".")[0].strip().lower()
                if main in subtype_idx:
                    c[subtype_idx[main]] += 1.0

        # ── Cost-based consumption ──
        if raw_line:
            for m in _RE_SAC_COST.findall(raw_line):
                sac_type = m.split("/")[0].split(".")[0].lower()
                c = _ensure_c(oid)
                if sac_type == "creature":
                    _add_tuple(c, ("sacrifice", "creature", None, None))
                    _add_tuple(c, ("available", "creature", None, None))
                elif sac_type == "artifact":
                    _add_tuple(c, ("available", "artifact", None, None))
                elif sac_type != "cardname":
                    _add_tuple(c, ("sacrifice", None, None, None), weight=0.5)
                    _add_tuple(c, ("available", "creature", None, None), weight=0.5)
                    if sac_type in subtype_idx:
                        c[subtype_idx[sac_type]] += 1.0

            for m in _RE_DISCARD_COST.findall(raw_line):
                p = _ensure_p(oid)
                _add_tuple(p, ("discard", None, None, None))
                _add_tuple(p, ("zone_change", None, None, "gy"))

    # ── Scaling patterns from raw_line ──
    def _scaling_tuples():
        if preloaded_abilities is not None:
            for ab in abilities:
                raw = (ab[8] or "").lower()
                if raw:
                    yield ab[0], raw, (ab[9] if len(ab) > 9 else None)
        else:
            for row in conn.execute(
                "SELECT fnm.oracle_id, fa.raw_line, fa.amount "
                "FROM forge_abilities fa "
                "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
                "WHERE fa.raw_line IS NOT NULL"
            ):
                raw = (row[1] or "").lower()
                if raw:
                    yield row[0], raw, row[2]

    for oid, raw_line, amount in _scaling_tuples():
        if amount == "X" and "for each" in raw_line:
            idx = raw_line.find("for each")
            snippet = raw_line[idx:]
            for m in _RE_FOR_EACH.finditer(snippet):
                t = m.group(1)
                if t in subtype_idx:
                    c = _ensure_c(oid)
                    c[subtype_idx[t]] += 1.0
                elif t == "creature":
                    c = _ensure_c(oid)
                    _add_tuple(c, ("available", "creature", None, None))
                elif t == "artifact":
                    c = _ensure_c(oid)
                    _add_tuple(c, ("available", "artifact", None, None), weight=0.5)

        if "tap<" in raw_line:
            for m in _RE_TAP_COST.finditer(raw_line):
                t = m.group(1).split("/")[0].split(".")[0].lower()
                if t in subtype_idx:
                    c = _ensure_c(oid)
                    c[subtype_idx[t]] += 1.0
                    _add_tuple(c, ("tap", None, None, None), weight=0.5)
                elif t == "creature":
                    c = _ensure_c(oid)
                    _add_tuple(c, ("tap", None, None, None))
                    _add_tuple(c, ("available", "creature", None, None), weight=0.5)

    # ── Type-based produces ──
    if type_lines is None:
        type_lines = {}
        for row in conn.execute("SELECT oracle_id, type_line FROM cards"):
            type_lines[row[0]] = row[1] or ""

    for oid in set(produces.keys()) | set(consumes.keys()):
        tl = type_lines.get(oid, "")
        if not tl:
            continue
        if "Land" not in tl:
            p = _ensure_p(oid)
            _add_tuple(p, ("spell_cast", None, None, None))
        if "Creature" in tl:
            p = _ensure_p(oid)
            _add_tuple(p, ("attacks", None, None, None))
            _add_tuple(p, ("available", "creature", None, None), weight=0.5)

    # ── L2 normalize ──
    for oid in produces:
        norm = np.linalg.norm(produces[oid])
        if norm > 0:
            produces[oid] /= norm
    for oid in consumes:
        norm = np.linalg.norm(consumes[oid])
        if norm > 0:
            consumes[oid] /= norm

    # Update module-level compat vars in-place so importers see the new values.
    # Using .clear()/.update() instead of rebinding preserves references held
    # by modules that did `from mechanics_vectors import _concept_idx`.
    global N_CONCEPTS
    GAME_CONCEPTS.clear()
    GAME_CONCEPTS.extend(tuple_to_idx.keys())
    N_CONCEPTS = n_concepts
    _concept_idx.clear()
    _concept_idx.update(tuple_to_idx)

    if not quiet:
        print(f"  Mechanics vectors: {len(produces)} producers, "
              f"{len(consumes)} consumers, dim={dim} "
              f"({n_concepts} concepts + {len(top_subtypes)} subtypes)")

    return produces, consumes, dim, subtype_idx, category_dims


# ── Backward compatibility ──
# Populated after first build_mechanics_vectors() call.
# forge_compute.py imports _concept_idx for concept lookups.
GAME_CONCEPTS = []
N_CONCEPTS = 0
_concept_idx = {}


# ── Readable concept labels for CLI display ──
_CONCEPT_LABELS = {
    ("counter_add", None, None, None): "+1/+1 counters",
    ("token_created", None, None, None): "tokens",
    ("zone_change", None, None, "bf"): "ETB",
    ("zone_change", None, "bf", "gy"): "dies/graveyard",
    ("zone_change", None, "gy", "bf"): "reanimate",
    ("zone_change", "land", None, "bf"): "landfall",
    ("attacks", None, None, None): "attacks",
    ("spell_cast", None, None, None): "spellslinger",
    ("damage", None, None, None): "damage",
    ("draw", None, None, None): "card draw",
    ("sacrifice", None, None, None): "sacrifice",
    ("life_gain", None, None, None): "lifegain",
    ("life_lose", None, None, None): "life loss",
    ("discard", None, None, None): "discard",
    ("mill", None, None, None): "mill",  # refined to "self-mill" in summarize_commander
    ("etb_doubled", None, None, None): "ETB doubling",
    ("enters", None, None, None): "enters play",
    ("enters", "creature", None, None): "creature ETB",
    ("available", "creature", None, None): "creature presence",
    ("defender", None, None, None): "defenders",
    ("destroy", None, None, None): "destroy",
    ("tap", None, None, None): "tap",
    ("untap", None, None, None): "untap",
    ("counter_remove", None, None, None): "counter removal",
    ("mana", None, None, None): "mana",
}


def summarize_commander(
    produces: dict, consumes: dict, subtype_idx: dict,
    cmdr_oid: str, cmdr_subtypes: set[str] | None = None, max_labels: int = 6,
) -> list[str]:
    """Return human-readable mechanical identity labels for a commander.

    Merges creature subtypes (from type line) with top mechanical concepts
    from the produces/consumes vectors. Purely Forge-derived, no EDHREC.
    """
    labels = []
    seen = set()

    # Subtypes the commander mechanically interacts with:
    # from mechanics vectors (consumes/produces) OR from type line if
    # the commander also produces tokens of that type
    pv = produces.get(cmdr_oid)
    cv = consumes.get(cmdr_oid)
    mech_subtypes = set()
    for st, idx in subtype_idx.items():
        if (cv is not None and idx < len(cv) and cv[idx] > 0.1) or \
           (pv is not None and idx < len(pv) and pv[idx] > 0.1):
            mech_subtypes.add(st)

    # Also include commander's own subtypes if they appear in mech vectors
    # (e.g., Krenko is a Goblin AND makes Goblins)
    if cmdr_subtypes:
        for st in sorted(cmdr_subtypes):
            if st in mech_subtypes:
                label = f"{st}s" if not st.endswith("s") else st
                labels.append(label)
                seen.add(label)

    # Then add remaining mechanical subtypes not from type line
    for st in sorted(mech_subtypes):
        label = f"{st}s" if not st.endswith("s") else st
        if label not in seen:
            labels.append(label)
            seen.add(label)

    # Generic concepts that appear for almost every creature commander — skip
    _GENERIC_CONCEPTS = {
        ("attacks", None, None, None),
        ("spell_cast", None, None, None),
        ("available", "creature", None, None),
        ("available", "artifact", None, None),
    }

    # Top mechanical concepts from both vectors
    for vec in [cv, pv]:
        if vec is None:
            continue
        for i in sorted(range(len(GAME_CONCEPTS)), key=lambda i: -vec[i]):
            if vec[i] < 0.1:
                break
            concept = GAME_CONCEPTS[i]
            if concept in _GENERIC_CONCEPTS:
                continue
            label = _CONCEPT_LABELS.get(concept)
            if not label:
                continue
            if label not in seen:
                labels.append(label)
                seen.add(label)
            if len(labels) >= max_labels:
                break
        if len(labels) >= max_labels:
            break

    return labels
