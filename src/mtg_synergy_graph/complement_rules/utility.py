"""Utility complement matchers (opponent forcing, wheel, cost payoff, flicker, extra lands)."""

from __future__ import annotations

import sqlite3

from ..graph_engine import _trigger_only_matches_self
from ..penalties import CandidateCache
from .core import PortComplement, PortRow

# ---------------------------------------------------------------------------
# Opponent-forcing constants (used only by _find_opponent_forcing)
# ---------------------------------------------------------------------------

#: Trigger events that can be "fed" by opponent-facing effects.
_OPPONENT_TRIGGER_TO_EFFECT: dict[str, tuple[str, ...]] = {
    "Discarded": ("Discard",),
    "Sacrificed": ("Sacrifice", "SacrificeAll"),
}

#: valid_filter values that indicate opponent-targeting (not self-targeting).
_OPPONENT_FILTERS: frozenset[str] = frozenset(
    {
        "Opponent",
        "Player",
        "Player.Opponent",
        "Targeted",
        "TargetedPlayer",
        "TriggeredPlayer",
        "TriggeredDefendingPlayer",
        "TriggeredTarget",
        "TargetedController",
    }
)


def _find_opponent_forcing(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find cards that force opponents to perform the commander's trigger.

    Tergrid triggers on Discarded/Sacrificed by opponents -> find cards
    with opponent-facing Discard/Sacrifice effects (Smallpox, Dark Deal,
    Plaguecrafter). Nekusar triggers on Drawn -> find "each player draws"
    effects (Windfall, Wheel of Fortune).

    Narrower than trigger_effect because it only matches opponent-facing
    effects, giving these cards a second rule match and higher ranking.
    """
    # Collect trigger events that fire on opponent actions
    wanted_effects: dict[str, str] = {}  # effect_event -> trigger_event
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        vf = p.get("valid_filter") or ""
        # Must be opponent-triggered (OppCtrl, Opponent, etc.)
        if "Opp" in vf or "Each" in vf or "Player" in vf:
            for eff in _OPPONENT_TRIGGER_TO_EFFECT.get(ev, ()):
                wanted_effects[eff] = ev
            # Drawn trigger on opponent draws -> find "each player draws"
            if ev == "Drawn":
                wanted_effects["Draw"] = ev

    if not wanted_effects:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    for effect_ev, trigger_ev in wanted_effects.items():
        if effect_ev == "Draw":
            # For Draw, match "each player" or "opponent" draws
            cur = conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE port_type = 'effect' AND event_class = 'Draw' "
                "AND (valid_filter IN ('Player', 'Opponent', 'Player.Opponent', "
                "'Targeted', 'TargetedPlayer') "
                "OR valid_filter LIKE '%Each%')"
            )
        else:
            # For Discard/Sacrifice, match opponent-facing effects
            placeholders = ",".join("?" * len(_OPPONENT_FILTERS))
            cur = conn.execute(
                f"SELECT DISTINCT card_name FROM card_ports "
                f"WHERE port_type = 'effect' AND event_class = ? "
                f"AND valid_filter IN ({placeholders})",
                (effect_ev, *_OPPONENT_FILTERS),
            )

        for r in cur.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="opponent_forcing",
                        direction="synergy",
                        candidate=name,
                        cmdr_event=trigger_ev,
                        cand_event=f"force_{effect_ev}",
                    )
                )

    return results


def _find_wheel_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find wheel effects (discard + draw) for Drawn-trigger commanders.

    Locust God triggers on Draw -> Windfall (discard hand, draw equal)
    is premium because it draws 7+ cards at once. Cards with BOTH
    Discard AND Draw effects are wheel effects.

    ~100-200 cards, IDF ~ 0.14 -- higher than plain Draw (2000+, IDF ~ 0.09).
    """
    # Only fire for commanders whose Drawn trigger cares about opponent
    # draws (Nekusar: Card.OppOwn) or any player. Niv-Mizzet (Card.YouOwn)
    # wants self-draw cantrips, not wheels that also draw opponents.
    has_drawn_trigger = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() != "Drawn":
            continue
        vf = p.get("valid_filter") or ""
        if "Opp" in vf or "Player" in vf or "Each" in vf:
            has_drawn_trigger = True
            break

    if not has_drawn_trigger:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Draw' "
        "AND card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'effect' AND event_class = 'Discard'"
        ")"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="wheel_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="Drawn",
                    cand_event="wheel",
                )
            )

    return results


