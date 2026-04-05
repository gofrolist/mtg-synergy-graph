"""Shared commander mechanical pattern detection.

Extracts boolean flags describing what a commander does mechanically
(makes creatures, triggers on ETB, death triggers, spellcast triggers).
Used by both scoring.py and hidden_gems.py.
"""
from typing import NamedTuple


class CmdrMechanicalFlags(NamedTuple):
    """Boolean flags for commander mechanical patterns."""
    makes_creatures: bool
    triggers_etb: bool
    death_trigger: bool
    spellcast: bool


def detect_cmdr_patterns(cmdr_verbs: set, cmdr_triggers: set,
                         cmdr_trigger_filters: set) -> CmdrMechanicalFlags:
    """Detect mechanical patterns from a commander's forge profile.

    Args:
        cmdr_verbs: set of verb strings (e.g., {'Token', 'Animate'})
        cmdr_triggers: set of trigger mode strings (e.g., {'ChangesZone', 'SpellCast'})
        cmdr_trigger_filters: set of trigger filter strings (e.g., {'creature', 'permanent'})

    Returns:
        CmdrMechanicalFlags with boolean flags for each pattern.
    """
    makes_creatures = bool(cmdr_verbs & {'Token', 'Animate', 'Manifest'})

    triggers_etb = ('ChangesZone' in cmdr_triggers and
                    bool(cmdr_trigger_filters & {'creature', 'permanent', 'nontoken'}))

    death_trigger = (
        ('ChangesZone' in cmdr_triggers and 'creature' in cmdr_trigger_filters) or
        'Sacrificed' in cmdr_triggers or 'Destroyed' in cmdr_triggers
    )

    spellcast = 'SpellCast' in cmdr_triggers

    return CmdrMechanicalFlags(
        makes_creatures=makes_creatures,
        triggers_etb=triggers_etb,
        death_trigger=death_trigger,
        spellcast=spellcast,
    )
