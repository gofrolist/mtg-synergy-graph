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
from dataclasses import dataclass, field
from typing import Any, Literal

from .graph_engine import (
    CATCH_ALL_TRIGGERS,
    COST_FEEDS_TRIGGER,
    EVENT_MATCH_MAP,
    REPLACEMENT_BLOCKS_TRIGGER,
    _SELF_EVENT_TRIGGERS,
    _always,
    _counters_compatible,
    _filter_card_match,
    _rows_to_dicts,
    _trigger_only_matches_self,
    _zones_compatible,
    explode_filter,
    load_ports_for_set,
    match_event,
)
from .penalties import _token_subtype

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
    cmdr_port_type: str    # port_type to match on commander side
    cand_port_type: str    # port_type to match on candidate side
    event_pairs: dict[str, dict[str, EventCheck]]
    direction: Literal["synergy", "anti_synergy"] = "synergy"


# ---------------------------------------------------------------------------
# §2  Build the event-pair maps for each rule
# ---------------------------------------------------------------------------


def _invert_cost_feeds() -> dict[str, dict[str, EventCheck]]:
    """Invert COST_FEEDS_TRIGGER: trigger_event → {cost_event → _always}.

    COST_FEEDS_TRIGGER is cost→trigger; we need trigger→cost for the
    complement rule where the commander has a trigger and we look for
    candidate costs that feed it.
    """
    out: dict[str, dict[str, EventCheck]] = {}
    for cost_ev, trigger_evs in COST_FEEDS_TRIGGER.items():
        for trig_ev in trigger_evs:
            out.setdefault(trig_ev, {})[cost_ev] = _always
    return out


def _invert_replacement_blocks() -> dict[str, dict[str, EventCheck]]:
    """Invert REPLACEMENT_BLOCKS_TRIGGER: trigger_event → {repl_event → _always}.

    REPLACEMENT_BLOCKS_TRIGGER maps replacement_event → trigger_events.
    We need trigger_event → replacement_event for the complement rule.

    Excludes ``ChangesZone → Moved`` because zone-change replacements
    require zone-aware filtering (which zone is being replaced?) that
    the simple port-pair matcher can't express. Without filtering, every
    zone-change replacement card becomes anti-synergy for every
    ChangesZone-trigger commander — a massive false positive.
    """
    out: dict[str, dict[str, EventCheck]] = {}
    for repl_ev, trig_evs in REPLACEMENT_BLOCKS_TRIGGER.items():
        for trig_ev in trig_evs:
            # Skip ChangesZone ↔ Moved — needs zone-aware filtering
            if trig_ev == "ChangesZone" and repl_ev == "Moved":
                continue
            out.setdefault(trig_ev, {})[repl_ev] = _always
    return out


# Resonant effects: same effect on commander and candidate compounds
_RESONANT_EFFECTS: frozenset[str] = frozenset({
    "Proliferate", "Mill", "DigUntil", "Scry", "Surveil",
    "Untap", "Investigate",
})

_RESONANT_EFFECT_FAMILY: dict[str, frozenset[str]] = {
    "DigUntil": frozenset({"DigUntil", "Mill"}),
}

# Replacement resonance: commander effect → candidate replacement that doubles it
_REPLACEMENT_RESONANCE: dict[str, dict[str, EventCheck]] = {
    "PutCounter":    {"AddCounter": _always},
    "PutCounterAll": {"AddCounter": _always},
    "Proliferate":   {"AddCounter": _always},
    "Token":         {"CreateToken": _always},
    "Draw":          {"Draw": _always, "DrawCards": _always},
    "GainLife":      {"GainLife": _always},
    "Mana":          {"ProduceMana": _always},
}

