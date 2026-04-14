"""Combat-related complement matchers."""

from __future__ import annotations

import sqlite3

from ..graph_engine import _trigger_only_matches_self
from .core import PortComplement, PortRow, _commander_subtypes_from_ports


def _find_combat_enhancers(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find extra-combat and double-strike cards for DamageDone commanders.

    Saskia triggers on combat damage -> wants Aurelia (extra combat),
    Gisela (damage doubling), creatures with double strike.

    Dedicated IDF group (~212 cards) lifts these above the 1875-card
    trigger_effect pool where they'd be buried.
    """
    has_damage_trigger = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev == "DamageDone":
            vf = p.get("valid_filter") or ""
            if not _trigger_only_matches_self(vf):
                has_damage_trigger = True
                break

    if not has_damage_trigger:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Extra combat steps
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'effect' AND event_class = 'AddPhase'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="combat_enhancer",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="DamageDone",
                    cand_event="AddPhase",
                )
            )

    # Double Strike keywords
    cur2 = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'keyword' AND event_class = 'Double Strike'"
    )
    for r in cur2.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="combat_enhancer",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="DamageDone",
                    cand_event="DoubleStrike",
                )
            )

    return results


def _find_evasion_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find self-unblockable creatures for combat-damage trigger commanders.

    Yuriko, Saskia, Derevi, etc. trigger on combat damage to players.
    Creatures with CantBlockBy (unblockable) statics reliably connect
    for damage triggers.

    Only matches self-unblockable (ValidAttacker contains Self) -- not
    cards that grant unblockable to other creatures.
    """
    # Only fire for commanders whose combat damage trigger applies to
    # OTHER creatures (Yuriko: Ninja.YouCtrl, Saskia: Creature.YouCtrl,
    # Derevi: Creature.YouCtrl). Skip self-damage triggers (Brago,
    # Lathril -- they only care about their OWN damage, not evasion on others).
    has_combat_damage = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() != "DamageDone":
            continue
        raw = str(p.get("raw_line") or "")
        vf = p.get("valid_filter") or ""
        if "'CombatDamage': 'True'" in raw and not _trigger_only_matches_self(vf):
            has_combat_damage = True
            break

    if not has_combat_damage:
        return []

    # Skip for tribal commanders -- they already get evasive tribe members
    # from tribal_density. Adding generic unblockable creatures dilutes
    # a focused tribal strategy (e.g., Yuriko's Ninjas).
    subtypes = _commander_subtypes_from_ports(conn, list(cmdr_set), cmdr_ports)
    if subtypes:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'CantBlockBy' "
        "AND (raw_line LIKE '%Creature.Self%' OR raw_line LIKE '%Card.Self%')"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="evasion",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="DamageDone_combat",
                    cand_event="unblockable",
                )
            )

    return results


def _find_sacrifice_outlets(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find sacrifice outlet candidates for commanders with death triggers.

    Commanders with ``ChangesZone`` triggers on ``Battlefield -> Graveyard``
    (dying creatures) want candidates with sacrifice costs -- these enable
    the commander to profit from creature deaths on demand.

    Marchesa triggers when creatures with +1/+1 counters die. Meren
    triggers when creatures die. Both want Viscera Seer, Ashnod's Altar,
    etc.
    """
    # Check if commander has a ChangesZone death trigger
    has_death_trigger = False
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev != "ChangesZone":
            continue
        vf = p.get("valid_filter") or ""
        if _trigger_only_matches_self(vf):
            continue
        zd = (p.get("zone_destination") or "").strip()
        # Death = going to graveyard (from battlefield)
        if zd == "Graveyard":
            has_death_trigger = True
            break

    if not has_death_trigger:
        return []

    # Find all cards with sacrifice costs
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'cost' AND event_class = 'sacrifice'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        card = r["card_name"]
        if card not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="cost_feeds_trigger",
                    direction="synergy",
                    candidate=card,
                    cmdr_event="ChangesZone_death",
                    cand_event="sacrifice",
                )
            )

    return results


def _find_changeszone_resonance(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Zone-filtered ChangesZone resonance: find candidates that trigger
    on the same zone transition as the commander.

    Tatyova triggers on ``ChangesZone|Land`` -> find other cards that
    also trigger on lands entering (landfall payoffs). Omnath triggers
    on ``ChangesZone|Elemental`` -> find elemental-ETB payoffs.
    """
    _PRIMARY_TYPES = frozenset(
        {
            "Creature",
            "Artifact",
            "Enchantment",
            "Land",
            "Planeswalker",
        }
    )
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
                results.append(
                    PortComplement(
                        rule_id="zone_resonance",
                        direction="synergy",
                        candidate=card,
                        cmdr_event="ChangesZone",
                        cand_event=base,
                        branch_kind=r["branch_kind"] or "root",
                    )
                )
                break

    return results
