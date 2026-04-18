"""Utility complement matchers (opponent forcing, wheel, cost payoff, flicker, extra lands)."""

from __future__ import annotations

import re
import sqlite3

from ..graph_engine import _trigger_only_matches_self
from ..penalties import CandidateCache
from .core import PortComplement, PortRow, _is_static_continuous

#: Extracts ``<TYPE>`` from a ``counters_GE<N>_<TYPE>`` qualifier token
#: (e.g. ``counters_GE1_P1P1`` → ``P1P1``). The counter type determines
#: which candidate-side matches count as valid producers/payoffs.
_COUNTER_GATE_RE = re.compile(r"counters_GE\d*_([A-Z0-9]+)")

#: Validator for extracted counter-type tokens before they flow into
#: SQL placeholders. Re-applies the ``[A-Z0-9]+`` shape from
#: ``_COUNTER_GATE_RE``'s capture group so downstream SQL-fragment
#: construction stays safe even if the regex is loosened in future.
#: Mirrors the ``_VALID_*_EXPRS`` guard convention used elsewhere in
#: the codebase (see CLAUDE.md: "SQL fragment interpolation guarded by
#: _VALID_*_EXPRS frozensets + ValueError").
_VALID_COUNTER_TYPE_RE = re.compile(r"^[A-Z0-9]+$")

#: Extracts the ``Affected`` clause value from a Forge static raw_line.
#: Used by ``_has_creatures_are_lands_static`` to pull the type-bending
#: scope (e.g. ``Creature.!token+YouCtrl``). Hoisted to module level to
#: avoid recompiling inside the per-port loop.
_AFFECTED_CLAUSE_RE = re.compile(r"'Affected':\s*'([^']+)'")

#: Extracts the ``AddType`` clause value from a Forge static raw_line
#: (e.g. ``Forest & Land``). Paired with ``_AFFECTED_CLAUSE_RE`` in the
#: creatures-are-lands detector.
_ADD_TYPE_CLAUSE_RE = re.compile(r"'AddType':\s*'([^']+)'")

# ---------------------------------------------------------------------------
# Opponent-forcing constants (used only by _find_opponent_forcing)
# ---------------------------------------------------------------------------

#: Trigger events that can be "fed" by opponent-facing effects.
_OPPONENT_TRIGGER_TO_EFFECT: dict[str, tuple[str, ...]] = {
    "Discarded": ("Discard",),
    "Sacrificed": ("Sacrifice", "SacrificeAll"),
}

#: valid_filter values that indicate opponent-targeting (not self-targeting).
#: Effect events that make a self-ETB trigger worth flickering.
#: Vanilla effects (Pump, Untap) don't justify the cost of repeating
#: the ETB; high-value effects (Dig, modal, GainControl) do.
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


