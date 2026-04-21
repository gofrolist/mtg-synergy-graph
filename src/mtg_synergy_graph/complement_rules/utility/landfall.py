"""Landfall enablers and extra-land-play complement matchers."""

from __future__ import annotations

import sqlite3

from ..core import (
    _ADD_TYPE_CLAUSE_RE,
    _AFFECTED_CLAUSE_RE,
    PortComplement,
    PortRow,
    _is_static_continuous,
)


def _find_extra_land_plays(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Commanders that play extra lands (Azusa) want landfall triggers."""
    has_extra_lands = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "static":
            continue
        raw = str(p.get("raw_line") or "")
        if "AdjustLandPlays" in raw:
            has_extra_lands = True
            break

    if not has_extra_lands:
        return []

    # Find all landfall triggers (ChangesZone Land)
    cur = conn.execute(
        "SELECT card_name, branch_kind FROM card_ports "
        "WHERE port_type = 'trigger' AND event_class = 'ChangesZone' "
        "AND valid_filter LIKE '%Land%' "
        "AND zone_destination = 'Battlefield'"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        seen.add(card)
        results.append(
            PortComplement(
                rule_id="effect_feeds_trigger",
                direction="synergy",
                candidate=card,
                cmdr_event="extra_land_plays",
                cand_event="ChangesZone_Land",
                branch_kind=r["branch_kind"] or "root",
            )
        )

    return results


def _port_cares_about_lands(p: PortRow) -> bool:
    """True when a commander port cares about lands entering battlefield.

    Covers landfall triggers (``trigger=ChangesZone Land zd=Battlefield``)
    and land reanimation effects (``effect=ChangeZone[All] Land
    zo=Graveyard zd=Battlefield``).
    """
    pt = (p.get("port_type") or "").strip()
    ev = (p.get("event_class") or "").strip()
    vf = p.get("valid_filter") or ""
    zo = (p.get("zone_origin") or "").strip()
    zd = (p.get("zone_destination") or "").strip()
    if pt == "trigger" and ev == "ChangesZone" and "Land" in vf and zd == "Battlefield":
        return True
    return (
        pt == "effect"
        and ev in ("ChangeZone", "ChangeZoneAll")
        and "Land" in vf
        and zo == "Graveyard"
        and zd == "Battlefield"
    )


def _has_creatures_are_lands_static(cmdr_ports: list[PortRow]) -> bool:
    """True when commander has a type-bending static that adds ``Land`` to
    creatures you control (Ashaya, Soul of the Wild).

    Semantically, every creature ETB is also a land ETB under this static —
    so the commander mechanically "wants" everything a landfall trigger
    commander wants (Rampaging Baloths, Lotus Cobra, Avenger of Zendikar,
    Scute Swarm).

    Extracts the ``Affected`` + ``AddType`` clauses from the raw static
    dict rather than looking for a specific commander by name. Any future
    card with the same pattern automatically qualifies.
    """
    for p in cmdr_ports:
        if not _is_static_continuous(p):
            continue
        raw = str(p.get("raw_line") or "")
        aff_m = _AFFECTED_CLAUSE_RE.search(raw)
        add_m = _ADD_TYPE_CLAUSE_RE.search(raw)
        if not aff_m or not add_m:
            continue
        if "Creature" in aff_m.group(1) and "Land" in add_m.group(1):
            return True
    return False


def _find_creatures_as_lands_landfall(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Match Ashaya-style commanders with landfall-trigger payoffs.

    Ashaya, Soul of the Wild's type-bending static (``AddType: Forest &
    Land`` on ``Creature.!token+YouCtrl``) makes every creature ETB
    function as a land ETB. That turns the whole landfall family
    (Rampaging Baloths, Lotus Cobra, Avenger of Zendikar, Scute Swarm,
    Emeria Angel, Roil Elemental, Tireless Tracker) into legitimate
    payoffs — but without an explicit ``ChangesZone Land`` trigger on
    the commander herself, ``zone_resonance`` skips her entirely and
    she ends up scoring every land at a flat 0.30 (the ``scaling``
    floor from ``Land.YouCtrl`` SVar) with zero differentiation.

    Gate: ``_has_creatures_are_lands_static`` — detects the Affected
    = Creature / AddType = Land pattern generically from the raw static
    dict. Only Ashaya matches today; any future card with the same
    shape qualifies automatically.

    Candidate pool: ~237 cards with a ``LandPlayed`` trigger or
    ``ChangesZone Land zd=Battlefield`` trigger. IDF ≈ 0.126 baseline;
    the ``creatures_as_lands_landfall`` multiplier lifts landfall
    payoffs above the flat 0.30 land floor so they actually surface.
    """
    if not _has_creatures_are_lands_static(cmdr_ports):
        return []

    rows = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'trigger' "
        "AND ("
        "    event_class = 'LandPlayed'"
        " OR (event_class = 'ChangesZone' AND valid_filter LIKE '%Land%'"
        "     AND zone_destination = 'Battlefield')"
        ") "
        # Reject opponent-scoped triggers (Tectonic Instability-style
        # ``Land.OppCtrl`` landfall-punish cards) — opponents' lands
        # don't fire the commander's "creature = land" static.
        # Matches the defensive pattern used by gy_retrieval /
        # subject_zone_feeder.
        "AND (valid_filter IS NULL OR valid_filter NOT LIKE '%Opp%')"
    ).fetchall()

    return [
        PortComplement(
            rule_id="creatures_as_lands_landfall",
            direction="synergy",
            candidate=r["card_name"],
            cmdr_event="creatures_are_lands",
            cand_event="landfall_payoff",
        )
        for r in rows
        if r["card_name"] not in cmdr_set
    ]


def _find_landfall_enablers(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find land-play enablers for landfall trigger commanders.

    Tatyova/Titania/Omnath trigger on lands entering -- they want cards
    that let you play extra lands (Azusa, Exploration) or replay lands
    from the graveyard (Ramunap Excavator, Crucible of Worlds).

    Reverse of _find_extra_land_plays: that finds landfall triggers for
    extra-land commanders; this finds extra-land candidates for landfall
    commanders.
    """
    # Detect commander ports that care about lands entering battlefield:
    # - ChangesZone Land trigger (Tatyova, Titania, Omnath — landfall)
    # - ChangeZone Land effect from Graveyard to Battlefield (Windgrace —
    #   land reanimation). Both archetypes share the canonical support
    #   pool (Azusa, Crucible, Ramunap) plus landfall payoffs.
    if not any(_port_cares_about_lands(p) for p in cmdr_ports):
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Find candidates with AdjustLandPlays (Azusa, Exploration, Oracle)
    cur = conn.execute(
        "SELECT card_name FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'Continuous' "
        "AND raw_line LIKE '%AdjustLandPlays%'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="landfall_enabler",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="landfall_trigger",
                    cand_event="extra_land_plays",
                )
            )

    # Find candidates with MayPlay Land from Graveyard (Ramunap, Crucible).
    # Require Affected to start with 'Land' to exclude cards like
    # Abandoned Sarcophagus (Card.nonLand) or Chainer (Creature.nonLand).
    cur2 = conn.execute(
        "SELECT card_name FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'Continuous' "
        "AND raw_line LIKE '%MayPlay%' "
        "AND raw_line LIKE '%''Affected'':%Land.%' "
        "AND raw_line LIKE '%Graveyard%'"
    )
    for r in cur2.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="landfall_enabler",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="landfall_trigger",
                    cand_event="land_from_graveyard",
                )
            )

    return results
