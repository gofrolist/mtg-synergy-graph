"""Cross-submodule constants/regexes shared inside the utility package.

Each symbol here is used by functions living in 2+ submodules; keeping
them in one place avoids duplication while still letting individual
submodules stay focused on their own domain.
"""

from __future__ import annotations

import re

_SVAR_COMPARE_RE = re.compile(r"'(?:BranchCondition)?SVarCompare':\s*'([A-Z]+\d+)'")


_CHECK_SVAR_RE = re.compile(r"'(?:ConditionCheckSVar|CheckSVar|BranchConditionSVar)':\s*'([A-Z])'")


_HAND_SVAR_BINDING_RE = re.compile(r"SVar:([A-Z]):Count\$ValidHand Card\.YouOwn")


_UP_BIASED_SVAR_COMPARE_RE = re.compile(r"'SVarCompare':\s*'G[TE]")


_NUM_CARDS_RE = re.compile(r"'NumCards':\s*'([XYZ]|\d+)'")


_DAMAGE_AMP_RESULTS: frozenset[str] = frozenset(
    {
        "DmgTwice",
        "DmgTriple",
        "DmgPlus",
        "DmgPlus1",
        "DmgPlus2",
        "Dmg2",
        "Dmg3",
        "HarshDmg",
    }
)


_PING_TRIGGER_EVENTS: frozenset[str] = frozenset(
    {
        "SpellCast",
        "Phase",
        "LandPlayed",
        "TapsForMana",
        "Drawn",
        "Discarded",
        "Sacrificed",
        "ChangesZone",
    }
)
