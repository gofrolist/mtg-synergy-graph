"""Rule-gate registry for per-port attribution in the auditor.

Each ``RuleGate`` declares: "rule R consumes any commander port that
matches ``predicate(port) -> bool``". The auditor uses the registry
to attribute coverage per-port instead of per-commander — a multi-
port commander's `effect Pump` port can now be flagged as uncovered
even when their `trigger Attacks` port fires `combat_enhancer`.

Two sources populate ``RULE_GATES``:

1. **Formal rules**: auto-registered from ``COMPLEMENT_RULES``. Each
   rule's gate is "port_type matches AND event_class in event_pairs".
   These are exact and free — the rule already declares its
   structure.

2. **Card-attribute rules**: hand-registered by the rule's author
   when the gate is non-trivial (qualifier extraction, raw_line
   inspection, replacement_result classification, etc.). Adding a
   gate to the registry alongside the rule's helper function is
   the contract for new rules — without it, the auditor falls back
   to commander-level attribution and the rule's coverage is
   approximate.

Rules without a registered gate aren't broken — the auditor still
sees their PortComplement emissions and counts them at the cell
level. Registration just upgrades the accuracy from "probably
covered" to "definitely covered (this port)".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .core import COMPLEMENT_RULES, PortRow

# All 15 generated tribal + replacement-stack rules migrated to
# declarative rows in data/rules_seed.json (plan 003 Unit 8).
# Their per-port gate predicates are compiled at interpreter load
# time; auditor per-port attribution via interpreter.rule_ids is
# a deferred follow-up.


@dataclass(frozen=True)
class RuleGate:
    """Per-rule gate predicate against a single commander port row.

    ``predicate(port) -> True`` means "this port would activate the
    rule". The auditor walks every (commander, port) pair and for
    each rule that emitted a complement on that commander, checks the
    gate to attribute the activation to a specific port.
    """

    rule_id: str
    predicate: Callable[[PortRow], bool]


def _formal_gate(port_type: str, event_classes: frozenset[str]) -> Callable[[PortRow], bool]:
    """Build a gate from a formal ComplementRule's static structure."""

    def predicate(port: PortRow) -> bool:
        if (port.get("port_type") or "").strip() != port_type:
            return False
        return (port.get("event_class") or "").strip() in event_classes

    return predicate


def _formal_rule_gates() -> list[RuleGate]:
    """Auto-register every formal ComplementRule from event_pairs."""
    out: list[RuleGate] = []
    for rule in COMPLEMENT_RULES:
        events = frozenset(rule.event_pairs.keys())
        out.append(RuleGate(rule_id=rule.rule_id, predicate=_formal_gate(rule.cmdr_port_type, events)))
    return out


# ---------------------------------------------------------------------------
# Card-attribute rule gates (hand-registered)
# ---------------------------------------------------------------------------

#: Mirror of ``_DAMAGE_AMP_RESULTS`` in utility.py — re-declared here
#: so the registry is the single source of truth for the auditor and
#: the runtime can stay decoupled. If the runtime set changes, update
#: both (or refactor to share a constant module).
_DAMAGE_AMP_RESULTS: frozenset[str] = frozenset(
    {"DmgTwice", "DmgTriple", "DmgPlus", "DmgPlus1", "DmgPlus2", "Dmg2", "Dmg3", "HarshDmg"}
)


_MODIFIED_QUALIFIER_RE = re.compile(r"(?<![A-Za-z])modified(?![A-Za-z])")
_COUNTER_GATE_RE = re.compile(r"counters_GE\d*_[A-Z0-9]+")


def _damage_doubler_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "replacement":
        return False
    if (port.get("event_class") or "").strip() != "DamageDone":
        return False
    if (port.get("replacement_result") or "").strip() not in _DAMAGE_AMP_RESULTS:
        return False
    raw = str(port.get("raw_line") or "")
    if "'Prevent': 'True'" in raw or "'PreventionEffect': 'True'" in raw:
        return False
    # Opponent-targeting check — same logic as the runtime gate.
    m = re.search(r"'ValidTarget':\s*'([^']+)'", raw)
    if not m:
        return True
    val = m.group(1)
    return any(sub in val for sub in ("Opponent", "OppCtrl", "Player.Opp"))


