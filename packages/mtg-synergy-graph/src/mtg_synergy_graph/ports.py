"""Port extractors (SPEC §5.5).

Each parsed Forge ability line becomes one or more ``card_ports`` rows. The
extractors are pure functions returning lists of dicts, leaving database
insertion to ``importer.py``.

Branch metadata is propagated end-to-end: a cost port that belongs to a
``TrueSubAbility$`` of a triggered effect inherits ``branch_kind="true"``
and ``is_conditional=True`` so §7.2 can apply the same multiplier to it.
"""

from __future__ import annotations

from typing import Any

from .parser import (
    CHAIN_KEYS,
    ChainNode,
    parse_forge_line,
    walk_svar_chain,
)

PortRow = dict[str, Any]


# ---------------------------------------------------------------------------
# Cost parsing (§5.5)
# ---------------------------------------------------------------------------

#: Cost type detection. ORDER MATTERS — substring matching means more-specific
#: variants must come before their containing prefix (``ExileFromGrave`` before
#: ``Exile``).
COST_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ExileFromGrave", "exile_from_grave"),
    ("ExileFromHand",  "exile_from_hand"),
    ("ExileFromTop",   "exile_from_top"),
    ("Exile",          "exile"),
    ("SubCounter",     "remove_counter"),
    ("AddCounter",     "add_counter"),
    ("tapXType",       "tap_type"),
    ("untapYType",     "untap_type"),
    ("Sac",            "sacrifice"),
    ("Discard",        "discard"),
    ("Return",         "return"),
    ("Reveal",         "reveal"),
    ("PayLife",        "pay_life"),
    ("PayEnergy",      "pay_energy"),
    ("Mill",           "mill"),
    ("Exert",          "exert"),
)


def _branch_defaults(
    *,
    branch_kind: str = "root",
    branch_parent: str | None = None,
    source_svar: str | None = None,
    chain_depth: int = 0,
    is_conditional: bool = False,
) -> dict[str, Any]:
    """Common branch-metadata fields shared by every port row."""
    return {
        "branch_kind":    branch_kind,
        "branch_parent":  branch_parent,
        "source_svar":    source_svar,
        "chain_depth":    chain_depth,
        "is_conditional": is_conditional,
    }


def extract_cost_ports(
    card_name: str,
    cost_str: str,
    *,
    branch_kind: str = "root",
    branch_parent: str | None = None,
    source_svar: str | None = None,
    chain_depth: int = 0,
    is_conditional: bool = False,
) -> list[PortRow]:
    """Parse a Forge ``Cost$`` string into cost-port rows."""
    if not cost_str:
        return []

    branch = _branch_defaults(
        branch_kind=branch_kind,
        branch_parent=branch_parent,
        source_svar=source_svar,
        chain_depth=chain_depth,
        is_conditional=is_conditional,
    )

    ports: list[PortRow] = []
    for pattern, cost_type in COST_PATTERNS:
        if pattern not in cost_str:
            continue
        # Optional <subtype> in angle brackets after the keyword.
        subtype = ""
        idx = cost_str.find(pattern)
        bracket_start = cost_str.find("<", idx)
        if bracket_start != -1:
            bracket_end = cost_str.find(">", bracket_start)
            if bracket_end != -1:
                subtype = cost_str[bracket_start + 1 : bracket_end]
        ports.append(
            {
                "card_name":    card_name,
                "port_type":    "cost",
                "event_class":  cost_type,
                "cost_subtype": subtype,
                "raw_line":     cost_str,
                **branch,
            }
        )

    if "T" in cost_str.split():
        ports.append(
            {
                "card_name":   card_name,
                "port_type":   "cost",
                "event_class": "tap",
                "raw_line":    cost_str,
                **branch,
            }
        )

    return ports


# ---------------------------------------------------------------------------
# Effect ports (§5.5)
# ---------------------------------------------------------------------------


def _amount_from(parsed: dict[str, Any]) -> str:
    """Pick the first amount-style field present in a parsed segment."""
    for key in ("NumDmg", "NumCards", "LifeAmount", "CounterNum", "TokenAmount"):
        value = parsed.get(key)
        if value:
            return value
    return ""


