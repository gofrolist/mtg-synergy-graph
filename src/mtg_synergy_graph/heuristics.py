"""Strategic heuristic rules (§7.3) and staples list (§7.4).

Strategic rules capture strategy-level patterns that port joins can't see:
"low-cost evasion for a combat-damage commander", "mass pump for a token
commander", etc. Each rule is a dict with:

* ``name``      — stable identifier (appears in score breakdowns)
* ``condition`` — ``Callable[[list[PortRow]], bool]`` against the commander's ports
* ``boost``     — ``Callable[[dict], bool]`` against a candidate record (``{"card": dict, "ports": list[PortRow]}``)
* ``weight``    — int, added to the ``strategic`` bucket when both fire
* ``reason``    — human-readable string written into ``contributing_ports``

Staples are a static lookup of format-level inclusions organised by colour
pip: their bonus is a flat ``STAPLE_SCORE`` (12 by default). §7.4 notes this
layer is curated manually; it's not meant to capture mechanical synergy.
"""

from __future__ import annotations

import json
from typing import Any

PortRow = dict[str, Any]
CandidateRecord = dict[str, Any]  # {"card": cards-row dict, "ports": list[PortRow]}

# ---------------------------------------------------------------------------
# §7.3 strategic rules
# ---------------------------------------------------------------------------


def _has_effect(ports: list[PortRow], event: str) -> bool:
    return any(p.get("port_type") == "effect" and p.get("event_class") == event for p in ports)


def _has_effect_in(ports: list[PortRow], events: frozenset[str]) -> bool:
    return any(p.get("port_type") == "effect" and (p.get("event_class") or "") in events for p in ports)


def _card_keywords(card: dict[str, Any]) -> list[str]:
    raw = card.get("keywords") or "[]"
    if isinstance(raw, list):
        return raw
    try:
        return list(json.loads(raw))
    except (ValueError, TypeError):
        return []


def _cmdr_wants_combat_damage(cmdr_ports: list[PortRow]) -> bool:
    return any(
        p.get("port_type") == "trigger"
        and p.get("event_class") == "DamageDone"
        and "Player" in (p.get("valid_filter") or "")
        for p in cmdr_ports
    )


#: Canonical evasion-keyword vocabulary. Single source of truth for what counts
#: as "evasion" across the scorer; also consumed by
#: ``penalties._bulk_load_evasion_*`` (the ``attack_reward_evasion`` tiers) so the
#: staple heuristic and the rule's candidate pools cannot drift.
EVASION_KEYWORDS: frozenset[str] = frozenset(
    {"Flying", "Menace", "Shadow", "Fear", "Intimidate", "Skulk", "Horsemanship"}
)


def _evasion_boost(cand: CandidateRecord) -> bool:
    card = cand["card"]
    try:
        cmc = float(card.get("cmc") or 0)
    except (TypeError, ValueError):
        cmc = 0
    if cmc > 3:
        return False
    keywords = set(_card_keywords(card))
    return bool(keywords & EVASION_KEYWORDS)


def _mass_pump_boost(cand: CandidateRecord) -> bool:
    return any(
        p.get("port_type") == "static"
        and p.get("event_class") == "Continuous"
        and "Creature" in (p.get("affected_scope") or "")
        for p in cand["ports"]
    )


def _selfmill_boost(cand: CandidateRecord) -> bool:
    return any(
        p.get("port_type") == "effect"
        and (p.get("event_class") or "") in {"Mill", "Discard"}
        and "You" in (p.get("valid_filter") or "")
        for p in cand["ports"]
    )


def _token_boost(cand: CandidateRecord) -> bool:
    return _has_effect(cand["ports"], "Token")


def _proliferate_boost(cand: CandidateRecord) -> bool:
    return _has_effect(cand["ports"], "Proliferate")


def _mana_sink_boost(cand: CandidateRecord) -> bool:
    """A *true* mana sink consumes generic mana proportional to ``X``.

    The previous implementation matched any cost port (every card with a
    tap-cost activated ability), which made the bucket fire on ~30% of the
    legal pool. We now require either:

    * a ``scales_with`` port whose expression names ``X`` (the activated
      ability scales with how much mana you pay), OR
    * an effect whose ``raw_line`` contains ``Cost$ X`` — Forge's marker for
      "pay X" activated abilities.
    """
    for p in cand["ports"]:
        scaling_expr = p.get("scaling_expression") or ""
        if p.get("port_type") == "scales_with" and "X" in scaling_expr:
            return True
        raw = p.get("raw_line") or ""
        if "Cost$ X" in raw or "Cost$X" in raw:
            return True
    return False


