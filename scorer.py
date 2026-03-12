from enricher import CardFeatures
from dataclasses import dataclass


@dataclass
class DeckProfile:
    total_cards: int = 0
    human_count: int = 0
    ramp_count: int = 0
    draw_count: int = 0
    removal_count: int = 0
    protection_count: int = 0
    counter_engine_count: int = 0

    RAMP_TARGET = 10
    DRAW_TARGET = 10
    REMOVAL_TARGET = 8
    PROTECTION_TARGET = 8


SYNERGY_PAIRS = [
    ("Hardened Scales", "Ozolith, the Shattered Spire", "both double counter placement"),
    ("Hardened Scales", "The Ozolith", "both double counter placement"),
    ("Hardened Scales", "Pir, Imaginative Rascal", "stacks: Pir+1 then Scales+1 = +2 per trigger"),
    ("Hardened Scales", "Luminarch Aspirant", "each trigger gets extra counter"),
    ("Pir, Imaginative Rascal", "The Ozolith", "Ozolith preserves counters Pir amplified"),
    ("Pir, Imaginative Rascal", "Ozolith, the Shattered Spire", "Ozolith preserves counters Pir amplified"),
    ("Pir, Imaginative Rascal", "Luminarch Aspirant", "Aspirant triggers stack with Pir"),
    ("Kyler, Sigardian Emissary", "Thalia's Lieutenant", "both care about Humans entering"),
    ("Kyler, Sigardian Emissary", "Champion of Lambholt", "counters make Champion unblockable"),
    ("The Ozolith", "Saffi Eriksdotter", "Saffi dies → Ozolith saves counters"),
    ("The Ozolith", "Slippery Bogbonder", "Bogbonder moves counters, Ozolith catches them"),
    ("Gavony Township", "Champion of Lambholt", "Township pumps all, Champion becomes unblockable"),
    ("Roaming Throne", "Kyler, Sigardian Emissary", "Throne doubles Kyler's triggered ability"),
    ("Torens, Fist of the Angels", "Kyler, Sigardian Emissary", "Torens makes Humans, Kyler pumps them"),
    ("Adeline, Resplendent Cathar", "Kyler, Sigardian Emissary", "Adeline floods board with Humans"),
    ("Abzan Falconer", "Champion of Lambholt", "both make counter-matters creatures relevant in combat"),
    ("Surrak and Goreclaw", "Wild Beastmaster", "Surrak gives trample, Beastmaster pumps on attack"),
    ("Katilda, Dawnhart Prime", "Kyler, Sigardian Emissary", "Katilda taps Humans for mana"),
    ("Heronblade Elite", "Kyler, Sigardian Emissary", "Elite grows from Human count like Kyler"),
    ("Tribute to the World Tree", "Kyler, Sigardian Emissary", "draws cards when pumped Humans ETB"),
    ("The Great Henge", "Kyler, Sigardian Emissary", "Henge draws when pumped Humans ETB"),
    ("Aura Shards", "Kyler, Sigardian Emissary", "each Human entering destroys artifact/enchantment"),
    ("Roaming Throne", "Thalia's Lieutenant", "Throne doubles Thalia's trigger on each Human ETB"),
    ("Roaming Throne", "Adeline, Resplendent Cathar", "Throne doubles Adeline's attack trigger"),
    ("Roaming Throne", "Torens, Fist of the Angels", "Throne doubles Torens's cast trigger"),
]


def build_deck_profile(deck: list) -> DeckProfile:
    profile = DeckProfile(total_cards=len(deck))
    for card in deck:
        if card.is_human:
            profile.human_count += 1
        if card.role == "ramp":
            profile.ramp_count += 1
        elif card.role == "draw":
            profile.draw_count += 1
        elif card.role == "removal":
            profile.removal_count += 1
        elif card.role == "protection":
            profile.protection_count += 1
        if card.places_counters_on_others or card.doubles_counters or card.interacts_with_counters:
            profile.counter_engine_count += 1
    return profile