def _find_cost_payoff_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find cards that recoup what the commander discards as a cost.

    Borborygmos Enraged discards Lands to deal damage -> wants cards
    that return lands from graveyard (Crucible of Worlds, Life from
    the Loam) and Retrace/Dredge cards.

    Only fires for commanders with typed discard costs (``Discard<1/Land>``),
    not generic discard (``Discard<1/Card>``).
    """
    # Detect typed discard costs
    discard_types: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "cost":
            continue
        if (p.get("event_class") or "").strip() != "discard":
            continue
        cs = p.get("cost_subtype") or ""
        # e.g. "1/Land" -> "Land"
        parts = cs.split("/")
        if len(parts) >= 2:
            card_type = parts[1]
            # Skip generic types
            if card_type not in ("Card", "Hand", "CARDNAME", "NICKNAME"):
                discard_types.add(card_type)

    if not discard_types:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    for dtype in discard_types:
        # Cards that return discarded type from graveyard
        cur = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' AND event_class = 'ChangeZone' "
            "AND zone_origin = 'Graveyard' "
            "AND valid_filter LIKE ?",
            (f"%{dtype}%",),
        )
        for r in cur.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="cost_payoff",
                        direction="synergy",
                        candidate=name,
                        cmdr_event=f"discard_{dtype}",
                        cand_event="graveyard_return",
                    )
                )

    # Retrace keyword (cast from graveyard by discarding a land)
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'keyword' AND event_class = 'Retrace'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="cost_payoff",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="discard_Land",
                    cand_event="Retrace",
                )
            )

    # Dredge keyword (self-mill to return, keeps lands flowing)
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'keyword' AND event_class LIKE 'Dredge%'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="cost_payoff",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="discard_Land",
                    cand_event="Dredge",
                )
            )

    return results


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
    _HIGH_VALUE_EFFECTS = frozenset(
        {
            "Dig",
            "GenericChoice",
            "GainControl",
        }
    )
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "trigger" and ev == "ChangesZone":
            vf = p.get("valid_filter") or ""
            zd = (p.get("zone_destination") or "").strip()
            if _trigger_only_matches_self(vf) and zd == "Battlefield":
                has_self_etb = True
        if pt == "effect" and ev in _HIGH_VALUE_EFFECTS:
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
    has_flicker_effect = False
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
    has_flicker_effect = cmdr_has_exile and cmdr_has_return

    if not has_flicker_effect:
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
    # Detect: commander has a ChangesZone Land trigger to battlefield
    has_landfall = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        vf = p.get("valid_filter") or ""
        zd = (p.get("zone_destination") or "").strip()
        if pt == "trigger" and ev == "ChangesZone" and "Land" in vf and zd == "Battlefield":
            has_landfall = True
            break

    if not has_landfall:
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
        from ..penalties import _bulk_load_untap_combo_cards

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


def _find_damage_effect_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find DealDamage/DamageAll effects for non-combat damage commanders.

    Niv-Mizzet triggers on DamageDone (any damage, not just combat) ->
    wants ping effects like Guttersnipe, Electrostatic Field.
    Toralf triggers on excess damage -> wants mass damage effects.

    Skips combat-damage-only commanders (Saskia, Derevi) -- those use
    combat_enhancer. Also skips self-only damage triggers.
    DealDamage/DamageAll effects are excluded from the generic
    trigger_effect rule to prevent dilution.
    N ~ 300.
    """
    has_noncombat_damage_trigger = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() != "DamageDone":
            continue
        vf = p.get("valid_filter") or ""
        if _trigger_only_matches_self(vf):
            continue
        raw = str(p.get("raw_line") or "")
        # Skip combat-damage-only triggers (handled by combat_enhancer)
        if "'CombatDamage': 'True'" in raw:
            continue
        has_noncombat_damage_trigger = True
        break

    if not has_noncombat_damage_trigger:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class IN ('DealDamage', 'DamageAll')"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="damage_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="DamageDone",
                    cand_event="DealDamage",
                )
            )

    return results


def _find_mana_doubler_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find mana doublers for TapsForMana trigger commanders.

    Selvala triggers on TapsForMana -> Mana Reflection doubles output.
    Kinnan triggers on TapsForMana -> Nyxbloom Ancient triples mana.

    Matches replacement effects with ProduceMana (mana doublers/triplers).
    Very narrow: N ~ 15-30 (excellent IDF).
    """
    has_mana_trigger = any(
        (p.get("port_type") or "").strip() == "trigger" and (p.get("event_class") or "").strip() == "TapsForMana"
        for p in cmdr_ports
    )
    if not has_mana_trigger:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'replacement' AND replacement_event = 'ProduceMana'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="mana_doubler",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="TapsForMana",
                    cand_event="ProduceMana_doubler",
                )
            )

    return results


#: Effects that make a cascade hit valuable (powerful ETB/cast effects).
_CASCADE_VALUE_EFFECTS: tuple[str, ...] = (
    "Draw",
    "Destroy",
    "DestroyAll",
    "Token",
    "ChangeZone",
    "ChangeZoneAll",
    "DealDamage",
    "GainControl",
    "Mill",
    "Mana",
)


def _find_cascade_value(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """High-CMC value spells for Cascade commanders.

    Maelstrom Wanderer cascades twice → wants expensive spells with
    powerful effects to hit. CMC 7+: ``cascade_high``; CMC 5-6:
    ``cascade_mid``. Low-CMC spells don't benefit from cascade.
    """
    has_cascade = any(
        (p.get("port_type") or "").strip() == "keyword" and "Cascade" in ((p.get("event_class") or "").strip())
        for p in cmdr_ports
    )
    if not has_cascade:
        return []

    ph = ",".join("?" * len(_CASCADE_VALUE_EFFECTS))
    # Cards with CMC 5+ and at least one valuable effect
    cur = conn.execute(
        "SELECT DISTINCT c.name, c.cmc FROM cards c "
        "WHERE c.cmc >= 5 "
        "AND c.name IN ("
        f"  SELECT card_name FROM card_ports "
        f"  WHERE port_type = 'effect' AND event_class IN ({ph})"
        ")",
        _CASCADE_VALUE_EFFECTS,
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["name"]
        if name in cmdr_set:
            continue
        label = "cascade_high" if r["cmc"] >= 7 else "cascade_mid"
        results.append(
            PortComplement(
                rule_id="cascade_value",
                direction="synergy",
                candidate=name,
                cmdr_event="Cascade",
                cand_event="high_cmc_value",
                filter_group=label,
            )
        )
    return results
