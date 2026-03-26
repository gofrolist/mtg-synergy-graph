#!/usr/bin/env python3
"""Derive provides tags from Forge DSL abilities.

Task 1: Core derivation engine — verb→provides mapping.

Reads forge_abilities joined with forge_name_map to get oracle_ids,
applies verb/keyword mappings, and writes derived tags to the provides table.

Usage:
    python3 derive_forge_tags.py --dry-run   # Print tags without writing to DB
    python3 derive_forge_tags.py --write     # Write tags to provides table
    python3 derive_forge_tags.py --stats     # Show derivation statistics
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from typing import Optional

# ---------------------------------------------------------------------------
# Verb → provides tag mapping
# ---------------------------------------------------------------------------

VERB_TO_PROVIDES: dict[str, str] = {
    "Token": "token",
    "Draw": "draw",
    "Mana": "mana",
    "ManaReflected": "mana",
    "DealDamage": "deal-damage",
    "DamageAll": "deal-damage-all",
    "Destroy": "destroy",
    "DestroyAll": "destroy-all",
    "PutCounter": "put-counter",
    "PutCounterAll": "put-counter-all",
    "RemoveCounter": "remove-counter",
    "MultiplyCounter": "multiply-counter",
    "GainLife": "gain-life",
    "LoseLife": "lose-life",
    "Pump": "pump",
    "PumpAll": "pump-all",
    "Debuff": "debuff",
    "Mill": "mill",
    "Scry": "scry",
    "Surveil": "surveil",
    "Dig": "dig",
    "DigUntil": "dig",
    "DigMultiple": "dig",
    "Discard": "discard",
    "Sacrifice": "force-sacrifice",
    "SacrificeAll": "force-sacrifice-all",
    "Counter": "counter-spell",
    "GainControl": "gain-control",
    "CopyPermanent": "copy-permanent",
    "Clone": "copy-permanent",
    "CopySpellAbility": "copy-spell",
    "ReduceCost": "reduce-cost",
    "RaiseCost": "raise-cost",
    "Animate": "animate",
    "AnimateAll": "animate",
    "ChangeZoneAll": "change-zone-all",
    "Untap": "untap",
    "UntapAll": "untap-all",
    "Tap": "tap-target",
    "TapAll": "tap-all",
    "Fight": "fight",
    "Proliferate": "proliferate",
    "Explore": "explore",
    "Connive": "connive",
    "Investigate": "investigate",
    "Goad": "goad",
    "MustAttack": "goad",
    "Amass": "amass",
    "Fog": "fog",
    "PreventDamage": "prevent-damage",
    "Regenerate": "regenerate",
    "CantBlockBy": "evasion-grant",
    "CantBlock": "restrict-block",
    "CantAttack": "restrict-attack",
    "CantAttackUnless": "restrict-attack",
    "CantAttack,CantBlock": "restrict-combat",
    "CantAttack,CantBlock,CantBeActivated": "full-lockdown",
    "Play": "free-cast",
    "Seek": "tutor",
    "AddTurn": "extra-turn",
    "Attach": "attach",
    "CastWithFlash": "flash-grant",
    "Protection": "protection-grant",
    "SetState": "set-state",
    "PeekAndReveal": "peek",
    "SetLife": "set-life",
    "MoveCounter": "move-counter",
    "AddOrRemoveCounter": "add-or-remove-counter",
    "ManifestDread": "manifest",
    "Manifest": "manifest",
    "Incubate": "incubate",
    "Discover": "discover",
    "Vote": "vote",
    "Poison": "poison",
    "Detain": "detain",
    "ExchangeControl": "exchange-control",
    "MustBlock": "force-block",
    "RingTemptsYou": "ring-tempts",
    "TakeInitiative": "take-initiative",
    "Clash": "clash",
    "RearrangeTopOfLibrary": "rearrange-top",
    "Phases": "phase-out",
    "Endure": "endure",
    "WinsGame": "wins-game",
    "LosesGame": "loses-game",
    "CantTarget": "cant-target",
    "CantGainLife": "cant-gain-life",
    "CantBeCast": "cant-cast",
    "CantPlayLand": "cant-play-land",
    "CantPreventDamage": "cant-prevent-damage",
    "CantSacrifice": "cant-sacrifice",
    "CombatDamageToughness": "damage-toughness",
    "AssignCombatDamageAsUnblocked": "damage-unblocked",
    "SkipPhase": "skip-phase",
    "AddPhase": "add-phase",
    "FlipACoin": "random-outcome",
    "RollDice": "random-outcome",
    "Radiation": "radiation",
    "GainControlVariant": "gain-control",
}

# ---------------------------------------------------------------------------
# Keyword → provides tag mapping
# ---------------------------------------------------------------------------

KEYWORD_TO_PROVIDES: dict[str, str] = {
    "Flying": "flying",
    "Trample": "trample",
    "Haste": "haste",
    "Vigilance": "vigilance",
    "Lifelink": "lifelink",
    "Deathtouch": "deathtouch",
    "First Strike": "first-strike",
    "Double Strike": "double-strike",
    "Menace": "menace",
    "Reach": "reach",
    "Flash": "flash",
    "Hexproof": "hexproof",
    "Indestructible": "indestructible",
    "Defender": "defender",
    "Equip": "equip",
    "Enchant": "enchant",
    "Prowess": "prowess",
    "Flashback": "flashback",
    "Cycling": "cycling",
    "Madness": "madness",
    "Changeling": "changeling",
    "Crew": "crew",
    "Ward": "ward",
    "Affinity": "affinity",
    "Convoke": "convoke",
    "Shroud": "shroud",
    "Landwalk": "landwalk",
}

# ---------------------------------------------------------------------------
# Skipped verbs — produce no provides tags
# ---------------------------------------------------------------------------

SKIPPED_VERBS: set[str] = {
    "Continuous",
    "Effect",
    "Charm",
    "DelayedTrigger",
    "RepeatEach",
    "Repeat",
    "ChooseCard",
    "ChooseType",
    "ChooseColor",
    "ChoosePlayer",
    "ChooseSource",
    "ChooseNumber",
    "ChooseDirection",
    "ChooseEvenOdd",
    "ChooseSector",
    "GenericChoice",
    "Branch",
    "VillainousChoice",
    "TwoPiles",
    "AlternativeCost",
    "OptionalCost",
    "OptionalAttackCost",
    "Cleanup",
    "StoreSVar",
    "AlterAttribute",
    "ChangeText",
    "Panharmonicon",
    "Reveal",
    "RevealHand",
    "LookAt",
    "NameCard",
    "Draft",
    "AssembleContraption",
    "OpenAttraction",
    "Abandon",
    "Subgame",
    "RestartGame",
    "PermanentCreature",
    "PermanentNoncreature",
}

# ---------------------------------------------------------------------------
# Tribal types for detecting tribal tokens
# ---------------------------------------------------------------------------

TRIBAL_TYPES: tuple[str, ...] = (
    "Goblin", "Zombie", "Elf", "Human", "Dragon", "Vampire", "Merfolk",
    "Soldier", "Spirit", "Angel", "Demon", "Dinosaur", "Beast", "Bird",
    "Cat", "Dog", "Rat", "Wizard", "Warrior", "Knight", "Pirate", "Rogue",
    "Shaman", "Cleric", "Druid", "Elemental", "Faerie", "Giant", "Horror",
    "Insect", "Phyrexian", "Sliver", "Fungus", "Treefolk", "Saproling",
    "Skeleton", "Wurm", "Drake", "Sphinx", "Hydra", "Artifact",
)

# ---------------------------------------------------------------------------
# Token type patterns for special tokens
# ---------------------------------------------------------------------------

TOKEN_TYPE_PATTERNS: dict[str, str] = {
    "treasure": "token-treasure",
    "clue": "token-clue",
    "food": "token-food",
    "blood": "token-blood",
}

# ---------------------------------------------------------------------------
# Regex for standalone T (tap symbol) in cost
# ---------------------------------------------------------------------------

_TAP_RE = re.compile(r"(?<![A-Za-z0-9])T(?![A-Za-z0-9])")


def _parse_zone_from_raw(raw_line: str) -> tuple[Optional[str], Optional[str]]:
    """Extract Origin$ and Destination$ values from a raw Forge DSL line."""
    origin = None
    destination = None
    for token in raw_line.split("|"):
        token = token.strip()
        if token.startswith("Origin$"):
            origin = token.split("$", 1)[1].strip()
        elif token.startswith("Destination$"):
            destination = token.split("$", 1)[1].strip()
    return origin, destination


def derive_provides_from_ability(
    verb: Optional[str],
    keyword: Optional[str],
    cost: Optional[str],
    token_script: Optional[str],
    counter_type: Optional[str],
    raw_line: str,
    target: Optional[str],
) -> set[str]:
    """Derive provides tags from a single Forge ability row.

    Args:
        verb: Forge effect verb (e.g. "Token", "Draw", "ChangeZone").
        keyword: Forge keyword (e.g. "Flying", "Equip").
        cost: Raw cost string (e.g. "T", "2 T", "Sac<1/Creature/a creature>").
        token_script: Token script string (e.g. "r_1_1_goblin").
        counter_type: Counter type string (e.g. "P1P1", "+1/+1").
        raw_line: Full raw DSL line for fallback parsing.
        target: Target filter string.

    Returns:
        Set of provides tag strings.
    """
    tags: set[str] = set()

    # --- Verb-based provides ---
    if verb:
        if verb in SKIPPED_VERBS:
            # Explicitly skipped; skip keyword/cost processing too for this verb
            # but still process keyword and cost independently
            pass
        elif verb == "ChangeZone":
            origin, destination = _parse_zone_from_raw(raw_line)
            origin = (origin or "").strip()
            destination = (destination or "").strip()
            if origin == "Graveyard" and destination == "Battlefield":
                tags.add("reanimate")
            elif origin == "Graveyard" and destination == "Hand":
                tags.add("graveyard-to-hand")
            elif origin == "Battlefield" and destination in ("Graveyard", "Exile"):
                tags.add("remove")
            elif origin in ("Hand", "Library") and destination == "Battlefield":
                tags.add("cheat-into-play")
            else:
                tags.add("change-zone")
        elif verb == "Token":
            tags.add("token")
            if token_script:
                script_lower = token_script.lower()
                # Check for special token types
                for pattern, token_tag in TOKEN_TYPE_PATTERNS.items():
                    if pattern in script_lower:
                        tags.add(token_tag)
                        break
                else:
                    # Check for tribal types in token script
                    for tribal in TRIBAL_TYPES:
                        if tribal.lower() in script_lower:
                            tags.add(f"{tribal.lower()}-tribal")
                            break
        elif verb == "Investigate":
            tags.add("investigate")
            tags.add("token-clue")
        elif verb in VERB_TO_PROVIDES:
            tags.add(VERB_TO_PROVIDES[verb])

    # --- Keyword-based provides ---
    if keyword:
        if keyword in KEYWORD_TO_PROVIDES:
            tags.add(KEYWORD_TO_PROVIDES[keyword])

    # --- Cost-based provides ---
    if cost:
        if "Sac" in cost:
            tags.add("sacrifice-outlet")
        if _TAP_RE.search(cost):
            tags.add("tap-ability")

    return tags


# ---------------------------------------------------------------------------
# Trigger mode → wants tag mapping
# ---------------------------------------------------------------------------

TRIGGER_TO_WANTS: dict[str, str] = {
    "Attacks": "attacks",
    "AttackersDeclared": "attackers-declared",
    "AttackerBlocked": "attacker-blocked",
    "AttackerBlockedByCreature": "attacker-blocked",
    "AttackerUnblocked": "attacker-unblocked",
    "Blocks": "blocks",
    "SpellCast": "spell-cast",
    "SpellCastOrCopy": "spell-cast",
    "AbilityCast": "spell-cast",
    "DamageDone": "damage-done",
    "DamageDoneOnce": "damage-done",
    "DamageDealtOnce": "damage-done",
    "Sacrificed": "sacrificed",
    "SacrificedOnce": "sacrificed",
    "LifeGained": "life-gained",
    "LifeLost": "life-lost",
    "Drawn": "card-drawn",
    "Discarded": "discarded",
    "DiscardedAll": "discarded",
    "Phase": "phase-trigger",
    "CounterAdded": "counter-added",
    "CounterAddedOnce": "counter-added",
    "Taps": "tapped",
    "Untaps": "untapped",
    "BecomesTarget": "becomes-target",
    "BecomesTargetOnce": "becomes-target",
    "Cycled": "cycled",
    "Scry": "scry-trigger",
    "Surveil": "surveil-trigger",
    "TapsForMana": "taps-for-mana",
    "LandPlayed": "land-played",
    "Transformed": "transformed",
    "TurnFaceUp": "turn-face-up",
    "Exploited": "exploited",
    "TokenCreated": "token-created",
    "TokenCreatedOnce": "token-created",
    "Crewed": "crewed",
    "BecomesCrewed": "crewed",
    "Mutates": "mutated",
    "Explores": "explored",
    "ChangesZoneAll": "mass-zone-change",
    "Exiled": "exiled",
    "CommitCrime": "commit-crime",
    "RolledDie": "rolled-die",
    "RolledDieOnce": "rolled-die",
    "FlippedCoin": "flipped-coin",
    "Proliferate": "proliferated",
}

# ChangesZone trigger: (origin, destination) → wants tag
ZONE_TRIGGER_MAP: dict[tuple[Optional[str], Optional[str]], str] = {
    ("Battlefield", "Graveyard"): "dies",
    ("Any", "Battlefield"): "enters-battlefield",
    (None, "Battlefield"): "enters-battlefield",
    ("Battlefield", "Exile"): "exiled",
    ("Graveyard", "Battlefield"): "leaves-graveyard",
    ("Any", "Graveyard"): "enters-graveyard",
    ("Library", "Graveyard"): "enters-graveyard",
    ("Battlefield", "Hand"): "bounced",
    ("Battlefield", "Any"): "leaves-battlefield",
}

# Types that are NOT tribal (generic/non-creature-type filters)
_NON_TRIBAL_TYPES: frozenset[str] = frozenset({"Card", "Self", "Creature", "Permanent"})


def derive_wants_from_trigger(
    trigger_mode: Optional[str],
    origin: Optional[str],
    destination: Optional[str],
    trigger_filter: Optional[str],
) -> set[str]:
    """Derive wants tags from a Forge trigger row.

    Args:
        trigger_mode: Forge trigger mode (e.g. "ChangesZone", "Attacks", "SpellCast").
        origin: Zone origin for ChangesZone triggers (e.g. "Battlefield").
        destination: Zone destination for ChangesZone triggers (e.g. "Graveyard").
        trigger_filter: Filter string (e.g. "Goblin.YouCtrl", "Creature.Zombie+Other").

    Returns:
        Set of wants tag strings.
    """
    tags: set[str] = set()

    if not trigger_mode:
        return tags

    if trigger_mode == "ChangesZone":
        # Try exact (origin, destination) lookup first
        key = (origin, destination)
        if key in ZONE_TRIGGER_MAP:
            tags.add(ZONE_TRIGGER_MAP[key])
        elif origin == "Battlefield":
            # Fallback for unknown battlefield departures
            tags.add(ZONE_TRIGGER_MAP[("Battlefield", "Any")])
    else:
        # Simple trigger_mode lookup
        tag = TRIGGER_TO_WANTS.get(trigger_mode)
        if tag:
            tags.add(tag)

    # Parse trigger_filter for tribal creature types
    if trigger_filter:
        # Split filter on common Forge delimiters: . + space comma
        # e.g. "Goblin.YouCtrl" → ["Goblin", "YouCtrl"]
        # e.g. "Creature.Zombie+Other+YouCtrl" → ["Creature", "Zombie", "Other", "YouCtrl"]
        parts = re.split(r"[.+, ]+", trigger_filter)
        for part in parts:
            part = part.strip()
            if part and part not in _NON_TRIBAL_TYPES:
                # Check if this part is a recognised tribal type (case-insensitive match)
                for tribal in TRIBAL_TYPES:
                    if part.lower() == tribal.lower():
                        tags.add(f"{tribal.lower()}-tribal")
                        break

    return tags


def derive_wants_from_cost(cost: Optional[str]) -> set[str]:
    """Derive wants tags from a Forge ability cost string.

    Args:
        cost: Raw cost string (e.g. "Sac<1/Creature/a creature>", "T", "2").

    Returns:
        Set of wants tag strings.
    """
    tags: set[str] = set()
    if cost and "Sac" in cost:
        tags.add("sacrifice-fodder")
    return tags


# ---------------------------------------------------------------------------
# Role derivation from provides tags + type_line
# ---------------------------------------------------------------------------

REMOVAL_TAGS: frozenset[str] = frozenset({
    "destroy", "destroy-all", "counter-spell", "exile",
    "remove", "force-sacrifice", "force-sacrifice-all",
})
RAMP_TAGS: frozenset[str] = frozenset({"mana", "reduce-cost"})
DRAW_TAGS: frozenset[str] = frozenset({"draw", "dig", "scry", "surveil"})
PROTECTION_TAGS: frozenset[str] = frozenset({
    "fog", "prevent-damage", "regenerate", "hexproof",
    "indestructible", "ward", "shroud", "protection-grant",
})
THREAT_TAGS: frozenset[str] = frozenset({
    "token", "pump", "pump-all", "deal-damage", "deal-damage-all",
    "put-counter", "put-counter-all",
})


def derive_role(provides_tags: set, type_line: str) -> str:
    """Derive card role from provides tags and type_line.

    Priority: land > removal > ramp > draw > protection > threat > utility
    First matching role wins.

    Args:
        provides_tags: Set of provides tag strings derived from Forge abilities.
        type_line: Card type line string (e.g. "Basic Land — Plains", "Instant").

    Returns:
        Role string: one of "land", "removal", "ramp", "draw",
        "protection", "threat", or "utility".
    """
    if "Land" in type_line:
        return "land"
    if provides_tags & REMOVAL_TAGS:
        return "removal"
    if provides_tags & RAMP_TAGS:
        return "ramp"
    if provides_tags & DRAW_TAGS:
        return "draw"
    if provides_tags & PROTECTION_TAGS:
        return "protection"
    if provides_tags & THREAT_TAGS:
        return "threat"
    return "utility"


# ---------------------------------------------------------------------------
# DB pipeline: derive_all — full pipeline
# ---------------------------------------------------------------------------

def derive_all(db_path: str, dry_run: bool = False) -> dict:
    """Derive all provides/wants/role tags from Forge abilities and write to DB.

    1. Load ALL forge_abilities joined with forge_name_map
    2. Group abilities by oracle_id (merging DFC faces, adventure halves, etc.)
    3. For each oracle_id: derive provides, wants, and role tags
    4. Add tribal tags from type_line
    5. If not dry_run: wipe provides/wants, bulk insert, update cards.role

    Args:
        db_path: Path to tags.db SQLite database.
        dry_run: If True, compute everything but don't write to DB.

    Returns:
        Stats dict with coverage counts and per-card tag/role data.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # --- Step 1: Load all forge abilities joined with oracle_id ---
    cur = conn.execute("""
        SELECT fa.card_name, fa.ability_index, fa.ability_type, fa.verb,
               fa.trigger_mode, fa.trigger_filter, fa.trigger_origin,
               fa.trigger_destination, fa.target, fa.cost, fa.keyword,
               fa.token_script, fa.counter_type, fa.raw_line,
               fnm.oracle_id
        FROM forge_abilities fa
        JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        WHERE fnm.oracle_id IS NOT NULL
    """)
    rows = cur.fetchall()

    # --- Step 2: Group abilities by oracle_id ---
    abilities_by_oid: dict[str, list] = defaultdict(list)
    for row in rows:
        oracle_id = row[14]  # last column
        abilities_by_oid[oracle_id].append(row)

    # Count cards in forge_abilities that have no forge_name_map entry
    cur = conn.execute("""
        SELECT COUNT(DISTINCT fa.card_name)
        FROM forge_abilities fa
        LEFT JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        WHERE fnm.forge_name IS NULL
    """)
    cards_skipped = cur.fetchone()[0]

    # --- Step 3-4: Load type_lines and derive tags ---
    # Preload type_lines for all cards
    type_lines: dict[str, str] = {}
    cur = conn.execute("SELECT oracle_id, type_line FROM cards")
    for oid, tl in cur.fetchall():
        type_lines[oid] = tl or ""

    card_tags: dict[str, dict[str, set[str]]] = {}
    card_roles: dict[str, str] = {}

    for oracle_id, ability_rows in abilities_by_oid.items():
        provides: set[str] = set()
        wants: set[str] = set()

        for row in ability_rows:
            (card_name, ability_index, ability_type, verb, trigger_mode,
             trigger_filter, trigger_origin, trigger_destination, target,
             cost, keyword, token_script, counter_type, raw_line, _oid) = row

            # Derive provides from every ability
            p_tags = derive_provides_from_ability(
                verb=verb,
                keyword=keyword,
                cost=cost,
                token_script=token_script,
                counter_type=counter_type,
                raw_line=raw_line or "",
                target=target,
            )
            provides.update(p_tags)

            # Derive wants from triggered abilities (ability_type='T')
            if ability_type == "T":
                w_tags = derive_wants_from_trigger(
                    trigger_mode=trigger_mode,
                    origin=trigger_origin,
                    destination=trigger_destination,
                    trigger_filter=trigger_filter,
                )
                wants.update(w_tags)

            # Derive wants from cost (any ability with sacrifice cost)
            if cost:
                w_cost = derive_wants_from_cost(cost)
                wants.update(w_cost)

        # --- Step 5-6: Add tribal from type_line ---
        type_line = type_lines.get(oracle_id, "")
        if " \u2014 " in type_line:
            # Handle DFC type_lines like "Legendary Creature — God // Legendary Artifact"
            # Split on " // " first to handle each face, then extract subtypes
            for face_type in type_line.split(" // "):
                parts = face_type.split(" \u2014 ")
                if len(parts) >= 2:
                    subtypes = parts[1].strip().split()
                    for word in subtypes:
                        for tribal in TRIBAL_TYPES:
                            if word.lower() == tribal.lower():
                                provides.add(f"{tribal.lower()}-tribal")
                                break

        # --- Step 7: Derive role ---
        role = derive_role(provides, type_line)

        card_tags[oracle_id] = {"provides": provides, "wants": wants}
        card_roles[oracle_id] = role

    # --- Step 8: Write to DB if not dry_run ---
    if not dry_run:
        conn.execute("DELETE FROM provides")
        conn.execute("DELETE FROM wants")

        provides_rows = []
        for oracle_id, tags in card_tags.items():
            for tag in sorted(tags["provides"]):
                provides_rows.append((oracle_id, tag))

        wants_rows = []
        for oracle_id, tags in card_tags.items():
            for tag in sorted(tags["wants"]):
                wants_rows.append((oracle_id, tag))

        conn.executemany(
            "INSERT OR IGNORE INTO provides (oracle_id, tag) VALUES (?, ?)",
            provides_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO wants (oracle_id, tag) VALUES (?, ?)",
            wants_rows,
        )

        role_rows = [(role, oid) for oid, role in card_roles.items()]
        conn.executemany(
            "UPDATE cards SET role = ? WHERE oracle_id = ?",
            role_rows,
        )

        conn.commit()

    # --- Step 9: Compute stats ---
    total_cards = len(card_tags)
    cards_with_provides = sum(1 for t in card_tags.values() if t["provides"])
    cards_with_wants = sum(1 for t in card_tags.values() if t["wants"])
    total_provides_tags = sum(len(t["provides"]) for t in card_tags.values())
    total_wants_tags = sum(len(t["wants"]) for t in card_tags.values())

    conn.close()

    return {
        "total_cards": total_cards,
        "cards_with_provides": cards_with_provides,
        "cards_with_wants": cards_with_wants,
        "total_provides_tags": total_provides_tags,
        "total_wants_tags": total_wants_tags,
        "cards_skipped": cards_skipped,
        "card_tags": card_tags,
        "card_roles": card_roles,
    }