def extract_effect_ports(
    card_name: str,
    parsed_or_node: dict[str, Any] | ChainNode,
    svars: dict[str, str],
) -> list[PortRow]:
    """Convert a parsed A:/DB$/SP$ line (or ``ChainNode``) into card_port rows.

    Recursively follows ``Execute$``/``SubAbility$`` etc. so activated-ability
    chains are not lost. Branch context is propagated to every emitted port.
    """
    if isinstance(parsed_or_node, ChainNode):
        node = parsed_or_node
        parsed = node.parsed
        branch_kind    = node.branch_kind
        branch_parent  = node.branch_parent
        source_svar    = node.source_svar
        chain_depth    = node.chain_depth
        is_conditional = node.is_conditional
    else:
        parsed = parsed_or_node
        branch_kind    = "root"
        branch_parent  = None
        source_svar    = None
        chain_depth    = 0
        is_conditional = False

    verb = (
        parsed.get("_verb")
        or parsed.get("DB")
        or parsed.get("SP")
        or parsed.get("AB")
        or ""
    )

    branch = _branch_defaults(
        branch_kind=branch_kind,
        branch_parent=branch_parent,
        source_svar=source_svar,
        chain_depth=chain_depth,
        is_conditional=is_conditional,
    )

    port: PortRow = {
        "card_name":        card_name,
        "port_type":        "effect",
        "event_class":      verb,
        "valid_filter":     parsed.get("ValidTgts") or parsed.get("Defined") or parsed.get("ValidCards", ""),
        "zone_origin":      parsed.get("Origin", ""),
        "zone_destination": parsed.get("Destination", ""),
        "amount":           _amount_from(parsed),
        "counter_type":     parsed.get("CounterType", ""),
        "granted_keyword":  parsed.get("KW", ""),
        "duration":         parsed.get("Duration", ""),
        "is_curse":         parsed.get("IsCurse", "") == "True",
        "raw_line":         repr(parsed),
        **branch,
    }

    cost_ports = extract_cost_ports(
        card_name,
        parsed.get("Cost", ""),
        branch_kind=branch_kind,
        branch_parent=branch_parent,
        source_svar=source_svar,
        chain_depth=chain_depth,
        is_conditional=is_conditional,
    )

    sub_ports: list[PortRow] = []
    for key, child_branch_kind in CHAIN_KEYS.items():
        ref_name = parsed.get(key)
        if not ref_name:
            continue
        chain = walk_svar_chain(
            ref_name,
            svars,
            branch_kind=child_branch_kind,
            branch_parent=source_svar,
            chain_depth=chain_depth + 1,
        )
        for sub_node in chain:
            sub_ports.extend(extract_effect_ports(card_name, sub_node, svars))

    return [port] + cost_ports + sub_ports


# ---------------------------------------------------------------------------
# Trigger ports (§5.5)
# ---------------------------------------------------------------------------


def extract_trigger_ports(
    card_name: str,
    parsed: dict[str, Any],
    svars: dict[str, str],
) -> list[PortRow]:
    """Convert a parsed T: line into card_port rows."""
    trigger_port: PortRow = {
        "card_name":        card_name,
        "port_type":        "trigger",
        "event_class":      parsed.get("Mode", ""),
        "valid_filter":     parsed.get("ValidCard") or parsed.get("ValidSource", ""),
        "zone_origin":      parsed.get("Origin", ""),
        "zone_destination": parsed.get("Destination", ""),
        "phase":            parsed.get("Phase", ""),
        "is_optional":      "OptionalDecider" in parsed,
        "is_combat":        parsed.get("CombatDamage", "") == "True",
        "execute_ref":      parsed.get("Execute", ""),
        "raw_line":         repr(parsed),
        **_branch_defaults(),
    }
    ports: list[PortRow] = [trigger_port]

    execute_ref = parsed.get("Execute")
    if execute_ref:
        chain = walk_svar_chain(
            execute_ref,
            svars,
            branch_kind="execute",
            branch_parent=None,
            chain_depth=1,
        )
        for node in chain:
            ports.extend(extract_effect_ports(card_name, node, svars))

    return ports


# ---------------------------------------------------------------------------
# Static ports (§5.5)
# ---------------------------------------------------------------------------


def extract_static_ports(card_name: str, parsed: dict[str, Any]) -> list[PortRow]:
    """Convert a parsed S: line into card_port rows.

    Phase 1.5b: the full ``Mode$`` value is the ``event_class`` so tags like
    ``Static$Panharmonicon`` / ``Static$ReduceCost`` / ``Static$Continuous``
    are first-class port keys.
    """
    has_condition = bool(parsed.get("Condition") or parsed.get("IsPresent"))
    return [
        {
            "card_name":      card_name,
            "port_type":      "static",
            "event_class":    parsed.get("Mode", ""),
            "affected_scope": parsed.get("Affected", ""),
            "effect_zone":    parsed.get("EffectZone", ""),
            "granted_keyword": parsed.get("AddKeyword", ""),
            "granted_ability": parsed.get("AddAbility", ""),
            "valid_filter":   parsed.get("IsPresent", ""),
            "amount":         parsed.get("AddPower", ""),
            "is_conditional": has_condition,
            "branch_kind":    "static_condition" if has_condition else "root",
            "branch_parent":  None,
            "source_svar":    None,
            "chain_depth":    0,
            "raw_line":       repr(parsed),
        }
    ]


# ---------------------------------------------------------------------------
# Replacement ports (§5.5)
# ---------------------------------------------------------------------------