def _modified_axis_gate(port: PortRow) -> bool:
    pt = (port.get("port_type") or "").strip()
    if pt not in ("trigger", "scales_with", "static", "effect", "cost", "replacement"):
        return False
    vf = (port.get("valid_filter") or "").strip()
    if vf and _MODIFIED_QUALIFIER_RE.search(vf):
        # Reject Self-anchored first OR-alt (mirror runtime).
        first_alt = vf.split(",")[0]
        alt_tokens = first_alt.replace("+", ".").split(".")
        if not any(tok.lstrip("!").strip() == "Self" for tok in alt_tokens):
            return True
    raw = str(port.get("raw_line") or "")
    return bool(raw and _MODIFIED_QUALIFIER_RE.search(raw))


def _cardpower_axis_gate(port: PortRow) -> bool:
    """Mirror the runtime gate of ``_find_cardpower_axis_feeders``.

    Fires on ``scales_with.CardPower`` ports. ``Count$CardPower`` resolves
    to the commander's own power, so no Self-qualification filter is
    needed — the port type itself is the signal.
    """
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return (port.get("event_class") or "").strip() == "CardPower"


def _tap_type_feeder_gate(port: PortRow) -> bool:
    """Mirror the runtime gate of ``_find_tap_type_feeders``.

    Fires on ``cost.tap_type`` ports (``tapXType<N/SUBJECT>`` costs).
    The port type/event pair uniquely identifies the archetype.
    """
    if (port.get("port_type") or "").strip() != "cost":
        return False
    return (port.get("event_class") or "").strip() == "tap_type"


def _lifegain_feeder_gate(port: PortRow) -> bool:
    """Mirror the runtime gate of ``_find_lifegain_feeders``.

    Fires on ``scales_with LifeYouGainedThisTurn`` ports. The axis
    is monotonic-positive (every such commander wants more lifegain)
    so no small-side rejection filter is needed.
    """
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return (port.get("event_class") or "").strip() == "LifeYouGainedThisTurn"


def _life_total_feeder_gate(port: PortRow) -> bool:
    """Mirror the per-port signature of ``_find_life_total_feeders``.

    Fires on ``scales_with YourLifeTotal`` ports. The helper additionally
    requires commander-level up-bias (a GainLife replacement amp OR a
    static.Continuous with ``SVarCompare`` starting ``GT``/``GE``), which
    is a cmdr-wide AND-check not expressible in a single-port predicate.
    Every Bilbo/Elenda port on this axis still passes the port-level
    gate; the helper filters at call time. Gate-miss auditing over the
    golden set remains meaningful (no overlap with the 8 YourLifeTotal
    cmdrs anyway).
    """
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return (port.get("event_class") or "").strip() == "YourLifeTotal"


def _land_bounce_feeder_gate(port: PortRow) -> bool:
    """Mirror the runtime gate of ``_find_land_bounce_feeders``.

    Fires on ``cost.return`` ports whose ``cost_subtype`` is ``<N>/Land*``
    and whose ``cost_target`` is ``any`` (external land, not self-
    bounce). Self-bounce commanders (Rootha / Shigeki / Bilbo returning
    themselves) are a different archetype and are excluded.
    """
    if (port.get("port_type") or "").strip() != "cost":
        return False
    if (port.get("event_class") or "").strip() != "return":
        return False
    if (port.get("cost_target") or "").strip() != "any":
        return False
    cst = (port.get("cost_subtype") or "").strip()
    parts = cst.split("/")
    return len(parts) >= 2 and parts[1] == "Land"


def _creature_died_feeder_gate(port: PortRow) -> bool:
    """Mirror the runtime gate of ``_find_creature_died_feeders``.

    Fires on any ``scales_with`` port whose ``event_class`` starts
    with ``ThisTurnEntered_Graveyard_from_Battlefield_Creature`` —
    aristocrats-archetype commanders whose payoff scales with the
    creature-death count this turn. Covers all Forge filter variants
    (.YouCtrl / .YouOwn / .!token / .!namedX).
    """
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    ec = (port.get("event_class") or "").strip()
    return ec.startswith("ThisTurnEntered_Graveyard_from_Battlefield_Creature")


def _gy_fuel_feeder_gate(port: PortRow) -> bool:
    """Mirror the runtime gate of ``_find_gy_fuel_feeders``.

    Fires on ``cost.exile_from_grave`` ports where ``cost_target``
    is ``any`` (not ``self``). Self-target costs (Wilson, Symbiote
    Spider-Man, Venom, Morbius, Beetle, Spider-Slayer, Tocasia)
    are a different archetype — escape-style recursion — and this
    rule isn't aimed at them.
    """
    if (port.get("port_type") or "").strip() != "cost":
        return False
    if (port.get("event_class") or "").strip() != "exile_from_grave":
        return False
    return (port.get("cost_target") or "").strip() == "any"


