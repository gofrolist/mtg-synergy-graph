"""Mapping from Forge effect verbs to the trigger modes they produce.

When a card has an effect verb (e.g., Token), it produces game events
that other cards' triggers respond to (e.g., ChangesZone with
Destination=Battlefield).

This replaces the old verb_resolvers.py StateChange system.
"""

# Each entry: verb → list of {trigger_mode, origin?, destination?, card_type?}
_VERB_EVENT_MAP = {
    # Zone changes
    "Token": [
        {"trigger_mode": "ChangesZone", "destination": "Battlefield"},
    ],
    "ChangeZone": [
        {"trigger_mode": "ChangesZone"},  # origin/dest from the ability itself
    ],
    "ChangeZoneAll": [
        {"trigger_mode": "ChangesZone"},
    ],
    "Destroy": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
    ],
    "DestroyAll": [
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
    ],
    "Mill": [
        {"trigger_mode": "ChangesZone", "origin": "Library", "destination": "Graveyard"},
    ],
    "Exile": [
        {"trigger_mode": "ChangesZone", "destination": "Exile"},
    ],
    "ExileAll": [
        {"trigger_mode": "ChangesZone", "destination": "Exile"},
    ],
    "CopyPermanent": [
        {"trigger_mode": "ChangesZone", "destination": "Battlefield"},
    ],

    # Damage
    "DealDamage": [
        {"trigger_mode": "DamageDone"},
    ],
    "DamageAll": [
        {"trigger_mode": "DamageDone"},
    ],

    # Life
    "GainLife": [
        {"trigger_mode": "LifeGained"},
    ],

    # Cards
    "Draw": [
        {"trigger_mode": "Drawn"},
    ],
    "Dig": [
        {"trigger_mode": "Drawn"},
    ],
    "Discard": [
        {"trigger_mode": "Discarded"},
    ],

    # Sacrifice
    "Sacrifice": [
        {"trigger_mode": "Sacrificed"},
        {"trigger_mode": "ChangesZone", "destination": "Graveyard"},
    ],

    # Tap
    "Tap": [
        {"trigger_mode": "Taps"},
    ],
    "TapAll": [
        {"trigger_mode": "Taps"},
    ],

    # Counters (no standard trigger mode, but some cards trigger on counters)
    "PutCounter": [
        {"trigger_mode": "CounterAdded"},
    ],
    "PutCounterAll": [
        {"trigger_mode": "CounterAdded"},
    ],

    # These verbs don't produce triggerable events
    # Pump, PumpAll, Animate, AnimateAll, GainControl, Counter,
    # Scry, Untap, UntapAll, LoseLife, Mana, ReduceCost, etc.
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
