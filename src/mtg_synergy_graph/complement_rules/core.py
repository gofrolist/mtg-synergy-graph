"""Universal port complement matcher.

Replaces 27 hand-tuned scoring detectors with a single knowledge base of
~10 ``ComplementRule`` objects.  Each rule declares when a (commander_port,
candidate_port) pair creates a mechanical synergy (or anti-synergy).

Score = count of distinct complementary port pairs.  No weights, no penalties.
Different commanders with different ports automatically get different results.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..attributes import classify_attr_token, explode_filter
from ..graph_engine import (
    _PRODUCING_EFFECT_EVENTS,
    CATCH_ALL_TRIGGERS,
    COST_FEEDS_TRIGGER,
    EVENT_MATCH_MAP,
    REPLACEMENT_BLOCKS_TRIGGER,
    _always,
    _counters_compatible,
    _effect_produced_attrs,
    _trigger_only_matches_self,
    _zones_compatible,
    load_ports_for_set,
)
from ..penalties import CandidateCache, _token_subtype

PortRow = dict[str, Any]
EventCheck = Callable[[PortRow, PortRow], bool]


# ---------------------------------------------------------------------------
# §1  ComplementRule dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplementRule:
    """Declares when a (commander_port, candidate_port) pair is complementary.

    The matcher iterates commander ports of ``cmdr_port_type`` and candidate
    ports of ``cand_port_type``, checking ``event_pairs[cmdr_event][cand_event]``
    for compatibility.
    """

    rule_id: str
    cmdr_port_type: str  # port_type to match on commander side
    cand_port_type: str  # port_type to match on candidate side
    event_pairs: dict[str, dict[str, EventCheck]]
    direction: Literal["synergy", "anti_synergy"] = "synergy"


# ---------------------------------------------------------------------------
# §1c  Player-scope compatibility for player-centric trigger/effect pairs
# ---------------------------------------------------------------------------
# A commander trigger whose valid_filter names a specific player (OppCtrl,
# YouCtrl) should only match candidate effects that act on that same player.
# Tergrid's trigger fires on OPPONENT sacrificing; Lich's Tomb sacs YOUR
# permanents — no complement.

#: Candidate effect events where an empty valid_filter implies the controller
#: (you) performs / receives the effect. For Sacrifice / Discard / Draw /
#: GainLife, Forge's default actor is the ability's controller.
_IMPLICIT_YOU_EFFECT_DEFAULTS: frozenset[str] = frozenset({"Sacrifice", "Discard", "Draw", "GainLife"})


def _is_static_continuous(p: PortRow) -> bool:
    """True iff ``p`` is a ``static`` port with ``event_class`` ``Continuous``.

    Shared across gate helpers that scan commander statics looking for
    specific raw-line shapes (type-bending, MayPlay, GY-replay grants,
    vanilla-anchor detection). Centralising the pair check here keeps
    the port/event convention in one place if Forge ever renames either
    side.
    """
    return (p.get("port_type") or "").strip() == "static" and (p.get("event_class") or "").strip() == "Continuous"


#: Extracts the ``Affected`` clause value from a Forge static raw_line
#: (e.g. ``Creature.!token+YouCtrl``). Raw lines use ``repr``-ed dict
#: syntax with single-quoted keys, so a simple regex is sufficient.
_AFFECTED_CLAUSE_RE = re.compile(r"'Affected':\s*'([^']+)'")

#: Extracts the ``AddType`` clause value (e.g. ``Forest & Land``).
_ADD_TYPE_CLAUSE_RE = re.compile(r"'AddType':\s*'([^']+)'")

#: Extracts the ``ValidTarget`` clause value (self-protection statics,
#: e.g. ``Card.Self``).
_VALID_TARGET_CLAUSE_RE = re.compile(r"'ValidTarget':\s*'([^']+)'")


def _parse_scope_string(vf: str) -> str:
    """Classify a Forge valid_filter string as opp / you / any."""
    s = vf.strip()
    if not s:
        return "any"
    # Opponent markers take precedence over You (a filter like
    # "Card.YouCtrl+OppOwn" shouldn't exist, but if it did "opp" wins
    # because the event scope is the active player of the trigger).
    if "OppCtrl" in s or "OppOwn" in s or "Player.Opponent" in s or "+Opp" in s or s == "Opponent":
        return "opp"
    if "YouCtrl" in s or "YouOwn" in s or "+You" in s or "Player.You" in s or s == "You":
        return "you"
    # Bare "Player", "Each*", "Remembered", "Triggered*", "Targeted" —
    # each-player or context-dependent. Be permissive.
    return "any"


def _parse_trigger_scope(trigger_port: PortRow) -> str:
    """Classify the player-scope of a commander trigger port."""
    vf = trigger_port.get("valid_filter") or ""
    return _parse_scope_string(vf)


def _parse_cand_scope(cand_port: PortRow) -> str:
    """Classify the player-scope of a candidate effect port.

    For Sacrifice / Discard / Draw / GainLife effects with an empty
    valid_filter, the controller (you) is the default actor — this is the
    Lich's Tomb / Oath of Lim-Dûl pattern.
    """
    vf = (cand_port.get("valid_filter") or "").strip()
    if not vf:
        ev = (cand_port.get("event_class") or "").strip()
        if ev in _IMPLICIT_YOU_EFFECT_DEFAULTS:
            return "you"
        return "any"
    return _parse_scope_string(vf)


def _scope_compatible(cmdr_scope: str, cand_scope: str) -> bool:
    """True iff a commander trigger can fire on the candidate's effect.

    ``any`` on either side matches. Otherwise the scopes must agree.
    """
    if cmdr_scope == "any" or cand_scope == "any":
        return True
    return cmdr_scope == cand_scope


#: Player-centric trigger events where the commander's valid_filter names
#: a specific player (you / opp) and the candidate's effect filter names
#: who performs the corresponding action. For these events the scopes must
#: be compatible. Other events (ChangesZone, CounterAdded, DamageDone,
#: SpellCast, Attacks, etc.) have their own compatibility logic (zones,
#: counters, catch-all) and are left untouched.
_SCOPED_TRIGGER_EVENTS: frozenset[str] = frozenset({"Sacrificed", "Discarded", "Drawn", "LifeLost", "LifeGained"})


def _scoped_event_match_map() -> dict[str, dict[str, EventCheck]]:
    """Clone EVENT_MATCH_MAP and wrap player-centric entries with a
    commander-trigger × candidate-effect scope check.

    Only the events in ``_SCOPED_TRIGGER_EVENTS`` are wrapped — everything
    else uses the original check (zone compatibility, counter compatibility,
    or _always for catch-alls). EVENT_MATCH_MAP itself is not mutated
    because it is consumed by ``match_event`` in graph_engine.py where the
    simpler semantics are still desired.
    """
    out: dict[str, dict[str, EventCheck]] = {}
    for trig_ev, effects in EVENT_MATCH_MAP.items():
        if trig_ev not in _SCOPED_TRIGGER_EVENTS:
            out[trig_ev] = dict(effects)
            continue
        wrapped: dict[str, EventCheck] = {}
        for eff_ev, base_check in effects.items():
            wrapped[eff_ev] = _make_scoped_check(base_check)
        out[trig_ev] = wrapped
    return out


def _make_scoped_check(base: EventCheck) -> EventCheck:
    """Compose ``base`` with a trigger/effect scope compatibility check."""

    def _check(trigger: PortRow, effect: PortRow) -> bool:
        if not base(trigger, effect):
            return False
        return _scope_compatible(_parse_trigger_scope(trigger), _parse_cand_scope(effect))

    return _check


def _make_scoped_trigger_trigger_check(base: EventCheck) -> EventCheck:
    """Compose ``base`` with a scope check for trigger ↔ trigger rules.

    Both sides are triggers on a player-centric event. Both filters refer
    to the player whose action the trigger watches. Scopes must agree.
    """

    def _check(cmdr_trigger: PortRow, cand_trigger: PortRow) -> bool:
        if not base(cmdr_trigger, cand_trigger):
            return False
        return _scope_compatible(
            _parse_trigger_scope(cmdr_trigger),
            _parse_trigger_scope(cand_trigger),
        )

    return _check


# ---------------------------------------------------------------------------
# §2  Build the event-pair maps for each rule
# ---------------------------------------------------------------------------


def _invert_cost_feeds() -> dict[str, dict[str, EventCheck]]:
    """Invert COST_FEEDS_TRIGGER: trigger_event -> {cost_event -> check}.

    COST_FEEDS_TRIGGER is cost->trigger; we need trigger->cost for the
    complement rule where the commander has a trigger and we look for
    candidate costs that feed it.

    For player-centric events (Sacrificed, Discarded, LifeLost) the check
    also rejects opp-scoped commander triggers — a Forge cost is always
    paid by the activator (you), so an opponent-only trigger cannot fire
    from any candidate cost.
    """
    out: dict[str, dict[str, EventCheck]] = {}
    for cost_ev, trigger_evs in COST_FEEDS_TRIGGER.items():
        for trig_ev in trigger_evs:
            if trig_ev in _SCOPED_TRIGGER_EVENTS:
                out.setdefault(trig_ev, {})[cost_ev] = _reject_opp_trigger
            else:
                out.setdefault(trig_ev, {})[cost_ev] = _always
    return out


def _reject_opp_trigger(trigger: PortRow, _cost_port: PortRow) -> bool:
    """Reject commander triggers scoped to OppCtrl / OppOwn for cost feeders.

    Costs are paid by the activator (you), so no candidate cost can cause
    an opponent-scoped trigger to fire.
    """
    return _parse_trigger_scope(trigger) != "opp"


def _cand_trigger_not_self(_cmdr_port: PortRow, cand_port: PortRow) -> bool:
    """Reject candidate triggers that only fire on Self.

    Used by effect_feeds_trigger: a candidate with 'Card.Self' ETB trigger
    only fires on itself entering, not on tokens the commander creates.
    """
    vf = cand_port.get("valid_filter") or ""
    return not _trigger_only_matches_self(vf)


def _invert_event_match_map() -> dict[str, dict[str, EventCheck]]:
    """Invert EVENT_MATCH_MAP: effect_event -> {trigger_event -> check}.

    EVENT_MATCH_MAP is trigger->effect; this produces effect->trigger for
    the rule where the commander has an **effect** and we look for
    candidate **triggers** that fire from it.

    Skip catch-all trigger events (SpellCast, Attacks, etc.)
    on the candidate side -- these match everything and would flood results.
    Also skip candidate triggers that only fire on Self (Card.Self filter).

    Check functions have their arguments swapped since the original checks
    are (trigger, effect) but here cmdr=effect and cand=trigger.
    """

    def _not_self_and_zones(e: PortRow, t: PortRow) -> bool:
        return _cand_trigger_not_self(e, t) and _zones_compatible(t, e)

    def _not_self_and_counters(e: PortRow, t: PortRow) -> bool:
        return _cand_trigger_not_self(e, t) and _counters_compatible(t, e)

    # Zone-change effects produce 900-6600 matches -- handled by the
    # filter-aware _find_effect_feeds_etb() function instead.
    _EXCLUDED_EFFECT_EVENTS = frozenset(
        {
            "ChangeZone",
            "ChangeZoneAll",  # -> ChangesZone (1000+ matches)
            "Token",
            "CopyPermanent",
            "Animate",  # -> ChangesZone (900+)
            "DealDamage",
            "DamageAll",  # -> DamageDone (247, dilutes Niv-Mizzet)
            "Draw",  # -> Drawn (121, dilutes Tuvasa/Sythis)
        }
    )

    out: dict[str, dict[str, EventCheck]] = {}
    for trig_ev, effects in EVENT_MATCH_MAP.items():
        if trig_ev in CATCH_ALL_TRIGGERS:
            continue
        if "*" in effects:
            continue
        for eff_ev, check in effects.items():
            if eff_ev in _EXCLUDED_EFFECT_EVENTS:
                continue
            # Build a check that (1) rejects self-only candidate triggers
            # and (2) applies the original compatibility check with swapped args
            fn: EventCheck
            if check is _always:
                fn = _cand_trigger_not_self
            elif check is _zones_compatible:
                fn = _not_self_and_zones
            elif check is _counters_compatible:
                fn = _not_self_and_counters
            else:

                def _make_combined(e: dict, t: dict, _c: Any = check) -> bool:
                    return _cand_trigger_not_self(e, t) and _c(t, e)

                fn = _make_combined

            out.setdefault(eff_ev, {})[trig_ev] = fn
    return out


#: Replacement results that amplify the event rather than prevent it.
#: These should NOT be treated as anti-synergy -- Gisela's DmgTwice
#: doubles damage triggers, not blocks them.
_AMPLIFIER_REPLACEMENT_RESULTS: frozenset[str] = frozenset(
    {
        "DmgTwice",
        "DmgTriple",
        "DmgPlus1",
        "DmgPlus2",
        "DmgPlus",
        "Dmg2",
        "Dmg3",
        "CreateToken",
        # DBReplace is used for token enhancements (Chatterfang, Xorn) and
        # damage halving (Gisela) -- neither is a true anti-synergy blocker
        "DBReplace",
    }
)


def _not_amplifier(_cmdr_port: PortRow, cand_port: PortRow) -> bool:
    """Reject replacement ports that amplify rather than block."""
    repl_result = (cand_port.get("replacement_result") or "").strip()
    return repl_result not in _AMPLIFIER_REPLACEMENT_RESULTS


def _invert_replacement_blocks() -> dict[str, dict[str, EventCheck]]:
    """Invert REPLACEMENT_BLOCKS_TRIGGER: trigger_event -> {repl_event -> check}.

    REPLACEMENT_BLOCKS_TRIGGER maps replacement_event -> trigger_events.
    We need trigger_event -> replacement_event for the complement rule.

    Excludes ``ChangesZone -> Moved`` because zone-change replacements
    require zone-aware filtering (which zone is being replaced?) that
    the simple port-pair matcher can't express. Without filtering, every
    zone-change replacement card becomes anti-synergy for every
    ChangesZone-trigger commander -- a massive false positive.
    """
    out: dict[str, dict[str, EventCheck]] = {}
    for repl_ev, trig_evs in REPLACEMENT_BLOCKS_TRIGGER.items():
        for trig_ev in trig_evs:
            # Skip ChangesZone <-> Moved -- needs zone-aware filtering
            if trig_ev == "ChangesZone" and repl_ev == "Moved":
                continue
            out.setdefault(trig_ev, {})[repl_ev] = _not_amplifier
    return out


# Resonant effects: same effect on commander and candidate compounds
_RESONANT_EFFECTS: frozenset[str] = frozenset(
    {
        "Proliferate",
        "Mill",
        "DigUntil",
        "Scry",
        "Surveil",
        "Untap",
        "TapOrUntap",
        "Investigate",
        "BecomeMonarch",
    }
)

_RESONANT_EFFECT_FAMILY: dict[str, frozenset[str]] = {
    "DigUntil": frozenset({"DigUntil", "Mill"}),
    "Untap": frozenset({"Untap", "UntapAll", "TapOrUntap"}),
    "TapOrUntap": frozenset({"Untap", "UntapAll", "TapOrUntap", "Tap", "TapAll"}),
}

# Replacement resonance: commander effect -> candidate replacement that doubles it
_REPLACEMENT_RESONANCE: dict[str, dict[str, EventCheck]] = {
    "PutCounter": {"AddCounter": _always},
    "PutCounterAll": {"AddCounter": _always},
    "Proliferate": {"AddCounter": _always},
    "Token": {"CreateToken": _always},
    "Draw": {"Draw": _always},
    "GainLife": {"GainLife": _always},
    "Mana": {"ProduceMana": _always},
}

# Reverse: commander replacement -> candidate effect that produces the input
_REPLACEMENT_WANTS_PRODUCER: dict[str, dict[str, EventCheck]] = {
    "CreateToken": {"Token": _always},
    "AddCounter": {"PutCounter": _always, "PutCounterAll": _always, "Proliferate": _always, "MultiplyCounter": _always},
    "Draw": {"Draw": _always},
    "DrawCards": {"Draw": _always},
    "GainLife": {"GainLife": _always},
    "ProduceMana": {"Mana": _always},
    "Mill": {"Mill": _always},  # Bruvac doubles mill -> find millers
}


#: Base card types recognised at the head of a ``ChangeZone`` valid_filter.
#: ``Permanent`` expands to every permanent type; ``Card`` to everything.
#: Runtime tokens (``TriggeredCard``, ``DelayTriggerRemembered*``,
#: ``Targeted``, ``Card.Self``) and empty filters are rejected — they
#: don't identify a card-type family.
_CHANGEZONE_PERMANENT_TYPES: frozenset[str] = frozenset(
    {"Artifact", "Creature", "Enchantment", "Land", "Planeswalker", "Battle"}
)
_CHANGEZONE_ALL_CARD_TYPES: frozenset[str] = _CHANGEZONE_PERMANENT_TYPES | frozenset({"Instant", "Sorcery", "Tribal"})
_CHANGEZONE_RUNTIME_HEADS: frozenset[str] = frozenset(
    {"TriggeredCard", "Targeted", "Remembered", "RememberedCard", "DelayTriggerRemembered", "DelayTriggerRememberedLKI"}
)

#: Splits a Forge valid_filter alt into its head token and qualifiers.
#: ``Card.Creature+cmcLE2+YouCtrl`` -> [``Card``, ``Creature``, ``cmcLE2``,
#: ``YouCtrl``]. Used by ``_changezone_type_set`` to find the specific
#: type qualifier sitting alongside scope and CMC qualifiers when the
#: head is ``Card``.
_CHANGEZONE_QUALIFIER_SPLIT_RE = re.compile(r"[.+]")


def _changezone_type_set(valid_filter: str | None) -> frozenset[str] | None:
    """Extract the card-type family a ``ChangeZone`` effect targets.

    Returns ``None`` when the filter is empty, runtime-bound, or
    ``Card.Self`` only — these don't describe a card-type family and
    therefore don't participate in resonance. A filter head of
    ``Permanent`` expands to the full permanent-type set.

    The ``Card.<Type>`` shape deserves special care: Forge uses it to
    mean "a <Type> card" (e.g. Ajani's ``Card.Creature+cmcLE2+YouCtrl``
    is a creature-card recursion, not universal). A bare ``Card`` or
    ``Card.<Scope>`` with no type qualifier is the universal Eternal-
    Witness pattern and expands to all card types.
    """
    s = (valid_filter or "").strip()
    if not s:
        return None
    types: set[str] = set()
    for alt in s.split(","):
        alt = alt.strip()
        if not alt or alt.startswith("Card.Self"):
            continue
        head = alt.split(".", 1)[0].split("+", 1)[0].strip()
        if not head:
            continue
        if head in _CHANGEZONE_RUNTIME_HEADS or head.startswith("DelayTrigger"):
            continue
        if head == "Permanent":
            types.update(_CHANGEZONE_PERMANENT_TYPES)
            continue
        if head == "Card":
            # Look for a specific type qualifier after the dot. The
            # remainder can contain +/ . separated tokens — Type
            # qualifiers sit alongside scope / CMC qualifiers.
            qualifiers = _CHANGEZONE_QUALIFIER_SPLIT_RE.split(alt)[1:]
            found_specific: set[str] = set()
            for token in qualifiers:
                tok = token.strip()
                if tok == "Permanent":
                    found_specific.update(_CHANGEZONE_PERMANENT_TYPES)
                elif tok in _CHANGEZONE_ALL_CARD_TYPES:
                    found_specific.add(tok)
            if found_specific:
                types.update(found_specific)
            else:
                types.update(_CHANGEZONE_ALL_CARD_TYPES)
            continue
        if head in _CHANGEZONE_ALL_CARD_TYPES:
            types.add(head)
    return frozenset(types) if types else None


def _changezone_resonance_check(cmdr: PortRow, cand: PortRow) -> bool:
    """Two ``ChangeZone`` effects resonate iff they move cards between
    the same zones AND their card-type families overlap.

    Canonical pair: Meren (Creature.YouOwn, Graveyard→Battlefield) ×
    Karmic Guide (Creature.YouCtrl, same zones) → both fire "creature
    from graveyard to battlefield" effects. Disallows Meren × Lord
    Windgrace (Land) — disjoint types — and Meren × Eternal Witness
    (Graveyard→Hand) — different destination. Runtime-bound filters
    (Tergrid's ``TriggeredCard``, Marchesa's
    ``DelayTriggerRememberedLKI``) return ``None`` from the type
    extractor and are silently excluded.
    """
    if (cmdr.get("zone_origin") or "").strip() != (cand.get("zone_origin") or "").strip():
        return False
    if (cmdr.get("zone_destination") or "").strip() != (cand.get("zone_destination") or "").strip():
        return False
    cmdr_types = _changezone_type_set(cmdr.get("valid_filter"))
    cand_types = _changezone_type_set(cand.get("valid_filter"))
    if cmdr_types is None or cand_types is None:
        return False
    return bool(cmdr_types & cand_types)


def _build_resonance_pairs() -> dict[str, dict[str, EventCheck]]:
    """effect_event -> {effect_event -> _always} for resonant effects.

    ``ChangeZone`` and ``ChangeZoneAll`` are added with a narrow
    zone+filter compatibility check so graveyard-recursion commanders
    (Meren, Sharuum) resonate with candidates doing the same thing
    (Karmic Guide, Sun Titan, Reveillark, Daretti) without polluting
    the bucket with mismatched zone pairs or card-type families.
    """
    out: dict[str, dict[str, EventCheck]] = {}
    for ev in _RESONANT_EFFECTS:
        family = _RESONANT_EFFECT_FAMILY.get(ev, frozenset({ev}))
        out[ev] = {fam_ev: _always for fam_ev in family}
    out["ChangeZone"] = {
        "ChangeZone": _changezone_resonance_check,
        "ChangeZoneAll": _changezone_resonance_check,
    }
    out["ChangeZoneAll"] = {
        "ChangeZone": _changezone_resonance_check,
        "ChangeZoneAll": _changezone_resonance_check,
    }
    return out


def _build_trigger_resonance_pairs() -> dict[str, dict[str, EventCheck]]:
    """trigger_event -> {trigger_event -> check} for resonant triggers.

    Triggers on the same event resonate (both fire on the same game event).
    Exclude catch-all triggers (SpellCast, Attacks) and ChangesZone
    (handled by ETB-self with filter matching).

    For player-centric events (_SCOPED_TRIGGER_EVENTS), also enforce scope
    compatibility on both sides: opp-scoped commander × you-scoped
    candidate don't watch the same game event.
    """
    _RESONANT_TRIGGERS = frozenset(
        {
            "Sacrificed",
            "CounterAdded",
            "DamageDone",
            "LifeGained",
            "Discarded",
            "Drawn",
            "Taps",
            "Untaps",
            "TapsForMana",
            "LifeLost",
            "Proliferate",
            "Investigated",
            "Surveil",
        }
    )
    out: dict[str, dict[str, EventCheck]] = {}
    for ev in _RESONANT_TRIGGERS:
        if ev in _SCOPED_TRIGGER_EVENTS:
            out[ev] = {ev: _make_scoped_trigger_trigger_check(_always)}
        else:
            out[ev] = {ev: _always}
    return out


def _build_sacrifice_cluster_pairs() -> dict[str, dict[str, EventCheck]]:
    """Death-axis clustering: commander triggers on sacrifice/death events ->
    find candidates that ALSO trigger on death events (Blood Artist pattern).

    Also matches commander sacrifice triggers -> candidate death-related
    effects (Zulaport Cutthroat's LoseLife on opponent when creature dies).

    Enforces scope compatibility: Sacrificed and LifeLost are both in
    _SCOPED_TRIGGER_EVENTS, so an opp-scoped commander trigger is paired
    only with candidates whose scope also covers opponents.
    """
    _DEATH_TRIGGERS = frozenset({"Sacrificed", "LifeLost"})
    _DEATH_EFFECTS = frozenset({"Sacrifice", "SacrificeAll"})
    out: dict[str, dict[str, EventCheck]] = {}
    for trig in _DEATH_TRIGGERS:
        targets: dict[str, EventCheck] = {}
        # Other death triggers (payoff cluster) — trigger↔trigger scope
        for t2 in _DEATH_TRIGGERS:
            targets[t2] = _make_scoped_trigger_trigger_check(_always)
        # Death effects on candidates — trigger↔effect scope
        for eff in _DEATH_EFFECTS:
            targets[eff] = _make_scoped_check(_always)
        out[trig] = targets
    return out


# ---------------------------------------------------------------------------
# §3  The 10 complement rules
# ---------------------------------------------------------------------------


COMPLEMENT_RULES: tuple[ComplementRule, ...] = (
    # 1. Commander trigger -> candidate effect (core synergy engine)
    ComplementRule(
        rule_id="trigger_effect",
        cmdr_port_type="trigger",
        cand_port_type="effect",
        event_pairs=_scoped_event_match_map(),
    ),
    # 2. Commander trigger -> candidate cost feeds it
    ComplementRule(
        rule_id="cost_feeds_trigger",
        cmdr_port_type="trigger",
        cand_port_type="cost",
        event_pairs=_invert_cost_feeds(),
    ),
    # 3. Commander trigger -> candidate trigger (shared axis)
    ComplementRule(
        rule_id="trigger_resonance",
        cmdr_port_type="trigger",
        cand_port_type="trigger",
        event_pairs=_build_trigger_resonance_pairs(),
    ),
    # 4. Commander effect -> candidate effect (resonance)
    ComplementRule(
        rule_id="effect_resonance",
        cmdr_port_type="effect",
        cand_port_type="effect",
        event_pairs=_build_resonance_pairs(),
    ),
    # 5. Commander effect -> candidate replacement doubles it
    ComplementRule(
        rule_id="replacement_resonance",
        cmdr_port_type="effect",
        cand_port_type="replacement",
        event_pairs=_REPLACEMENT_RESONANCE,
    ),
    # 6. Commander replacement -> candidate effect produces what it doubles
    ComplementRule(
        rule_id="replacement_producer",
        cmdr_port_type="replacement",
        cand_port_type="effect",
        event_pairs=_REPLACEMENT_WANTS_PRODUCER,
    ),
    # 7. Candidate replacement blocks commander trigger (anti-synergy)
    ComplementRule(
        rule_id="replacement_blocks",
        cmdr_port_type="trigger",
        cand_port_type="replacement",
        event_pairs=_invert_replacement_blocks(),
        direction="anti_synergy",
    ),
    # 8. Sacrifice/death cluster: commander death trigger -> candidate also
    #    triggers on or produces death events (Blood Artist, Zulaport)
    ComplementRule(
        rule_id="sacrifice_cluster",
        cmdr_port_type="trigger",
        cand_port_type="trigger",
        event_pairs=_build_sacrifice_cluster_pairs(),
    ),
    # 9. Commander effect -> candidate trigger fires from it
    #    (e.g. commander makes tokens -> candidate triggers on ChangesZone)
    ComplementRule(
        rule_id="effect_feeds_trigger",
        cmdr_port_type="effect",
        cand_port_type="trigger",
        event_pairs=_invert_event_match_map(),
    ),
)


# ---------------------------------------------------------------------------
# §4  PortComplement -- one matched pair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortComplement:
    """A single complementary (commander_port, candidate_port) pair."""

    rule_id: str
    direction: Literal["synergy", "anti_synergy"]
    candidate: str  # candidate card name
    cmdr_event: str  # commander port event_class
    cand_event: str  # candidate port event_class
    filter_group: str = ""  # filter context from commander's valid_filter
    branch_kind: str = "root"


# ---------------------------------------------------------------------------
# §5  Universal matcher
# ---------------------------------------------------------------------------


#: Commander trigger event -> stax static events that block the strategy.
_STAX_MAP: list[tuple[frozenset[str], tuple[str, ...]]] = [
    (frozenset({"ChangesZone"}), ("DisableTriggers",)),
    (frozenset({"Sacrificed"}), ("CantSacrifice",)),
    (frozenset({"SpellCast"}), ("RaiseCost", "CantBeCast")),
    (frozenset({"LifeGained"}), ("CantGainLife",)),
    (frozenset({"Drawn"}), ("CantDraw",)),
    (frozenset({"CounterAdded"}), ("CantPutCounter",)),
]


def _build_stax_exclusion(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    candidate_cache: CandidateCache | None = None,
) -> set[str]:
    """Build set of cards to globally exclude -- stax pieces that actively
    hurt this commander's strategy.

    - DisableTriggers (Torpor Orb) excluded for ETB-trigger commanders
    - CantSacrifice (Angel of Jubilation) excluded for sacrifice commanders
    - RaiseCost/CantBeCast excluded for spell-trigger commanders

    If ``candidate_cache`` is provided, every stax-axis lookup reads from
    ``candidate_cache.stax_cards_by_event`` instead of issuing fresh SQL,
    saving up to six ``SELECT DISTINCT`` roundtrips per call.
    """
    excluded: set[str] = set()

    # Detect commander strategy axes from trigger events
    cmdr_trigger_events: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() == "trigger":
            cmdr_trigger_events.add((p.get("event_class") or "").strip())

    for trigger_axes, stax_events in _STAX_MAP:
        if not (cmdr_trigger_events & trigger_axes):
            continue
        if candidate_cache is not None:
            for ev in stax_events:
                excluded.update(candidate_cache.stax_cards_by_event.get(ev, frozenset()))
        else:
            # Safety: ph is "?,?,..." placeholders only; stax_events are
            # internal string constants from _STAX_MAP, never user input.
            ph = ",".join("?" * len(stax_events))
            excluded.update(
                r["card_name"]
                for r in conn.execute(
                    f"SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'static' AND event_class IN ({ph})",
                    stax_events,
                ).fetchall()
            )

    return excluded


def _filter_cmdr_port(port: PortRow) -> bool:
    """Skip commander ports that shouldn't produce matches.

    Same exclusions as find_trigger_feeders: self-only triggers,
    catch-all triggers (SpellCast, Attacks etc. that match everything),
    and opponent-scoped triggers.
    """
    pt = (port.get("port_type") or "").strip()
    if pt == "trigger":
        ev = (port.get("event_class") or "").strip()
        if ev in CATCH_ALL_TRIGGERS:
            return False
        vf = port.get("valid_filter") or ""
        if _trigger_only_matches_self(vf):
            return False
    return True


#: Trigger event classes that indicate a non-creature primary strategy.
#: Used by gate 4 in _commander_subtypes_from_ports to suppress tribal
#: density for incidental token producers (Locust God makes Insects but
#: cares about Drawn, not Insect tribal).
_NONCREATURE_TRIGGERS: frozenset[str] = frozenset({"Drawn", "SpellCast", "DamageDone", "CounterAdded"})


def _has_any_noncreature_trigger(cmdr_ports: list[PortRow]) -> bool:
    """Return True if commander has any non-creature trigger (Drawn, SpellCast, etc.)."""
    return any(
        (p.get("port_type") or "").strip() == "trigger"
        and (p.get("event_class") or "").strip() in _NONCREATURE_TRIGGERS
        for p in cmdr_ports
    )


#: Base card types (not creature subtypes). Used when extracting
#: creature-subtype identity from filter strings or MayPlay ``Affected``
#: fields — these tokens are recognised card types, not tribes.
_BASE_TYPES: frozenset[str] = frozenset(
    {
        "Creature",
        "Artifact",
        "Enchantment",
        "Land",
        "Planeswalker",
        "Card",
        "Permanent",
        "Spell",
        "Aura",
        "Equipment",
        "Instant",
        "Sorcery",
        "Battle",
        "Vehicle",
    }
)


#: Base types whose presence signals a *competing* non-creature
#: archetype. Used by Gate 5 in ``_commander_subtypes_from_ports`` to
#: suppress incidental token subtypes on artifact/enchantment-focused
#: commanders (Urza's Constructs, Breya's Thopters, Jan Jansen's
#: Treasures — all token flavor, not tribal strategy).
_NON_CREATURE_ARCHETYPE_TYPES: frozenset[str] = frozenset({"Artifact", "Enchantment"})

#: Minimum number of commander ports that must reference a
#: ``_NON_CREATURE_ARCHETYPE_TYPES`` token before Gate 5 considers the
#: commander to have a competing archetype. Two is empirical: one
#: artifact reference is common on incidental cards, two indicates an
#: intentional artifact engine.
_COMPETING_ARCHETYPE_THRESHOLD: int = 2


def _commander_subtypes_from_ports(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    cmdr_ports: list[PortRow],
) -> set[str]:
    """Extract commander subtypes that are mechanically relevant.

    Includes:
    1. Literal subtypes that appear in the commander's port data
       (same logic as find_lord_matches in graph_engine.py)
    2. Token subtypes from TokenScript (Slimefoot creates Saprolings
       but is literally a Fungus -- the tribal axis is Saproling)
    """
    rows = conn.execute(
        "SELECT subtypes, types FROM cards WHERE name IN ({})".format(",".join("?" * len(commander_set))),
        tuple(commander_set),
    ).fetchall()
    literal: set[str] = set()
    _cmdr_is_planeswalker = False
    for r in rows:
        if "Planeswalker" in (r["types"] or ""):
            _cmdr_is_planeswalker = True
        if r["subtypes"]:
            literal.update(r["subtypes"].split())

    # Keep literal subtypes mentioned in port data
    relevant: set[str] = set()
    for p in cmdr_ports:
        haystack = " ".join(
            [
                p.get("valid_filter") or "",
                p.get("affected_scope") or "",
                str(p.get("raw_line") or ""),
            ]
        )
        for sub in literal:
            if sub in haystack:
                relevant.add(sub)

    # Extract subtypes from MayPlay statics (Gisa and Geralf grant
    # MayPlay for Zombie.YouCtrl from graveyard — "Zombie" is the tribal
    # identity even though the card itself is Human Wizard).
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "static" and ev == "Continuous":
            raw = str(p.get("raw_line") or "")
            if "'MayPlay'" not in raw:
                continue
            m = re.search(r"'Affected':\s*'([^']+)'", raw)
            if not m:
                continue
            for part in m.group(1).split(","):
                base = part.strip().split(".")[0].split("+")[0].strip()
                # Only include creature subtypes (not a base type)
                if base and base[0].isupper() and base not in _BASE_TYPES:
                    relevant.add(base)

    # Extract token subtypes from TokenScript -- but only when the
    # commander genuinely cares about that creature type.
    #
    # Three gates (any passes -> include):
    # 1. Token subtype matches a literal subtype (Krenko IS a Goblin)
    # 2. Token subtype appears in some port filter (Slimefoot triggers
    #    on Saproling.YouCtrl)
    # 3. Commander does NOT sacrifice the specific token type as fuel
    #    (Kykar sacs Spirits -> Spirits are fuel, not the strategy)
    #
    # Exclusion: planeswalker ultimates produce tokens that are almost
    # never the strategy (Windgrace's -11 makes Cats but he cares about
    # lands). Detected via LOYALTY cost on the token-producing ability.
    sac_fuel_subtypes: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() == "cost":
            cs = p.get("cost_subtype") or ""
            # e.g. "1/Spirit" -> extract "Spirit"
            parts = cs.split("/")
            if len(parts) >= 2 and parts[-1][0:1].isupper():
                sac_fuel_subtypes.add(parts[-1])

    # Precompute whether the commander has a competing non-creature
    # archetype. For Urza/Breya/Jan Jansen the actual strategy is
    # Artifact / Treasure / combo; their ETB-created Construct/Thopter
    # tokens are flavor, not tribal. Detected as at least
    # ``_COMPETING_ARCHETYPE_THRESHOLD`` ports referencing a base type
    # other than Creature (Artifact, Enchantment, …).
    archetype_type_refs = 0
    for p in cmdr_ports:
        vf = p.get("valid_filter") or ""
        cs = p.get("cost_subtype") or ""
        raw = str(p.get("raw_line") or "")
        haystack = f"{vf} {cs} {raw}"
        for t in _NON_CREATURE_ARCHETYPE_TYPES:
            # Word-boundary match: ``"Artifact" in haystack`` alone
            # would also match ``nonArtifact`` / ``CounterArtifact``
            # identifiers in Forge scripts. Guard against false
            # positives even though the current DB happens to be safe.
            if re.search(rf"\b{t}\b", haystack):
                archetype_type_refs += 1
                break  # count each port at most once
    has_competing_archetype = archetype_type_refs >= _COMPETING_ARCHETYPE_THRESHOLD

    for p in cmdr_ports:
        ev = (p.get("event_class") or "").strip()
        if ev == "Token":
            raw = str(p.get("raw_line") or "")
            m = re.search(r"'TokenScript':\s*'([^']+)'", raw)
            if m:
                sub = _token_subtype(m.group(1))
                if not sub:
                    continue
                # Gate 1: literal subtype match (Krenko IS a Goblin)
                if sub in literal:
                    relevant.add(sub)
                    continue
                # Gate 2: token subtype mentioned in some port filter
                # (Slimefoot has Saproling in a ChangesZone trigger)
                mentioned = False
                for p2 in cmdr_ports:
                    haystack = " ".join(
                        [
                            p2.get("valid_filter") or "",
                            p2.get("affected_scope") or "",
                        ]
                    )
                    if sub in haystack:
                        mentioned = True
                        break
                if mentioned:
                    relevant.add(sub)
                    continue
                # Gate 3: commander does NOT sacrifice this specific type
                # as fuel. If it does (Kykar sacs Spirits), skip -- tokens
                # are currency, not the strategy. Also skip planeswalker
                # token production (almost always ult/minus, incidental).
                if sub in sac_fuel_subtypes:
                    continue
                if _cmdr_is_planeswalker:
                    continue
                # Gate 4: skip when tokens are incidental byproduct of
                # another strategy. Locust God triggers on Drawn and makes
                # Insects, but doesn't care about Insects being Insects.
                # Detect: the Token effect is produced by a non-creature
                # trigger (Drawn, SpellCast, DamageDone) and the commander
                # has no other connection to the token subtype.
                if _has_any_noncreature_trigger(cmdr_ports):
                    continue
                # Gate 5: skip when the commander has a competing non-
                # creature archetype (≥2 Artifact/Enchantment port refs)
                # AND the token subtype didn't pass Gate 1 or Gate 2.
                # Urza's Constructs, Breya's Thopters, Jan Jansen's
                # Treasures/Constructs are flavor byproducts of an
                # artifact strategy — they match 200+ creatures in the
                # DB and bury actual artifact-synergy picks (tutors,
                # untap-combo pieces) under a tribal_density floor.
                if has_competing_archetype:
                    continue
                # Passed all gates -- token IS the strategy
                relevant.add(sub)

    return relevant


def _cost_filter_group(cost_port: PortRow) -> str:
    """Classify a cost port for IDF sub-grouping.

    Splits sacrifice costs into meaningful sub-pools:

    * ``free_outlet`` — sacrifices other permanents with no mana cost
      (Viscera Seer, Ashnod's Altar). N≈305, IDF≈0.12.
    * ``paid_outlet`` — sacrifices other permanents but requires mana
      (Attrition, Birthing Pod). N≈400, IDF≈0.11.
    * ``self_sac`` — sacrifices itself (Spore Frog, Sakura-Tribe Elder).
      N≈1064, IDF≈0.10.

    Non-sacrifice costs return empty string (no sub-grouping).
    """
    ev = (cost_port.get("event_class") or "").strip()
    if ev != "sacrifice":
        return ""
    target = (cost_port.get("cost_target") or "").strip()
    if target == "self":
        return "self_sac"
    # For 'any' and 'other': check if activation requires mana
    raw = str(cost_port.get("raw_line") or "")
    # Remove the Sac<...> portion; check remaining for mana symbols
    without_sac = re.sub(r"Sac<[^>]+>", "", raw).strip()
    # Remove tap cost (T) — tap is free
    without_tap = re.sub(r"\bT\b", "", without_sac).strip()
    # Any remaining digit or WUBRG means mana is required
    if re.search(r"[0-9WUBRGP]", without_tap):
        return "paid_outlet"
    return "free_outlet"


# ---------------------------------------------------------------------------
# Submodule imports (placed after PortRow/PortComplement/_commander_subtypes
# are defined to avoid circular imports — submodules import from this module)
# ---------------------------------------------------------------------------

from .combat import (  # noqa: E402
    _find_attack_payoffs,
    _find_changeszone_resonance,
    _find_combat_enhancers,
    _find_evasion_complements,
    _find_sacrifice_outlets,
    _find_subject_zone_feeders,
)
from .density import (  # noqa: E402
    _find_cheat_cmc_bonus,
    _find_cost_reduction_targets,
    _find_counter_doubler_synergy,
    _find_counter_keyword_synergy,
    _find_etb_self_complements,
    _find_land_to_gy_synergy,
    _find_lord_complements,
    _find_proliferate_synergy,
    _find_scales_with_density,
    _find_scaling_complements,
    _find_spellcast_density_complements,
    _find_spellcast_resonance,
    _find_toughness_matters,
    _find_tribal_density_complements,
    _find_value_engine_density,
)

# Plan 003 Units 7 & 8: all 16 auto-generated tribal /
# replacement-stack rules migrated to declarative rows in
# data/rules_seed.json. The ``generated/`` subpackage is empty now;
# the interpreter (port_graph.interpreter.RuleInterpreter) handles
# every migrated rule via DECLARATIVE_RULE_IDS in registry.py.
from .graveyard import (  # noqa: E402
    _find_artifact_recursion,
    _find_copy_synergy,
    _find_dies_drain,
    _find_graveyard_fillers,
    _find_graveyard_sac_value,
    _find_gy_loader,
)
from .panharmonicon import (  # noqa: E402
    _find_panharmonicon_complements,
    _find_panharmonicon_stacking,
    _find_reverse_panharmonicon,
)
from .statics import (  # noqa: E402
    _find_affinity_archetype,
    _find_cost_reduction_synergy,
    _find_edict_feeders,
    _find_graveyard_play_synergy,
)
from .tokens import (  # noqa: E402
    _find_effect_feeds_etb,
    _find_static_strategy,
    _find_token_etb_damage,
    _find_token_producers_for_trigger,
)
from .utility import (  # noqa: E402
    _find_cardpower_axis_feeders,
    _find_cascade_value,
    _find_cost_payoff_complements,
    _find_counter_axis_feeders,
    _find_counter_target_payoff,
    _find_creature_died_feeders,
    _find_creature_untap_engine,
    _find_creatures_as_lands_landfall,
    _find_damage_doubler_synergy,
    _find_etb_tapped_stax_feeders,
    _find_extra_land_plays,
    _find_flicker_payoffs,
    _find_flicker_synergy,
    _find_gy_fuel_feeders,
    _find_hand_size_feeders,
    _find_land_bounce_feeders,
    _find_landfall_enablers,
    _find_life_total_feeders,
    _find_lifegain_feeders,
    _find_mana_doubler_synergy,
    _find_modified_axis_feeders,
    _find_monarch_synergy,
    _find_multicolor_untap,
    _find_opponent_forcing,
    _find_party_feeders,
    _find_tap_type_feeders,
    _find_untap_combo,
    _find_untap_synergy,
    _find_wheel_synergy,
)


def _extract_filter_group(valid_filter: str) -> str:
    """Extract the most specific type/subtype from a port's valid_filter.

    Used to segment IDF groups: a commander triggering on
    ``Creature.Goblin.YouCtrl`` gets filter_group ``"Goblin"`` so its IDF
    is computed against only Goblin-producing candidates, not all creatures.

    Priority: subtype > type (more specific wins).
    Returns ``""`` when the filter is empty or has no useful type info.
    """
    if not valid_filter:
        return ""
    best_subtype = ""
    best_type = ""
    for attr in explode_filter(valid_filter):
        if attr["is_negated"]:
            continue
        kind = attr["attr_kind"]
        if kind == "subtype" and not best_subtype:
            best_subtype = attr["attr_value"]
        elif kind == "type" and not best_type:
            best_type = attr["attr_value"]
    return best_subtype or best_type


#: Effect events that cannot feed a combat-damage trigger. Burn spells
#: deal damage from a spell/ability source, not from a creature attacking.
_NON_COMBAT_DAMAGE_EFFECTS: frozenset[str] = frozenset({"DealDamage", "DamageAll"})

#: Filter groups that are too broad to usefully filter candidates.
_GENERIC_FILTER_GROUPS: frozenset[str] = frozenset({"Card", "Permanent", "Spell", "You", "OppOwn", "YouOwn"})


def _effect_matches_filter_group(effect_port: PortRow, filter_group: str) -> bool:
    """Check if a candidate effect produces something matching the filter_group.

    Only applied to producing effects (Token, ChangeZone, etc.). For
    non-producing effects, always returns True (no filtering).

    This is the candidate-side counterpart to ``_extract_filter_group``:
    the commander's trigger says "I care about Creatures" and this
    function checks "does this Token effect actually produce a Creature?"
    """
    ev = (effect_port.get("event_class") or "").strip()
    if ev not in _PRODUCING_EFFECT_EVENTS:
        return True

    produced = _effect_produced_attrs(effect_port)
    if produced is None:
        # Ambiguous — give benefit of the doubt
        return True
    if not produced:
        # Non-producing effect (e.g. Animate)
        return False

    # Classify filter_group to know what kind of attr to check
    fg_kind = classify_attr_token(filter_group)
    return (fg_kind, filter_group) in produced


def find_all_complements(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    rules: Sequence[ComplementRule] = COMPLEMENT_RULES,
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Find all port-pair complements between commander and candidate cards.

    Algorithm:
    1. Load commander ports (cached)
    2. Derive needed (cand_port_type, event_class) pairs from all rules
    3. Bulk-fetch candidate ports matching those pairs (1 SQL query)
    4. Index candidate ports for O(1) lookup
    5. For each rule, for each commander port, find matching candidates

    When ``candidate_cache`` is provided, helpers that would otherwise
    re-issue commander-independent SQL (stax exclusions, Panharmonicon
    statics, Token effects) read from the cache instead — saving ~30 %
    of wall time in a 100-commander batch run.
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    cmdr_set = set(commander_set)

    # Group commander ports by port_type
    cmdr_by_type: dict[str, list[PortRow]] = defaultdict(list)
    for p in cmdr_ports:
        if _filter_cmdr_port(p):
            pt = (p.get("port_type") or "").strip()
            cmdr_by_type[pt].append(p)

    # Build stax exclusion set -- cards that actively hurt this commander
    stax_excluded = _build_stax_exclusion(conn, cmdr_ports, candidate_cache)

    # Derive which (cand_port_type, event_class) pairs we need
    needed_cand: set[tuple[str, str]] = set()
    for rule in rules:
        cmdr_ports_for_rule = cmdr_by_type.get(rule.cmdr_port_type, [])
        for cp in cmdr_ports_for_rule:
            cmdr_ev = (cp.get("event_class") or "").strip()
            targets = rule.event_pairs.get(cmdr_ev)
            if targets is None:
                continue
            if "*" in targets:
                # Wildcard: need ALL candidate events of this port type
                needed_cand.add((rule.cand_port_type, "*"))
            else:
                for cand_ev in targets:
                    needed_cand.add((rule.cand_port_type, cand_ev))

    def _card_attr_complements() -> list[PortComplement]:
        out: list[PortComplement] = []
        out.extend(_find_lord_complements(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_etb_self_complements(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_scaling_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_spellcast_density_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_tribal_density_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_changeszone_resonance(conn, cmdr_ports, cmdr_set))
        out.extend(_find_effect_feeds_etb(conn, cmdr_ports, cmdr_set))
        out.extend(_find_sacrifice_outlets(conn, cmdr_ports, cmdr_set))
        out.extend(_find_subject_zone_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_panharmonicon_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_graveyard_fillers(conn, cmdr_ports, cmdr_set))
        out.extend(_find_extra_land_plays(conn, cmdr_ports, cmdr_set))
        out.extend(_find_landfall_enablers(conn, cmdr_ports, cmdr_set))
        out.extend(_find_creatures_as_lands_landfall(conn, cmdr_ports, cmdr_set))
        out.extend(_find_scales_with_density(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_flicker_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_cost_payoff_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_opponent_forcing(conn, cmdr_ports, cmdr_set))
        out.extend(_find_token_producers_for_trigger(conn, cmdr_ports, cmdr_set))
        out.extend(_find_static_strategy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_combat_enhancers(conn, cmdr_ports, cmdr_set))
        out.extend(_find_attack_payoffs(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_wheel_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_artifact_recursion(conn, cmdr_ports, cmdr_set))
        out.extend(_find_copy_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_token_etb_damage(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_dies_drain(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_gy_loader(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_reverse_panharmonicon(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_panharmonicon_stacking(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_evasion_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_spellcast_resonance(conn, cmdr_ports, cmdr_set))
        out.extend(_find_untap_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_untap_combo(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_multicolor_untap(conn, cmdr_ports, cmdr_set))
        out.extend(_find_cost_reduction_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_graveyard_play_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_affinity_archetype(conn, cmdr_ports, cmdr_set))
        out.extend(_find_edict_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_value_engine_density(conn, cmdr_ports, cmdr_set))
        out.extend(_find_cheat_cmc_bonus(conn, cmdr_ports, cmdr_set))
        out.extend(_find_counter_doubler_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_counter_keyword_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_proliferate_synergy(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_mana_doubler_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_graveyard_sac_value(conn, cmdr_ports, cmdr_set))
        out.extend(_find_cost_reduction_targets(conn, cmdr_ports, cmdr_set, candidate_cache))
        out.extend(_find_land_to_gy_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_toughness_matters(conn, cmdr_ports, cmdr_set))
        out.extend(_find_cascade_value(conn, cmdr_ports, cmdr_set))
        out.extend(_find_flicker_payoffs(conn, cmdr_ports, cmdr_set))
        out.extend(_find_monarch_synergy(conn, cmdr_ports, cmdr_set))
        out.extend(_find_counter_target_payoff(conn, cmdr_ports, cmdr_set))
        out.extend(_find_creature_untap_engine(conn, cmdr_ports, cmdr_set))
        out.extend(_find_counter_axis_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_modified_axis_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_cardpower_axis_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_tap_type_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_hand_size_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_gy_fuel_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_lifegain_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_life_total_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_land_bounce_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_etb_tapped_stax_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_party_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_creature_died_feeders(conn, cmdr_ports, cmdr_set))
        out.extend(_find_damage_doubler_synergy(conn, cmdr_ports, cmdr_set))
        # Plan 003 Units 7 & 8: all 16 auto-generated keyword /
        # replacement-stack tribal rules migrated to declarative
        # rows. The interpreter block just below this comment
        # executes every rule_id in DECLARATIVE_RULE_IDS.
        # Declarative rules (plan 003 Unit 5) — the interpreter is the
        # single entry point for any rule_id in DECLARATIVE_RULE_IDS.
        # Placed inside _card_attr_complements so every caller path
        # (including the two early returns in find_all_complements
        # below) picks up the interpreter output. Empty
        # DECLARATIVE_RULE_IDS short-circuits cleanly.
        from .registry import DECLARATIVE_RULE_IDS  # local import: avoids cycle

        if DECLARATIVE_RULE_IDS:
            from ..port_graph.interpreter import RuleInterpreter

            interpreter = RuleInterpreter(conn)
            out.extend(interpreter.find_complements(conn, cmdr_ports, cmdr_set))

        return out

    if not needed_cand:
        return _card_attr_complements()

    # Build SQL query for candidate ports
    has_wildcard_types: set[str] = set()
    specific_pairs: list[tuple[str, str]] = []
    for pt, ev in needed_cand:
        if ev == "*":
            has_wildcard_types.add(pt)
        else:
            specific_pairs.append((pt, ev))

    # Construct WHERE clause
    conditions = []
    params: list[str] = []
    if has_wildcard_types:
        placeholders = ",".join("?" * len(has_wildcard_types))
        conditions.append(f"port_type IN ({placeholders})")
        params.extend(sorted(has_wildcard_types))
    if specific_pairs:
        pair_conds = " OR ".join("(port_type = ? AND event_class = ?)" for _ in specific_pairs)
        conditions.append(f"({pair_conds})")
        for pt, ev in specific_pairs:
            params.extend([pt, ev])

    if not conditions:
        return _card_attr_complements()

    # Safety: conditions are string literals like "port_type IN (?,?)" or
    # "(port_type = ? AND event_class = ?)" built from internal rule data.
    # All values are passed via params; no user input is interpolated.
    sql = (
        "SELECT card_name, port_type, event_class, valid_filter, "
        "zone_origin, zone_destination, counter_type, branch_kind, "
        "is_conditional, replacement_event, replacement_result, amount, "
        "cost_target, raw_line "
        f"FROM card_ports WHERE {' OR '.join(conditions)}"
    )
    rows = conn.execute(sql, params).fetchall()

    # Index candidate ports: (port_type, event_class) -> [ports]
    # Filter out self-referential effect ports (valid_filter = Self/Imprinted/
    # Remembered) -- these move the card itself between zones and never
    # create cross-card synergy. Timmerian Fiends has 20+ such ports.
    _SELF_EFFECT_FILTERS = frozenset({"Self", "Imprinted", "Remembered"})
    cand_index: dict[tuple[str, str], list[PortRow]] = defaultdict(list)
    for r in rows:
        card = r["card_name"]
        if card in cmdr_set or card in stax_excluded:
            continue
        pt = (r["port_type"] or "").strip()
        ev = (r["event_class"] or "").strip()
        # Skip self-referential effects
        if pt == "effect":
            vf = (r["valid_filter"] or "").strip()
            if vf in _SELF_EFFECT_FILTERS:
                continue
        cand_index[(pt, ev)].append(dict(r))

    # Match: for each rule, for each commander port, look up candidates
    results: list[PortComplement] = []
    seen: set[tuple[str, str, str, str]] = set()  # (rule_id, cmdr_ev, cand_card, cand_ev)

    for rule in rules:
        cmdr_ports_for_rule = cmdr_by_type.get(rule.cmdr_port_type, [])
        for cp in cmdr_ports_for_rule:
            cmdr_ev = (cp.get("event_class") or "").strip()
            if not cmdr_ev:
                continue
            targets = rule.event_pairs.get(cmdr_ev)
            if targets is None:
                continue

            # Extract filter context for IDF segmentation and candidate filtering
            fg = _extract_filter_group(cp.get("valid_filter") or "")
            # Only apply candidate-side filter for meaningful filter groups
            apply_cand_filter = fg != "" and fg not in _GENERIC_FILTER_GROUPS and rule.cand_port_type == "effect"
            # Combat-damage triggers (is_combat=1) cannot be fed by
            # DealDamage/DamageAll effects — burn spells don't cause
            # creatures to deal combat damage.
            is_combat = bool(cp.get("is_combat"))

            # For cost_feeds_trigger, enrich filter_group with cost
            # metadata (free_outlet / paid_outlet / self_sac) to create
            # sub-IDF groups within the broad sacrifice pool.
            enrich_cost = rule.rule_id == "cost_feeds_trigger"

            # Effect-conditional suffix: when the commander trigger's
            # execute has a runtime condition (Selvala's power compare,
            # Meren's XP-vs-CMC), matches into trigger_effect are tagged
            # with ":cond" so the IDF computer can halve their weight —
            # the trigger fires broadly but the payoff is gated, so most
            # matches don't pan out in practice.
            cond_suffix = ":cond" if rule.rule_id == "trigger_effect" and cp.get("effect_conditional") else ""

            if "*" in targets:
                # Wildcard: check every candidate port of this type
                check = targets["*"]
                for (pt, ev), cand_ports in cand_index.items():
                    if pt != rule.cand_port_type:
                        continue
                    if is_combat and ev in _NON_COMBAT_DAMAGE_EFFECTS:
                        continue
                    for cand_p in cand_ports:
                        key = (rule.rule_id, cmdr_ev, cand_p["card_name"], ev)
                        if key in seen:
                            continue
                        if check(cp, cand_p):
                            if apply_cand_filter and not _effect_matches_filter_group(cand_p, fg):
                                continue
                            seen.add(key)
                            cfp = _cost_filter_group(cand_p) if enrich_cost else ""
                            enriched_fg = f"{fg}_{cfp}" if fg and cfp else (fg or cfp)
                            if cond_suffix:
                                enriched_fg = f"{enriched_fg}{cond_suffix}" if enriched_fg else cond_suffix
                            results.append(
                                PortComplement(
                                    rule_id=rule.rule_id,
                                    direction=rule.direction,
                                    candidate=cand_p["card_name"],
                                    cmdr_event=cmdr_ev,
                                    cand_event=ev,
                                    filter_group=enriched_fg,
                                    branch_kind=(cand_p.get("branch_kind") or "root"),
                                )
                            )
            else:
                for cand_ev, check in targets.items():
                    if is_combat and cand_ev in _NON_COMBAT_DAMAGE_EFFECTS:
                        continue
                    cand_ports = cand_index.get((rule.cand_port_type, cand_ev), [])
                    for cand_p in cand_ports:
                        key = (rule.rule_id, cmdr_ev, cand_p["card_name"], cand_ev)
                        if key in seen:
                            continue
                        if check(cp, cand_p):
                            if apply_cand_filter and not _effect_matches_filter_group(cand_p, fg):
                                continue
                            seen.add(key)
                            cfp = _cost_filter_group(cand_p) if enrich_cost else ""
                            enriched_fg = f"{fg}_{cfp}" if fg and cfp else (fg or cfp)
                            if cond_suffix:
                                enriched_fg = f"{enriched_fg}{cond_suffix}" if enriched_fg else cond_suffix
                            results.append(
                                PortComplement(
                                    rule_id=rule.rule_id,
                                    direction=rule.direction,
                                    candidate=cand_p["card_name"],
                                    cmdr_event=cmdr_ev,
                                    cand_event=cand_ev,
                                    filter_group=enriched_fg,
                                    branch_kind=(cand_p.get("branch_kind") or "root"),
                                )
                            )

    # -- Card-attribute rules -----------------------------------------------
    results.extend(_card_attr_complements())

    return results
