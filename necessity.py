"""
necessity_score: how much does this card contribute to deck function,
independent of commander synergy?

Two components:
  1. staple_value   — universal EDH strength of this card in its role
  2. role_deficit   — how badly the deck needs cards in this slot right now
  3. redundancy_pen — penalty if this slot is already overfull
"""

from enricher import CardFeatures
from scorer import DeckProfile

# ── STAPLE TIERS ──────────────────────────────────────────────────────────────
# Cards are evaluated on their universal EDH strength, regardless of commander.
# These are hand-curated values — the core of what makes necessity different
# from synergy. This list will grow as we test more decks.

STAPLE_VALUES = {
    # Tier 1: irreplaceable (+4)
    "Sol Ring":              4,
    "Swords to Plowshares":  4,
    "Heroic Intervention":   4,
    "Teferi's Protection":   4,
    "Windswept Heath":       4,

    # Tier 2: best-in-slot for their role (+3)
    "Nature's Lore":         3,
    "Three Visits":          3,
    "Cultivate":             3,
    "Farseek":               3,
    "Boseiju, Who Endures":  3,
    "Cavern of Souls":       3,
    "Flawless Maneuver":     3,
    "Mother of Runes":       3,
    "Aura Shards":           3,

    # Tier 2.5: effectively always correct in this archetype
    "Command Tower":         3,
    "Path of Ancestry":      3,
    "Plaza of Heroes":       3,
    "Temple Garden":         3,

    # Tier 3: strong, minor alternatives exist (+2)
    "Rampant Growth":        2,
    "Swiftfoot Boots":       2,
    "Sylvan Safekeeper":     2,
    "Ranger-Captain of Eos": 2,
    "Recruiter of the Guard":2,
    "Everybody Lives!":      2,
    "Flare of Fortitude":    2,
    "Clever Concealment":    2,
    "Grand Abolisher":       2,
    "Avacyn's Pilgrim":      2,
    "Gavony Township":       2,

    # Tier 4: situationally correct (+1)
    "Damning Verdict":       1,
    "Loran of the Third Path":1,
    "Druid of Purification": 1,
    "Boromir, Warden of the Tower": 1,
    "Sigarda, Font of Blessings": 1,
    "Saryth, the Viper's Fang": 1,
}

# Role slot targets — how many of each role a healthy Kyler deck wants
ROLE_TARGETS = {
    "ramp":       10,
    "draw":       10,
    "removal":    8,
    "protection": 8,
    "land":       36,
}

# Roles where overcrowding matters
ROLE_SOFT_CAP = {
    "protection": 10,  # above this, each additional protection spell is less necessary
    "ramp":       12,
    "draw":       12,
    "removal":    10,
}


def necessity_score(card: CardFeatures, deck: list, profile: DeckProfile) -> tuple[float, list]:
    """
    Returns (score, explanation_lines).
    Score is independent of commander synergy.
    """
    explanation = []

    # ── 1. STAPLE VALUE ───────────────────────────────────────────────────────
    staple = STAPLE_VALUES.get(card.name, 0)
    if staple > 0:
        tier = {4: "T1", 3: "T2", 2: "T3", 1: "T4"}.get(staple, "T?")
        explanation.append(f"staple: +{staple} ({tier} — universal EDH strength)")
    else:
        explanation.append("staple: +0 (no universal staple value)")

    # ── 2. ROLE DEFICIT ───────────────────────────────────────────────────────
    role = card.role
    role_bonus = 0.0

    if role in ROLE_TARGETS:
        current = getattr(profile, f"{role}_count", 0)
        target = ROLE_TARGETS[role]
        deficit = max(0, target - current)

        if deficit >= 5:
            role_bonus = 3.0
            explanation.append(f"role_deficit: +3 ({role} severely short: {current}/{target})")
        elif deficit >= 3:
            role_bonus = 2.0
            explanation.append(f"role_deficit: +2 ({role} short: {current}/{target})")
        elif deficit >= 1:
            role_bonus = 1.0
            explanation.append(f"role_deficit: +1 ({role} mild gap: {current}/{target})")
        else:
            soft_cap = ROLE_SOFT_CAP.get(role, target + 4)
            surplus = current - soft_cap
            if surplus > 0:
                role_bonus = -1.0
                explanation.append(f"role_deficit: -1 ({role} overcrowded: {current} > cap {soft_cap})")
            else:
                explanation.append(f"role_deficit: +0 ({role} covered: {current}/{target})")
    else:
        explanation.append(f"role_deficit: +0 (role '{role}' — no slot target)")

    # ── 3. REDUNDANCY PENALTY ─────────────────────────────────────────────────
    # If multiple cards in deck do exactly the same thing, later ones are less necessary.
    # For now: simple duplicate-role penalty when slot is already at or over target.
    redundancy_pen = 0.0
    if role in ROLE_TARGETS:
        current = getattr(profile, f"{role}_count", 0)
        target = ROLE_TARGETS[role]
        if current > target + 2:
            redundancy_pen = 1.0
            explanation.append(f"redundancy: -{redundancy_pen:.0f} ({role} well over target)")

    # ── FINAL ─────────────────────────────────────────────────────────────────
    score = round(staple + role_bonus - redundancy_pen, 2)
    return score, explanation
