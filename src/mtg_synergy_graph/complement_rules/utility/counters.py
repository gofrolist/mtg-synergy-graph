"""Counter-target payoff and creature-untap-engine rules."""

from __future__ import annotations

import sqlite3

from ..core import (
    PortComplement,
    PortRow,
)


def _find_counter_target_payoff(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find +1/+1 counter payoff creatures for XP-scaling P1P1
    distributor commanders.

    The archetype combines TWO mechanics (both Forge-port-level, not
    commander-name): the commander scales with Experience counters
    (``scales_with YourCountersExperience`` — an SVar emitted by any
    card using the XP-counter mechanism, currently Ezuri but open to
    any future printing) AND actively distributes P1P1 counters via
    ``effect=PutCounter[All] counter_type=P1P1`` on Creature.Other.

    Both gates are needed — generalizing to "any P1P1 distributor"
    causes regressions on Ghave (sac-driven), Heliod/Lathiel
    (lifegain-driven), and Hamza (passive scaler) because EDHREC
    data ranks their Hi-Syn around tribal lords / lifegain staples /
    sacrifice payoffs rather than pure counter receivers. The XP axis
    specifically (Ezuri's slow, one-counter-per-ETB trigger) matches
    the counter-receiver payoff pattern exactly. Other counter-caring
    archetypes route through ``counter_axis_feeder``, ``counter_producer``,
    and ``proliferate_synergy``.

    Matches:
    - ``trigger=CounterAdded`` with P1P1 type (Fathom Mage draws,
      Bloodcrazed Hoplite pumps on every counter).
    - ``scales_with=CardCounters.P1P1`` (Gyre Sage, Chasm Skulker,
      Cold-Eyed Selkie — any "counts its own +1/+1 counters" card).

    Pool ~280 cards.
    """
    has_xp_scaling = any(
        (p.get("port_type") or "").strip() == "scales_with" and "YourCountersExperience" in (p.get("event_class") or "")
        for p in cmdr_ports
    )
    if not has_xp_scaling:
        return []

    has_counter_target = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "effect":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in ("PutCounter", "PutCounterAll"):
            continue
        if (p.get("counter_type") or "").strip() != "P1P1":
            continue
        vf = p.get("valid_filter") or ""
        if "Creature" in vf and "Self" not in vf:
            has_counter_target = True
            break
    if not has_counter_target:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE "
        "(port_type = 'trigger' AND event_class = 'CounterAdded' "
        " AND (counter_type = '' OR counter_type IS NULL OR counter_type = 'P1P1')) "
        "OR (port_type = 'scales_with' AND event_class LIKE '%CardCounters.P1P1%')"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="counter_target_payoff",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="PutCounter_P1P1",
                    cand_event="counter_receiver",
                )
            )
    return results


def _find_creature_untap_engine(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find creature-untappers for tap-for-mana commanders.

    Selvala, Heart of the Wilds taps herself for X green mana (where
    X = greatest power you control). Untapping her repeatedly is the
    combo:

    - Quirion Ranger / Scryb Ranger: ``effect=Untap valid_filter=Creature``
      with ``cost=return Forest`` — free untap per Forest.
    - Hyrax Tower Scout: triggered creature-untap on ETB.
    - Staff of Domination: ``effect=Untap`` (any target) plus its own
      payoff modes (draw, GainLife).

    ``untap_combo`` already covers the Urza/Emry artifact-untap pool
    but explicitly excludes ``Untap valid_filter=Creature`` because
    it hurt artifact-combo commanders. ``untap_synergy`` fires for
    every tap-cost commander (Krenko, Kumena) so Selvala's canonical
    untap engines get the same low IDF signal as tribal-tap cards.

    Gate: commander has ``cost=tap`` plain (not ``tap_type``) + an
    ``effect=Mana``. That's tap-for-mana creature commanders
    (Selvala, Bloom Tender-style) and excludes artifact-tap engines
    (Urza) and non-mana tap archetypes (Krenko). Pool ~150 cards.
    """
    has_plain_tap = False
    has_mana_effect = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "cost" and ev == "tap":
            has_plain_tap = True
        elif pt == "effect" and ev == "Mana":
            has_mana_effect = True
    if not (has_plain_tap and has_mana_effect):
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Untap' "
        "AND (valid_filter LIKE '%Creature%' "
        "     OR valid_filter = '' OR valid_filter IS NULL)"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="creature_untap_engine",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="tap_for_mana",
                    cand_event="creature_untap",
                )
            )
    return results