def _hand_size_feeder_gate(port: PortRow) -> bool:
    """Mirror the runtime gate of ``_find_hand_size_feeders``.

    Fires on ``scales_with ValidHand Card.YouOwn`` ports. The
    small-hand rejection (LE0/LE1/EQ0/GE2/GE3 compare on the
    hand-binding SVar) lives in the runtime helper because it
    inspects other ports on the same commander; at the per-port
    level the registry can't see that context, so this gate is
    permissive (covers all hand-size commanders for coverage-audit
    purposes). The runtime still correctly rejects Hazoret / Neheb /
    Djeru-and-Hazoret / Flubs.
    """
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return (port.get("event_class") or "").strip() == "ValidHand Card.YouOwn"


def _counter_axis_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() not in ("trigger", "scales_with", "static"):
        return False
    vf = (port.get("valid_filter") or "").strip()
    if not vf or "counters_GE" not in vf:
        return False
    first_alt = vf.split(",")[0]
    alt_tokens = first_alt.replace("+", ".").split(".")
    return not any(tok.lstrip("!").strip() == "Self" for tok in alt_tokens)


def _opponent_forcing_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    ev = (port.get("event_class") or "").strip()
    if ev not in ("Discarded", "Sacrificed", "Drawn"):
        return False
    vf = port.get("valid_filter") or ""
    return any(t in vf for t in ("OppCtrl", "OppOwn", "Opponent", "Player.Opp", "EachPlayer"))


def _wheel_synergy_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    return (port.get("event_class") or "").strip() == "Drawn"


def _mana_doubler_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    return (port.get("event_class") or "").strip() == "TapsForMana"


def _extra_land_plays_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "static":
        return False
    return (port.get("event_class") or "").strip() == "AdjustLandPlays"


def _cost_payoff_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "cost":
        return False
    if (port.get("event_class") or "").strip() != "discard":
        return False
    cs = port.get("cost_subtype") or ""
    parts = cs.split("/")
    if len(parts) < 2:
        return False
    return parts[1] not in ("Card", "Hand", "CARDNAME", "NICKNAME")


def _etb_self_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    if (port.get("event_class") or "").strip() != "ChangesZone":
        return False
    if (port.get("zone_destination") or "").strip() != "Battlefield":
        return False
    vf = port.get("valid_filter") or ""
    return "Self" in vf or vf == ""


def _combat_enhancer_gate(port: PortRow) -> bool:
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt != "trigger":
        return False
    if ev == "DamageDone" and port.get("is_combat"):
        return True
    if ev == "Attacks":
        vf = port.get("valid_filter") or ""
        return "Self" in vf
    return False


def _attack_payoff_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    return (port.get("event_class") or "").strip() in ("Attacks", "AttackersDeclared")


def _panharmonicon_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "static":
        return False
    return (port.get("event_class") or "").strip() == "Panharmonicon"


def _voltron_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "keyword":
        return False
    return (port.get("event_class") or "").strip() in ("Hexproof", "Exalted", "Shroud", "Trample")


def _evasion_gate(port: PortRow) -> bool:
    """combat_enhancer-adjacent: commander triggers on combat damage
    AND wants to make their attacker unblockable."""
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    ev = (port.get("event_class") or "").strip()
    return bool(ev == "DamageDone" and port.get("is_combat"))


def _token_etb_damage_gate(port: PortRow) -> bool:
    """Commander has a Token-creating effect AND ETB-damage trigger
    pattern. The runtime checks this aggregately — gate uses the
    Token effect as the discriminating signal."""
    if (port.get("port_type") or "").strip() != "effect":
        return False
    return (port.get("event_class") or "").strip() == "Token"