def _lki_scaling_boost(cand: CandidateRecord) -> bool:
    return any(
        p.get("port_type") == "scales_with" and "LKI" in (p.get("scaling_expression") or "") for p in cand["ports"]
    )


def _tribal_density_boost(cand: CandidateRecord) -> bool:
    return any(
        p.get("port_type") == "static"
        and "ValidCard" in (p.get("raw_line") or "")
        and "AddKeyword" in (p.get("raw_line") or "")
        for p in cand["ports"]
    )


# Phase D3 combat modifier helpers were removed alongside the reverted
# ``combat_modifier_for_attack_triggers`` rule — every tuning cost
# ~0.5pp Hi-Syn on the golden set for zero NDCG gain. The regression
# guard in tests/test_heuristics.py
# (``test_combat_modifier_rule_not_in_strategic_rules``) ensures the
# rule can't be re-introduced without being caught. If a future phase
# wants to resurrect the underlying vocabulary, the set to use is::
#
#     {"CantBlockBy", "MustAttack", "MustBlock"}
#
# — 350 / 97 / 8 corpus cards respectively.


def _anti_stax_self_hose(cand: CandidateRecord) -> bool:
    return any(
        p.get("port_type") in ("replacement", "static")
        and (p.get("event_class") or "") in {"CantActivate", "CantTap", "CantUntap"}
        for p in cand["ports"]
    )


StrategicRule = dict[str, Any]

# Module-level rule set. Declared as a tuple so a rogue ``.append()`` from
# a test or a script raises rather than silently mutating shared state.
STRATEGIC_RULES: tuple[StrategicRule, ...] = (
    {
        "name": "evasion_for_combat_damage",
        "condition": _cmdr_wants_combat_damage,
        "boost": _evasion_boost,
        "weight": 5,
        "reason": "Low-cost evasion enables combat damage triggers",
    },
    {
        "name": "mass_pump_for_tokens",
        "condition": lambda cmdr_ports: _has_effect(cmdr_ports, "Token"),
        "boost": _mass_pump_boost,
        "weight": 4,
        "reason": "Mass pump closes games with token armies",
    },
    {
        "name": "selfmill_for_graveyard",
        "condition": lambda cmdr_ports: any(
            p.get("port_type") == "trigger"
            and p.get("event_class") == "ChangesZone"
            and p.get("zone_destination") == "Graveyard"
            for p in cmdr_ports
        ),
        "boost": _selfmill_boost,
        "weight": 4,
        "reason": "Self-mill fills graveyard for commander synergy",
    },
    {
        "name": "tokens_for_sacrifice",
        "condition": lambda cmdr_ports: any(
            p.get("port_type") in ("trigger", "cost") and (p.get("event_class") or "") in {"Sacrificed", "sacrifice"}
            for p in cmdr_ports
        ),
        "boost": _token_boost,
        "weight": 5,
        "reason": "Token production provides sacrifice fodder",
    },
    {
        "name": "proliferate_for_counters",
        "condition": lambda cmdr_ports: any(
            p.get("port_type") in ("trigger", "effect")
            and (p.get("event_class") or "") in {"PutCounter", "CounterAdded", "PutCounterAll"}
            for p in cmdr_ports
        ),
        "boost": _proliferate_boost,
        "weight": 4,
        "reason": "Proliferate multiplies counter synergies",
    },
    {
        # Atraxa-class: commander proliferates → wants any card that PUTS
        # counters (so there's something to proliferate). The mirror of
        # proliferate_for_counters above. Low weight because the
        # PutCounter universe is huge — this is a tiebreaker, not a
        # primary signal.
        "name": "counters_for_proliferate",
        "condition": lambda cmdr_ports: any(
            p.get("port_type") in ("trigger", "effect") and (p.get("event_class") or "") == "Proliferate"
            for p in cmdr_ports
        ),
        "boost": lambda cand: any(
            p.get("port_type") == "effect" and (p.get("event_class") or "") in {"PutCounter", "PutCounterAll"}
            for p in cand["ports"]
        ),
        "weight": 2,
        "reason": "Counter producers feed the commander's proliferate",
    },
    {
        # ``Token`` was removed from the condition: half the legal pool
        # creates tokens, so it made the rule fire on every commander.
        # Treasure stays because Treasure decks (Korvold, Prosper) genuinely
        # accumulate spare mana that an X-cost sink can convert.
        "name": "mana_sink",
        "condition": lambda cmdr_ports: any(
            p.get("port_type") == "effect" and (p.get("event_class") or "") in {"Mana", "Treasure"} for p in cmdr_ports
        ),
        "boost": _mana_sink_boost,
        "weight": 4,
        "reason": "Commander generates excess resources; X-cost sinks convert them to value",
    },
    {
        # ``ChangesZone`` was removed: every commander has a
        # ``Card.Self ChangesZone Battlefield`` ETB trigger, so the rule
        # used to fire on essentially every commander. The intent is
        # "commander cares about permanents leaving the battlefield",
        # which is captured by sacrifice events alone.
        "name": "lki_scaling",
        "condition": lambda cmdr_ports: any(
            (p.get("event_class") or "") in {"Sacrifice", "Sacrificed"} for p in cmdr_ports
        ),
        "boost": _lki_scaling_boost,
        "weight": 4,
        "reason": "LKI scaling stays correct when permanents leave play",
    },
    {
        "name": "tribal_density",
        "condition": lambda cmdr_ports: any((p.get("valid_filter") or "").startswith("Creature") for p in cmdr_ports),
        "boost": _tribal_density_boost,
        "weight": 3,
        "reason": "Tribal support from noncreature static lines",
    },
    {
        "name": "anti_stax_activated_abilities",
        "condition": lambda cmdr_ports: _has_effect_in(cmdr_ports, frozenset({"Tap", "Untap", "Mana"})),
        "boost": _anti_stax_self_hose,
        "weight": -4,
        "reason": "Avoid self-hosing stax pieces for activated-ability commanders",
    },
)