def extract_replacement_ports(card_name: str, parsed: dict[str, Any]) -> list[PortRow]:
    """Convert a parsed R: line into card_port rows.

    ``ValidPlayer$`` is a SCOPE qualifier (which player's events this watches),
    not a conditional gate — it must NEVER flip ``is_conditional``. Only a
    real ``Condition$`` / ``CheckSVar$`` gate triggers the flag. The
    opponent-only replacement case is handled by §4.5 penalty rule #10.
    """
    has_condition = bool(parsed.get("Condition") or parsed.get("CheckSVar"))
    replace_with = parsed.get("ReplaceWith") or ("Prevent" if "Prevent" in parsed else "")
    return [
        {
            "card_name":          card_name,
            "port_type":          "replacement",
            "event_class":        parsed.get("Event", ""),
            "replacement_event":  parsed.get("Event", ""),
            "replacement_result": replace_with,
            "replacement_player": parsed.get("ValidPlayer", ""),
            "valid_filter":       parsed.get("ValidCard") or parsed.get("ValidSource", ""),
            "is_conditional":     has_condition,
            "branch_kind":        "replacement_condition" if has_condition else "root",
            "branch_parent":      None,
            "source_svar":        None,
            "chain_depth":        0,
            "raw_line":           repr(parsed),
        }
    ]


# ---------------------------------------------------------------------------
# Keyword ports (§5.5)
# ---------------------------------------------------------------------------


def extract_keyword_ports(card_name: str, keyword_lines: list[str]) -> list[PortRow]:
    """Convert raw K: lines into one keyword-port row each."""
    ports: list[PortRow] = []
    for line in keyword_lines:
        line = line.strip()
        if not line:
            continue
        # Forge K: lines vary in form. The first whitespace-separated token
        # is the keyword name; the rest is preserved as raw_line.
        keyword = line.split()[0] if line else ""
        ports.append(
            {
                "card_name":       card_name,
                "port_type":       "keyword",
                "event_class":     keyword,
                "granted_keyword": keyword,
                "raw_line":        line,
                **_branch_defaults(),
            }
        )
    return ports


# ---------------------------------------------------------------------------
# Scaling ports (§5.5)
# ---------------------------------------------------------------------------

#: Recognised metric tokens for ``Count$`` SVars (§5.5 v1 vocabulary).
_KNOWN_COUNT_METRICS = frozenset(
    {
        "CardInGraveyard",
        "CardManaCost",
        "CardManaCostLKI",
        "Power",
        "Toughness",
        "ManaCost",
        "Remembered",
    }
)


def extract_scaling_ports(card_name: str, svars: dict[str, str]) -> list[PortRow]:
    """Extract ``SVar:Count$`` patterns as scaling ports.

    Recognises Count$Valid, Count$CountValid, Count$Triggered, plus the
    standalone metrics in :data:`_KNOWN_COUNT_METRICS`.
    """
    ports: list[PortRow] = []
    for svar_name, svar_value in svars.items():
        if not svar_value.startswith("Count$"):
            continue
        expression = svar_value[len("Count$"):]
        filter_str = ""
        metric = expression

        if expression.startswith("Valid "):
            filter_str = expression.split(" ", 1)[1]
            metric = "Valid"
        elif expression.startswith("CountValid "):
            filter_str = expression.split(" ", 1)[1]
            metric = "CountValid"
        elif expression.startswith("Triggered"):
            metric = "Triggered"
        elif expression in _KNOWN_COUNT_METRICS:
            metric = expression

        ports.append(
            {
                "card_name":          card_name,
                "port_type":          "scales_with",
                "event_class":        metric,
                "valid_filter":       filter_str,
                "scaling_expression": svar_value,
                "source_svar":        svar_name,
                "branch_kind":        "root",
                "branch_parent":      None,
                "is_conditional":     False,
                "chain_depth":        0,
                "raw_line":           f"SVar:{svar_name}:{svar_value}",
            }
        )
    return ports


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------

# All extractors share the same ``(card_name, parsed, svars)`` signature
# even when ``svars`` is unused, so the dispatch loop in ``extract_all_ports``
# can call them uniformly. The static and replacement extractors ignore
# ``svars`` today; if SVar expansion is added to either, no signature
# rewiring is needed.
def _static_ext(card: str, parsed: dict[str, Any], svars: dict[str, str]) -> list[PortRow]:
    del svars  # static ports don't follow SVar chains
    return extract_static_ports(card, parsed)


def _replacement_ext(card: str, parsed: dict[str, Any], svars: dict[str, str]) -> list[PortRow]:
    del svars  # replacement ports don't follow SVar chains
    return extract_replacement_ports(card, parsed)


_ABILITY_EXTRACTORS = {
    "trigger":     extract_trigger_ports,
    "ability":     extract_effect_ports,
    "static":      _static_ext,
    "replacement": _replacement_ext,
}


def extract_all_ports(card: dict[str, Any]) -> list[PortRow]:
    """Extract every port for a parsed card dict (output of ``parse_card_text``).

    Combines triggers, A: effects (with embedded sub-ability chains), statics,
    replacements, K: keywords, and ``SVar:Count$`` scaling ports into a single
    ordered list.
    """
    name = card.get("name", "")
    svars: dict[str, str] = card.get("svars", {})
    ports: list[PortRow] = []

    for ability_kind, parsed in card.get("abilities", []):
        extractor = _ABILITY_EXTRACTORS.get(ability_kind)
        if extractor is None:
            continue
        ports.extend(extractor(name, parsed, svars))

    ports.extend(extract_keyword_ports(name, card.get("keywords", [])))
    ports.extend(extract_scaling_ports(name, svars))
    return ports
