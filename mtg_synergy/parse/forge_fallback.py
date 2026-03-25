"""Forge DSL verb mapping and effect fallback.

Maps Forge card script verbs to our effect vocabulary.
Used as fallback when the regex parser produces empty effects.
"""
import json
import re
import sqlite3
from typing import Optional

from mtg_synergy.parse.ast_types import Effect, Amount, ObjectFilter

FORGE_VERB_MAP = {
    "DealDamage": "deal_damage",
    "DrawCard": "draw",
    "GainLife": "gain_life",
    "LoseLife": "lose_life",
    "CreateToken": "create",
    "Destroy": "destroy",
    "DestroyAll": "destroy",
    "PutCounter": "put_counter",
    "PutCounterAll": "put_counter",
    "Mill": "mill",
    "Discard": "discard",
    "Proliferate": "put_counter",
    "Sacrifice": "sacrifice",
    "Tap": "tap",
    "TapAll": "tap",
    "Untap": "untap",
    "UntapAll": "untap",
    "ExileAll": "exile",
    "Exile": "exile",
    "Dig": "draw",
    "PumpAll": "pump",
    "Pump": "pump",
    "Counter": "counter",
    "Scry": "scry",
    "Token": "create",
    "ManaReflected": "add_mana",
    "Mana": "add_mana",
}

_CHANGE_ZONE_MAP = {
    ("Graveyard", "Battlefield"): "return",
    ("Graveyard", "Hand"): "return",
    ("Hand", "Graveyard"): "discard",
    ("Battlefield", "Exile"): "exile",
    ("Battlefield", "Graveyard"): "sacrifice",
    ("Library", "Hand"): "search",
    ("Library", "Battlefield"): "search",
    ("Exile", "Battlefield"): "return",
    ("Exile", "Hand"): "return",
}


def map_forge_verb(forge_verb: str, origin: str = None, destination: str = None) -> Optional[str]:
    """Map a Forge DSL verb to our effect vocabulary.

    Args:
        forge_verb: The Forge verb (e.g. 'DealDamage', 'ChangeZone').
        origin: For ChangeZone verbs, the source zone.
        destination: For ChangeZone verbs, the target zone.

    Returns:
        Our verb string, or None if unmapped.
    """
    if forge_verb in ("ChangeZone", "ChangeZoneAll"):
        if origin and destination:
            return _CHANGE_ZONE_MAP.get((origin, destination))
        return None
    return FORGE_VERB_MAP.get(forge_verb)


def parse_forge_ability_line(line: str) -> Optional[dict]:
    """Parse a single Forge DSL ability line into a structured dict.

    Forge lines have the format:
        A:SP$ DealDamage | Cost$ R | Tgt$ TgtCP | NumDmg$ 3
        T:Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | ...

    Args:
        line: A single line from a Forge card script file.

    Returns:
        Dict with keys: prefix, forge_verb, trigger_type, target, amount,
        origin, destination, fields. Or None if unparseable.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    prefix = None
    for p in ("A:", "T:", "S:", "K:", "SVar:"):
        if line.startswith(p):
            prefix = p.rstrip(":")
            line = line[len(p):]
            break
    if prefix is None:
        return None

    fields = {}
    for pair in line.split(" | "):
        pair = pair.strip()
        if "$ " in pair:
            key, val = pair.split("$ ", 1)
            fields[key.strip()] = val.strip()
        elif "$" in pair:
            key, val = pair.split("$", 1)
            fields[key.strip()] = val.strip()

    forge_verb = fields.get("SP") or fields.get("Mode")

    return {
        "prefix": prefix,
        "forge_verb": forge_verb,
        "trigger_type": fields.get("Mode") if prefix == "T" else None,
        "target": fields.get("Tgt") or fields.get("ValidTgts"),
        "amount": fields.get("NumDmg") or fields.get("TokenAmount") or fields.get("CounterNum"),
        "origin": fields.get("Origin"),
        "destination": fields.get("Destination"),
        "fields": fields,
    }


def ensure_forge_schema(conn):
    """Create the forge_effects table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_effects (
            card_name TEXT NOT NULL,
            ability_index INTEGER NOT NULL,
            forge_verb TEXT NOT NULL,
            our_verb TEXT,
            target TEXT,
            amount TEXT,
            trigger_type TEXT,
            PRIMARY KEY (card_name, ability_index, forge_verb)
        )
    """)
    conn.commit()


def load_forge_effects(conn, card_name: str) -> list[Effect]:
    """Load pre-imported Forge effects for a card as AST Effect objects.

    Args:
        conn: SQLite connection with forge_effects table.
        card_name: Card name to look up.

    Returns:
        List of Effect objects from the Forge data.
    """
    rows = conn.execute(
        "SELECT our_verb, target, amount FROM forge_effects WHERE card_name = ? AND our_verb IS NOT NULL",
        (card_name,)
    ).fetchall()
    effects = []
    for our_verb, target, amount in rows:
        amt = None
        if amount:
            try:
                amt = Amount(value=int(amount))
            except ValueError:
                amt = Amount(value=amount)
        effects.append(Effect(verb=our_verb, amount=amt))
    return effects
