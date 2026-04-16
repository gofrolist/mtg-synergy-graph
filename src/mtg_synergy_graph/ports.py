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
    # Phase A4: order matters — more-specific prefixes must come before
    # their substring variants. ``ExileAnyGrave`` must precede ``Exile``;
    # ``CollectEvidence`` is its own prefix. ``Draw`` is safe at the end
    # because cost_str is a cost-token string and never contains the
    # ``Drawn`` trigger or ``DrawCards`` effect verb.
    ("ExileFromGrave", "exile_from_grave"),
    ("ExileFromHand", "exile_from_hand"),
    ("ExileFromTop", "exile_from_top"),
    ("ExileAnyGrave<", "exile_any_grave"),  # A4: 14 cards (delve-class)
    ("Exile", "exile"),
    ("SubCounter", "remove_counter"),
    ("AddCounter", "add_counter"),
    ("tapXType", "tap_type"),
    ("untapYType", "untap_type"),
    ("Sac", "sacrifice"),
    ("Discard", "discard"),
    ("Return", "return"),
    ("Reveal", "reveal"),
    ("PayLife", "pay_life"),
    ("PayEnergy", "pay_energy"),
    ("Mill", "mill"),
    ("Exert", "exert"),
    # A4: require trailing ``<`` so the substring match cannot fire on
    # tokens like ``Discard<1/LastDrawn>`` (which contains ``Draw``).
    ("CollectEvidence<", "collect_evidence"),  # A4: 17 cards
    ("DamageYou<", "damage_self"),  # A4: 18 cards
    ("Draw<", "draw_cost"),  # A4: 45 cards
)

#: Cost types that take a ``<count/typespec[/desc]>`` payload describing
#: which permanents the controller picks. These are the only costs where
#: the self / other distinction is meaningful — `PayLife`, `tap_type` etc.
#: have no permanent picker, so they default to ``cost_target=None``.
#: Phase A1 (SPEC §5.5).
_COST_TARGETED_TYPES: frozenset[str] = frozenset(
    {
        "sacrifice",
        "discard",
        "exile",
        "exile_from_grave",
        "exile_from_hand",
        "return",
    }
)


def _classify_cost_target(subtype: str) -> str | None:
    """Classify a cost-payload typespec as ``self``, ``other``, or ``any``.

    The Forge cost format is ``<count/typespec[/desc]>`` where ``typespec`` is
    a ``;``-separated list of single-card filters such as
    ``Creature.Other``, ``CARDNAME``, ``Permanent.YouCtrl``,
    ``Artifact;Creature;Land``.

    - ``self``  — the source card is the only valid pick (typespec is exactly
      ``CARDNAME`` / ``Card.Self``). Suspend-style sac.
    - ``other`` — every alternative carries a ``.Other`` qualifier (the source
      cannot be picked). True "outlet for other creatures" pattern.
    - ``any``   — neither: the source MAY be picked alongside other matches
      (Viscera Seer's ``Sac<1/Creature>``, Goblin Bombardment, Greater
      Gargadon's ``Sac<1/Artifact;Creature;Land>``).

    Returns ``None`` for empty / unparseable payloads so non-targeted cost
    types fall through cleanly.
    """
    if not subtype:
        return None
    # Strip the leading ``count/`` and the trailing ``/desc`` if present.
    parts = subtype.split("/", 2)
    if len(parts) < 2:
        return None
    typespec = parts[1].strip()
    if not typespec:
        return None
    alts = [a.strip() for a in typespec.split(";") if a.strip()]
    if not alts:
        return None
    if all(a in ("CARDNAME", "Card.Self") for a in alts):
        return "self"
    if all(".Other" in a for a in alts):
        return "other"
    return "any"


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
        "branch_kind": branch_kind,
        "branch_parent": branch_parent,
        "source_svar": source_svar,
        "chain_depth": chain_depth,
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
        # Phase A1: classify the source/other distinction for the cost types
        # whose payload picks a permanent. A bare ``Sacrifice`` (no bracket,
        # used by suspend-style abilities like Greater Gargadon's keyword
        # form) defaults to ``self`` because the rules text "sacrifice
        # CARDNAME" is implicit.
        cost_target: str | None = None
        if cost_type in _COST_TARGETED_TYPES:
            cost_target = _classify_cost_target(subtype) or "self"
        ports.append(
            {
                "card_name": card_name,
                "port_type": "cost",
                "event_class": cost_type,
                "cost_subtype": subtype,
                "cost_target": cost_target,
                "raw_line": cost_str,
                **branch,
            }
        )

    if "T" in cost_str.split():
        ports.append(
            {
                "card_name": card_name,
                "port_type": "cost",
                "event_class": "tap",
                "cost_target": None,
                "raw_line": cost_str,
                **branch,
            }
        )

    return ports