def score_card(card: CardFeatures, deck: list, profile: DeckProfile) -> CardFeatures:
    explanation = []
    deck_names = {c.name for c in deck}

    # ── A) TRIBAL ────────────────────────────────────────────────────────────
    if card.is_human:
        tribal = 3.0
        explanation.append("tribal: +3 (is Human)")
    elif card.can_become_human:
        tribal = 2.5
        explanation.append("tribal: +2.5 (can become Human by choice — triggers Kyler on ETB)")
    elif card.mentions_human:
        tribal = 2.0
        explanation.append("tribal: +2 (mentions Human)")
    else:
        tribal = 0.0
        explanation.append("tribal: +0 (no Human synergy)")
    card.tribal_score = tribal

    # ── B) COUNTER SCORE ─────────────────────────────────────────────────────
    counter = 0.0
    if card.doubles_counters:
        if card.doubles_counters_scope == "board":
            base_val = 3.0
            explanation.append("counter: +3 (board-wide counter doubling)")
        else:
            base_val = 2.0
            explanation.append("counter: +2 (single-target counter doubling)")
        # Fix 4: permanent doublers are worth more than one-shot spells
        if card.is_permanent:
            counter += base_val + 1.5
            explanation.append("counter: +1.5 (permanent — stays on battlefield)")
        else:
            counter += base_val
    # Fix 5: trigger-doublers amplify commander's counter distribution → indirect counter engine
    if card.doubles_triggered_abilities:
        counter += 3.0
        explanation.append("counter: +3 (doubles triggered abilities — amplifies Kyler's counter distribution)")

    if card.places_counters_on_others:
        counter += 3.0
        explanation.append("counter: +3 (places counters on other creatures)")
    if card.interacts_with_counters and not card.doubles_counters:
        counter += 2.0
        explanation.append("counter: +2 (interacts with counters)")
    if card.places_counter_on_self and not card.places_counters_on_others:
        counter += 1.0
        explanation.append("counter: +1 (places counter on self only)")
    if counter == 0:
        explanation.append("counter: +0 (no counter interaction)")
    card.counter_score = counter

    # ── C) ROLE SCORE ────────────────────────────────────────────────────────
    role_score = compute_role_score(card, profile, explanation)
    card.role_score = role_score

    # ── D) COMMANDER MULTIPLIER ───────────────────────────────────────────────
    mult = compute_commander_multiplier(card, explanation)
    card.commander_multiplier = mult

    # ── E) SYNERGY BONUS ─────────────────────────────────────────────────────
    syn_bonus = 0.0
    syn_reasons = []
    for name_a, name_b, reason in SYNERGY_PAIRS:
        if card.name == name_a and name_b in deck_names:
            syn_bonus += 1.0
            syn_reasons.append(f"↔ {name_b}")
        elif card.name == name_b and name_a in deck_names:
            syn_bonus += 1.0
            syn_reasons.append(f"↔ {name_a}")
    syn_bonus = min(syn_bonus, 3.0)
    if syn_reasons:
        explanation.append(f"synergy: +{syn_bonus:.0f} ({', '.join(syn_reasons[:3])})")
    else:
        explanation.append("synergy: +0")
    card.synergy_bonus = syn_bonus

    # ── F) CMC PENALTY (FIX 3) ───────────────────────────────────────────────
    # Counter doublers / enablers at high CMC are less efficient
    cmc_penalty = 0.0
    if card.role in ("enabler", "utility") and card.cmc > 2:
        cmc_penalty = round((card.cmc - 2) * 0.4, 1)
        explanation.append(f"cmc_penalty: -{cmc_penalty} (enabler at CMC {card.cmc:.0f})")
    card.cmc_penalty = cmc_penalty

    # ── FINAL ─────────────────────────────────────────────────────────────────
    base = tribal + counter + role_score
    card.final_score = round(base * mult + syn_bonus - cmc_penalty, 2)
    card.explanation = explanation
    return card


def compute_role_score(card, profile, explanation):
    role = card.role
    p = profile
    deficits = {
        "ramp":       max(0, p.RAMP_TARGET - p.ramp_count),
        "draw":       max(0, p.DRAW_TARGET - p.draw_count),
        "removal":    max(0, p.REMOVAL_TARGET - p.removal_count),
        "protection": max(0, p.PROTECTION_TARGET - p.protection_count),
    }
    if role == "land":
        explanation.append("role: +1 (land)")
        return 1.0
    if role in deficits:
        deficit = deficits[role]
        if deficit > 3:
            explanation.append(f"role: +2 ({role}, deficit: {deficit})")
            return 2.0
        elif deficit > 0:
            explanation.append(f"role: +1 ({role}, mild deficit)")
            return 1.0
        else:
            explanation.append(f"role: +0 ({role}, slot covered)")
            return 0.0
    if role == "enabler":
        explanation.append("role: +1 (enabler)")
        return 1.0
    if role == "threat":
        explanation.append("role: +1 (threat)")
        return 1.0
    explanation.append("role: +0 (utility)")
    return 0.0


def compute_commander_multiplier(card, explanation):
    oracle = card.oracle_text.lower()
    name = card.name

    direct_kyler = {
        "Adeline, Resplendent Cathar", "Torens, Fist of the Angels",
        "Thalia's Lieutenant", "Champion of Lambholt",
        "Heronblade Elite",
    }

    # Roaming Throne is special: it doubles triggered abilities, and chosen as Human
    # means Kyler's trigger fires twice → every Human entering gives 2× the counters
    if name == "Roaming Throne":
        explanation.append("commander_mult: ×2.0 (doubles Kyler's triggered ability — uniquely powerful)")
        return 2.0
    if name in direct_kyler:
        explanation.append("commander_mult: ×1.5 (directly amplifies Kyler's trigger)")
        return 1.5

    # Board-wide counter doublers amplify Kyler's board-wide distribution
    if card.doubles_counters and card.doubles_counters_scope == "board":
        explanation.append("commander_mult: ×1.5 (board-wide doubling amplifies Kyler)")
        return 1.5

    # Single-target doublers are useful but don't amplify Kyler's AOE directly
    if card.doubles_counters and card.doubles_counters_scope == "single":
        explanation.append("commander_mult: ×1.2 (single-target doubling, partial Kyler synergy)")
        return 1.2

    indirect_kyler = {"Garruk's Uprising", "Tribute to the World Tree", "The Great Henge", "Aura Shards"}
    if name in indirect_kyler:
        explanation.append("commander_mult: ×1.2 (benefits from Kyler's counters)")
        return 1.2

    explanation.append("commander_mult: ×1.0")
    return 1.0