def _graveyard_play_gate(port: PortRow) -> bool:
    """Commander has effects that interact with the graveyard
    (cast-from-grave, GY-replay grants, mass return). Gate fires on
    static GY-replay grants OR on ChangeZone effects from Graveyard."""
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if (
        pt == "effect"
        and ev in ("ChangeZone", "ChangeZoneAll", "Play", "PlayLand")
        and (port.get("zone_origin") or "").strip() == "Graveyard"
    ):
        return True
    if pt == "static" and ev == "Continuous":
        raw = str(port.get("raw_line") or "")
        if "AddKeyword" in raw and any(
            k in raw for k in ("Unearth", "Embalm", "Eternalize", "Encore", "Escape", "Flashback", "Jump-start")
        ):
            return True
    return False


def _gy_loader_gate(port: PortRow) -> bool:
    """Commander has self-mill / discard / GY-fill effect AND wants
    things in the graveyard. Gate matches Mill / Discard effects."""
    if (port.get("port_type") or "").strip() != "effect":
        return False
    ev = (port.get("event_class") or "").strip()
    return ev in ("Mill", "Discard", "DigUntil", "ChangeZone")


def _cost_reduction_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "static":
        return False
    return (port.get("event_class") or "").strip() == "ReduceCost"


def _spellcast_density_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    return (port.get("event_class") or "").strip() == "SpellCast"


# ---------------------------------------------------------------------------
# Batch 2: registry sweep (2026-04-18) — one-line gates per helper
# ---------------------------------------------------------------------------

_PRIMARY_TYPES: frozenset[str] = frozenset(
    {"Creature", "Artifact", "Enchantment", "Planeswalker", "Land", "Instant", "Sorcery"}
)

_TYPE_TOKENS: frozenset[str] = frozenset(
    {"Aura", "Equipment", "Enchantment", "Artifact", "Land", "Instant", "Sorcery", "Planeswalker"}
)


def _artifact_recursion_gate(port: PortRow) -> bool:
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt != "effect":
        return False
    vf = port.get("valid_filter") or ""
    raw = str(port.get("raw_line") or "")
    if ev == "ChangeZone" and (port.get("zone_origin") or "").strip() == "Graveyard":
        return "Artifact" in vf or "Artifact" in raw
    if ev == "CopyPermanent":
        return "Artifact" in vf or "Artifact" in raw
    return False


def _copy_synergy_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "effect":
        return False
    if (port.get("event_class") or "").strip() != "CopyPermanent":
        return False
    vf = port.get("valid_filter") or ""
    raw = str(port.get("raw_line") or "")
    return "Populate" in raw or "Creature" in vf or "Creature" in raw


def _dies_drain_gate(port: PortRow) -> bool:
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt == "trigger" and ev == "ChangesZone":
        if (port.get("zone_origin") or "").strip() != "Battlefield":
            return False
        if (port.get("zone_destination") or "").strip() != "Graveyard":
            return False
        vf = port.get("valid_filter") or ""
        main = vf.split(",")[0].split(".")[0].split("+")[0].strip()
        return main in ("Creature", "Permanent") or (main == "Card" and "P1P1" in vf)
    if pt == "static" and ev == "Panharmonicon":
        raw = str(port.get("raw_line") or "")
        return (
            "'Origin': 'Battlefield'" in raw
            and "'Destination': 'Graveyard'" in raw
            and "'ValidCause': 'Creature" in raw
        )
    return False


def _subject_zone_feeder_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    if (port.get("event_class") or "").strip() != "ChangesZone":
        return False
    if (port.get("zone_destination") or "").strip() != "Graveyard":
        return False
    vf = port.get("valid_filter") or ""
    main = vf.split(",")[0].split(".")[0].split("+")[0].strip()
    return main in ("Land", "Artifact", "Enchantment", "Planeswalker")


def _cost_reducer_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    if (port.get("event_class") or "").strip() != "SpellCast":
        return False
    vf = port.get("valid_filter") or ""
    return any(
        t in vf
        for t in ("Instant", "Sorcery", "Creature", "Artifact", "Enchantment", "Aura", "Equipment", "nonCreature")
    )


def _edict_feeder_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    ev = (port.get("event_class") or "").strip()
    if ev == "Sacrificed":
        return True
    if ev != "ChangesZone":
        return False
    if (port.get("zone_destination") or "").strip() != "Graveyard":
        return False
    vf = port.get("valid_filter") or ""
    # Approximate _trigger_only_matches_self: reject Card.Self main token.
    first_alt = vf.split(",")[0]
    alt_tokens = first_alt.replace("+", ".").split(".")
    return not any(tok.lstrip("!").strip() == "Self" for tok in alt_tokens)