def _find_monarch_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find monarch-related cards for monarch-granting commanders.

    Queen Marchesa's ETB makes her monarch. The archetype pairs with:
    - Other cards that also make you monarch (Courts, Thorn of the
      Black Rose, Archon of Coronation) — stack monarch triggers.
    - Pillowfort statics (``CantAttackUnless``: Ghostly Prison,
      Windborn Muse, Propaganda) — protect the monarch so the upkeep
      card draw keeps firing.

    Pool ~60 cards, IDF ≈ 0.17. The archetype is narrow (Q. Marchesa
    is the canonical commander in the golden set) so collisions with
    other commanders are rare.
    """
    has_monarch = any(
        (p.get("port_type") or "").strip() == "effect" and (p.get("event_class") or "").strip() == "BecomeMonarch"
        for p in cmdr_ports
    )
    if not has_monarch:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE (port_type = 'effect' AND event_class = 'BecomeMonarch') "
        "   OR (port_type = 'static' AND event_class = 'CantAttackUnless')"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="monarch_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="BecomeMonarch",
                    cand_event="monarch_or_pillowfort",
                )
            )
    return results


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


def _find_counter_axis_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """General rule for commanders whose filter axis includes a
    ``counters_GE_<TYPE>`` qualifier on a broad scope.

    Extracts the (main_subject, counter_type) pair from any commander
    port (trigger, scales_with, or static Continuous) whose valid_filter
    carries ``counters_GE_<TYPE>`` AND whose main token is NOT ``Self``
    (self-counter scalers like Incubation Druid / Ochre Jelly describe
    a single card growing with its own counters, not a commander-level
    axis). Marchesa's ``Card.YouCtrl+counters_GE1_P1P1`` trigger and
    Hamza's ``Creature.YouCtrl+counters_GE1_P1P1`` scaler both qualify.

    Emits one complement per card across four priority tiers:

    - ``counter_axis_payoff``: candidate port (scales_with or static
      Continuous) shares the same counter_GE filter — Inspiring Call,
      Armorcraft Judge, Abzan Falconer.
    - ``counter_producer``: candidate has ``effect=PutCounter[All]``
      with matching counter_type and a Creature-targeting valid_filter.
      Activated-with-self-sac cards (Pizzasaur, Selfcraft Mechan) are
      excluded because their PutCounter is an incidental side effect.
    - ``etb_counter_keyword``: ``etbCounter:<TYPE>:N`` keyword — Iron
      Apprentice, Walking Ballista.
    - ``self_recur_keyword`` (P1P1 axis only): Persist / Undying /
      Modular keyword — Glen Elendra, Strangleroot Geist, Arcbound
      Ravager. These cycle P1P1/M1M1 counters natively.

    The gate is narrow (golden-set commanders: Marchesa, Hamza) but
    the logic is fully general — any future commander / card with a
    ``counters_GE`` filter axis activates it automatically.
    """
    counter_types: set[str] = set()
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        if pt not in ("trigger", "scales_with", "static"):
            continue
        vf = (p.get("valid_filter") or "").strip()
        if not vf or "counters_GE" not in vf:
            continue
        # Reject self-only scalers: neither the main token nor any
        # subsequent qualifier in the first OR-alt may be ``Self`` —
        # that describes a single card growing with its own counters
        # (Incubation Druid, Ochre Jelly, Card.Self+counters_*), not
        # a commander-level axis.
        first_alt = vf.split(",")[0]
        alt_tokens = first_alt.replace("+", ".").split(".")
        if any(tok.lstrip("!").strip() == "Self" for tok in alt_tokens):
            continue
        for m in _COUNTER_GATE_RE.finditer(vf):
            counter_types.add(m.group(1))

    if not counter_types:
        return []

    # Build a `counter_type IN (...)` parameter list. In practice this
    # will be a single counter type (P1P1) but the general logic handles
    # multi-axis commanders too. Every extracted token is re-validated
    # against the ``[A-Z0-9]+`` shape before the placeholder string is
    # built — the actual values still bind via SQL parameters, but the
    # explicit guard makes the safety of the f-string interpolation
    # audit-clear and matches the project's _VALID_*_EXPRS convention.
    types_list = sorted(counter_types)
    for ct in types_list:
        if not _VALID_COUNTER_TYPE_RE.fullmatch(ct):
            raise ValueError(f"invalid counter type token: {ct!r}")
    types_placeholder = ",".join("?" * len(types_list))

    # Tier 1: same-axis payoff — scales_with / static Continuous filter
    # mentions ``counters_GE`` + <TYPE>.
    payoff_set: set[str] = set()
    for ct in types_list:
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE ((port_type = 'scales_with' AND valid_filter LIKE '%counters_GE%' AND valid_filter LIKE ?)"
            "       OR (port_type = 'static' AND event_class = 'Continuous' "
            "           AND raw_line LIKE '%Affected%counters_GE%' AND raw_line LIKE ?))",
            (f"%{ct}%", f"%{ct}%"),
        ):
            payoff_set.add(row["card_name"])

    # Tier 2: counter producers. Cards whose PutCounter is gated behind
    # a self-sac cost (their only sac cost targets themselves) are
    # excluded — the PutCounter is an incidental side effect of the
    # activated ability, not a sustainable distributor on the axis.
    producer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' "
            "AND event_class IN ('PutCounter', 'PutCounterAll') "
            f"AND counter_type IN ({types_placeholder}) "
            "AND (valid_filter LIKE '%Creature%' "
            "     OR valid_filter = '' OR valid_filter IS NULL)",
            types_list,
        )
    }
    producer_set = producer_set - _only_self_sac_cost(conn)

    # Tier 3: etbCounter:<TYPE>:N keyword (Iron Apprentice → P1P1:1,
    # Walking Ballista → P1P1:X).
    etb_counter_set: set[str] = set()
    for ct in types_list:
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'keyword' AND event_class LIKE ?",
            (f"etbCounter:{ct}%",),
        ):
            etb_counter_set.add(row["card_name"])

    # Tier 4: Persist / Undying / Modular — only when the axis is
    # P1P1 (the keywords natively cycle +1/+1 and -1/-1 counters).
    self_recur_set: set[str] = set()
    if "P1P1" in counter_types:
        self_recur_set = {
            row["card_name"]
            for row in conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE port_type = 'keyword' "
                "AND event_class IN ('Persist', 'Undying', 'Modular')"
            )
        }

    tier_priority = (
        ("counter_axis_payoff", payoff_set),
        ("counter_producer", producer_set),
        ("etb_counter_keyword", etb_counter_set),
        ("self_recur_keyword", self_recur_set),
    )
    seen: set[str] = set()
    results: list[PortComplement] = []
    for cand_event, candidates in tier_priority:
        for name in candidates:
            if name in cmdr_set or name in seen:
                continue
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="counter_axis_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="counter_axis",
                    cand_event=cand_event,
                )
            )
    return results


def _only_self_sac_cost(conn: sqlite3.Connection) -> frozenset[str]:
    """Return card names whose ONLY sacrifice cost targets themselves.

    A card with a root-level ``cost: sacrifice cost_target=self`` AND no
    other sacrifice cost whose target is ``any`` or ``other`` uses its
    PutCounter effect (if any) as the payoff of an activated ability
    paid by sacrificing itself — not a sustainable distributor.
    """
    with_self = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'cost' AND event_class = 'sacrifice' "
            "AND cost_target = 'self'"
        )
    }
    with_other = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'cost' AND event_class = 'sacrifice' "
            "AND cost_target IN ('any', 'other')"
        )
    }
    return frozenset(with_self - with_other)


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
