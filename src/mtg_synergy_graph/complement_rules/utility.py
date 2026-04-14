"""Utility complement matchers (opponent forcing, wheel, cost payoff, flicker, extra lands)."""

from __future__ import annotations

import sqlite3

from ..graph_engine import _trigger_only_matches_self
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

    # True flicker: ChangeZone Battlefield->Exile AND Exile->Battlefield
    flicker_names: set[str] = set()
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'ChangeZone' "
        "AND zone_origin = 'Battlefield' AND zone_destination = 'Exile' "
        "AND card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'effect' AND event_class = 'ChangeZone' "
        "  AND zone_origin = 'Exile' AND zone_destination = 'Battlefield'"
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
    if not has_tap_cost:
        return []

    # Find Untap effects that can target creatures (commander is a creature)
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
                    rule_id="untap_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="tap_ability",
                    cand_event="Untap",
                )
            )

    return results
