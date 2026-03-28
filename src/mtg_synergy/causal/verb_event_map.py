"""Mapping from Forge effect verbs to the trigger modes they produce.

Extracted from Forge's Java source code (forge-game/src/main/java/forge/game/).
The mapping traces the actual code path: Effect class → game engine method → runTrigger(TriggerType).

Sources:
  - forge/game/ability/effects/*Effect.java  (direct trigger calls)
  - forge/game/GameAction.java               (changeZone, destroy, sacrifice, scry, mill)
  - forge/game/card/Card.java                (tap, untap, damage, counters)
  - forge/game/player/Player.java            (gainLife, loseLife, draw, discard, mill, surveil)
  - forge/game/combat/CombatUtil.java        (attacks)
  - forge/game/zone/MagicStack.java          (spellCast)
"""

# Each entry: verb → list of {trigger_mode, origin?, destination?}
# Zone-specific fields (origin, destination) are only set when the verb
# implies a specific zone transition. Otherwise the ability's own fields are used.
_VERB_EVENT_MAP = {
    # --- Zone changes (GameAction.changeZone → ChangesZone) ---
    "Token": [
        {"trigger_mode": "ChangesZone", "destination": "Battlefield"},
        {"trigger_mode": "TokenCreated"},
    ],
    "ChangeZone": [
        {"trigger_mode": "ChangesZone"},  # origin/dest from the ability itself
    ],
    "ChangeZoneAll": [
        {"trigger_mode": "ChangesZone"},
    ],
    "Destroy": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
        {"trigger_mode": "Destroyed"},
    ],
    "DestroyAll": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
        {"trigger_mode": "Destroyed"},
    ],
    "Mill": [
        {"trigger_mode": "ChangesZone", "origin": "Library", "destination": "Graveyard"},
        {"trigger_mode": "Milled"},
    ],
    "Exile": [
        {"trigger_mode": "ChangesZone", "destination": "Exile"},
        {"trigger_mode": "Exiled"},
    ],
    "ExileAll": [
        {"trigger_mode": "ChangesZone", "destination": "Exile"},
        {"trigger_mode": "Exiled"},
    ],
    "CopyPermanent": [
        {"trigger_mode": "ChangesZone", "destination": "Battlefield"},
    ],
    "Sacrifice": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
        {"trigger_mode": "Sacrificed"},
    ],
    "SacrificeAll": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
        {"trigger_mode": "Sacrificed"},
    ],

    # --- Damage (Card.addDamageAfterPrevention → DamageDone) ---
    "DealDamage": [
        {"trigger_mode": "DamageDone"},
    ],
    "DamageAll": [
        {"trigger_mode": "DamageDone"},
    ],

    # --- Life (Player.gainLife/loseLife) ---
    "GainLife": [
        {"trigger_mode": "LifeGained"},
    ],
    "LoseLife": [
        {"trigger_mode": "LifeLost"},
    ],

    # --- Cards (Player.doDraw/discard) ---
    "Draw": [
        {"trigger_mode": "Drawn"},
    ],
    "Dig": [
        {"trigger_mode": "Drawn"},
    ],
    "Discard": [
        {"trigger_mode": "Discarded"},
    ],
    "DiscardAll": [
        {"trigger_mode": "Discarded"},
    ],

    # --- Tap/Untap (Card.tap/untap) ---
    "Tap": [
        {"trigger_mode": "Taps"},
    ],
    "TapAll": [
        {"trigger_mode": "Taps"},
    ],
    "Untap": [
        {"trigger_mode": "Untaps"},
    ],
    "UntapAll": [
        {"trigger_mode": "Untaps"},
    ],

    # --- Counters (Card/Player.addCounterInternal) ---
    "PutCounter": [
        {"trigger_mode": "CounterAdded"},
    ],
    "PutCounterAll": [
        {"trigger_mode": "CounterAdded"},
    ],
    "RemoveCounter": [
        {"trigger_mode": "CounterRemoved"},
    ],

    # --- Scry/Surveil (GameAction.scry, Player.surveil) ---
    "Scry": [
        {"trigger_mode": "Scry"},
    ],
    "Surveil": [
        {"trigger_mode": "Surveil"},
    ],

    # --- Control (GameAction.changeZone → ChangesController) ---
    "GainControl": [
        {"trigger_mode": "ChangesController"},
    ],
    "ControlGain": [
        {"trigger_mode": "ChangesController"},
    ],

    # --- Combat (FightEffect, BlockEffect) ---
    "Fight": [
        {"trigger_mode": "Fight"},
        {"trigger_mode": "DamageDone"},
    ],
    "Block": [
        {"trigger_mode": "Blocks"},
        {"trigger_mode": "AttackerBlocked"},
    ],

    # --- Spells (MagicStack.add → SpellCast) ---
    "Counter": [
        {"trigger_mode": "Countered"},
    ],
    "Play": [
        {"trigger_mode": "SpellCast"},
    ],

    # --- Mana (Mana effect → TapsForMana) ---
    "Mana": [
        {"trigger_mode": "TapsForMana"},
    ],
    "ManaReflected": [
        {"trigger_mode": "TapsForMana"},
    ],

    # --- Keyword mechanics ---
    "Proliferate": [
        {"trigger_mode": "Proliferate"},
        {"trigger_mode": "CounterAdded"},
    ],
    "Connive": [
        {"trigger_mode": "Drawn"},
        {"trigger_mode": "Discarded"},
    ],
    "Explore": [
        {"trigger_mode": "Explores"},
    ],
    "Investigate": [
        {"trigger_mode": "ChangesZone", "destination": "Battlefield"},
        {"trigger_mode": "Investigated"},
    ],
    "Mutate": [
        {"trigger_mode": "Mutates"},
    ],
    "RingTemptsYou": [
        {"trigger_mode": "RingTemptsYou"},
    ],

    # --- Misc ---
    "Abandon": [
        {"trigger_mode": "Abandoned"},
    ],
    "FlipCoin": [
        {"trigger_mode": "FlippedCoin"},
    ],
    "RollDice": [
        {"trigger_mode": "RolledDie"},
    ],
    "Vote": [
        {"trigger_mode": "Vote"},
    ],
}

# Build reverse map: trigger_mode → set of verbs
_EVENT_VERB_MAP = {}
for verb, events in _VERB_EVENT_MAP.items():
    for event in events:
        mode = event["trigger_mode"]
        _EVENT_VERB_MAP.setdefault(mode, set()).add(verb)


def verb_to_events(verb: str) -> list[dict]:
    """Get the trigger events produced by a Forge effect verb.

    Returns list of dicts with trigger_mode and optional origin/destination.
    """
    return _VERB_EVENT_MAP.get(verb, [])


def event_to_verbs(trigger_mode: str) -> set[str]:
    """Get which Forge verbs can produce a given trigger mode."""
    return _EVENT_VERB_MAP.get(trigger_mode, set())
