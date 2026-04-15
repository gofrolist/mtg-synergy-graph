"""Panharmonicon-style complement matchers."""

from __future__ import annotations

import re
import sqlite3

from ..graph_engine import _trigger_only_matches_self
from .core import PortComplement, PortRow


def _find_panharmonicon_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Panharmonicon-style commanders double specific trigger types.

    Yarok doubles ETB triggers. Isshin doubles attack triggers.
    Teysa doubles death triggers. Every card with a matching non-self
    trigger type is a complement.

    Reads the ``ValidMode`` from the Panharmonicon port's raw_line to
    determine which trigger event classes are doubled.
    """
    doubled_modes: set[str] = set()
    doubled_zones: dict[str, str] = {}  # event -> zone_destination filter

    for p in cmdr_ports:
        ev = (p.get("event_class") or "").strip()
        if ev != "Panharmonicon":
            continue
        raw = str(p.get("raw_line") or "")
        m = re.search(r"'ValidMode':\s*'([^']+)'", raw)
        if not m:
            continue
        modes = [mode.strip() for mode in m.group(1).split(",") if mode.strip()]
        doubled_modes.update(modes)
        # Extract zone destination filter (Teysa: Destination=Graveyard)
        m_dest = re.search(r"'Destination':\s*'([^']+)'", raw)
        if m_dest:
            for mode in modes:
                doubled_zones[mode] = m_dest.group(1)

    if not doubled_modes:
        return []

    placeholders = ",".join("?" * len(doubled_modes))
    cur = conn.execute(
        f"SELECT card_name, event_class, valid_filter, zone_destination, branch_kind "
        f"FROM card_ports "
        f"WHERE port_type = 'trigger' AND event_class IN ({placeholders})",
        tuple(doubled_modes),
    )

    # Collect matching candidates -- both self-ETB and non-self triggers.
    # Self-ETB creatures (Mulldrifter, Coiling Oracle) ARE what Panharmonicon
    # commanders want to double. We gate on valuable effects to avoid flooding.
    _VALUABLE_EFFECTS = frozenset(
        {
            "Draw",
            "Destroy",
            "DestroyAll",
            "Token",
            "GainControl",
            "DealDamage",
            "DamageAll",
            "PutCounter",
            "Mill",
            "Dig",
            "ChangeZone",
            "Sacrifice",
            "SacrificeAll",
            "Mana",
            "LoseLife",
            "GainLife",
        }
    )
    matched: list[tuple[str, str, str, bool]] = []  # (card, ev, branch_kind, is_self)
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        vf = r["valid_filter"] or ""
        is_self = _trigger_only_matches_self(vf)
        ev = r["event_class"]
        if ev in doubled_zones:
            cand_zd = (r["zone_destination"] or "").strip()
            if cand_zd and cand_zd != doubled_zones[ev]:
                continue
        seen.add(card)
        matched.append((card, ev, r["branch_kind"] or "root", is_self))

    if not matched:
        return []

    # Load effects for matched candidates to filter on quality.
    # Single query — SQLite handles large IN clauses efficiently and
    # avoids 12+ round-trip batches (1.15s → ~0.1s for Yarok).
    card_names = [m[0] for m in matched]
    effect_by_card: dict[str, str] = {}
    ph = ",".join("?" * len(card_names))
    rows = conn.execute(
        f"SELECT card_name, event_class FROM card_ports WHERE card_name IN ({ph}) AND port_type = 'effect'",
        tuple(card_names),
    ).fetchall()
    for r in rows:
        cn = r["card_name"]
        eff = r["event_class"]
        if eff in _VALUABLE_EFFECTS and cn not in effect_by_card:
            effect_by_card[cn] = eff

    results: list[PortComplement] = []
    for card, ev, bk, is_self in matched:
        # Skip cards with no valuable effect -- doubling a vanilla
        # trigger is worthless.
        best_eff = effect_by_card.get(card)
        if not best_eff:
            continue

        if is_self:  # noqa: SIM108
            # Self-ETB: use a single IDF group ("ChangesZone_etb") to avoid
            # tiny per-effect-type groups with artificial high IDF.
            cand_ev = f"{ev}_etb"
        else:
            # Non-self: enrich with effect type for narrower IDF groups.
            cand_ev = f"{ev}_{best_eff}"

        results.append(
            PortComplement(
                rule_id="panharmonicon",
                direction="synergy",
                candidate=card,
                cmdr_event=f"Panharmonicon_{ev}",
                cand_event=cand_ev,
                branch_kind=bk,
            )
        )

    return results


def _find_reverse_panharmonicon(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find candidates with Panharmonicon static that double the commander's triggers.

    Harmonic Prodigy has Panharmonicon for Shamans/Wizards. If the commander
    IS a Wizard (Kykar), Harmonic Prodigy doubles all of Kykar's triggers.

    The existing panharmonicon rule only finds candidates whose triggers
    would be doubled by the commander. This is the reverse: candidates
    that double the commander's own triggers.
    """
    # Get commander creature subtypes
    cmdr_list = list(cmdr_set)
    cmdr_subtypes: set[str] = set()
    for row in conn.execute(
        "SELECT subtypes FROM cards WHERE name IN ({})".format(",".join("?" * len(cmdr_list))),
        tuple(cmdr_list),
    ).fetchall():
        if row["subtypes"]:
            cmdr_subtypes.update(row["subtypes"].split())

    # Get commander trigger event classes
    cmdr_trigger_events: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() == "trigger":
            ev = (p.get("event_class") or "").strip()
            if ev:
                cmdr_trigger_events.add(ev)

    if not cmdr_subtypes or not cmdr_trigger_events:
        return []

    # Find Panharmonicon statics where the commander matches ValidCard
    cur = conn.execute(
        "SELECT card_name, raw_line FROM card_ports WHERE port_type = 'static' AND event_class = 'Panharmonicon'"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        raw = str(r["raw_line"])

        # Extract ValidCard filter
        m_vc = re.search(r"'ValidCard':\s*'([^']+)'", raw)
        if not m_vc:
            continue
        valid_card = m_vc.group(1)

        # Check if commander matches ValidCard
        # ValidCard like "Wizard.Other+YouCtrl" -> check if "Wizard" in cmdr_subtypes
        # ValidCard like "Permanent.YouCtrl" -> always matches
        # Only match when ValidCard requires a specific subtype that the
        # commander has. Generic filters (Permanent, Card, Creature) match
        # every commander and would flood results -- those are handled by
        # the regular panharmonicon rule instead.
        matches_cmdr = False
        for alt in valid_card.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base in cmdr_subtypes:
                matches_cmdr = True
                break

        if not matches_cmdr:
            continue

        # Check ValidMode overlap with commander's triggers
        m_vm = re.search(r"'ValidMode':\s*'([^']+)'", raw)
        doubled_modes = {m.strip() for m in m_vm.group(1).split(",") if m.strip()} if m_vm else {"any"}

        # Check if any commander trigger would be doubled
        if "any" in doubled_modes or doubled_modes & cmdr_trigger_events:
            seen.add(card)
            results.append(
                PortComplement(
                    rule_id="panharmonicon",
                    direction="synergy",
                    candidate=card,
                    cmdr_event="reverse_panharmonicon",
                    cand_event="doubles_cmdr_triggers",
                )
            )

    return results


def _find_panharmonicon_stacking(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find Panharmonicon-like cards when the commander IS a Panharmonicon.

    Yarok + Panharmonicon artifact = 4x ETB triggers (they stack).
    This is one of the most powerful synergy pairs in Commander.

    Matches candidates with Panharmonicon static whose ValidMode overlaps
    with the commander's Panharmonicon ValidMode.
    """
    cmdr_pan_modes: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "static":
            continue
        if (p.get("event_class") or "").strip() != "Panharmonicon":
            continue
        raw = str(p.get("raw_line") or "")
        m = re.search(r"'ValidMode':\s*'([^']+)'", raw)
        if m:
            cmdr_pan_modes.update(mode.strip() for mode in m.group(1).split(",") if mode.strip())

    if not cmdr_pan_modes:
        return []

    # Find candidates also having Panharmonicon with overlapping ValidMode
    cur = conn.execute(
        "SELECT card_name, raw_line FROM card_ports WHERE port_type = 'static' AND event_class = 'Panharmonicon'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set:
            continue
        raw = str(r["raw_line"])
        m = re.search(r"'ValidMode':\s*'([^']+)'", raw)
        if not m:
            continue
        cand_modes = {mode.strip() for mode in m.group(1).split(",") if mode.strip()}
        if cand_modes & cmdr_pan_modes:
            results.append(
                PortComplement(
                    rule_id="panharmonicon",
                    direction="synergy",
                    candidate=card,
                    cmdr_event="Panharmonicon_stack",
                    cand_event="Panharmonicon",
                )
            )

    return results
