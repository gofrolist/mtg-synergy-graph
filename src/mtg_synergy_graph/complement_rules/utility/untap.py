"""Untap synergies (multi-color untappers, mana-rock untap, Kiki-style combo)."""

from __future__ import annotations

import sqlite3

from ...penalties import CandidateCache
from ..core import (
    PortComplement,
    PortRow,
)


def _find_multicolor_untap(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Narrow rule: untap-effect commanders want multicolor mana dorks.

    Derevi, Empyrial Tactician's combat-damage trigger has a
    ``TapOrUntap Permanent`` effect — every combat damage lets her
    untap a creature. Repeatedly untapping Bloom Tender or Faeburrow
    Elder (produce mana in every color among permanents you control)
    is a premium payoff.

    Gate: commander has ``effect: TapOrUntap`` or ``effect: Untap``
    (not just a tap cost — must actively untap).

    Only 4 cards produce ``EachColorAmong`` mana (Bloom Tender,
    Faeburrow Elder, Sunbird Standard, Tarnation Vista). Narrow match
    → high IDF, giving these a dedicated synergy signal.
    """
    has_untap_effect = any(
        (p.get("port_type") or "").strip() == "effect"
        and (p.get("event_class") or "").strip() in ("TapOrUntap", "Untap", "UntapAll")
        for p in cmdr_ports
    )
    if not has_untap_effect:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Mana' "
        "AND raw_line LIKE '%EachColorAmong%'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="multicolor_untap",
                direction="synergy",
                candidate=name,
                cmdr_event="untap_effect",
                cand_event="eachcolor_dork",
            )
        )
    return results


def _find_untap_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find untap effects for commanders with tap-activated abilities.

    Selvala taps for mana -> Quirion Ranger untaps her -> tap again.
    Krenko taps to make Goblins -> Thousand-Year Elixir lets him untap.
    Emry taps to cast from graveyard -> Mirran Spy untaps her.

    Matches creature-targeted and unfiltered Untap effects against
    commanders with tap costs. N ≈ 150, IDF ≈ 0.14.
    """
    has_tap_cost = any(
        (p.get("port_type") or "").strip() == "cost" and (p.get("event_class") or "").strip() == "tap"
        for p in cmdr_ports
    )

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Forward: commander has tap cost → find untap effects
    if has_tap_cost:
        cur = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' AND event_class = 'Untap' "
            "AND (valid_filter LIKE '%Creature%' "
            "     OR valid_filter = '' OR valid_filter IS NULL)"
        )
        for r in cur.fetchall():
            name = r["card_name"]
            if name not in cmdr_set:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="untap_synergy",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="tap_ability",
                        cand_event="Untap",
                    )
                )

    # Reverse: commander untaps permanents → find mana dorks (tap + Mana)
    # Derevi untaps permanents on combat damage → Bloom Tender, Birds of
    # Paradise become repeatable mana sources.  N ≈ 1684, IDF ≈ 0.09.
    has_untap_effect = any(
        (p.get("port_type") or "").strip() == "effect"
        and (p.get("event_class") or "").strip() in ("TapOrUntap", "Untap")
        for p in cmdr_ports
    )
    if has_untap_effect:
        cur2 = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'cost' AND event_class = 'tap' "
            "AND card_name IN ("
            "  SELECT card_name FROM card_ports "
            "  WHERE port_type = 'effect' AND event_class = 'Mana'"
            ")"
        )
        for r in cur2.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                results.append(
                    PortComplement(
                        rule_id="untap_synergy",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="untap_effect",
                        cand_event="tap_mana",
                    )
                )

    # Extended reverse: tap + valuable non-mana effects (Draw, Token, etc.)
    # Arcanis taps to draw 3, Krenko taps to make tokens — premium untap
    # targets. Separate IDF group from tap_mana; cards with both effects
    # get two complements and a multi-rule bonus.  Restrict to high-value
    # effects only (Draw, Token, Mill) — broader effects (DealDamage,
    # PutCounter, GainLife) match too many cards (~1600 vs ~400).
    if has_untap_effect:
        cur3 = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'cost' AND event_class = 'tap' "
            "AND card_name IN ("
            "  SELECT card_name FROM card_ports "
            "  WHERE port_type = 'effect' AND event_class IN "
            "  ('Draw', 'Token', 'Mill', 'Destroy')"
            ")"
        )
        for r in cur3.fetchall():
            name = r["card_name"]
            # Intentionally skip `seen` check: cards with both tap+Mana
            # and tap+Draw get two complements (tap_mana + tap_utility)
            # in separate IDF groups — this is the desired behaviour.
            if name not in cmdr_set:
                results.append(
                    PortComplement(
                        rule_id="untap_synergy",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="untap_effect",
                        cand_event="tap_utility",
                    )
                )

    return results


def _find_untap_combo(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Match mass/broad-scope untap cards to tap-activated commanders.

    Complements ``_find_untap_synergy`` which handles the narrower
    creature-targeted Quirion-Ranger slice. This rule targets the
    combo half of the same mechanic — Urza / Emry / Isochron-Scepter-
    style engines where untapping *artifacts* or *all permanents*
    doubles or infinitely loops the tap ability.

    Urza's current state illustrates the gap: his tap cost is
    ``tap_type<1/Artifact>`` (not the plain ``tap`` the existing rule
    gates on), so Dramatic Reversal, Unwinding Clock, Voltaic Key,
    Paradox Engine all match zero rules for him today.

    Gate (narrow): commander has a ``cost: tap`` / ``cost: tap_type``
    port **paired with an** ``effect: Mana`` on the same card (Urza,
    Selvala), OR a ``trigger: TapsForMana`` (Kinnan-style untap-
    scaling engine). Tribal tap-for-tokens/draw commanders (Krenko,
    Lathril, Kumena) don't qualify — earlier looser gates regressed
    their tribal-specific picks.

    Candidate pool: see ``_bulk_load_untap_combo_cards`` — classic
    combo shapes (UntapAll, Untap-on-Artifact, UntapOtherPlayer
    statics, TapOrUntapAll).
    """
    has_tap_cost = False
    has_mana_effect = False
    has_tapsformana = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "cost" and ev in ("tap", "tap_type"):
            has_tap_cost = True
        if pt == "effect" and ev == "Mana":
            has_mana_effect = True
        if pt == "trigger" and ev == "TapsForMana":
            has_tapsformana = True
    # Narrow: tap cost *paired with a mana ability* (Urza, Selvala), or
    # TapsForMana trigger (Kinnan). Tribal/non-mana tap costs (Krenko,
    # Lathril, Kumena) don't qualify — their archetype isn't combo
    # mana, and the rule was pushing Dramatic Reversal / Clock of Omens
    # into their top-30 over tribal-specific EDHREC picks.
    if not ((has_tap_cost and has_mana_effect) or has_tapsformana):
        return []

    if candidate_cache is not None:
        pool: frozenset[str] = candidate_cache.untap_combo_cards
    else:
        from ...penalties import _bulk_load_untap_combo_cards

        pool = _bulk_load_untap_combo_cards(conn)

    return [
        PortComplement(
            rule_id="untap_combo",
            direction="synergy",
            candidate=name,
            cmdr_event="tap_engine",
            cand_event="broad_untap",
        )
        for name in pool
        if name not in cmdr_set
    ]