# Reverse: commander replacement → candidate effect that produces the input
_REPLACEMENT_WANTS_PRODUCER: dict[str, dict[str, EventCheck]] = {
    "CreateToken":  {"Token": _always},
    "AddCounter":   {"PutCounter": _always, "PutCounterAll": _always, "Proliferate": _always},
    "Draw":         {"Draw": _always},
    "DrawCards":    {"Draw": _always},
    "GainLife":     {"GainLife": _always},
    "ProduceMana":  {"Mana": _always},
}


def _build_resonance_pairs() -> dict[str, dict[str, EventCheck]]:
    """effect_event → {effect_event → _always} for resonant effects."""
    out: dict[str, dict[str, EventCheck]] = {}
    for ev in _RESONANT_EFFECTS:
        family = _RESONANT_EFFECT_FAMILY.get(ev, frozenset({ev}))
        out[ev] = {fam_ev: _always for fam_ev in family}
    return out


def _build_trigger_resonance_pairs() -> dict[str, dict[str, EventCheck]]:
    """trigger_event → {trigger_event → _always} for resonant triggers.

    Triggers on the same event resonate (both fire on the same game event).
    Exclude catch-all triggers (SpellCast, Attacks) and ChangesZone
    (handled by ETB-self with filter matching).
    """
    _RESONANT_TRIGGERS = frozenset({
        "Sacrificed", "CounterAdded", "DamageDone",
        "LifeGained", "Discarded", "Drawn", "Taps", "Untaps",
        "TapsForMana", "LifeLost", "Proliferate", "Investigated", "Surveil",
    })
    return {ev: {ev: _always} for ev in _RESONANT_TRIGGERS}


def _build_sacrifice_cluster_pairs() -> dict[str, dict[str, EventCheck]]:
    """Death-axis clustering: commander triggers on sacrifice/death events →
    find candidates that ALSO trigger on death events (Blood Artist pattern).

    Also matches commander sacrifice triggers → candidate death-related
    effects (Zulaport Cutthroat's LoseLife on opponent when creature dies).
    """
    _DEATH_TRIGGERS = frozenset({"Sacrificed", "LifeLost"})
    _DEATH_EFFECTS = frozenset({"Sacrifice", "SacrificeAll"})
    out: dict[str, dict[str, EventCheck]] = {}
    for trig in _DEATH_TRIGGERS:
        targets: dict[str, EventCheck] = {}
        # Other death triggers (payoff cluster)
        for t2 in _DEATH_TRIGGERS:
            targets[t2] = _always
        # Death effects on candidates
        for eff in _DEATH_EFFECTS:
            targets[eff] = _always
        out[trig] = targets
    return out


# ---------------------------------------------------------------------------
# §3  The 10 complement rules
# ---------------------------------------------------------------------------


COMPLEMENT_RULES: tuple[ComplementRule, ...] = (
    # 1. Commander trigger → candidate effect (core synergy engine)
    ComplementRule(
        rule_id="trigger_effect",
        cmdr_port_type="trigger",
        cand_port_type="effect",
        event_pairs=EVENT_MATCH_MAP,
    ),
    # 2. Commander trigger → candidate cost feeds it
    ComplementRule(
        rule_id="cost_feeds_trigger",
        cmdr_port_type="trigger",
        cand_port_type="cost",
        event_pairs=_invert_cost_feeds(),
    ),
    # 3. Commander trigger → candidate trigger (shared axis)
    ComplementRule(
        rule_id="trigger_resonance",
        cmdr_port_type="trigger",
        cand_port_type="trigger",
        event_pairs=_build_trigger_resonance_pairs(),
    ),
    # 4. Commander effect → candidate effect (resonance)
    ComplementRule(
        rule_id="effect_resonance",
        cmdr_port_type="effect",
        cand_port_type="effect",
        event_pairs=_build_resonance_pairs(),
    ),
    # 5. Commander effect → candidate replacement doubles it
    ComplementRule(
        rule_id="replacement_resonance",
        cmdr_port_type="effect",
        cand_port_type="replacement",
        event_pairs=_REPLACEMENT_RESONANCE,
    ),
    # 6. Commander replacement → candidate effect produces what it doubles
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
    # 8. Sacrifice/death cluster: commander death trigger → candidate also
    #    triggers on or produces death events (Blood Artist, Zulaport)
    ComplementRule(
        rule_id="sacrifice_cluster",
        cmdr_port_type="trigger",
        cand_port_type="trigger",
        event_pairs=_build_sacrifice_cluster_pairs(),
    ),
)


