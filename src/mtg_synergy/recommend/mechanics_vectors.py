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
    "Attach":        ["enchantment_enters"],
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

# Cost types → game concepts consumed
COST_CONCEPTS = {
    "sac_creature":  "creature_sacrificed",
    "sac_artifact":  "artifact_enters",  # needs artifact to sacrifice
    "tap_creature":  "creature_tapped",
    "discard":       "card_discarded",
}


def build_mechanics_vectors(conn):
    """Build dense mechanics vectors for all cards with Forge data.

    Returns:
        produces: dict[oracle_id → numpy float32 vector]
        consumes: dict[oracle_id → numpy float32 vector]
        dim: total vector dimension
        subtype_idx: dict[subtype → dimension index]
    """
    # Discover subtypes from trigger_filter and token_script
    subtype_counts = Counter()
    card_types_set = {"card", "creature", "artifact", "enchantment",
                      "instant", "sorcery", "permanent", "land",
                      "planeswalker", "spell", "tribal", "battle"}

    for row in conn.execute(
        "SELECT trigger_filter FROM forge_abilities WHERE trigger_filter IS NOT NULL"
    ):
        for part in row[0].split(","):
            main = part.split(".")[0].strip()
            if main and main[0].isupper() and main.lower() not in card_types_set:
                subtype_counts[main.lower()] += 1

    for row in conn.execute(
        "SELECT token_script FROM forge_abilities WHERE token_script IS NOT NULL"
    ):
        parts = row[0].lower().split("_")
        kw_skip = {"flying", "haste", "trample", "vigilance", "deathtouch",
                   "lifelink", "menace", "reach", "defender", "first",
                   "strike", "double", "indestructible", "hexproof",
                   "sac", "unblockable", "shroud", "wither", "persist"}
        if len(parts) >= 4:
            for p in parts[3:]:
                if p and p not in kw_skip and len(p) > 1:
                    subtype_counts[p] += 1

    # Take top 80 subtypes (covers goblin, human, vampire, etc.)
    top_subtypes = [st for st, _ in subtype_counts.most_common(80)]
    subtype_idx = {st: N_CONCEPTS + i for i, st in enumerate(top_subtypes)}
    dim = N_CONCEPTS + len(top_subtypes)

    # Build name → oracle_id mapping
    forge_to_oid = {}
    for row in conn.execute("SELECT forge_name, oracle_id FROM forge_name_map"):
        forge_to_oid[row[0]] = row[1]

    produces = {}  # oid → vector
    consumes = {}  # oid → vector

    for row in conn.execute(
        "SELECT card_name, verb, trigger_mode, trigger_filter, cost, "
        "keyword, token_script, counter_type, raw_line "
        "FROM forge_abilities"
    ):
        card_name = row[0]
        oid = forge_to_oid.get(card_name)
        if not oid:
            continue

        verb, trig_mode, trig_filter = row[1], row[2], row[3]
        cost, token_script = row[4], row[6]
        raw_line = row[8] or ""

        # --- PRODUCES: effect verb → game concepts ---
        if verb and verb in VERB_TO_CONCEPTS:
            p = produces.setdefault(oid, np.zeros(dim, dtype=np.float32))
            for concept in VERB_TO_CONCEPTS[verb]:
                p[_concept_idx[concept]] += 1.0

        # Token with subtype → produces that subtype
        if token_script:
            parts = token_script.lower().split("_")
            kw_skip = {"flying", "haste", "trample", "vigilance", "deathtouch",
                       "lifelink", "menace", "reach", "defender", "first",
                       "strike", "double", "indestructible", "hexproof",
                       "sac", "unblockable", "shroud", "wither", "persist"}
            if len(parts) >= 4:
                p = produces.setdefault(oid, np.zeros(dim, dtype=np.float32))
                for part in parts[3:]:
                    if part in subtype_idx:
                        p[subtype_idx[part]] += 1.0

        # --- CONSUMES: trigger mode → game concepts ---
        if trig_mode and trig_mode in TRIGGER_TO_CONCEPTS:
            c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
            for concept in TRIGGER_TO_CONCEPTS[trig_mode]:
                c[_concept_idx[concept]] += 1.0

        # Trigger filter subtypes → consumes those subtypes
        if trig_filter:
            c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
            for part in trig_filter.split(","):
                main = part.split(".")[0].strip().lower()
                if main in subtype_idx:
                    c[subtype_idx[main]] += 1.0

        # Cost → consumes resources (check raw_line always, cost field often empty)
        if raw_line:
            sac_match = re.findall(r"Sac<\d+/([^/>]+)", raw_line, re.IGNORECASE)
            for m in sac_match:
                sac_type = m.split("/")[0].split(".")[0].lower()
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
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

    # Parse "for each [Type]" scaling from Forge raw_line SpellDescription
    # (replaces oracle text fallback). Amount=X abilities with "for each" in
    # their SpellDescription encode entity-count scaling that Forge doesn't
    # capture in structured fields.
    for row in conn.execute(
        "SELECT fnm.oracle_id, fa.raw_line FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fa.amount = 'X' AND fa.raw_line LIKE '%for each%'"
    ):
        oid, raw = row[0], (row[1] or "").lower()
        idx = raw.find("for each")
        if idx < 0:
            continue
        snippet = raw[idx:]
        for m in re.finditer(r"for each (\w+)", snippet):
            t = m.group(1)
            if t in subtype_idx:
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[subtype_idx[t]] += 1.0
            elif t == "creature":
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[_concept_idx["creature_available"]] += 1.0
            elif t == "artifact":
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[_concept_idx["artifact_enters"]] += 0.5

    # Parse "tap an untapped [Type]" from Forge raw_line cost patterns
    for row in conn.execute(
        "SELECT fnm.oracle_id, fa.raw_line FROM forge_abilities fa "
        "JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name "
        "WHERE fa.raw_line LIKE '%Tap<%'"
    ):
        oid, raw = row[0], (row[1] or "").lower()
        for m in re.finditer(r"tap<\d+/([^/>]+)", raw):
            t = m.group(1).split("/")[0].split(".")[0].lower()
            if t in subtype_idx:
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[subtype_idx[t]] += 1.0
                c[_concept_idx["creature_tapped"]] += 0.5
            elif t == "creature":
                c = consumes.setdefault(oid, np.zeros(dim, dtype=np.float32))
                c[_concept_idx["creature_tapped"]] += 1.0
                c[_concept_idx["creature_available"]] += 0.5

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
