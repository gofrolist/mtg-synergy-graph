"""Political / forced-action / wheel / monarch complement matchers."""

from __future__ import annotations

import sqlite3

from ..core import (
    PortComplement,
    PortRow,
)

_OPPONENT_TRIGGER_TO_EFFECT: dict[str, tuple[str, ...]] = {
    "Discarded": ("Discard",),
    "Sacrificed": ("Sacrifice", "SacrificeAll"),
}


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

    Fires when the commander's Drawn payoff is cumulative — more draws
    always = more value:

    - Opp/Player/Each-facing trigger (Nekusar): wheels punish opponents
      for drawing.
    - Self-facing trigger with a Token or PutCounter effect (Locust
      God creates 1/1 flyer per draw; Chasm Skulker adds counter per
      draw): mass-draw = mass payoff.

    Self-facing triggers with damage/spellcast payoffs (Niv-Mizzet
    Parun) prefer cheap cantrips over wheels — EDHREC runs high-count
    repeatable draws, not burst. Excluded here.

    Cards with BOTH Discard (``Mode: Hand``) AND Draw effects are
    true wheels. Loot (Bag of Holding: NumCards=1) is excluded.
    ~100-200 cards, IDF ~ 0.14.
    """
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
        if "YouCtrl" in vf or "YouOwn" in vf or vf == "":
            has_cumulative_payoff = any(
                (q.get("port_type") or "").strip() == "effect"
                and (q.get("event_class") or "").strip() in ("Token", "PutCounter")
                for q in cmdr_ports
            )
            if has_cumulative_payoff:
                has_drawn_trigger = True
                break

    if not has_drawn_trigger:
        return []

    # True wheels: the Discard effect uses ``Mode: Hand`` (discard the
    # entire hand). Loot cards (Bag of Holding, Faithless Looting)
    # discard NumCards=1 and aren't wheels — they dilute the wheel
    # signal for token/damage-per-draw payoffs (Locust God, Nekusar).
    # Tolerate both Python dict-repr variants (spaced and compact) so
    # an importer formatting change doesn't silently zero out the
    # pool.
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Draw' "
        "AND card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'effect' AND event_class = 'Discard' "
        "  AND (raw_line LIKE '%''Mode'': ''Hand''%' "
        "       OR raw_line LIKE '%''Mode'':''Hand''%')"
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
