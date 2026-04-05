"""Build dense mechanics vectors from Forge structured ability data.

Each card gets PRODUCE and CONSUME vectors in a SHARED concept space.
Effects and triggers map to the same dimensions, so the dot product
of one card's produces and another's consumes captures synergy.

Game concepts (shared dimensions):
  creature_enters — Token creates creature, ChangesZone responds to ETB
  damage_dealt — DealDamage produces, DamageDone triggers on it
  card_drawn — Draw produces, Drawn triggers on it
  etc.

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
_RE_TRIGGER_PREFIX = re.compile(r'^Trigger|^Trig')

# AddTrigger$ SVar name → TRIGGER_TO_CONCEPTS key mapping
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

# Shared game concept dimensions — effects and triggers map to these
GAME_CONCEPTS = [
    "creature_enters",      # Token/ChangeZone → ChangesZone+Battlefield
    "artifact_enters",      # Token(artifact)/ChangeZone → ChangesZone+Battlefield
    "enchantment_enters",   # Attach/ChangeZone → ChangesZone+Battlefield
    "permanent_enters",     # Any permanent → ChangesZone+Battlefield
    "damage_dealt",         # DealDamage → DamageDone
    "card_drawn",           # Draw → Drawn
    "counter_added",        # PutCounter → CounterAdded
    "life_gained",          # GainLife → LifeGained
    "life_lost",            # LoseLife/DealDamage → LifeLost
    "creature_dies",        # Sacrifice/Destroy → ChangesZone to graveyard
    "permanent_destroyed",  # Destroy → Destroyed
    "card_discarded",       # Discard → Discarded
    "card_milled",          # Mill → Milled
    "spell_cast",           # Being a spell → SpellCast
    "creature_attacks",     # Being a creature → Attacks
    "creature_blocks",      # Being a creature → Blocks
    "target_chosen",        # Target abilities → BecomesTarget
    "creature_tapped",      # Tap → Taps
    "creature_untapped",    # Untap → Untaps
    "creature_pumped",      # Pump → power/toughness change
    "mana_produced",        # Mana → enables costs
    "token_created",        # Token → TokenCreated
    "creature_sacrificed",  # Sacrifice → Sacrificed
    "counter_removed",      # RemoveCounter → CounterRemoved
    "phase_trigger",        # Phase-based abilities
    "creature_available",   # Creature exists → can be tapped/sacrificed/counted
    "artifact_available",   # Artifact exists → can be sacrificed/tapped
    # Zone-aware concepts (from trigger_origin/destination data)
    "enters_from_graveyard",  # ChangesZone from Graveyard → Battlefield (reanimation)
    "enters_from_exile",      # ChangesZone from Exile → Battlefield (blink return)
    "enters_from_hand",       # ChangesZone from Hand → Battlefield (cheat into play)
    "goes_to_graveyard",      # ChangesZone to Graveyard (death/discard/mill)
    "goes_to_exile",          # ChangesZone to Exile (exile removal/blink)
    # Theme-specific concepts
    "equipment_enters",       # Equipment ETB / being cast
    "equipment_equipped",     # Equip action / attachment
    "defender_available",     # Defender/Wall on battlefield
    "etb_doubled",            # ETB trigger doubled (Panharmonicon-class)
]

# Effect verb → game concepts it PRODUCES
VERB_TO_CONCEPTS = {
    "Token":         ["creature_enters", "permanent_enters", "token_created", "creature_available"],
    "ChangeZone":    ["creature_enters", "permanent_enters", "creature_available"],
    "ChangeZoneAll": ["creature_enters", "permanent_enters"],
    "DealDamage":    ["damage_dealt", "life_lost"],
    "DamageAll":     ["damage_dealt", "life_lost"],
    "Draw":          ["card_drawn"],
    "Dig":           ["card_drawn"],
    "PutCounter":    ["counter_added"],
    "GainLife":      ["life_gained"],
    "LoseLife":      ["life_lost"],
    "Destroy":       ["permanent_destroyed", "creature_dies"],
    "DestroyAll":    ["permanent_destroyed", "creature_dies"],
    "Sacrifice":     ["creature_dies", "creature_sacrificed"],
    "Discard":       ["card_discarded"],
    "Mill":          ["card_milled"],
    "Tap":           ["creature_tapped"],
    "Untap":         ["creature_untapped"],
    "Pump":          ["creature_pumped"],
    "PumpAll":       ["creature_pumped"],
    "Mana":          ["mana_produced"],
    "Counter":       [],  # countering spells doesn't produce synergy events
    "Animate":       ["creature_enters"],
    "Attach":        ["enchantment_enters", "equipment_equipped"],
    "Equip":         ["equipment_equipped"],
    "CopyPermanent": ["permanent_enters"],
    "Fight":         ["damage_dealt"],
    "Proliferate":   ["counter_added"],
}

# Trigger mode → game concepts it CONSUMES
TRIGGER_TO_CONCEPTS = {
    "ChangesZone":     ["creature_enters", "artifact_enters", "enchantment_enters", "permanent_enters", "creature_dies"],
    "ChangesZoneAll":  ["creature_enters", "permanent_enters"],
    "DamageDone":      ["damage_dealt"],
    "DamageDoneOnce":  ["damage_dealt"],
    "DamageAll":       ["damage_dealt"],
    "Drawn":           ["card_drawn"],
    "CounterAdded":    ["counter_added"],
    "CounterAddedOnce":["counter_added"],
    "LifeGained":      ["life_gained"],
    "LifeLost":        ["life_lost"],
    "Destroyed":       ["permanent_destroyed"],
    "Sacrificed":      ["creature_sacrificed", "creature_dies"],
    "Discarded":       ["card_discarded"],
    "Milled":          ["card_milled"],
    "SpellCast":       ["spell_cast"],
    "Attacks":         ["creature_attacks"],
    "AttackersDeclared": ["creature_attacks"],
    "Blocks":          ["creature_blocks"],
    "BecomesTarget":   ["target_chosen"],
    "Taps":            ["creature_tapped"],
    "Untaps":          ["creature_untapped"],
    "TokenCreated":    ["token_created"],
    "CounterRemoved":  ["counter_removed"],
    "Phase":           ["phase_trigger"],
    "TurnFaceUp":      [],
}

N_CONCEPTS = len(GAME_CONCEPTS)
_concept_idx = {c: i for i, c in enumerate(GAME_CONCEPTS)}

# Verbs that move permanents between zones (used for zone-aware concept population)
_ZONE_VERBS = {"ChangeZone", "ChangeZoneAll", "Sacrifice", "Destroy",
               "DestroyAll", "Mill", "Discard"}

def _is_opponent_only(defined: str | None, raw_line: str) -> bool:
    """Check if an ability targets only opponents (not self/any player).

    Uses two signals:
    - defined field: "Player.Opponent", "Opponent" → explicit opponent targeting
    - defined + ValidPlayer$: "TriggeredPlayer"/"TriggeredTarget" with
      ValidPlayer$Opponent means the triggered player IS the opponent
    - ValidPlayer$ alone (for R: abilities with no defined field)
    """
    d = (defined or "").lower()
    # Explicit opponent in defined field (e.g., Manic Scribe: defined=Player.Opponent)
    if "opponent" in d and "you" not in d:
        return True
    # Triggered player/target with ValidPlayer$Opponent context
    # (e.g., Mindcrank: defined=TriggeredPlayer, ValidPlayer$Opponent)
    if d in ("triggeredplayer", "triggeredtarget"):
        if ("ValidPlayer$ Player.Opponent" in raw_line
                or "ValidPlayer$ Opponent" in raw_line):
            return True
    # R: abilities without defined: check ValidPlayer$ directly
    if not defined and raw_line.startswith("R:"):
        if ("ValidPlayer$ Player.Opponent" in raw_line
                or "ValidPlayer$ Opponent" in raw_line):
            return True
    # Affected$ scope: effects targeting only opponent's permanents
    if "Affected$" in raw_line:
        m = _RE_AFFECTED.search(raw_line)
        if m:
            val = m.group(1).lower()
            if ".oppctrl" in val and ".youctrl" not in val:
                return True
    return False


def _normalize_addtrigger(name):
    """Normalize AddTrigger$ SVar names to TRIGGER_TO_CONCEPTS keys."""
    # Strip common prefixes: Trigger, Trig
    n = _RE_TRIGGER_PREFIX.sub('', name)
    return _ADDTRIGGER_MAP.get(n, n if n in TRIGGER_TO_CONCEPTS else None)


# R: replacement Event$ types → equivalent verb for VERB_TO_CONCEPTS lookup
# E.g., Event$DamageDone → treat like verb "DealDamage" for concept production
_REPLACEMENT_EVENT_TO_VERB = {
    "Mill":        "Mill",
    "DamageDone":  "DealDamage",
    "GainLife":    "GainLife",
    "Draw":        "Draw",
    "LoseLife":    "LoseLife",
}


def _collect_subtypes(abilities):
    """Count subtypes from trigger_filter and token_script, return top 80 with indices."""
    subtype_counts = Counter()
    card_types_set = {"card", "creature", "artifact", "enchantment",
                      "instant", "sorcery", "permanent", "land",
                      "planeswalker", "spell", "tribal", "battle"}

    kw_skip = {"flying", "haste", "trample", "vigilance", "deathtouch",
               "lifelink", "menace", "reach", "defender", "first",
               "strike", "double", "indestructible", "hexproof",
               "sac", "unblockable", "shroud", "wither", "persist"}

    for ab in abilities:
        trig_filter, token_script = ab[3], ab[6]
        if trig_filter:
            for part in trig_filter.split(","):
                main = part.split(".")[0].strip()
                if main and main[0].isupper() and main.lower() not in card_types_set:
                    subtype_counts[main.lower()] += 1
        if token_script:
            parts = token_script.lower().split("_")
            if len(parts) >= 4:
                for p in parts[3:]:
                    if p and p not in kw_skip and len(p) > 1:
                        subtype_counts[p] += 1

    top_subtypes = [st for st, _ in subtype_counts.most_common(80)]
    subtype_idx = {st: N_CONCEPTS + i for i, st in enumerate(top_subtypes)}
    dim = N_CONCEPTS + len(top_subtypes)
    return top_subtypes, subtype_idx, dim


def _add_type_based_produces(type_lines, produces, consumes, dim):
    """Add type-based produces: non-land cards produce spell_cast, creatures produce attacks.

    Args:
        type_lines: dict[oracle_id → type_line] (from CardProvider or conn).
    """
    for oid in set(produces.keys()) | set(consumes.keys()):
        tl = type_lines.get(oid, "")
        if not tl:
            continue
        # Non-land cards produce spell_cast (they are spells when cast)
        if "Land" not in tl:
            if oid not in produces:
                produces[oid] = np.zeros(dim, dtype=np.float32)
            produces[oid][_concept_idx["spell_cast"]] += 1.0
        # Creatures produce creature_attacks and creature_available
        if "Creature" in tl:
            if oid not in produces:
                produces[oid] = np.zeros(dim, dtype=np.float32)
            produces[oid][_concept_idx["creature_attacks"]] += 1.0
            produces[oid][_concept_idx["creature_available"]] += 0.5


def build_mechanics_vectors(conn, preloaded_abilities=None, type_lines=None):
    """Build dense mechanics vectors for all cards with Forge data.

    Args:
        conn: SQLite connection
        type_lines: optional dict[oracle_id → type_line] from CardProvider.
            When provided, avoids querying the cards table.
        preloaded_abilities: optional list of (oracle_id, verb, trigger_mode,
            trigger_filter, cost, keyword, token_script, counter_type, raw_line,
            amount, trigger_origin, trigger_destination[, defined]) tuples.
            When provided, skips the forge_abilities DB scan.
            The optional `defined` field (index 12) carries player targeting
            (e.g., "You", "Player.Opponent", "TriggeredPlayer").

    Returns:
        produces: dict[oracle_id → numpy float32 vector]
        consumes: dict[oracle_id → numpy float32 vector]
        dim: total vector dimension
        subtype_idx: dict[subtype → dimension index]
    """
    if preloaded_abilities is not None:
        # Use pre-loaded data — no DB scan needed
        abilities = preloaded_abilities
    else:
        # Fall back to DB scan (for standalone use)
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


    top_subtypes, subtype_idx, dim = _collect_subtypes(abilities)

    produces = {}  # oid → vector
    consumes = {}  # oid → vector

    for ab in abilities:
        oid = ab[0]
        verb, trig_mode, trig_filter = ab[1], ab[2], ab[3]
        token_script = ab[6]
        raw_line = ab[8] or ""
        trig_origin = ab[10] if len(ab) > 10 else None
        trig_dest = ab[11] if len(ab) > 11 else None
        defined = ab[12] if len(ab) > 12 else None

        # Parse Origin$ and Destination$ once per row for reuse below
        _parsed_dest = None
        _parsed_orig = None
        if raw_line:
            m_dest = _RE_DESTINATION.search(raw_line)
            if m_dest:
                _parsed_dest = m_dest.group(1)
            m_orig = _RE_ORIGIN.search(raw_line)
            if m_orig:
                _parsed_orig = m_orig.group(1)

        # --- PRODUCES: effect verb → game concepts ---
        # For R: replacement effects, parse Event$ from raw_line and map
        # to equivalent verb. R: abilities have verb=NULL in forge_abilities
        # to avoid polluting forge_profiles with false verb alignment signals.
        is_replacement = raw_line.startswith("R:")
        effective_verb = verb
        if is_replacement:
            # Parse Event$ from raw_line (verb column is NULL for R: abilities)
            m = _RE_EVENT.search(raw_line)
            if m:
                event_type = m.group(1)
                effective_verb = _REPLACEMENT_EVENT_TO_VERB.get(event_type)
                # Skip if prevention/restriction (not amplification)
                if ("Prevent$ True" in raw_line
                        or "PreventionEffect$ True" in raw_line
                        or "Layer$ CantHappen" in raw_line):
                    effective_verb = None
            else:
                effective_verb = None

        # Skip opponent-only effects: cards that only affect opponents don't
        # produce concepts that self-targeting commanders can consume.
        # Checked for ALL ability types (R:, T:, A:) using both defined field
        # and ValidPlayer$ from raw_line.
        # E.g., Bruvac (R: Mill opponent), Manic Scribe (T: Mill opponent),
        # Mindcrank (T: opponent loses life → mill opponent)
        if effective_verb and effective_verb in VERB_TO_CONCEPTS:
            opponent_only = _is_opponent_only(defined, raw_line)
            if not opponent_only:
                if oid not in produces:
                    produces[oid] = np.zeros(dim, dtype=np.float32)
                p = produces[oid]
                for concept in VERB_TO_CONCEPTS[effective_verb]:
                    p[_concept_idx[concept]] += 1.0

        # Theme-specific PRODUCES
        keyword = ab[5]
        if verb == 'Panharmonicon':
            if oid not in produces:
                produces[oid] = np.zeros(dim, dtype=np.float32)
            produces[oid][_concept_idx["etb_doubled"]] += 1.0
        if verb == 'CanAttackDefender':
            # Defender-matters cards consume defenders
            if oid not in consumes:
                consumes[oid] = np.zeros(dim, dtype=np.float32)
            consumes[oid][_concept_idx["defender_available"]] += 1.0
        if keyword == 'Equip':
            if oid not in produces:
                produces[oid] = np.zeros(dim, dtype=np.float32)
            produces[oid][_concept_idx["equipment_enters"]] += 1.0
            produces[oid][_concept_idx["equipment_equipped"]] += 1.0
        if keyword == 'Defender':
            if oid not in produces:
                produces[oid] = np.zeros(dim, dtype=np.float32)
            produces[oid][_concept_idx["defender_available"]] += 1.0

        # AddTrigger$ — granting triggers produces those trigger events (weaker)
        if raw_line and 'AddTrigger$' in raw_line:
            at_m = _RE_ADDTRIGGER.search(raw_line)
            if at_m:
                trig_name = _normalize_addtrigger(at_m.group(1))
                if trig_name and trig_name in TRIGGER_TO_CONCEPTS:
                    if oid not in produces:
                        produces[oid] = np.zeros(dim, dtype=np.float32)
                    p = produces[oid]
                    for concept in TRIGGER_TO_CONCEPTS[trig_name]:
                        p[_concept_idx[concept]] += 0.5

        # Zone-aware PRODUCES: verb + destination → zone concept
        # Use trigger_destination column if available, fall back to raw_line
        dest_str = trig_dest or ""
        if not dest_str and verb and verb in _ZONE_VERBS and _parsed_dest:
            dest_str = _parsed_dest
        if verb and verb in _ZONE_VERBS and dest_str:
            if oid not in produces:
                produces[oid] = np.zeros(dim, dtype=np.float32)
            p = produces[oid]
            if "Graveyard" in dest_str:
                p[_concept_idx["goes_to_graveyard"]] += 1.0
            if "Exile" in dest_str:
                p[_concept_idx["goes_to_exile"]] += 1.0

        # Token with subtype → produces that subtype
        if token_script:
            parts = token_script.lower().split("_")
            if len(parts) >= 4:
                if oid not in produces:
                    produces[oid] = np.zeros(dim, dtype=np.float32)
                p = produces[oid]
                for part in parts[3:]:
                    if part in subtype_idx:
                        p[subtype_idx[part]] += 1.0

        # --- CONSUMES: trigger mode → game concepts ---
        if trig_mode and trig_mode in TRIGGER_TO_CONCEPTS:
            if oid not in consumes:
                consumes[oid] = np.zeros(dim, dtype=np.float32)
            c = consumes[oid]
            for concept in TRIGGER_TO_CONCEPTS[trig_mode]:
                c[_concept_idx[concept]] += 1.0

        # Equipment-related CONSUMES: triggers on Equipment entering/being attached
        if trig_filter and 'Equipment' in trig_filter:
            if oid not in consumes:
                consumes[oid] = np.zeros(dim, dtype=np.float32)
            consumes[oid][_concept_idx["equipment_enters"]] += 1.0
        # ETB-doubled CONSUMES: ETB triggers benefit from Panharmonicon
        etb_dest = trig_dest or ""
        if not etb_dest and trig_mode in ('ChangesZone', 'ChangesZoneAll') and _parsed_dest:
            etb_dest = _parsed_dest
        if trig_mode in ('ChangesZone', 'ChangesZoneAll') and 'Battlefield' in etb_dest:
            if oid not in consumes:
                consumes[oid] = np.zeros(dim, dtype=np.float32)
            consumes[oid][_concept_idx["etb_doubled"]] += 1.0

        # Trigger filter subtypes → consumes those subtypes
        if trig_filter:
            if oid not in consumes:
                consumes[oid] = np.zeros(dim, dtype=np.float32)
            c = consumes[oid]
            for part in trig_filter.split(","):
                main = part.split(".")[0].strip().lower()
                if main in subtype_idx:
                    c[subtype_idx[main]] += 1.0

        # Zone-aware CONSUMES: trigger + origin → zone concept
        # Use trigger_origin column if available, fall back to raw_line
        orig_str = trig_origin or ""
        if not orig_str and trig_mode and _parsed_orig:
            orig_str = _parsed_orig
        if trig_mode and orig_str:
            if oid not in consumes:
                consumes[oid] = np.zeros(dim, dtype=np.float32)
            c = consumes[oid]
            if "Graveyard" in orig_str:
                c[_concept_idx["enters_from_graveyard"]] += 1.0
            if "Exile" in orig_str:
                c[_concept_idx["enters_from_exile"]] += 1.0
            if "Hand" in orig_str:
                c[_concept_idx["enters_from_hand"]] += 1.0
        # Trigger with Destination=Graveyard → consumes goes_to_graveyard (death triggers)
        trig_dest_str = trig_dest or ""
        if not trig_dest_str and trig_mode and _parsed_dest:
            trig_dest_str = _parsed_dest
        if trig_mode and "Graveyard" in trig_dest_str:
            if oid not in consumes:
                consumes[oid] = np.zeros(dim, dtype=np.float32)
            consumes[oid][_concept_idx["goes_to_graveyard"]] += 1.0

        # ChangeZone with origin → produces zone-specific entry (reanimate, blink return)
        # Use column values first, fall back to raw_line
        verb_orig = trig_origin or ""
        verb_dest = trig_dest or ""
        if verb in ("ChangeZone", "ChangeZoneAll"):
            if not verb_orig and _parsed_orig:
                verb_orig = _parsed_orig
            if not verb_dest and _parsed_dest:
                verb_dest = _parsed_dest
        if verb in ("ChangeZone", "ChangeZoneAll") and verb_dest and verb_orig:
            if "Battlefield" in verb_dest:
                if oid not in produces:
                    produces[oid] = np.zeros(dim, dtype=np.float32)
                p = produces[oid]
                if "Graveyard" in verb_orig:
                    p[_concept_idx["enters_from_graveyard"]] += 1.0
                if "Exile" in verb_orig:
                    p[_concept_idx["enters_from_exile"]] += 1.0
                if "Hand" in verb_orig:
                    p[_concept_idx["enters_from_hand"]] += 1.0

        # Cost → consumes resources (check raw_line always, cost field often empty)
        if raw_line:
            sac_match = _RE_SAC_COST.findall(raw_line)
            for m in sac_match:
                sac_type = m.split("/")[0].split(".")[0].lower()
                if oid not in consumes:
                    consumes[oid] = np.zeros(dim, dtype=np.float32)
                c = consumes[oid]
                if sac_type == "creature":
                    c[_concept_idx["creature_sacrificed"]] += 1.0
                    c[_concept_idx["creature_available"]] += 1.0
                elif sac_type == "artifact":
                    c[_concept_idx["artifact_available"]] += 1.0
                elif sac_type != "cardname":
                    c[_concept_idx["creature_sacrificed"]] += 0.5
                    c[_concept_idx["creature_available"]] += 0.5
                    if sac_type in subtype_idx:
                        c[subtype_idx[sac_type]] += 1.0

    # Parse "for each [Type]" and "Tap" patterns from raw_line.
    # Build iterator of (oid, raw_line, amount) from preloaded data or DB.
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
        # "for each [Type]" scaling — only when amount='X'
        if amount == "X" and "for each" in raw_line:
            idx = raw_line.find("for each")
            snippet = raw_line[idx:]
            for m in _RE_FOR_EACH.finditer(snippet):
                t = m.group(1)
                if t in subtype_idx:
                    if oid not in consumes:
                        consumes[oid] = np.zeros(dim, dtype=np.float32)
                    consumes[oid][subtype_idx[t]] += 1.0
                elif t == "creature":
                    if oid not in consumes:
                        consumes[oid] = np.zeros(dim, dtype=np.float32)
                    consumes[oid][_concept_idx["creature_available"]] += 1.0
                elif t == "artifact":
                    if oid not in consumes:
                        consumes[oid] = np.zeros(dim, dtype=np.float32)
                    consumes[oid][_concept_idx["artifact_enters"]] += 0.5

        # "Tap" cost patterns
        if "tap<" in raw_line:
            for m in _RE_TAP_COST.finditer(raw_line):
                t = m.group(1).split("/")[0].split(".")[0].lower()
                if t in subtype_idx:
                    if oid not in consumes:
                        consumes[oid] = np.zeros(dim, dtype=np.float32)
                    consumes[oid][subtype_idx[t]] += 1.0
                    consumes[oid][_concept_idx["creature_tapped"]] += 0.5
                elif t == "creature":
                    if oid not in consumes:
                        consumes[oid] = np.zeros(dim, dtype=np.float32)
                    consumes[oid][_concept_idx["creature_tapped"]] += 1.0
                    consumes[oid][_concept_idx["creature_available"]] += 0.5

    # Add type-based produces (uses type_lines from CardProvider if available)
    if type_lines is None:
        # Fallback: load from conn (for standalone use / training)
        type_lines = {}
        for row in conn.execute("SELECT oracle_id, type_line FROM cards"):
            type_lines[row[0]] = row[1] or ""
    _add_type_based_produces(type_lines, produces, consumes, dim)

    # L2 normalize all vectors
    for oid in produces:
        norm = np.linalg.norm(produces[oid])
        if norm > 0:
            produces[oid] /= norm
    for oid in consumes:
        norm = np.linalg.norm(consumes[oid])
        if norm > 0:
            consumes[oid] /= norm

    print(f"  Mechanics vectors: {len(produces)} producers, "
          f"{len(consumes)} consumers, dim={dim} "
          f"({N_CONCEPTS} concepts + {len(top_subtypes)} subtypes)")

    return produces, consumes, dim, subtype_idx
