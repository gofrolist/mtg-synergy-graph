"""Graveyard-related complement matchers."""

from __future__ import annotations

import re
import sqlite3

from .core import PortComplement, PortRow


def _find_graveyard_fillers(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find graveyard fillers for commanders that reanimate or cast from GY.

    Commanders with ChangeZone effects from Graveyard (Meren, Marchesa)
    or static MayPlay from Graveyard (Karador, Muldrotha, Kess) want
    cards that fill the graveyard: self-mill, dredge, self-discard, and
    creatures that put themselves in the graveyard (evoke, self-sacrifice).
    """
    wants_gy = False

    # Check for ChangeZone from Graveyard effects (Meren, Marchesa)
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "effect" and ev == "ChangeZone":
            zo = (p.get("zone_origin") or "").strip()
            if zo == "Graveyard":
                wants_gy = True
                break
        # Static graveyard-cast (Karador, Muldrotha, Kess)
        if pt == "static" and ev == "Continuous":
            raw = str(p.get("raw_line") or "")
            if "'MayPlay'" in raw and "'Graveyard'" in raw:
                wants_gy = True
                break
        # scales_with graveyard (Karador, Mimeoplasm)
        if pt == "scales_with" and ("Graveyard" in ev or "graveyard" in ev):
            wants_gy = True
            break

    if not wants_gy:
        return []

    # Also detect which card types the commander can recast from GY
    recast_types: set[str] = set()
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "static" and ev == "Continuous":
            raw = str(p.get("raw_line") or "")
            if "'MayPlay'" in raw and "'Graveyard'" in raw:
                m = re.search(r"'Affected':\s*'([^']+)'", raw)
                if m:
                    for alt in m.group(1).split(","):
                        base = alt.strip().split(".")[0].split("+")[0].strip()
                        if base and base[0].isupper() and base != "Card":
                            recast_types.add(base)

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Self-mill effects (targeted: cards that specifically mill yourself)
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class IN ('Mill', 'DigUntil', 'Surveil') "
        "AND (valid_filter LIKE '%YouCtrl%' OR valid_filter LIKE '%YouOwn%' "
        "OR valid_filter = 'You' OR valid_filter LIKE 'You.%')"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="trigger_effect",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="graveyard_filler",
                    cand_event="self_mill",
                )
            )

    # Recast-type density: Kess wants instants/sorceries, Karador wants
    # creatures with ETBs. Match cards of the recastable types.
    _CASTABLE_TYPES = frozenset({"Instant", "Sorcery"})
    for card_type in recast_types & _CASTABLE_TYPES:
        cur = conn.execute(
            "SELECT name FROM cards WHERE card_types LIKE ?",
            (f"%{card_type}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="spell_density",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="graveyard_cast",
                        cand_event=card_type,
                    )
                )

    return results


def _find_artifact_recursion(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find self-sacrificing and ETB artifacts for artifact recursion commanders.

    Osgir copies exiled artifacts -> wants Ichor Wellspring (sac for value,
    then copy from exile). Daretti returns artifacts from graveyard ->
    wants artifacts that put themselves in the graveyard.
    """
    wants_artifact_gy = False
    wants_artifact_copy = False

    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        vf = p.get("valid_filter") or ""
        raw = str(p.get("raw_line") or "")

        if pt == "effect" and ev == "ChangeZone":
            zo = (p.get("zone_origin") or "").strip()
            if zo == "Graveyard" and "Artifact" in vf:
                wants_artifact_gy = True
        if pt == "effect" and ev == "CopyPermanent" and ("Artifact" in vf or "Artifact" in raw):
            wants_artifact_copy = True

    if not wants_artifact_gy and not wants_artifact_copy:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Artifacts with sacrifice cost (self-recycling)
    cur = conn.execute(
        "SELECT DISTINCT cp.card_name FROM card_ports cp "
        "JOIN cards c ON cp.card_name = c.name "
        "WHERE cp.port_type = 'cost' AND cp.event_class = 'sacrifice' "
        "AND c.card_types LIKE '%Artifact%'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="artifact_recursion",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="graveyard_artifact",
                    cand_event="sac_artifact",
                )
            )

    # Artifacts with self-ETB + valuable effects (for copy commanders)
    if wants_artifact_copy:
        cur2 = conn.execute(
            "SELECT DISTINCT cp.card_name FROM card_ports cp "
            "JOIN cards c ON cp.card_name = c.name "
            "WHERE cp.port_type = 'trigger' AND cp.event_class = 'ChangesZone' "
            "AND cp.valid_filter LIKE '%Card.Self%' "
            "AND cp.zone_destination = 'Battlefield' "
            "AND c.card_types LIKE '%Artifact%' "
            "AND cp.card_name IN ("
            "  SELECT card_name FROM card_ports WHERE port_type = 'effect' "
            "  AND event_class IN ('Draw', 'Destroy', 'Token', 'DealDamage', 'Mana')"
            ")"
        )
        for r in cur2.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="artifact_recursion",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="copy_artifact",
                        cand_event="etb_artifact",
                    )
                )

    return results