# ---------------------------------------------------------------------------
# §4  PortComplement — one matched pair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortComplement:
    """A single complementary (commander_port, candidate_port) pair."""

    rule_id: str
    direction: str            # "synergy" or "anti_synergy"
    candidate: str            # candidate card name
    cmdr_event: str           # commander port event_class
    cand_event: str           # candidate port event_class
    branch_kind: str = "root"


# ---------------------------------------------------------------------------
# §5  Universal matcher
# ---------------------------------------------------------------------------


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


def find_all_complements(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    rules: Sequence[ComplementRule] = COMPLEMENT_RULES,
) -> list[PortComplement]:
    """Find all port-pair complements between commander and candidate cards.

    Algorithm:
    1. Load commander ports (cached)
    2. Derive needed (cand_port_type, event_class) pairs from all rules
    3. Bulk-fetch candidate ports matching those pairs (1 SQL query)
    4. Index candidate ports for O(1) lookup
    5. For each rule, for each commander port, find matching candidates
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    cmdr_set = set(commander_set)

    # Group commander ports by port_type
    cmdr_by_type: dict[str, list[PortRow]] = defaultdict(list)
    for p in cmdr_ports:
        if _filter_cmdr_port(p):
            pt = (p.get("port_type") or "").strip()
            cmdr_by_type[pt].append(p)

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
        out.extend(_find_lord_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_etb_self_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_scaling_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_spellcast_density_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_tribal_density_complements(conn, cmdr_ports, cmdr_set))
        out.extend(_find_changeszone_resonance(conn, cmdr_ports, cmdr_set))
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
        pair_conds = " OR ".join(
            "(port_type = ? AND event_class = ?)" for _ in specific_pairs
        )
        conditions.append(f"({pair_conds})")
        for pt, ev in specific_pairs:
            params.extend([pt, ev])

    if not conditions:
        return _card_attr_complements()

    sql = (
        "SELECT card_name, port_type, event_class, valid_filter, "
        "zone_origin, zone_destination, counter_type, branch_kind, "
        "is_conditional, replacement_event "
        f"FROM card_ports WHERE {' OR '.join(conditions)}"
    )
    rows = conn.execute(sql, params).fetchall()

    # Index candidate ports: (port_type, event_class) → [ports]
    cand_index: dict[tuple[str, str], list[PortRow]] = defaultdict(list)
    for r in rows:
        card = r["card_name"]
        if card in cmdr_set:
            continue
        pt = (r["port_type"] or "").strip()
        ev = (r["event_class"] or "").strip()
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

            if "*" in targets:
                # Wildcard: check every candidate port of this type
                check = targets["*"]
                for (pt, ev), cand_ports in cand_index.items():
                    if pt != rule.cand_port_type:
                        continue
                    for cand_p in cand_ports:
                        key = (rule.rule_id, cmdr_ev, cand_p["card_name"], ev)
                        if key in seen:
                            continue
                        if check(cp, cand_p):
                            seen.add(key)
                            results.append(PortComplement(
                                rule_id=rule.rule_id,
                                direction=rule.direction,
                                candidate=cand_p["card_name"],
                                cmdr_event=cmdr_ev,
                                cand_event=ev,
                                branch_kind=(cand_p.get("branch_kind") or "root"),
                            ))
            else:
                for cand_ev, check in targets.items():
                    cand_ports = cand_index.get((rule.cand_port_type, cand_ev), [])
                    for cand_p in cand_ports:
                        key = (rule.rule_id, cmdr_ev, cand_p["card_name"], cand_ev)
                        if key in seen:
                            continue
                        if check(cp, cand_p):
                            seen.add(key)
                            results.append(PortComplement(
                                rule_id=rule.rule_id,
                                direction=rule.direction,
                                candidate=cand_p["card_name"],
                                cmdr_event=cmdr_ev,
                                cand_event=cand_ev,
                                branch_kind=(cand_p.get("branch_kind") or "root"),
                            ))

    # -- Card-attribute rules -----------------------------------------------
    results.extend(_card_attr_complements())

    return results


# ---------------------------------------------------------------------------
# §6  Card-attribute complement matchers
# ---------------------------------------------------------------------------


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
       but is literally a Fungus — the tribal axis is Saproling)
    """
    rows = conn.execute(
        "SELECT subtypes FROM cards WHERE name IN ({})".format(
            ",".join("?" * len(commander_set))
        ),
        tuple(commander_set),
    ).fetchall()
    literal: set[str] = set()
    for r in rows:
        if r["subtypes"]:
            literal.update(r["subtypes"].split())

    # Keep literal subtypes mentioned in port data
    relevant: set[str] = set()
    for p in cmdr_ports:
        haystack = " ".join([
            p.get("valid_filter") or "",
            p.get("affected_scope") or "",
            str(p.get("raw_line") or ""),
        ])
        for sub in literal:
            if sub in haystack:
                relevant.add(sub)

    # Extract token subtypes from TokenScript — these may differ from
    # the commander's literal subtypes (Slimefoot is Fungus but makes
    # Saproling tokens; Edgar is Vampire Knight and makes Vampire tokens).
    for p in cmdr_ports:
        ev = (p.get("event_class") or "").strip()
        if ev == "Token":
            raw = str(p.get("raw_line") or "")
            m = re.search(r"'TokenScript':\s*'([^']+)'", raw)
            if m:
                sub = _token_subtype(m.group(1))
                if sub:
                    relevant.add(sub)

    return relevant