# ---------------------------------------------------------------------------
# Effect ports (§5.5)
# ---------------------------------------------------------------------------

#: Phase B3: effect verbs that are pure flow-control / iteration primitives.
#: They are still emitted as their own ``event_class`` (so the chain walker
#: keeps following ``TrueSubAbility$`` / ``RepeatSubAbility$`` etc.), but
#: extract_effect_ports also emits an extra synthetic port with
#: ``event_class='combo_primitive'`` to give downstream matchers a single
#: SQL key for "this card has loop / branch combo potential".
#: ``Chain`` was in the audit's list but has zero corpus uses — verified
#: with grep against data/forge/forge-gui/res/cardsfolder.
COMBO_PRIMITIVE_VERBS: frozenset[str] = frozenset(
    {
        "Branch",  # 115 cards
        "Repeat",  # 57  cards
        "RepeatEach",  # 347 cards
    }
)


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
        branch_kind = node.branch_kind
        branch_parent = node.branch_parent
        source_svar = node.source_svar
        chain_depth = node.chain_depth
        is_conditional = node.is_conditional
    else:
        parsed = parsed_or_node
        branch_kind = "root"
        branch_parent = None
        source_svar = None
        chain_depth = 0
        is_conditional = False

    verb = parsed.get("_verb") or parsed.get("DB") or parsed.get("SP") or parsed.get("AB") or ""

    branch = _branch_defaults(
        branch_kind=branch_kind,
        branch_parent=branch_parent,
        source_svar=source_svar,
        chain_depth=chain_depth,
        is_conditional=is_conditional,
    )

    # Phase A3: Mana effects with ``RestrictValid$`` restrict spending of
    # the produced mana (e.g., Cavern of Souls "spend only on creatures of
    # the chosen type", Nexos "spend only on costs that contain {X}").
    # The restriction is the synergy signal — store it on the effect port
    # so D2's mana-restriction matcher can join on it.
    mana_restriction = parsed.get("RestrictValid", "") if verb == "Mana" else ""

    # ChangeZone effects carry their type-scope in ChangeType$, separate
    # from ValidTgts/Defined. Store it on the port so the importer can
    # explode it into port_attributes under attr_kind='change_type'.
    change_type = parsed.get("ChangeType", "") if verb == "ChangeZone" else ""

    port: PortRow = {
        "card_name": card_name,
        "port_type": "effect",
        "event_class": verb,
        "valid_filter": parsed.get("ValidTgts") or parsed.get("Defined") or parsed.get("ValidCards", ""),
        "zone_origin": parsed.get("Origin", ""),
        "zone_destination": parsed.get("Destination", ""),
        "amount": _amount_from(parsed),
        "counter_type": parsed.get("CounterType", ""),
        "granted_keyword": parsed.get("KW", ""),
        "duration": parsed.get("Duration", ""),
        "is_curse": parsed.get("IsCurse", "") == "True",
        "mana_restriction": mana_restriction,
        "raw_line": repr(parsed),
        "_change_type": change_type,  # consumed by importer, stripped before insert
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
    # When invoked with a ChainNode, walk_svar_chain has already flattened
    # the entire SubAbility tree — re-walking here causes 2^N port explosion
    # (see test_akroma_vision_of_ixidor_does_not_explode). Only the
    # top-level raw-dict entry path walks the chain.
    is_chain_node = isinstance(parsed_or_node, ChainNode)
    if not is_chain_node:
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

    # GenericChoice modal expansion: walk each choice SVar to extract
    # the hidden Token/Draw/ChangeZone effects (e.g., Tireless Provisioner
    # has Choices$ Food,Treasure → Token effects for each).
    if verb == "GenericChoice":
        choices_str = parsed.get("Choices", "")
        for choice_name in choices_str.split(","):
            choice_name = choice_name.strip()
            if choice_name and choice_name in svars:
                chain = walk_svar_chain(
                    choice_name,
                    svars,
                    branch_kind="choice",
                    branch_parent=source_svar,
                    chain_depth=chain_depth + 1,
                )
                for sub_node in chain:
                    sub_ports.extend(extract_effect_ports(card_name, sub_node, svars))

    # StaticAbilities$ SVar walking: extract static ports from referenced
    # SVars (e.g., "StaticAbilities$ Play" → MayPlay continuous static).
    static_refs = parsed.get("StaticAbilities", "")
    if static_refs:
        for ref_name in static_refs.split(","):
            ref_name = ref_name.strip()
            if ref_name and ref_name in svars:
                static_parsed = parse_forge_line(svars[ref_name])
                sub_ports.extend(extract_static_ports(card_name, static_parsed))

    # Phase B3: emit a synthetic combo_primitive port alongside the original
    # Branch / Repeat / RepeatEach effect so SQL queries can find combo
    # cards via a single ``event_class='combo_primitive'`` key. The original
    # port is preserved so the chain walker (which keys on the verb name)
    # still works.
    extra_ports: list[PortRow] = []
    if verb in COMBO_PRIMITIVE_VERBS:
        extra_ports.append(
            {
                "card_name": card_name,
                "port_type": "effect",
                "event_class": "combo_primitive",
                "granted_ability": verb,
                "raw_line": repr(parsed),
                **branch,
            }
        )

    return [port, *cost_ports, *sub_ports, *extra_ports]


# ---------------------------------------------------------------------------
# Trigger ports (§5.5)
# ---------------------------------------------------------------------------


def extract_trigger_ports(
    card_name: str,
    parsed: dict[str, Any],
    svars: dict[str, str],
) -> list[PortRow]:
    """Convert a parsed T: line into card_port rows.

    Phase A2: ``FirstTime$ True`` is recorded as
    ``trigger_source='first_time'`` so cadence-sensitive matchers can
    distinguish "first time each turn" triggers from any-time. The
    ``BecomesTarget`` ``ValidTarget$`` reordering originally planned for
    A2 was deferred — bisect showed it removed port_attribute cardinality
    that downstream matchers depend on. It will land together with the D3
    combat-modifier matcher whose new joins offset the cardinality drop.
    """
    valid_filter = parsed.get("ValidCard") or parsed.get("ValidSource", "")

    trigger_source: str | None = None
    if parsed.get("FirstTime", "") == "True":
        trigger_source = "first_time"

    trigger_port: PortRow = {
        "card_name": card_name,
        "port_type": "trigger",
        "event_class": parsed.get("Mode", ""),
        "valid_filter": valid_filter,
        "zone_origin": parsed.get("Origin", ""),
        "zone_destination": parsed.get("Destination", ""),
        "phase": parsed.get("Phase", ""),
        "is_optional": "OptionalDecider" in parsed,
        "is_combat": parsed.get("CombatDamage", "") == "True",
        "execute_ref": parsed.get("Execute", ""),
        "trigger_source": trigger_source,
        "raw_line": repr(parsed),
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
            "card_name": card_name,
            "port_type": "static",
            "event_class": parsed.get("Mode", ""),
            "affected_scope": parsed.get("Affected", ""),
            "effect_zone": parsed.get("EffectZone", ""),
            "granted_keyword": parsed.get("AddKeyword", ""),
            "granted_ability": parsed.get("AddAbility", ""),
            "valid_filter": parsed.get("IsPresent", ""),
            "amount": parsed.get("AddPower", ""),
            "is_conditional": has_condition,
            "branch_kind": "static_condition" if has_condition else "root",
            "branch_parent": None,
            "source_svar": None,
            "chain_depth": 0,
            "raw_line": repr(parsed),
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
    # Phase C1: capture Origin$/Destination$ on replacement ports so the
    # zone-aware conflict matcher can scope ``Event$ Moved | Prevent$ True``
    # graveyard-hate effects (Grafdigger's Cage, Soulless Jailer) to ONLY
    # the reanimator commanders whose triggers actually fire on
    # GY→BF zone changes — and not flag every ChangesZone trigger commander
    # like Brago/Yarok/Yuriko as anti-synergy.
    return [
        {
            "card_name": card_name,
            "port_type": "replacement",
            "event_class": parsed.get("Event", ""),
            "replacement_event": parsed.get("Event", ""),
            "replacement_result": replace_with,
            "replacement_player": parsed.get("ValidPlayer", ""),
            "valid_filter": parsed.get("ValidCard") or parsed.get("ValidSource", ""),
            "zone_origin": parsed.get("Origin", ""),
            "zone_destination": parsed.get("Destination", ""),
            "is_conditional": has_condition,
            "branch_kind": "replacement_condition" if has_condition else "root",
            "branch_parent": None,
            "source_svar": None,
            "chain_depth": 0,
            "raw_line": repr(parsed),
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
                "card_name": card_name,
                "port_type": "keyword",
                "event_class": keyword,
                "granted_keyword": keyword,
                "raw_line": line,
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
        expression = svar_value[len("Count$") :]
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
                "card_name": card_name,
                "port_type": "scales_with",
                "event_class": metric,
                "valid_filter": filter_str,
                "scaling_expression": svar_value,
                "source_svar": svar_name,
                "branch_kind": "root",
                "branch_parent": None,
                "is_conditional": False,
                "chain_depth": 0,
                "raw_line": f"SVar:{svar_name}:{svar_value}",
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
    "trigger": extract_trigger_ports,
    "ability": extract_effect_ports,
    "static": _static_ext,
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