def _landfall_enabler_gate(port: PortRow) -> bool:
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt == "trigger" and ev == "LandPlayed":
        return True
    if pt == "trigger" and ev == "ChangesZone":
        vf = port.get("valid_filter") or ""
        return "Land" in vf
    if pt == "effect" and ev == "ChangeZone":
        zo = (port.get("zone_origin") or "").strip()
        zd = (port.get("zone_destination") or "").strip()
        vf = port.get("valid_filter") or ""
        raw = str(port.get("raw_line") or "")
        return zo == "Graveyard" and zd == "Battlefield" and ("Land" in vf or "Land" in raw)
    return False


def _multicolor_untap_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "effect":
        return False
    return (port.get("event_class") or "").strip() in ("TapOrUntap", "Untap", "UntapAll")


def _untap_synergy_gate(port: PortRow) -> bool:
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt == "cost" and ev == "tap":
        return True
    return pt == "effect" and ev in ("TapOrUntap", "Untap")


def _cascade_value_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "keyword":
        return False
    return "Cascade" in (port.get("event_class") or "")


def _token_producer_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    if (port.get("event_class") or "").strip() != "ChangesZone":
        return False
    if (port.get("zone_destination") or "").strip() != "Battlefield":
        return False
    vf = port.get("valid_filter") or ""
    if "!token" in vf.lower():
        return False
    first_alt = vf.split(",")[0]
    alt_tokens = first_alt.replace("+", ".").split(".")
    if any(tok.lstrip("!").strip() == "Self" for tok in alt_tokens):
        return False
    main = vf.split(",")[0].split(".")[0].split("+")[0].strip()
    return main == "Creature"


def _scaling_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    raw = str(port.get("raw_line") or "")
    vf = port.get("valid_filter") or ""
    return any(t in raw or t in vf for t in _TYPE_TOKENS)


_COUNTER_GATE_P1P1_RE = re.compile(r"counters_\w*P1P1")


def _has_counter_interest(port: PortRow) -> bool:
    """Single-port shape that signals P1P1 counter interest — matches
    counter_doubler / counter_keyword / proliferate_synergy gates.
    """
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt == "trigger" and ev in ("CounterAdded", "CounterAddedOnce", "CounterAddedAll"):
        ct = (port.get("counter_type") or "").strip()
        return ct in ("", "P1P1")
    if pt == "scales_with":
        return "P1P1" in ev or "P1P1" in (port.get("valid_filter") or "")
    if pt == "trigger":
        return "P1P1" in (port.get("valid_filter") or "")
    if pt == "effect" and ev in ("PutCounter", "PutCounterAll"):
        if (port.get("counter_type") or "").strip() != "P1P1":
            return False
        vf = port.get("valid_filter") or ""
        return bool(vf) and "Other" in vf
    return False


def _proliferate_synergy_gate(port: PortRow) -> bool:
    pt = (port.get("port_type") or "").strip()
    ev = (port.get("event_class") or "").strip()
    if pt == "scales_with" and "Experience" in ev:
        return True
    if pt == "effect" and ev == "MultiplyCounter":
        return True
    if ev == "Proliferate":
        return True
    return _has_counter_interest(port)


def _counter_target_payoff_gate(port: PortRow) -> bool:
    """Ezuri-style XP-counter scaler. Gate on scales_with experience."""
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return "Experience" in (port.get("event_class") or "")


def _zone_resonance_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "trigger":
        return False
    if (port.get("event_class") or "").strip() != "ChangesZone":
        return False
    vf = port.get("valid_filter") or ""
    if not vf:
        return False
    first_alt = vf.split(",")[0]
    alt_tokens = first_alt.replace("+", ".").split(".")
    if any(tok.lstrip("!").strip() == "Self" for tok in alt_tokens):
        return False
    main = vf.split(",")[0].split(".")[0].split("+")[0].strip()
    return main in _PRIMARY_TYPES


def _exalted_density_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "keyword":
        return False
    return (port.get("event_class") or "").strip() == "Exalted"


def _aura_equipment_support_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return "Equipment" in (port.get("valid_filter") or "")


