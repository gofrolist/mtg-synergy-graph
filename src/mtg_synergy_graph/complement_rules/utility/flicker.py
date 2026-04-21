"""Flicker synergy and flicker-payoff rules."""

from __future__ import annotations

import sqlite3

from ...graph_engine import _trigger_only_matches_self
from ..core import (
    PortComplement,
    PortRow,
)

_FLICKER_HIGH_VALUE_EFFECTS: frozenset[str] = frozenset(
    {
        "Dig",
        "GenericChoice",
        "GainControl",
        # Lagrella, the Magpie exile-a-creature-until-I-leave is
        # functionally a blink on other creatures — flickering Lagrella
        # herself re-triggers the exile and replays the ETBs. The zone
        # check inside _find_flicker_synergy's sibling gate narrows this
        # to Battlefield-origin ChangeZone effects (exile/reanimate
        # shapes) rather than plain tutor-to-hand.
        "ChangeZone",
        # Lavinia of the Tenth's ETB detains opponent permanents — a
        # temporary disable that snaps back when Lavinia leaves, the
        # same mechanical shape as Lagrella's exile-until-I-leave.
        # Flickering Lavinia re-detains different targets = repeated
        # removal. Detain is always on opponent permanents, so no
        # additional zone / filter qualifier is needed beyond the
        # event class.
        "Detain",
    }
)


def _find_flicker_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find flicker/blink effects for self-ETB commanders.

    Gonti, Brago (as target), Sharuum -- commanders whose value comes
    from their own ETB trigger want cards that can repeatedly exile and
    return them to the battlefield.

    Only matches "true flicker" (exile + return) to avoid matching
    plain bounce (585 cards) or exile-removal (1000+ cards).
    """
    # Only fire for commanders whose ETB is a high-value ability --
    # the commander has a self-ETB trigger AND a non-trivial effect
    # (Dig, Effect, GenericChoice, or ChangeZone from opponent) that
    # makes re-triggering the ETB worthwhile.
    # Exclude commanders with many other synergy axes (Emry has
    # artifact synergy; Sidisi has mill) -- flicker is a supplement,
    # not their main strategy.
    has_self_etb = False
    etb_effect_count = 0
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "trigger" and ev == "ChangesZone":
            vf = p.get("valid_filter") or ""
            zd = (p.get("zone_destination") or "").strip()
            if _trigger_only_matches_self(vf) and zd == "Battlefield":
                has_self_etb = True
        if pt == "effect" and ev in _FLICKER_HIGH_VALUE_EFFECTS:
            # ChangeZone is only flicker-worthy when the ETB creates
            # a *temporary exile* that returns later — Lagrella's
            # ``ReturnAbility`` pattern (exile until I leave → replay
            # her ETB to re-exile = double ETB triggers on targets).
            # Plain bounce (Battlefield→Hand, Brinelin) or saga-like
            # exile-then-return-at-end (Vorinclex, Joshua) don't
            # benefit from flickering the commander herself.
            if ev == "ChangeZone":
                raw = str(p.get("raw_line") or "")
                zo = (p.get("zone_origin") or "").strip()
                zd = (p.get("zone_destination") or "").strip()
                is_temporary_exile = zo == "Battlefield" and zd == "Exile" and "ReturnAbility" in raw
                if not is_temporary_exile:
                    continue
            etb_effect_count += 1

    if not has_self_etb or etb_effect_count == 0:
        return []

    # True flicker: ChangeZone Battlefield->Exile AND (Exile|All)->Battlefield
    # Some cards (Conjurer's Closet) use zone_origin='All' for the return step.
    flicker_names: set[str] = set()
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'ChangeZone' "
        "AND zone_origin = 'Battlefield' AND zone_destination = 'Exile' "
        "AND card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'effect' AND event_class = 'ChangeZone' "
        "  AND zone_origin IN ('Exile', 'All') AND zone_destination = 'Battlefield'"
        ")"
    )
    flicker_names.update(r["card_name"] for r in cur.fetchall())

    # Flickerwisp pattern: Battlefield->Exile + DelayedTrigger (return EOT)
    cur2 = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'ChangeZone' "
        "AND zone_origin = 'Battlefield' AND zone_destination = 'Exile' "
        "AND card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE event_class = 'DelayedTrigger'"
        ")"
    )
    flicker_names.update(r["card_name"] for r in cur2.fetchall())

    results: list[PortComplement] = []
    for name in sorted(flicker_names):
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="flicker_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="self_etb",
                    cand_event="flicker",
                )
            )

    return results


def _find_flicker_payoffs(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """ETB creatures with valuable effects for flicker commanders.

    Brago, Aminatou, Emiel exile and return permanents → wants creatures
    whose ETB triggers are valuable (Mulldrifter draws 2, Peregrine Drake
    untaps 5 lands, Aether Channeler modal ETB).

    Detected by commander effect: ChangeZone Battlefield→Exile with a
    paired Exile/All→Battlefield effect (true flicker, not bounce).
    N≈1902 ETB creatures with valuable effects.
    """
    cmdr_has_exile = False
    cmdr_has_return = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt != "effect" or ev != "ChangeZone":
            continue
        zo = (p.get("zone_origin") or "").strip()
        zd = (p.get("zone_destination") or "").strip()
        if zo == "Battlefield" and zd == "Exile":
            cmdr_has_exile = True
        if zo in ("Exile", "All") and zd == "Battlefield":
            cmdr_has_return = True

    if not (cmdr_has_exile and cmdr_has_return):
        return []

    cur = conn.execute(
        "SELECT DISTINCT a.card_name FROM card_ports a "
        "JOIN cards c ON a.card_name = c.name "
        "WHERE a.port_type = 'trigger' AND a.event_class = 'ChangesZone' "
        "AND a.valid_filter LIKE '%Card.Self%' "
        "AND a.zone_destination = 'Battlefield' "
        "AND c.card_types LIKE '%Creature%' "
        "AND a.card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'effect' AND event_class IN "
        "  ('Draw', 'Destroy', 'GainControl', 'Dig', 'ChangeZone', "
        "   'Mana', 'Token', 'Untap', 'Mill')"
        ")"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="flicker_payoff",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="flicker_effect",
                    cand_event="etb_creature",
                )
            )
    return results
