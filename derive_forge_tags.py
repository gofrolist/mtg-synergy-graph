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
# DB pipeline: read forge_abilities, derive tags, write to provides
# ---------------------------------------------------------------------------

def derive_all_tags(conn) -> dict[str, set[str]]:
    """Derive provides tags for all Forge abilities joined with oracle_ids.

    Returns:
        dict mapping oracle_id → set of provides tags
    """
    results: dict[str, set[str]] = defaultdict(set)

    cur = conn.execute("""
        SELECT
            fnm.oracle_id,
            fa.verb,
            fa.keyword,
            fa.cost,
            fa.token_script,
            fa.counter_type,
            fa.raw_line,
            fa.target
        FROM forge_abilities fa
        JOIN forge_name_map fnm ON fa.card_name = fnm.forge_name
        WHERE fnm.oracle_id IS NOT NULL
    """)

    for row in cur.fetchall():
        oracle_id, verb, keyword, cost, token_script, counter_type, raw_line, target = row
        tags = derive_provides_from_ability(
            verb=verb,
            keyword=keyword,
            cost=cost,
            token_script=token_script,
            counter_type=counter_type,
            raw_line=raw_line or "",
            target=target,
        )
        if tags:
            results[oracle_id].update(tags)

    return dict(results)


def write_tags_to_db(conn, tags_by_oracle_id: dict[str, set[str]], source: str = "forge") -> int:
    """Write derived tags to the provides table.

    Only writes tags that don't already exist (INSERT OR IGNORE).

    Returns:
        Number of new rows inserted.
    """
    rows = []
    for oracle_id, tags in tags_by_oracle_id.items():
        for tag in sorted(tags):
            rows.append((oracle_id, tag))

    conn.executemany(
        "INSERT OR IGNORE INTO provides (oracle_id, tag) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def show_stats(conn) -> None:
    """Print derivation statistics."""
    tags_by_id = derive_all_tags(conn)
    total_cards = len(tags_by_id)
    total_tags = sum(len(t) for t in tags_by_id.values())

    # Tag frequency
    freq: dict[str, int] = defaultdict(int)
    for tags in tags_by_id.values():
        for t in tags:
            freq[t] += 1

    print(f"Cards with derived tags: {total_cards}")
    print(f"Total derived tag rows:  {total_tags}")
    print(f"Unique tag values:       {len(freq)}")
    print("\nTop 20 most common tags:")
    for tag, count in sorted(freq.items(), key=lambda x: -x[1])[:20]:
        print(f"  {count:6d}  {tag}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive provides tags from Forge DSL abilities"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print derived tags without writing to DB")
    parser.add_argument("--write", action="store_true",
                        help="Write tags to provides table")
    parser.add_argument("--stats", action="store_true",
                        help="Show derivation statistics")
    parser.add_argument("--card", type=str,
                        help="Derive tags for a single card name only")
    args = parser.parse_args()

    from mtg_synergy.db import get_connection
    conn = get_connection()

    if args.stats:
        show_stats(conn)
    elif args.dry_run:
        tags_by_id = derive_all_tags(conn)
        if args.card:
            # Filter by card name via forge_name_map
            cur = conn.execute(
                "SELECT oracle_id FROM forge_name_map WHERE forge_name = ?",
                (args.card,),
            )
            row = cur.fetchone()
            if row:
                oid = row[0]
                print(f"{args.card} ({oid}): {sorted(tags_by_id.get(oid, set()))}")
            else:
                print(f"Card not found in forge_name_map: {args.card!r}")
        else:
            for oracle_id, tags in sorted(tags_by_id.items()):
                print(f"{oracle_id}: {sorted(tags)}")
    elif args.write:
        tags_by_id = derive_all_tags(conn)
        n = write_tags_to_db(conn, tags_by_id)
        print(f"Inserted {n} provides rows from Forge abilities.")
    else:
        parser.print_help()

    conn.close()


if __name__ == "__main__":
    main()