def active_rules_for_commander(cmdr_ports: list[PortRow]) -> list[StrategicRule]:
    """Filter the rule set to the rules whose ``condition`` fires on this commander."""
    return [rule for rule in STRATEGIC_RULES if rule["condition"](cmdr_ports)]


def evaluate_strategic_rules(
    active: list[StrategicRule],
    cand: CandidateRecord,
) -> tuple[float, list[str]]:
    """Return ``(total_weight, reasons)`` for rules that fire on this candidate."""
    total = 0.0
    reasons: list[str] = []
    for rule in active:
        if rule["boost"](cand):
            total += rule["weight"]
            reasons.append(rule["reason"])
    return total, reasons


# ---------------------------------------------------------------------------
# §7.4 staples list
# ---------------------------------------------------------------------------

STAPLES: dict[str, tuple[str, ...]] = {
    "C": (
        "Sol Ring",
        "Arcane Signet",
        "Command Tower",
        "Thought Vessel",
        "Lightning Greaves",
        "Swiftfoot Boots",
    ),
    "W": (
        "Swords to Plowshares",
        "Path to Exile",
        "Smothering Tithe",
        "Teferi's Protection",
        "Generous Gift",
    ),
    "U": (
        "Rhystic Study",
        "Cyclonic Rift",
        "Counterspell",
        "Fierce Guardianship",
        "Mystic Remora",
    ),
    "B": (
        "Demonic Tutor",
        "Toxic Deluge",
        "Necropotence",
        "Feed the Swarm",
        "Bolas's Citadel",
    ),
    "R": (
        "Dockside Extortionist",
        "Deflecting Swat",
        "Chaos Warp",
        "Jeska's Will",
        "Blasphemous Act",
    ),
    "G": (
        "Beast Within",
        "Heroic Intervention",
        "Nature's Lore",
        "Sylvan Library",
        "Kodama's Reach",
    ),
}

#: Flat bonus applied when a candidate is a staple for one of the
#: commander's colours (§7.4). Large enough to appear in top ~100 but below
#: any mechanical synergy bucket.
STAPLE_SCORE: int = 12


def _normalise_colours(colours: set[str] | frozenset[str] | None) -> frozenset[str]:
    if not colours:
        return frozenset({"C"})
    return frozenset(c for c in colours if c)


def staple_bonus(card_name: str, commander_colours: set[str] | frozenset[str] | None) -> int:
    """Return the staple bonus for a candidate given a commander's colours.

    Always checks the colourless bucket in addition to the explicit colours —
    Sol Ring / Arcane Signet are staples regardless of identity.
    """
    colours = _normalise_colours(commander_colours) | {"C"}
    for pip in colours:
        if card_name in STAPLES.get(pip, ()):
            return STAPLE_SCORE
    return 0