def _land_to_gy_gate(port: PortRow) -> bool:
    """Commander has a land-to-graveyard trigger (Gitrog, Titania Voice).

    Delegates the filter check to
    ``density._filter_has_land_base_type`` so the gate and helper share
    one source of truth — a bare ``"Land" in valid_filter`` substring
    test would false-positive on ``Creature.nonLand+YouCtrl`` (Princess
    Yue) and other non-land reanimator filters.
    """
    from .density import _filter_has_land_base_type

    if (port.get("port_type") or "").strip() != "trigger":
        return False
    if (port.get("event_class") or "").strip() not in ("ChangesZoneAll", "ChangesZone"):
        return False
    if (port.get("zone_destination") or "").strip() != "Graveyard":
        return False
    return _filter_has_land_base_type((port.get("valid_filter") or "").strip())


_CARD_ATTR_GATES: tuple[RuleGate, ...] = (
    # Batch 1
    RuleGate("damage_doubler_synergy", _damage_doubler_gate),
    RuleGate("modified_axis_feeder", _modified_axis_gate),
    RuleGate("counter_axis_feeder", _counter_axis_gate),
    RuleGate("cardpower_axis_feeder", _cardpower_axis_gate),
    RuleGate("tap_type_feeder", _tap_type_feeder_gate),
    RuleGate("hand_size_feeder", _hand_size_feeder_gate),
    RuleGate("gy_fuel_feeder", _gy_fuel_feeder_gate),
    RuleGate("lifegain_feeder", _lifegain_feeder_gate),
    RuleGate("life_total_feeder", _life_total_feeder_gate),
    RuleGate("land_bounce_feeder", _land_bounce_feeder_gate),
    RuleGate("creature_died_feeder", _creature_died_feeder_gate),
    RuleGate("opponent_forcing", _opponent_forcing_gate),
    RuleGate("wheel_synergy", _wheel_synergy_gate),
    RuleGate("mana_doubler", _mana_doubler_gate),
    RuleGate("extra_land_plays", _extra_land_plays_gate),
    RuleGate("cost_payoff", _cost_payoff_gate),
    RuleGate("etb_self", _etb_self_gate),
    RuleGate("combat_enhancer", _combat_enhancer_gate),
    RuleGate("attack_payoffs", _attack_payoff_gate),
    RuleGate("panharmonicon", _panharmonicon_gate),
    RuleGate("voltron", _voltron_gate),
    RuleGate("evasion", _evasion_gate),
    RuleGate("token_etb_damage", _token_etb_damage_gate),
    RuleGate("graveyard_play", _graveyard_play_gate),
    RuleGate("gy_loader", _gy_loader_gate),
    RuleGate("cost_reduction_target", _cost_reduction_gate),
    RuleGate("land_to_gy_synergy", _land_to_gy_gate),
    RuleGate("spell_density", _spellcast_density_gate),
    RuleGate("spellcast_resonance", _spellcast_density_gate),
    # Batch 2 — registry sweep
    RuleGate("artifact_recursion", _artifact_recursion_gate),
    RuleGate("copy_synergy", _copy_synergy_gate),
    RuleGate("populate_stack", _copy_synergy_gate),  # same helper, second rule_id
    RuleGate("dies_drain", _dies_drain_gate),
    RuleGate("subject_zone_feeder", _subject_zone_feeder_gate),
    RuleGate("cost_reducer", _cost_reducer_gate),
    RuleGate("edict_feeder", _edict_feeder_gate),
    RuleGate("landfall_enabler", _landfall_enabler_gate),
    RuleGate("multicolor_untap", _multicolor_untap_gate),
    RuleGate("untap_synergy", _untap_synergy_gate),
    RuleGate("cascade_value", _cascade_value_gate),
    RuleGate("token_producer", _token_producer_gate),
    RuleGate("scaling", _scaling_gate),
    RuleGate("proliferate_synergy", _proliferate_synergy_gate),
    RuleGate("counter_doubler", _has_counter_interest),
    RuleGate("counter_keyword", _has_counter_interest),
    RuleGate("counter_target_payoff", _counter_target_payoff_gate),
    RuleGate("zone_resonance", _zone_resonance_gate),
    RuleGate("exalted_density", _exalted_density_gate),
    RuleGate("aura_equipment_support", _aura_equipment_support_gate),
    # Unit 8: all 15 tribal / replacement-stack rules migrated to
    # declarative rows. Previously enumerated per-rule RuleGate
    # entries here; now the interpreter's own gate predicates are
    # the canonical attribution source. Per-port auditor attribution
    # through the interpreter is a deferred follow-up item in plan
    # 003 Open Questions.
)