def _find_copy_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find good copy targets for CopyPermanent commanders.

    Ghired (populate) -> token producers give more targets to populate.
    Riku (creature copy) -> creatures with valuable ETBs are best copy targets.
    """
    has_populate = False
    has_creature_copy = False

    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt != "effect" or ev != "CopyPermanent":
            continue
        raw = str(p.get("raw_line") or "")
        if "'Populate'" in raw:
            has_populate = True
        vf = p.get("valid_filter") or ""
        if "Creature" in vf or "Creature" in raw:
            has_creature_copy = True

    if not has_populate and not has_creature_copy:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Populate: find token producers (more targets to copy)
    if has_populate:
        cur = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'effect' AND event_class = 'Token'"
        )
        for r in cur.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="copy_synergy",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="CopyPermanent_populate",
                        cand_event="Token",
                    )
                )

    # Creature copy: find creatures with self-ETB + valuable effects
    if has_creature_copy:
        _VALUABLE_LIST = (
            "Draw",
            "Destroy",
            "DestroyAll",
            "Token",
            "GainControl",
            "DealDamage",
            "ChangeZone",
            "Mana",
        )
        _val_ph = ",".join("?" * len(_VALUABLE_LIST))
        cur2 = conn.execute(
            "SELECT DISTINCT cp.card_name FROM card_ports cp "
            "JOIN cards c ON cp.card_name = c.name "
            "WHERE cp.port_type = 'trigger' AND cp.event_class = 'ChangesZone' "
            "AND cp.valid_filter LIKE '%Card.Self%' "
            "AND cp.zone_destination = 'Battlefield' "
            "AND c.card_types LIKE '%Creature%' "
            "AND cp.card_name IN ("
            "  SELECT card_name FROM card_ports WHERE port_type = 'effect' "
            f"  AND event_class IN ({_val_ph})"
            ")",
            _VALUABLE_LIST,
        )
        for r in cur2.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="copy_synergy",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="CopyPermanent_creature",
                        cand_event="etb_creature",
                    )
                )

    return results


def _wants_graveyard_recursion(
    cmdr_ports: list[PortRow],
) -> bool:
    """Check if the commander recurs its own cards from graveyard.

    True when the commander has a ChangeZone effect from Graveyard
    (Meren, Karador) or a MayPlay-from-Graveyard static (Kess, Muldrotha).

    False for steal-only commanders like Tergrid whose ChangeZone from
    Graveyard targets ``TriggeredCard`` (opponent's permanents). If a
    commander has both self-recursion AND steal, self-recursion wins.
    """
    has_self_recursion = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "effect" and ev == "ChangeZone":
            zo = (p.get("zone_origin") or "").strip()
            vf = p.get("valid_filter") or ""
            # TriggeredCard = steal from opponent (Tergrid), skip
            if zo == "Graveyard" and "TriggeredCard" not in vf:
                has_self_recursion = True
        if pt == "static" and ev == "Continuous":
            raw = str(p.get("raw_line") or "")
            if "'MayPlay'" in raw and "'Graveyard'" in raw:
                has_self_recursion = True
    return has_self_recursion


def _find_etb_sac_targets(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find creatures with self-ETB + sacrifice cost for reanimation commanders.

    Meren recurs creatures from graveyard -> wants Plaguecrafter (ETB forces
    sacrifice, then sacrifices itself), Spore Frog (sac to Fog), Sakura-Tribe
    Elder (sac to fetch land). These are "sac-recur" creatures.

    Narrow intersection: self-ETB trigger AND sacrifice cost -> ~200-400 cards.
    """
    if not _wants_graveyard_recursion(cmdr_ports):
        return []

    cur = conn.execute(
        "SELECT DISTINCT a.card_name FROM card_ports a "
        "WHERE a.port_type = 'trigger' AND a.event_class = 'ChangesZone' "
        "AND a.valid_filter LIKE '%Card.Self%' "
        "AND a.zone_destination = 'Battlefield' "
        "AND a.card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'cost' AND event_class = 'sacrifice'"
        ")"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="etb_sac_target",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="graveyard_reanimate",
                    cand_event="etb_sac_creature",
                )
            )

    return results