def _show_stats_from_db(conn) -> None:
    """Print current provides/wants/role stats from DB."""
    provides_count = conn.execute("SELECT COUNT(*) FROM provides").fetchone()[0]
    wants_count = conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0]
    provides_cards = conn.execute(
        "SELECT COUNT(DISTINCT oracle_id) FROM provides"
    ).fetchone()[0]
    wants_cards = conn.execute(
        "SELECT COUNT(DISTINCT oracle_id) FROM wants"
    ).fetchone()[0]

    # Role distribution
    role_dist = conn.execute(
        "SELECT role, COUNT(*) FROM cards WHERE role != '' GROUP BY role ORDER BY COUNT(*) DESC"
    ).fetchall()

    print(f"Provides: {provides_count} rows across {provides_cards} cards")
    print(f"Wants:    {wants_count} rows across {wants_cards} cards")
    print(f"\nRole distribution:")
    for role, count in role_dist:
        print(f"  {count:6d}  {role}")

    # Top 20 provides tags
    print(f"\nTop 20 provides tags:")
    top_provides = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM provides GROUP BY tag ORDER BY cnt DESC LIMIT 20"
    ).fetchall()
    for tag, count in top_provides:
        print(f"  {count:6d}  {tag}")

    # Top 20 wants tags
    print(f"\nTop 20 wants tags:")
    top_wants = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM wants GROUP BY tag ORDER BY cnt DESC LIMIT 20"
    ).fetchall()
    for tag, count in top_wants:
        print(f"  {count:6d}  {tag}")