#: Rules whose activation depends on the commander's CARD attributes
#: (subtypes, name, oracle text, color identity) rather than on a
#: specific port's mechanical shape. Their firing tells us nothing
#: about per-port coverage: a Goblin commander's `tribal_density`
#: activation doesn't mean their ``trigger.DamageDone[Self]`` port is
#: covered, just that they happen to be on a tribal axis. The auditor
#: SUBTRACTS these from the unregistered-rule fallback so they don't
#: bloat per-signature activation rates.
CARD_LEVEL_RULES: frozenset[str] = frozenset(
    {
        # Match on the commander's literal subtypes from `cards`, not
        # on any port — Goblin / Zombie / Dragon tribal lord identity.
        "tribal_density",
        "lord",
        # Aggregate signals (multiple ports + card text features) that
        # don't cleanly map to a single port shape.
        "value_engine",
        "affinity_archetype",
        "static_strategy",
        # Rules that require a conjunction of TWO distinct ports on
        # the commander (e.g. cost.tap AND effect.Mana). Gating on
        # either port alone would over-attribute to commanders that
        # have one but not the other. Treating them as card-level
        # keeps the auditor honest: their firing tells us nothing
        # about per-port coverage.
        "creature_untap_engine",  # cost.tap + effect.Mana
        "flicker_synergy",  # ETB-self trigger + high-value effect
        "flicker_payoff",  # effect Battlefield→Exile + Exile→Battlefield
        "untap_combo",  # cost.tap + effect.Mana (TapsForMana branch is port-level but minor)
        "cheat_cmc",  # path B has commander-wide negative guard
        "scales_with_density",  # already runtime, no port-level gate
        # Type-bending statics that operate on the commander's whole
        # static set rather than a single port's gate condition.
        "creatures_as_lands_landfall",
        # Depth-2 self-bridging pathway (plan 2026-04-23-001). Requires
        # TWO distinct commander ports to match into the candidate's
        # port bag; gating on a single port would over-attribute.
        "self_bridging_cascade",
    }
)


RULE_GATES: tuple[RuleGate, ...] = tuple(_formal_rule_gates()) + _CARD_ATTR_GATES


def _load_declarative_rule_ids() -> frozenset[str]:
    """Derive :data:`DECLARATIVE_RULE_IDS` at import time from
    ``data/rules_seed.json``.

    Previously a hand-maintained literal, the set is now computed
    from the seed so adding / removing a rule in the JSON cannot
    drift from the interpreter-routing set. Only rules with
    ``active == 1`` (or missing, treated as 1) are included —
    inactive rows exist in the seed but should not route through
    the interpreter.

    Fails loudly if the seed is missing: without the seed the
    interpreter has no rules to run and routing would silently
    fall through to the (non-existent) Python-helper path for
    every migrated rule_id.
    """
    from ..port_graph._paths import default_seed_path

    try:
        seed_path: Path = default_seed_path("rules_seed.json")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "DECLARATIVE_RULE_IDS: data/rules_seed.json not found; "
            "interpreter routing cannot be initialised. Run the importer "
            "or restore the seed file before importing registry."
        ) from exc
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for row in data.get("rules", []):
        # Treat missing ``active`` as 1 (the seed schema default).
        if int(row.get("active", 1)) != 1:
            continue
        rule_id = row.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            continue
        ids.add(rule_id)
    return frozenset(ids)


#: Rule ids owned by the declarative interpreter (plan 003 Unit 5).
#:
#: Derived at module import from ``data/rules_seed.json`` (every
#: ``active == 1`` row's ``rule_id``). A rule_id appears in EXACTLY
#: ONE of this set or the Python-helper dispatch — the auditor at
#: ``find_all_complements`` call time refuses duplicates. Adding a
#: declarative rule is a one-step change: edit the JSON, re-import.
DECLARATIVE_RULE_IDS: frozenset[str] = _load_declarative_rule_ids()


def registered_rule_ids() -> frozenset[str]:
    """Set of rule_ids the registry can attribute to a specific port."""
    return frozenset(g.rule_id for g in RULE_GATES)


def attributable_rules_for_port(port: PortRow) -> frozenset[str]:
    """Return rule_ids whose gate matches ``port`` — the rules a
    commander carrying this port WOULD activate (not necessarily what
    fired in any specific simulation, since gates ignore data-side
    pool size, IDF cutoffs, and stax rejection).
    """
    return frozenset(g.rule_id for g in RULE_GATES if g.predicate(port))
