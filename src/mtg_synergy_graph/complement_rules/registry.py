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

import re
from collections.abc import Callable
from dataclasses import dataclass

from .core import COMPLEMENT_RULES, PortRow


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

_PEER_BLOCKING_KEYWORDS: frozenset[str] = frozenset({"Horsemanship", "Shadow"})

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


def _peer_evasion_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "keyword":
        return False
    return (port.get("event_class") or "").strip() in _PEER_BLOCKING_KEYWORDS


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


def _monarch_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "effect":
        return False
    return (port.get("event_class") or "").strip() == "BecomeMonarch"


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


def _flicker_synergy_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "effect":
        return False
    ev = (port.get("event_class") or "").strip()
    return ev in ("ChangeZone", "ChangeZoneAll")


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


def _etb_sac_target_gate(port: PortRow) -> bool:
    """Commander has GY-reanimate effect (Meren / Karador shape).
    Gate matches the ChangeZone effect from Graveyard."""
    if (port.get("port_type") or "").strip() != "effect":
        return False
    if (port.get("event_class") or "").strip() != "ChangeZone":
        return False
    return (port.get("zone_origin") or "").strip() == "Graveyard"


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


def _power_matters_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return (port.get("event_class") or "").strip() in ("CardPower", "TotalPower")


def _toughness_gate(port: PortRow) -> bool:
    if (port.get("port_type") or "").strip() != "scales_with":
        return False
    return "CardToughness" in (port.get("event_class") or "")


_CARD_ATTR_GATES: tuple[RuleGate, ...] = (
    RuleGate("damage_doubler_synergy", _damage_doubler_gate),
    RuleGate("peer_evasion_tribal", _peer_evasion_gate),
    RuleGate("modified_axis_feeder", _modified_axis_gate),
    RuleGate("counter_axis_feeder", _counter_axis_gate),
    RuleGate("opponent_forcing", _opponent_forcing_gate),
    RuleGate("wheel_synergy", _wheel_synergy_gate),
    RuleGate("mana_doubler", _mana_doubler_gate),
    RuleGate("monarch_synergy", _monarch_gate),
    RuleGate("extra_land_plays", _extra_land_plays_gate),
    RuleGate("cost_payoff", _cost_payoff_gate),
    RuleGate("etb_self", _etb_self_gate),
    RuleGate("combat_enhancer", _combat_enhancer_gate),
    RuleGate("flicker_synergy", _flicker_synergy_gate),
    RuleGate("attack_payoffs", _attack_payoff_gate),
    RuleGate("panharmonicon", _panharmonicon_gate),
    RuleGate("voltron", _voltron_gate),
    RuleGate("evasion", _evasion_gate),
    RuleGate("token_etb_damage", _token_etb_damage_gate),
    RuleGate("etb_sac_target", _etb_sac_target_gate),
    RuleGate("graveyard_play", _graveyard_play_gate),
    RuleGate("gy_loader", _gy_loader_gate),
    RuleGate("cost_reduction_target", _cost_reduction_gate),
    RuleGate("spell_density", _spellcast_density_gate),
    RuleGate("spellcast_resonance", _spellcast_density_gate),
    RuleGate("power_matters", _power_matters_gate),
    RuleGate("toughness_synergy", _toughness_gate),
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
    }
)


RULE_GATES: tuple[RuleGate, ...] = tuple(_formal_rule_gates()) + _CARD_ATTR_GATES


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