def _show_card_tags(conn, db_path: str, card_name: str) -> None:
    """Show derived tags for a single card by name."""
    # Look up oracle_id from cards table
    row = conn.execute(
        "SELECT oracle_id, type_line FROM cards WHERE name = ?", (card_name,)
    ).fetchone()
    if not row:
        # Try partial match
        row = conn.execute(
            "SELECT oracle_id, type_line FROM cards WHERE name LIKE ?",
            (f"%{card_name}%",),
        ).fetchone()
    if not row:
        print(f"Card not found: {card_name!r}")
        return

    oracle_id, type_line = row

    # Load abilities for this oracle_id
    cur = conn.execute("""
        SELECT fa.card_name, fa.ability_index, fa.ability_type, fa.verb,
               fa.trigger_mode, fa.trigger_filter, fa.trigger_origin,
               fa.trigger_destination, fa.target, fa.cost, fa.keyword,
               fa.token_script, fa.counter_type, fa.raw_line
        FROM forge_abilities fa
        JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name
        WHERE fnm.oracle_id = ?
    """, (oracle_id,))
    ability_rows = cur.fetchall()

    if not ability_rows:
        print(f"{card_name} ({oracle_id}): no Forge abilities found")
        return

    provides: set[str] = set()
    wants: set[str] = set()

    for r in ability_rows:
        (card_name_r, ability_index, ability_type, verb, trigger_mode,
         trigger_filter, trigger_origin, trigger_destination, target,
         cost, keyword, token_script, counter_type, raw_line) = r

        p = derive_provides_from_ability(
            verb=verb, keyword=keyword, cost=cost,
            token_script=token_script, counter_type=counter_type,
            raw_line=raw_line or "", target=target,
        )
        provides.update(p)

        if ability_type == "T":
            w = derive_wants_from_trigger(
                trigger_mode=trigger_mode, origin=trigger_origin,
                destination=trigger_destination, trigger_filter=trigger_filter,
            )
            wants.update(w)

        if cost:
            w_cost = derive_wants_from_cost(cost)
            wants.update(w_cost)

    # Add tribal from type_line
    if " \u2014 " in type_line:
        for face_type in type_line.split(" // "):
            parts = face_type.split(" \u2014 ")
            if len(parts) >= 2:
                subtypes = parts[1].strip().split()
                for word in subtypes:
                    for tribal in TRIBAL_TYPES:
                        if word.lower() == tribal.lower():
                            provides.add(f"{tribal.lower()}-tribal")
                            break

    role = derive_role(provides, type_line)

    print(f"Card:     {card_name} ({oracle_id})")
    print(f"Type:     {type_line}")
    print(f"Role:     {role}")
    print(f"Provides: {sorted(provides)}")
    print(f"Wants:    {sorted(wants)}")
    print(f"Abilities: {len(ability_rows)}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive provides/wants/role tags from Forge DSL abilities"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute everything but don't write to DB")
    parser.add_argument("--stats", action="store_true",
                        help="Show current provides/wants/role stats from DB")
    parser.add_argument("--card", type=str,
                        help="Show derived tags for one card by name")
    parser.add_argument("--db", type=str, default=None,
                        help="Path to tags.db (default: auto-detect)")
    args = parser.parse_args()

    from mtg_synergy.db import get_connection

    if args.stats:
        conn = get_connection(args.db)
        _show_stats_from_db(conn)
        conn.close()
    elif args.card:
        conn = get_connection(args.db)
        _show_card_tags(conn, args.db or "", args.card)
        conn.close()
    else:
        # Full pipeline: derive all tags and write to DB (unless --dry-run)
        if args.db:
            db_path = args.db
        else:
            from mtg_synergy.config import DB_PATH
            db_path = str(DB_PATH)

        stats = derive_all(db_path, dry_run=args.dry_run)

        action = "DRY RUN" if args.dry_run else "WRITTEN"
        print(f"=== Forge tag derivation ({action}) ===")
        print(f"Total cards with Forge data: {stats['total_cards']}")
        print(f"Cards with provides tags:    {stats['cards_with_provides']}")
        print(f"Cards with wants tags:       {stats['cards_with_wants']}")
        print(f"Total provides tag rows:     {stats['total_provides_tags']}")
        print(f"Total wants tag rows:        {stats['total_wants_tags']}")
        print(f"Cards skipped (no mapping):  {stats['cards_skipped']}")

    return


if __name__ == "__main__":
    main()