def _find_lord_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Lord matching: candidate static Continuous ports whose affected_scope
    overlaps with commander's mechanically-relevant subtypes.
    """
    cmdr_subtypes = _commander_subtypes_from_ports(
        conn, list(cmdr_set), cmdr_ports,
    )
    if not cmdr_subtypes:
        return []

    cur = conn.execute(
        "SELECT card_name, affected_scope, branch_kind "
        "FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'Continuous' "
        "AND affected_scope IS NOT NULL AND affected_scope != ''"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        scope = r["affected_scope"] or ""
        attrs = explode_filter(scope)
        scope_subtypes = {a["attr_value"] for a in attrs if a["attr_kind"] == "subtype"}
        if scope_subtypes & cmdr_subtypes:
            seen.add(card)
            results.append(PortComplement(
                rule_id="lord",
                direction="synergy",
                candidate=card,
                cmdr_event="tribal",
                cand_event="Continuous",
                branch_kind=r["branch_kind"] or "root",
            ))
    return results


def _find_etb_self_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """ETB-self: candidate card's identity (type/subtype) satisfies
    a commander trigger's valid_filter.
    """
    # Collect commander self-event triggers with valid_filters
    triggers: list[PortRow] = []
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in _SELF_EVENT_TRIGGERS:
            continue
        vf = p.get("valid_filter") or ""
        if not vf or _trigger_only_matches_self(vf):
            continue
        triggers.append(p)

    if not triggers:
        return []

    # Parse each trigger's usable alternatives.
    # Skip overly broad types — Permanent/Card match nearly everything
    # and produce IDF ≈ 0.07, adding noise without signal.
    _SKIP_BASES = frozenset({"Permanent", "Card"})
    parsed: list[tuple[PortRow, list[str]]] = []
    for trig in triggers:
        vf = trig.get("valid_filter") or ""
        alts = [
            a.strip() for a in vf.split(",")
            if a.strip()
            and not a.strip().startswith("Card.Self")
            and a.strip().split(".")[0].split("+")[0].strip() not in _SKIP_BASES
        ]
        if alts:
            parsed.append((trig, alts))

    if not parsed:
        return []

    # Pre-compute SQL type hints from trigger filters to avoid full-table scan.
    # E.g. "Creature.Goblin+YouCtrl" → SQL WHERE card_types LIKE '%Creature%'
    _PRIMARY_TYPES = frozenset({
        "Creature", "Artifact", "Enchantment", "Land", "Instant",
        "Sorcery", "Planeswalker",
    })
    all_type_hints: set[str] = set()
    needs_full_scan = False
    for _trig, alts in parsed:
        for alt in alts:
            base = alt.split(".")[0].split("+")[0].strip()
            if base in _PRIMARY_TYPES:
                all_type_hints.add(base)
            else:
                needs_full_scan = True
                break
        if needs_full_scan:
            break

    if needs_full_scan or not all_type_hints:
        cards = _rows_to_dicts(conn.execute(
            "SELECT name, card_types, supertypes, subtypes, keywords, color_identity "
            "FROM cards"
        ).fetchall())
    else:
        where = " OR ".join(f"card_types LIKE '%{t}%'" for t in all_type_hints)
        cards = _rows_to_dicts(conn.execute(
            f"SELECT name, card_types, supertypes, subtypes, keywords, color_identity "
            f"FROM cards WHERE {where}"
        ).fetchall())

    results: list[PortComplement] = []
    seen: set[tuple[str, str]] = set()
    for trig, alts in parsed:
        ev = (trig.get("event_class") or "").strip()
        for card in cards:
            name = card["name"]
            if name in cmdr_set:
                continue
            key = (name, ev)
            if key in seen:
                continue
            if any(_filter_card_match(alt, card) for alt in alts):
                seen.add(key)
                results.append(PortComplement(
                    rule_id="etb_self",
                    direction="synergy",
                    candidate=name,
                    cmdr_event=ev,
                    cand_event="card_identity",
                ))

    return results


def _find_scaling_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Scaling: commander scales_with a type → candidates of that type.

    Uril scales_with Aura → every Aura card is a complement.
    """
    # Extract types from scales_with ports
    _TYPE_TOKENS = frozenset({
        "Aura", "Equipment", "Enchantment", "Artifact", "Land",
        "Instant", "Sorcery", "Planeswalker",
    })
    wanted_types: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "scales_with":
            continue
        raw = str(p.get("raw_line") or "")
        vf = p.get("valid_filter") or ""
        for tok in _TYPE_TOKENS:
            if tok in raw or tok in vf:
                wanted_types.add(tok)

    if not wanted_types:
        return []

    # Primary types go to card_types, subtypes to subtypes column
    _PRIMARY = frozenset({"Enchantment", "Artifact", "Land", "Instant", "Sorcery", "Planeswalker"})
    primary = wanted_types & _PRIMARY
    subtypes = wanted_types - _PRIMARY  # Aura, Equipment

    results: list[PortComplement] = []
    seen: set[str] = set()

    for type_name in primary:
        cur = conn.execute(
            "SELECT name FROM cards WHERE card_types LIKE ?",
            (f"%{type_name}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(PortComplement(
                    rule_id="scaling",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="scales_with",
                    cand_event=type_name,
                ))

    for sub in subtypes:
        cur = conn.execute(
            "SELECT name FROM cards WHERE subtypes LIKE ?",
            (f"%{sub}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(PortComplement(
                    rule_id="scaling",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="scales_with",
                    cand_event=sub,
                ))

    return results


