"""Phase B2 — ApiType effect-verb inventory regression test.

Snapshot of every distinct ``event_class`` that appears on an effect
port in the imported ``/tmp/synergy_full.db`` as of commit ``c611810``.
The test asserts the current DB's inventory matches this frozen set.

**Why this exists.** The Forge cardsfolder ships new ``Api$`` verbs with
every set — Cloak, ManifestDread, RingTemptsYou, TimeTravel, etc. are
all relatively recent additions. Most are low-impact for commander
scoring (Contraption / Vote / planar mechanics), but a silent addition
to the cardsfolder with no corresponding matcher update is the kind of
drift that kills recall over time. This test is the tripwire: when a
new verb appears, the test fails with a clear diff and the developer
decides whether to (a) add the verb to a matcher, (b) add it to the
deliberately-ignored list, or (c) update the baseline.

The baseline also includes ``combo_primitive`` which is the synthetic
event_class emitted by Phase B3 alongside Branch / Repeat / RepeatEach
effects. If B3 ever gets reverted, the test catches it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Snapshot taken 2026-04-08 against the full cardsfolder, 32327 cards,
# 174052 ports. When updating, run:
#
#   uv run python -c "import sqlite3; \
#     conn = sqlite3.connect('/tmp/synergy_full.db'); \
#     rows = conn.execute(\"SELECT DISTINCT event_class FROM card_ports \
#     WHERE port_type='effect' ORDER BY event_class\").fetchall(); \
#     print(sorted({r[0] for r in rows if r[0]}))"
#
# and paste the result here after inspecting the diff.
KNOWN_EFFECT_VERBS: frozenset[str] = frozenset(
    {
        "Abandon",
        "ActivateAbility",
        "AddOrRemoveCounter",
        "AddPhase",
        "AddTurn",
        "AdvanceCrank",
        "Airbend",
        "AlterAttribute",
        "Amass",
        "Animate",
        "AnimateAll",
        "AssembleContraption",
        "AssignGroup",
        "Attach",
        "Balance",
        "BecomeMonarch",
        "BecomesBlocked",
        "BidLife",
        "BlankLine",
        "Blight",
        "Block",
        "Branch",
        "ChangeCombatants",
        "ChangeSpeed",
        "ChangeTargets",
        "ChangeText",
        "ChangeX",
        "ChangeZone",
        "ChangeZoneAll",
        "ChaosEnsues",
        "Charm",
        "ChooseCard",
        "ChooseColor",
        "ChooseDirection",
        "ChooseEvenOdd",
        "ChooseNumber",
        "ChoosePlayer",
        "ChooseSector",
        "ChooseSource",
        "ChooseType",
        "Clash",
        "Cleanup",
        "Cloak",
        "Clone",
        "Connive",
        "ControlPlayer",
        "ControlSpell",
        "CopyPermanent",
        "CopySpellAbility",
        "Counter",
        "DamageAll",
        "DamageResolve",
        "DayTime",
        "DealDamage",
        "Debuff",
        "DelayedTrigger",
        "Destroy",
        "DestroyAll",
        "Detain",
        "Dig",
        "DigMultiple",
        "DigUntil",
        "Discard",
        "Discover",
        "Draft",
        "DrainMana",
        "Draw",
        "EachDamage",
        "Earthbend",
        "Effect",
        "EndCombatPhase",
        "EndTurn",
        "Endure",
        "ExchangeControl",
        "ExchangeControlVariant",
        "ExchangeLife",
        "ExchangeLifeVariant",
        "ExchangePower",
        "ExchangeTextBox",
        "ExchangeZone",
        "Explore",
        "Fight",
        "FlipACoin",
        "FlipOntoBattlefield",
        "Fog",
        "GainControl",
        "GainControlVariant",
        "GainLife",
        "GainOwnership",
        "GameDrawn",
        "GenericChoice",
        "Goad",
        "Heist",
        "ImmediateTrigger",
        "Incubate",
        "Intensify",
        "Investigate",
        "Learn",
        "LookAt",
        "LoseLife",
        "LosesGame",
        "MakeCard",
        "Mana",
        "ManaReflected",
        "Manifest",
        "ManifestDread",
        "Meld",
        "Mill",
        "MoveCounter",
        "MultiplePiles",
        "MultiplyCounter",
        "MustBlock",
        "NameCard",
        "OpenAttraction",
        "PeekAndReveal",
        "PermanentCreature",
        "PermanentNoncreature",
        "Phases",
        "Planeswalk",
        "Play",
        "Poison",
        "PreventDamage",
        "Proliferate",
        "Protection",
        "ProtectionAll",
        "Pump",
        "PumpAll",
        "PutCounter",
        "PutCounterAll",
        "Radiation",
        "RearrangeTopOfLibrary",
        "Regenerate",
        "RemoveCounter",
        "RemoveCounterAll",
        "RemoveFromCombat",
        "RemoveFromGame",
        "RemoveFromMatch",
        "ReorderZone",
        "Repeat",
        "RepeatEach",
        "RestartGame",
        "Reveal",
        "RevealHand",
        "ReverseTurnOrder",
        "RingTemptsYou",
        "RollDice",
        "RollPlanarDice",
        "RunChaos",
        "Sacrifice",
        "SacrificeAll",
        "Scry",
        "Seek",
        "SetInMotion",
        "SetLife",
        "SetState",
        "Shuffle",
        "SkipPhase",
        "SkipTurn",
        "StoreSVar",
        "Subgame",
        "Surveil",
        "SwitchBlock",
        "TakeInitiative",
        "Tap",
        "TapAll",
        "TapOrUntap",
        "TapOrUntapAll",
        "TaporUntapAll",
        "TimeTravel",
        "Token",
        "TwoPiles",
        "Unattach",
        "UnlockDoor",
        "Untap",
        "UntapAll",
        "Venture",
        "VillainousChoice",
        "Vote",
        "WinsGame",
        # Phase B3 synthetic port — emitted alongside Branch / Repeat /
        # RepeatEach effects so queries can find combo cards via a single
        # SQL key.
        "combo_primitive",
    }
)


#: Verbs the matchers deliberately ignore — novelty / rare mechanics
#: that have <50 EDH-legal cards each. Documented here so the intent
#: survives future refactoring.
DELIBERATELY_IGNORED: frozenset[str] = frozenset(
    {
        "AdvanceCrank",
        "AssembleContraption",
        "OpenAttraction",  # Contraption
        "Vote",
        "VillainousChoice",  # Voting
        "RollDice",
        "RollPlanarDice",
        "FlipACoin",
        "ChaosEnsues",  # Chaos
        "RunChaos",  # Chaos
        "Planeswalk",
        "SetInMotion",  # Planar
        "AddTurn",
        "AddPhase",
        "SkipPhase",
        "SkipTurn",
        "Phases",  # Temporal
        "EndTurn",
        "EndCombatPhase",  # Temporal
        "TimeTravel",  # Temporal
        "ExchangeLife",
        "ExchangeLifeVariant",
        "ExchangePower",  # Exchange
        "ExchangeZone",
        "ExchangeControl",
        "ExchangeControlVariant",  # Exchange
        "ExchangeTextBox",  # Exchange
        "Balance",
        "BidLife",
        "ChangeSpeed",
        "ChangeText",  # Niche
        "RestartGame",
        "Subgame",
        "GameDrawn",
        "Draft",  # Special
        "ReorderZone",
        "RearrangeTopOfLibrary",  # Library
        "ReverseTurnOrder",
        "BecomeMonarch",
        "TakeInitiative",  # Monarch/initiative
        "RingTemptsYou",  # LotR
        "NameCard",
        "MakeCard",
        "StoreSVar",
        "SetState",  # State tracking
        "Abandon",
        "BlankLine",
        "Cleanup",
        "DamageResolve",  # Engine internals
        "DelayedTrigger",
        "ImmediateTrigger",
        "Effect",  # Engine internals
        "ChangeTargets",  # Retargeting
    }
)


def _load_effect_verbs(db_path: Path) -> frozenset[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT event_class FROM card_ports WHERE port_type = 'effect'").fetchall()
    finally:
        conn.close()
    return frozenset({r[0] for r in rows if r[0]})


def test_effect_verb_inventory_matches_snapshot():
    """Fail with a clear diff when a new Forge verb appears or a known
    verb disappears. Run ``import_cardsfolder.py`` first to refresh the
    DB, then update ``KNOWN_EFFECT_VERBS`` if the diff is intentional."""
    db_path = Path("/tmp/synergy_full.db")
    if not db_path.exists():
        pytest.skip("/tmp/synergy_full.db not present — run import_cardsfolder first")

    current = _load_effect_verbs(db_path)
    added = current - KNOWN_EFFECT_VERBS
    removed = KNOWN_EFFECT_VERBS - current

    msg_parts = []
    if added:
        msg_parts.append(f"NEW verbs from Forge: {sorted(added)}")
    if removed:
        msg_parts.append(f"LOST verbs (matcher regression?): {sorted(removed)}")
    assert not msg_parts, "\n".join(msg_parts) + (
        "\n\nUpdate KNOWN_EFFECT_VERBS in tests/test_api_inventory.py "
        "after reviewing each diff entry. See the module docstring for "
        "the refresh snippet."
    )


def test_deliberately_ignored_verbs_are_in_known_set():
    """Sanity: every verb we deliberately ignore must ALSO be in the
    known-verb snapshot — otherwise the ignore list is drifting.
    """
    drift = DELIBERATELY_IGNORED - KNOWN_EFFECT_VERBS
    assert not drift, f"DELIBERATELY_IGNORED references verbs not in the snapshot: {sorted(drift)}"


def test_effect_verb_inventory_count_is_stable():
    """Cheap sanity check that runs without the DB — just asserts the
    known-set size matches a committed number so a stray paste can't
    silently shrink the snapshot."""
    assert len(KNOWN_EFFECT_VERBS) == 180