def _find_spellcast_density_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """SpellCast/LandPlayed density: commanders with catch-all triggers
    want cards of the type they trigger on.

    Talrand triggers on Instant/Sorcery → every Instant/Sorcery is a
    complement. Edgar triggers on Vampire → every Vampire creature.

    Extracts the type filter from the catch-all trigger's valid_filter.
    """
    _CASTABLE_TYPES = frozenset({
        "Instant", "Sorcery", "Creature", "Artifact", "Enchantment",
        "Planeswalker",
    })
    _TOO_BROAD = frozenset({"Creature", "Permanent", "Card"})

    wanted_types: set[str] = set()
    wanted_subtypes: set[str] = set()

    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in CATCH_ALL_TRIGGERS:
            continue
        vf = p.get("valid_filter") or ""
        if not vf:
            continue
        for alt in vf.split(","):
            alt = alt.strip()
            if not alt or alt.startswith("Card.Self"):
                continue
            base = alt.split(".")[0].split("+")[0].strip()
            if base in _CASTABLE_TYPES and base not in _TOO_BROAD:
                wanted_types.add(base)
            elif base not in _TOO_BROAD and base and base[0].isupper():
                wanted_subtypes.add(base)

    if not wanted_types and not wanted_subtypes:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    for type_name in wanted_types:
        cur = conn.execute(
            "SELECT name FROM cards WHERE card_types LIKE ?",
            (f"%{type_name}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(PortComplement(
                    rule_id="spell_density",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="SpellCast",
                    cand_event=type_name,
                ))

    for sub in wanted_subtypes:
        cur = conn.execute(
            "SELECT name FROM cards WHERE subtypes LIKE ?",
            (f"%{sub}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(PortComplement(
                    rule_id="spell_density",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="SpellCast",
                    cand_event=sub,
                ))

    return results


def _find_tribal_density_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Tribal density: every creature of the commander's tribe is a
    complement.

    Krenko is a Goblin commander → every Goblin creature is a complement.
    Sliver Overlord → every Sliver. Marrow-Gnawer → every Rat.

    Uses the same subtype extraction as the lord rule (includes token
    subtypes like Saproling for Slimefoot).
    """
    subtypes = _commander_subtypes_from_ports(conn, list(cmdr_set), cmdr_ports)
    if not subtypes:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    for sub in subtypes:
        cur = conn.execute(
            "SELECT name FROM cards WHERE subtypes LIKE ? AND card_types LIKE '%Creature%'",
            (f"%{sub}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(PortComplement(
                    rule_id="tribal_density",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="tribal",
                    cand_event=sub,
                ))

    return results


def _find_changeszone_resonance(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Zone-filtered ChangesZone resonance: find candidates that trigger
    on the same zone transition as the commander.

    Tatyova triggers on ``ChangesZone|Land`` → find other cards that
    also trigger on lands entering (landfall payoffs). Omnath triggers
    on ``ChangesZone|Elemental`` → find elemental-ETB payoffs.
    """
    _PRIMARY_TYPES = frozenset({
        "Creature", "Artifact", "Enchantment", "Land", "Planeswalker",
    })
    cmdr_types: set[str] = set()
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev != "ChangesZone":
            continue
        vf = p.get("valid_filter") or ""
        if not vf or _trigger_only_matches_self(vf):
            continue
        for alt in vf.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base in _PRIMARY_TYPES:
                cmdr_types.add(base)

    if not cmdr_types:
        return []

    cur = conn.execute(
        "SELECT card_name, valid_filter, branch_kind "
        "FROM card_ports "
        "WHERE port_type = 'trigger' AND event_class = 'ChangesZone' "
        "AND valid_filter IS NOT NULL AND valid_filter != ''"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        vf = r["valid_filter"] or ""
        if _trigger_only_matches_self(vf):
            continue
        for alt in vf.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base in cmdr_types:
                seen.add(card)
                results.append(PortComplement(
                    rule_id="zone_resonance",
                    direction="synergy",
                    candidate=card,
                    cmdr_event="ChangesZone",
                    cand_event=base,
                    branch_kind=r["branch_kind"] or "root",
                ))
                break

    return results
