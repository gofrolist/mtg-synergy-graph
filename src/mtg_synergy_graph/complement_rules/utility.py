"""Utility complement matchers (opponent forcing, wheel, cost payoff, flicker, extra lands)."""

from __future__ import annotations

import re
import sqlite3

from ..graph_engine import _trigger_only_matches_self
from ..penalties import CandidateCache
from .core import (
    _ADD_TYPE_CLAUSE_RE,
    _AFFECTED_CLAUSE_RE,
    PortComplement,
    PortRow,
    _is_static_continuous,
)

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


#: Word-boundary regex matching the standalone ``modified`` qualifier,
#: not substrings (e.g. ``unmodified``). Forge writes the qualifier as
#: a delimited token in valid_filter (``Creature.modified+YouCtrl``) or
#: in raw_line clauses (``Affected: 'Creature.modified+YouCtrl'``).
_MODIFIED_QUALIFIER_RE = re.compile(r"(?<![A-Za-z])modified(?![A-Za-z])")

#: Extracts (clause_key, clause_value) pairs from raw_line where the
#: value contains the ``modified`` qualifier, e.g.
#: ``'IsPresent': 'Card.Self+modified'`` → ``('IsPresent',
#: 'Card.Self+modified')``. Used both to reject Self-anchored clauses
#: and to skip ``TargetsValid`` (the qualifier is on the trigger's
#: targeted-permanent parameter, not on the trigger axis itself —
#: Pearl-Ear's "draw when an Aura targets a modified permanent" is
#: still fundamentally an Aura tribal commander).
_MODIFIED_CLAUSE_RE = re.compile(r"'([^']+)':\s*'([^']*\bmodified\b[^']*)'")

#: raw_line clause keys that carry the modified qualifier as a side
#: condition or as flavor text rather than as a payoff axis. Skip these
#: when deciding whether the commander has modified-axis intent.
#: ``TargetsValid`` qualifies the trigger's *targeted* permanent
#: (Pearl-Ear: "Aura that targets a modified permanent" — fundamentally
#: an Aura tribal commander). ``TriggerDescription`` / ``Description`` /
#: ``SpellDescription`` / ``StackDescription`` are flavor text where the
#: word "modified" appears prose-only and shouldn't drive matching.
_MODIFIED_NON_AXIS_KEYS: frozenset[str] = frozenset(
    {
        "TargetsValid",
        "TriggerDescription",
        "Description",
        "SpellDescription",
        "StackDescription",
        "PrecostDesc",
    }
)


def _is_self_qualified(filter_or_clause: str) -> bool:
    """Return True if the first OR-alt of ``filter_or_clause`` carries a
    ``Self`` token (with optional ``!`` negation prefix).

    Mirrors the self-only rejection used by ``_find_counter_axis_feeders``
    so commander-level "this commander is X" conditions don't get
    promoted to per-card payoff axes.
    """
    first_alt = filter_or_clause.split(",")[0]
    alt_tokens = first_alt.replace("+", ".").split(".")
    return any(tok.lstrip("!").strip() == "Self" for tok in alt_tokens)


def _find_modified_axis_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """General rule for commanders whose filter axis includes the
    ``modified`` qualifier.

    A creature is "modified" iff it has a +1/+1 counter, an Aura
    attached, OR an Equipment attached. Kodama of the West Tree's
    ``Continuous: Affected=Creature.modified+YouCtrl AddKeyword=Trample``
    is the canonical anthem-payoff shape. Red XIII, SP//dr, Sephiroth,
    Chishiro, and Goro-Goro all share the same filter axis through
    different port types (static, trigger, effect.IsPresent).

    Mirrors ``counter_axis_feeder`` for the implicit P1P1 axis (the
    counter mechanic dominates "modified" interpretations on EDHREC),
    plus an Aura/Equipment-keyword tier for cards that grant the other
    two flavors of modification.

    Tier priority:
    - ``modified_p1p1_producer``: ``effect=PutCounter[All]
      counter_type=P1P1`` on Creature scope (excluding self-sac-only).
    - ``modified_proliferate``: any ``effect=Proliferate`` (extends
      existing P1P1 counters).
    - ``modified_aura_equipment_grant``: cards with an etbCounter:P1P1
      keyword OR static that animates / attaches Aura+Equipment to a
      creature on ETB. Cast a wider net than just the subtype check
      (Aura subtype alone matches ~700 cards, mostly irrelevant
      pump-once auras), so we lean on the etb-counter keyword as the
      narrow proxy for modified-on-ETB cards (Iron Apprentice, Walking
      Ballista, all the Modular artifacts).

    Detection scope is wider than counter_axis_feeder — checks effect /
    cost / replacement ports too — because the ``modified`` qualifier
    appears in IsPresent clauses on Token effects and SacValid clauses
    on activated abilities, not just trigger filters.
    """
    has_modified = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        if pt not in ("trigger", "scales_with", "static", "effect", "cost", "replacement"):
            continue
        vf = (p.get("valid_filter") or "").strip()
        if vf and _MODIFIED_QUALIFIER_RE.search(vf):
            if _is_self_qualified(vf):
                continue
            has_modified = True
            break
        raw = str(p.get("raw_line") or "")
        if not raw or not _MODIFIED_QUALIFIER_RE.search(raw):
            continue
        # raw_line may carry the modified qualifier inside an
        # ``Affected``/``ValidCard``/``ValidSource``/``ValidTarget``/
        # ``IsPresent``/``ValidCards`` clause — extract every clause
        # value containing ``modified`` and reject when ALL are
        # Self-anchored. This catches Ian the Reckless's
        # ``IsPresent: Card.Self+modified`` (a self-condition, not a
        # commander-level payoff axis) without losing Goro-Goro's
        # ``IsPresent: Creature.YouCtrl+attacking+modified``.
        clause_pairs = _MODIFIED_CLAUSE_RE.findall(raw)
        axis_clauses = [v for k, v in clause_pairs if k not in _MODIFIED_NON_AXIS_KEYS and not _is_self_qualified(v)]
        if clause_pairs and not axis_clauses:
            continue
        has_modified = True
        break
    if not has_modified:
        return []

    producer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' "
            "AND event_class IN ('PutCounter', 'PutCounterAll') "
            "AND counter_type = 'P1P1' "
            "AND (valid_filter LIKE '%Creature%' "
            "     OR valid_filter = '' OR valid_filter IS NULL)"
        )
    }
    producer_set -= _only_self_sac_cost(conn)

    # Self-counter creatures (Forgotten Ancient, Champion of Lambholt,
    # Managorger Hydra) put P1P1 counters on themselves through their
    # own triggers — they enter as / become "modified" without help.
    # Restrict to creature cards so we don't pull in non-creature
    # self-counter artifacts that aren't on the modified payoff axis.
    self_grower_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT cp.card_name "
            "FROM card_ports cp JOIN cards c ON c.name = cp.card_name "
            "WHERE cp.port_type = 'effect' "
            "AND cp.event_class IN ('PutCounter', 'PutCounterAll') "
            "AND cp.counter_type = 'P1P1' "
            "AND cp.valid_filter LIKE '%Self%' "
            "AND c.types LIKE '%Creature%'"
        )
    }

    # Counter doublers: Hardened Scales, Kami of Whispered Hopes,
    # Doubling Season, Branching Evolution. They ``ReplaceWith:
    # AddOneMoreCounters`` on the AddCounter event for P1P1, making
    # every modified creature grow faster.
    doubler_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'replacement' AND event_class = 'AddCounter' "
            "AND raw_line LIKE '%ValidCounterType%' "
            "AND raw_line LIKE '%P1P1%' "
            "AND raw_line LIKE '%AddOneMoreCounters%'"
        )
    }

    proliferate_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'effect' AND event_class = 'Proliferate'"
        )
    }

    keyword_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'keyword' "
            "AND (event_class LIKE 'etbCounter:P1P1%' OR event_class = 'Modular')"
        )
    }

    tier_priority = (
        ("modified_p1p1_doubler", doubler_set),
        ("modified_p1p1_producer", producer_set),
        ("modified_self_grower", self_grower_set),
        ("modified_proliferate", proliferate_set),
        ("modified_etb_keyword", keyword_set),
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
                    rule_id="modified_axis_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="modified_axis",
                    cand_event=cand_event,
                )
            )
    return results


#: Matches Forge's ``AddPower`` value inside a static Continuous
#: raw_line. Captures integer amounts (``AddPower: '3'``) and scaling
#: SVar references (``AddPower: 'X'``, ``'Y'``, ``'Z'``).
_ADD_POWER_RE = re.compile(r"'AddPower':\s*'([XYZ]|\d+)'")

#: Marker for self-only ``UntapOtherPlayer`` statics. Cards whose
#: ``ValidCard`` is ``Card.Self`` untap themselves on every other
#: player's untap step (Bender's Waterskin, Endbringer, Victory Chimes,
#: Thousand Moons Infantry). They give a tap-cost commander no
#: re-tap headroom on external creatures, so they're excluded from
#: ``tap_type_feeder``'s sustained-untap tier.
_UNTAP_SELF_ONLY_MARKER = "'ValidCard': 'Card.Self'"

#: Extracts the SUBJECT tokens from a ``tapXType<N/SUBJECT[;SUBJECT2]/prose>``
#: cost raw_line. Captures the first segment after the ``/`` separator;
#: individual ``;``-separated subjects are then split by the caller.
_TAP_TYPE_SUBJECT_RE = re.compile(r"tapXType<[^/]+/([^/>]+)")

#: Subject head tokens that resolve to the **artifact** axis
#: (Urza / Shao Jun / Glacian / Meria tap Artifacts, Apothecary
#: White / Cabbage Merchant tap the Food artifact subtype).
#: ``Food`` is included as a literal because it's an Artifact
#: subtype named directly in the cost.
_ARTIFACT_SUBJECT_HEADS: frozenset[str] = frozenset({"Artifact", "Food"})

#: SVar comparison values that flag a commander's hand-size mechanic
#: as a **small-hand** archetype: the mechanic triggers when the
#: hand is empty or nearly empty (``LE0``/``LE1``/``EQ0``) OR is
#: blocked when the hand reaches 2+ cards (``GE2``/``GE3`` paired
#: with ``CantAttack``/``CantBlock``).
#:
#: Hazoret (``SVarCompare: GE2`` on CantAttack = can't unless ≤ 1),
#: Neheb (``LE1`` on +2/+0 static), Djeru and Hazoret (``LE1``
#: vigilance/haste), Flubs (``EQ0`` on draw-vs-discard branch). The
#: hand_size_feeder rule must skip these commanders — they want
#: cards OUT of hand, not in it.
#:
#: ``GE4``/``GE5``/``GE6`` are intentionally absent — they're
#: big-hand threshold signals in the observed corpus (Damia ``LT7``
#: refill-to-7, Kefnet ``LE6`` attack-unless-7+, Jin-Gitaxias
#: ``GE7`` transform-at-7). Any future attempt to expand this set
#: should re-audit the threshold against the 24 hand-size
#: commanders before committing.
_SMALL_HAND_COMPARES: frozenset[str] = frozenset({"LE0", "LE1", "EQ0", "GE2", "GE3"})

#: Matches ``'SVarCompare': 'VALUE'`` and ``'BranchConditionSVarCompare':
#: 'VALUE'`` clauses inside a Forge raw_line. Covers both the plain
#: CheckSVar comparison and the Branch effect's own compare clause
#: used by Flubs-style conditional branches.
_SVAR_COMPARE_RE = re.compile(r"'(?:BranchCondition)?SVarCompare':\s*'([A-Z]+\d+)'")

#: Matches ``'CheckSVar': 'VAR'``, ``'ConditionCheckSVar': 'VAR'``,
#: and ``'BranchConditionSVar': 'VAR'`` clauses. Used together with
#: :const:`_SVAR_COMPARE_RE` to pair a compare value with the SVar
#: it targets — only compares against the hand-size-bound SVar
#: matter for the small-hand rejection.
_CHECK_SVAR_RE = re.compile(r"'(?:ConditionCheckSVar|CheckSVar|BranchConditionSVar)':\s*'([A-Z])'")

#: Matches ``SVar:<NAME>:Count$ValidHand`` bindings. Extracts the
#: SVar name (``X``, ``Y``, ``Z``) that counts hand size. A
#: commander may have more than one — Jin-Gitaxias binds both X
#: and Y to Count$ValidHand.
_HAND_SVAR_BINDING_RE = re.compile(r"SVar:([A-Z]):Count\$ValidHand Card\.YouOwn")


def _find_cardpower_axis_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """General rule for commanders whose abilities scale with their own
    power via ``SVar:<X>:Count$CardPower``.

    ``Count$CardPower`` evaluates to the commander's current power.
    Combustion Man deals that much damage, Krenko Tin Street Kingpin
    makes that many Goblin tokens, Carmen and Alesha reanimate cards
    with mana value up to it, Inferno of the Star Mounts checks for
    power = 20. Every one of these wants **the commander itself
    pumped** — not high-power creatures on the board (the deleted
    ``power_matters`` rule's failure mode was conflating CardPower
    with TotalPower/greatestPower and feeding unrelated beaters).

    Two deduped tiers (highest priority wins per card):

    - ``cardpower_big_attachment``: Equipment/Aura with a static
      Continuous port whose ``AddPower`` value is ``>= 3`` OR a scaling
      SVar (``X``/``Y``/``Z``). Narrows the ~1900 combined Aura+Equipment
      pool to the ~220 "big pump" staples (Colossus Hammer +10,
      Eldrazi Conscription +10/+10, Grafted Wargear +X, Kaldra Compleat
      +5, Battle Mastery, Shadowspear). Small +1/+2 trinkets don't
      meaningfully feed a power-scaling effect.
    - ``cardpower_p1p1_producer``: ``effect=PutCounter[All] P1P1`` on a
      Creature target (excludes Self-only placements and self-sac-only
      distributors, via :func:`_only_self_sac_cost`). Grower
      archetypes (Alesha/Carmen/Krenko TSK/Agatha all put counters on
      themselves as part of their trigger chain) chain with external
      counter producers; non-grower CardPower commanders still benefit
      because a P1P1 counter on the commander raises the count.

    Disjoint from ``voltron`` (which gates on
    Hexproof/Exalted/Shroud/Trample keywords — only 4 of 67 CardPower
    commanders overlap) and from ``modified_axis_feeder`` /
    ``counter_axis_feeder`` (which require a ``modified`` /
    ``counters_GE`` qualifier on the commander — 2 overlap each). The
    CardPower axis is mechanically distinct: the commander itself is
    the scaling target.
    """
    has_cardpower = any(
        (p.get("port_type") or "").strip() == "scales_with" and (p.get("event_class") or "").strip() == "CardPower"
        for p in cmdr_ports
    )
    if not has_cardpower:
        return []

    big_attachment_set: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT cp.card_name, cp.raw_line "
        "FROM card_ports cp JOIN cards c ON c.name = cp.card_name "
        "WHERE cp.port_type = 'static' "
        "AND cp.raw_line LIKE '%AddPower%' "
        "AND (c.types LIKE '%Equipment%' OR c.types LIKE '%Aura%')"
    ):
        raw = row["raw_line"] or ""
        match = _ADD_POWER_RE.search(raw)
        if not match:
            continue
        value = match.group(1)
        if value in ("X", "Y", "Z"):
            big_attachment_set.add(row["card_name"])
            continue
        try:
            if int(value) >= 3:
                big_attachment_set.add(row["card_name"])
        except ValueError:
            continue

    producer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' "
            "AND event_class IN ('PutCounter', 'PutCounterAll') "
            "AND counter_type = 'P1P1' "
            "AND valid_filter LIKE '%Creature%' "
            "AND valid_filter NOT LIKE '%Self%'"
        )
    }
    producer_set -= _only_self_sac_cost(conn)

    tier_priority = (
        ("cardpower_big_attachment", big_attachment_set),
        ("cardpower_p1p1_producer", producer_set),
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
                    rule_id="cardpower_axis_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="cardpower_axis",
                    cand_event=cand_event,
                )
            )
    return results


def _classify_tap_type_axis(cmdr_ports: list[PortRow]) -> frozenset[str]:
    """Resolve the set of axis classes implied by a commander's
    ``cost.tap_type`` ports.

    Returns a subset of ``{"creature", "artifact", "permanent"}``:

    * ``permanent`` — commander taps untyped ``Permanent`` tokens
      (Baylen / Hazel). Matches every subsuming untap card.
    * ``artifact`` — commander taps ``Artifact`` or the ``Food``
      subtype (Urza / Shao Jun / Apothecary White).
    * ``creature`` — default. ``Creature`` itself plus every
      tribal subtype (Wizard for Azami, Knight for Aryel, Elf for
      Lathril, Merfolk for Kumena, Archer, Octopus, Halfling,
      Druid, Rebel, Advisor, Monk, Artificer, ...).

    A commander with mixed subjects (``Artifact;Creature`` on
    Caparocti) yields both classes. Empty result means the gate
    didn't find a valid ``cost.tap_type`` port.
    """
    classes: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "cost":
            continue
        if (p.get("event_class") or "").strip() != "tap_type":
            continue
        raw = str(p.get("raw_line") or "")
        match = _TAP_TYPE_SUBJECT_RE.search(raw)
        if not match:
            continue
        subject_list = match.group(1)
        for subject in subject_list.split(";"):
            # Head token is everything before the first ``.`` — the
            # type or subtype. Qualifiers (``YouCtrl``, ``!token``,
            # ``Other``) follow and don't change the axis class.
            head = subject.split(".", 1)[0].strip()
            if not head:
                continue
            if head == "Permanent":
                classes.add("permanent")
            elif head in _ARTIFACT_SUBJECT_HEADS:
                classes.add("artifact")
            else:
                # Creature, Wizard, Elf, Merfolk, Knight, Rebel,
                # Druid, Advisor, Monk, Artificer, Halfling,
                # Octopus, Archer, ... — all creature-axis.
                classes.add("creature")
    return frozenset(classes)


def _find_tap_type_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders with a ``cost.tap_type`` port
    (``tapXType<N/SUBJECT>``).

    These commanders pay by tapping N untapped permanents of a given
    subject (Wizards for Azami, Artifacts for Urza, Elves for Lathril,
    Knights for Aryel, Merfolk for Kumena). Regardless of the exact
    subject, every one of them wants **to fire the cost twice** — so
    the universal archetype reward is a sustained untap effect that
    refreshes the cost-targets each rotation.

    The axis class is extracted from the cost raw_line via
    :func:`_classify_tap_type_axis` so commanders are fed only untap
    candidates that actually touch their subject: Azami (tapping
    Wizards) gets Creature/Permanent untappers but NOT Unwinding Clock
    (artifact-only), and Urza (tapping Artifacts) gets Unwinding Clock
    but NOT Drumbellower (creature-only). Early spot-checks without
    axis filtering caused regressions on Aryel (-0.145) and Kumena
    (-0.107) by surfacing Artifact untappers to creature-tap tribes.

    Two deduped tiers (highest priority wins per card):

    - ``tap_type_sustained_untap``: ``static.UntapOtherPlayer`` whose
      ``ValidCard`` matches the commander's axis class. ~10 cards per
      axis. Seedborn Muse / Prophet of Kruphix / Murkfiend Liege for
      creature tappers; Unwinding Clock + Seedborn Muse (Permanent)
      for artifact tappers. Archetype-defining — these turn a
      once-per-turn tap-cost ability into a once-per-rotation engine.
    - ``tap_type_phase_untap``: ``trigger.Phase`` paired with
      ``effect.UntapAll`` on a valid_filter matching the axis. ~10
      cards per axis. Awakening, White Plume Adventurer, Virtue of
      Loyalty, Unstoppable Plan. Weaker than tier 1 (once per turn
      instead of once per opponent's turn) but still premium.

    Self-only untaps (Bender's Waterskin, Endbringer, Victory Chimes
    — ``ValidCard: Card.Self``) are rejected because they don't
    refresh external tap-cost targets.
    """
    axis_classes = _classify_tap_type_axis(cmdr_ports)
    if not axis_classes:
        return []

    # Build the set of filter tokens that are valid for this commander.
    # ``Permanent`` always qualifies because it subsumes every axis.
    # Assembled as an immutable frozenset — the tokens are fully
    # derived from ``axis_classes`` and never mutate after this.
    extra: set[str] = set()
    if "creature" in axis_classes or "permanent" in axis_classes:
        extra.add("Creature")
    if "artifact" in axis_classes or "permanent" in axis_classes:
        extra.add("Artifact")
    match_tokens: frozenset[str] = frozenset({"Permanent"} | extra)

    like_clauses = " OR ".join(["raw_line LIKE ?"] * len(match_tokens))
    like_params = [f"%{token}%" for token in sorted(match_tokens)]

    sustained_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            f"SELECT DISTINCT card_name FROM card_ports "
            f"WHERE port_type = 'static' "
            f"AND event_class = 'UntapOtherPlayer' "
            f"AND (raw_line IS NULL OR raw_line NOT LIKE ?) "
            f"AND ({like_clauses})",
            [f"%{_UNTAP_SELF_ONLY_MARKER}%", *like_params],
        )
    }

    # Phase-trigger + UntapAll needs filter matching on the EFFECT's
    # valid_filter (not the trigger's). Build the effect-side clause
    # from the same token set.
    effect_clauses = " OR ".join(["cp2.valid_filter LIKE ?"] * len(match_tokens))
    phase_untap_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            f"SELECT DISTINCT cp1.card_name "
            f"FROM card_ports cp1 "
            f"JOIN card_ports cp2 ON cp2.card_name = cp1.card_name "
            f"WHERE cp1.port_type = 'trigger' AND cp1.event_class = 'Phase' "
            f"AND cp2.port_type = 'effect' AND cp2.event_class = 'UntapAll' "
            f"AND ({effect_clauses})",
            [f"%{token}%" for token in sorted(match_tokens)],
        )
    }

    tier_priority = (
        ("tap_type_sustained_untap", sustained_set),
        ("tap_type_phase_untap", phase_untap_set),
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
                    rule_id="tap_type_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="tap_type_cost",
                    cand_event=cand_event,
                )
            )
    return results


def _is_big_hand_commander(cmdr_ports: list[PortRow]) -> bool:
    """Classify a commander with ``scales_with ValidHand Card.YouOwn``
    as big-hand (wants many cards in hand) or small-hand (wants an
    empty hand).

    The hand-size axis is bidirectional: Alandra/Damia/Kefnet/
    Tishana reward LARGE hands; Hazoret/Neheb/Djeru-and-Hazoret
    reward EMPTY hands. Feeding a SetMaxHandSize: Unlimited card to
    a small-hand commander is anti-synergy.

    The small-hand signal is a comparison on the SVar that binds
    ``Count$ValidHand``: ``LE0``/``LE1``/``EQ0`` (mechanic fires
    when hand is tiny) or ``GE2``/``GE3`` paired with a
    CantAttack / CantBlock blocker (Hazoret pattern — blocked when
    hand reaches 2+). Any such compare on a hand-binding SVar
    returns False.
    """
    hand_svars: set[str] = set()
    for p in cmdr_ports:
        raw = str(p.get("raw_line") or "")
        hand_svars.update(_HAND_SVAR_BINDING_RE.findall(raw))
    if not hand_svars:
        return False  # not a hand-size commander at all

    for p in cmdr_ports:
        raw = str(p.get("raw_line") or "")
        if not raw:
            continue
        checked_svars = set(_CHECK_SVAR_RE.findall(raw))
        if not (checked_svars & hand_svars):
            continue
        compares = set(_SVAR_COMPARE_RE.findall(raw))
        if compares & _SMALL_HAND_COMPARES:
            return False
    return True


def _find_hand_size_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for big-hand commanders — those with ``SVar:<X>:Count$
    ValidHand Card.YouOwn`` where the mechanic rewards MANY cards in
    hand.

    Alandra (pump Drakes by hand size), Damia (draw up to 7 each
    turn), Kefnet (can't attack unless 7+), Tishana (P/T = hand
    size + hand-size draw), Iymrith, Jin-Gitaxias, Kozilek, Soramaro,
    Syr Elenora, Kiyomaro, Jolrael (both), Kagemaro (board wipe -X/-X),
    Krang, Leonardo da Vinci, Mr. Foxglove, Doctor Octopus, Duggan,
    Alrund, A-Alrund, Akuta (more-than-opponents), Eleven the Mage.

    Rejects small-hand counterparts (Hazoret, Neheb, Djeru-and-
    Hazoret, Flubs) via :func:`_is_big_hand_commander` — these want
    an EMPTY hand and would be hurt by SetMaxHandSize: Unlimited
    cards, which let opponents also sit on big hands.

    Single tier:

    - ``hand_size_no_max``: ``static.Continuous`` with
      ``SetMaxHandSize: Unlimited``. Pool ~46 cards. Reliquary
      Tower, Thought Vessel, Library of Leng, Spellbook, Venser's
      Journal, Decanter of Endless Water, Folio of Fancies. These
      are archetype-defining for any big-hand deck — they remove
      the end-of-turn discard constraint that would otherwise cap
      the scaling-axis value at 7.
    """
    if not _is_big_hand_commander(cmdr_ports):
        return []

    no_max_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'static' "
            "AND event_class = 'Continuous' "
            "AND raw_line LIKE '%SetMaxHandSize'': ''Unlimited''%'"
        )
    }

    results: list[PortComplement] = []
    for name in no_max_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="hand_size_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="hand_size_axis",
                cand_event="hand_size_no_max",
            )
        )
    return results


#: Matches Forge's ``NumCards`` clause inside a Mill effect raw_line.
#: Captures integer amounts (``NumCards: '3'``) and scaling SVar
#: references (``NumCards: 'X'`` / ``'Y'`` / ``'Z'``).
_NUM_CARDS_RE = re.compile(r"'NumCards':\s*'([XYZ]|\d+)'")


def _find_gy_fuel_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders whose signature ability pays by **exiling
    cards from any graveyard** — ``cost.exile_from_grave`` with
    ``cost_target='any'``.

    Aphemia (tap + exile Enchantment → 2/2 Zombie), Ashnod (exile
    Creature → draw), Araumi (tap + exile X → encore targets),
    Drivnod (exile 3 Creatures → reset trigger), Egon (exile a
    Creature → Cleric/Demigod activation), Gorex (exile X Creatures
    → +X/+X), Imoen (exile Instant/Sorcery → copy), Ishkanah (exile
    2 cards → Spider token), Kethis (exile 2 legendary → cast from
    GY), Kroxa and Kunoros, Ludevic, Osgir, Sigarda, Taigam, Tawnos,
    Ultimecia, Varina, Winter, and Baron Helmut Zemo.

    The archetype-defining payoff is **self-mill**: the more cards
    in your graveyard, the more often you can pay the cost. Commanders
    with ``cost_target='self'`` (Wilson, Symbiote Spider-Man, Tocasia,
    Venom, Morbius, Spider-Slayer, Beetle) are a different archetype
    — they escape themselves, so their reward is sacrifice outlets
    and dies triggers, not GY filling. They're excluded by the gate.

    Single tier:

    - ``gy_fuel_self_mill``: ``effect.Mill`` with ``Defined: 'You'``
      and ``NumCards >= 3`` OR a scaling SVar (``X``/``Y``/``Z``).
      ~100 cards. Aftermath Analyst, Altar of Dementia (X), Hedron
      Crab, Ashiok Nightmare Weaver, Mesmeric Orb, Sphinx's Tutelage.
      Opponent-targeting (``Opponent``/``EachPlayer``) mills are
      rejected — they don't fill YOUR graveyard. The threshold was
      tightened from 2 to 3 after audit: NumCards=2 cantrip-mills
      (Peek at Peeler / Oneirophage) flooded Osgir's top-30 with
      one-shot cantrips, pushing out her archetype artifact picks
      (-0.093 golden-set NDCG, -0.436 on non-golden Ultimecia).

    Disjoint from ``graveyard_filler`` (fires on commanders with
    trigger.ChangesZone GY→BF or effect=Play from graveyard; these
    exile_from_grave commanders typically lack those ports — the
    gap_report confirmed 0% coverage for this signature before
    the rule was added).
    """
    has_any_target_cost = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "cost":
            continue
        if (p.get("event_class") or "").strip() != "exile_from_grave":
            continue
        if (p.get("cost_target") or "").strip() == "any":
            has_any_target_cost = True
            break
    if not has_any_target_cost:
        return []

    self_mill_set: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class = 'Mill' "
        "AND raw_line LIKE ? "
        "AND raw_line NOT LIKE '%Opponent%' "
        "AND raw_line NOT LIKE '%EachPlayer%'",
        ("%'Defined': 'You'%",),
    ):
        raw = row["raw_line"] or ""
        match = _NUM_CARDS_RE.search(raw)
        if not match:
            continue
        value = match.group(1)
        if value in ("X", "Y", "Z"):
            self_mill_set.add(row["card_name"])
            continue
        try:
            if int(value) >= 3:
                self_mill_set.add(row["card_name"])
        except ValueError:
            continue

    results: list[PortComplement] = []
    for name in self_mill_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="gy_fuel_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="gy_fuel_cost",
                cand_event="gy_fuel_self_mill",
            )
        )
    return results


def _find_lifegain_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders with a ``scales_with LifeYouGainedThisTurn``
    port — their mechanic fires off life gained this turn (draw N
    cards = life gained, deal N damage = life gained, +N/+N until
    end of turn = life gained).

    Aerith, Astarion, Betor, Bre of Clan Stoutarm, Celestine, Cerise,
    Frodo (Adventurous Hobbit), Gollum, Gwaihir, Haliya, Hope Estheim,
    Lathiel, Licia, Ragost, Ratchet, Rodolf, Saint Elenda, Shanna,
    Sorin of House Markov, The Gaffer, Tivash, Will, Willowdusk.

    The axis is strictly **monotonic-positive** — every commander
    wants MORE life gained, unlike hand_size which is bidirectional.
    No rejection filter needed at the gate.

    Two deduped tiers:

    - ``lifegain_amp``: ``replacement.GainLife`` with ``ValidPlayer:
      'You'`` and an amplifying ReplaceWith (``GainDouble`` / ``GainLife``
      / ``ReplaceGain``). Rejects prevention statics (Sulfuric
      Vortex — ``'Prevent': 'True'``) and opponent-targeting
      (Tainted Remedy / Plague Drone — ``ValidPlayer: 'Opponent'``
      converts gain → lose). ~12 cards: Alhammarret's Archive,
      Rhox Faithmender, Boon Reflection, The Wind Crystal, Cleric
      Class, Honor Troll, Heron of Hope, Angel of Vitality, Leyline
      of Hope, Bilbo Birthday Celebrant, Knight of Dawn's Light,
      Phial of Galadriel.
    - ``lifegain_etb_trigger``: ``trigger.ChangesZone`` whose
      ``valid_filter`` references Creature and ``Destination:
      Battlefield``, paired with ``effect.GainLife``. ~45 cards.
      Soul Warden, Auriok Champion, Soul's Attendant, Ajani's
      Welcome, Anointer Priest, Angelic Chorus, Daxos Blessed by
      the Sun, Authority of the Consuls. These fire on every
      creature ETB (yours or opponents') so they stack life gain
      across the whole table.
    """
    has_lifegain_axis = any(
        (p.get("port_type") or "").strip() == "scales_with"
        and (p.get("event_class") or "").strip() == "LifeYouGainedThisTurn"
        for p in cmdr_ports
    )
    if not has_lifegain_axis:
        return []

    amp_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'replacement' "
            "AND event_class = 'GainLife' "
            "AND raw_line LIKE '%''ValidPlayer'': ''You''%' "
            "AND raw_line NOT LIKE '%''Prevent'': ''True''%' "
            "AND ("
            "    raw_line LIKE '%GainDouble%' "
            "    OR raw_line LIKE '%''ReplaceWith'': ''GainLife''%' "
            "    OR raw_line LIKE '%ReplaceGain%'"
            ")"
        )
    }

    etb_trigger_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT cp1.card_name "
            "FROM card_ports cp1 "
            "JOIN card_ports cp2 ON cp2.card_name = cp1.card_name "
            "WHERE cp1.port_type = 'trigger' "
            "AND cp1.event_class = 'ChangesZone' "
            "AND cp1.valid_filter LIKE '%Creature%' "
            "AND cp1.raw_line LIKE '%''Destination'': ''Battlefield''%' "
            "AND cp2.port_type = 'effect' "
            "AND cp2.event_class = 'GainLife'"
        )
    }

    tier_priority = (
        ("lifegain_amp", amp_set),
        ("lifegain_etb_trigger", etb_trigger_set),
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
                    rule_id="lifegain_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="lifegain_axis",
                    cand_event=cand_event,
                )
            )
    return results


#: Matches a Forge ``'SVarCompare': 'GT*'`` or ``'SVarCompare': 'GE*'``
#: clause, signalling an UP-biased threshold ("life > X" / "life >= X").
#: Down-biased commanders (Bane's ``'LEX'`` — "life <= half starting")
#: carry the same ``scales_with.YourLifeTotal`` axis but want inverse
#: payoffs, so the up-bias check is the discriminator.
_UP_BIASED_SVAR_COMPARE_RE = re.compile(r"'SVarCompare':\s*'G[TE]")


def _find_life_total_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders with a ``scales_with YourLifeTotal`` port
    **AND** a strong up-biased lifegain signal.

    Target archetype: "grow with life" / "reward high life" commanders
    whose payoff scales upward. The YourLifeTotal axis alone is
    heterogeneous (Ayli uses life as an exile-power-cap query, Bane
    wants LOW life for indestructible, Cecil/Jerren use life-total
    thresholds as flip conditions — audit 2026-04-20 showed those
    cmdrs regress when fed generic lifegain peers). The up-bias gate
    narrows the rule to: Bilbo (Birthday Celebrant — GainLife
    replacement amp) and Elenda (Saint of Dusk — continuous stat
    boost conditional on ``SVarCompare: GTY`` / ``GEZ``).

    Up-bias signal (at least one of):

    - ``replacement.GainLife`` with ``ValidPlayer: 'You'`` (not
      ``Prevent: True``) — a lifegain amplifier on self.
    - ``static.Continuous`` whose raw line contains
      ``'SVarCompare': 'GT...'`` or ``'SVarCompare': 'GE...'`` (up-
      biased threshold comparing against a life-total SVar).

    Single tier:

    - ``life_total_peer``: other cards carrying ``scales_with.YourLifeTotal``
      that ALSO satisfy ``_is_positive_life_port`` (Lifelink / GainLife
      / replacement.GainLife). ~27 cards: Angel of Vitality, Blood
      Baron of Vizkopa, Divinity of Pride, Honor Troll, Path of
      Bravery, Righteous Valkyrie, Serra Ascendant, Sigarda's
      Splendor, Speaker of the Heavens, Leyline of Hope, Phial of
      Galadriel.
    """
    has_life_axis = any(
        (p.get("port_type") or "").strip() == "scales_with" and (p.get("event_class") or "").strip() == "YourLifeTotal"
        for p in cmdr_ports
    )
    if not has_life_axis:
        return []

    has_up_bias = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ec = (p.get("event_class") or "").strip()
        raw = p.get("raw_line") or ""
        if (
            pt == "replacement"
            and ec == "GainLife"
            and "'ValidPlayer': 'You'" in raw
            and "'Prevent': 'True'" not in raw
        ):
            has_up_bias = True
            break
        if pt == "static" and ec == "Continuous" and _UP_BIASED_SVAR_COMPARE_RE.search(raw):
            has_up_bias = True
            break
    if not has_up_bias:
        return []

    peer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT p1.card_name "
            "FROM card_ports p1 "
            "WHERE p1.port_type = 'scales_with' "
            "AND p1.event_class = 'YourLifeTotal' "
            "AND EXISTS ("
            "  SELECT 1 FROM card_ports p2 "
            "  WHERE p2.card_name = p1.card_name "
            "  AND ("
            "    (p2.port_type='keyword' AND p2.event_class='Lifelink') "
            "    OR (p2.port_type='effect' AND p2.event_class='GainLife' "
            "        AND p2.valid_filter='You') "
            "    OR (p2.port_type='replacement' AND p2.event_class='GainLife')"
            "  )"
            ")"
        )
    }

    results: list[PortComplement] = []
    for name in peer_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="life_total_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="life_total_axis",
                cand_event="life_total_peer",
            )
        )
    return results


def _find_land_bounce_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders whose activated ability requires returning a
    land from the battlefield to the hand as a cost.

    Target archetype: land-bounce / land-recursion commanders. Meloku
    the Clouded Mirror (return land → Spirit token), Mina and Denn
    Wildborn (return land → play extra land), Multani Yavimaya's
    Avatar (return 2 lands → creature pump), Sutina Speaker of the
    Tajuru (return land → ramp / counters), Soramaro First to Dream
    (return land → Dig X), Tameshi Reality Architect (return land →
    flicker/recast non-creature permanent).

    Gate:

    - cmdr has ``cost.return`` port with ``cost_subtype`` of the form
      ``<N>/Land...`` (e.g. ``1/Land``, ``2/Land``, ``1/Land/land``)
    - AND ``cost_target='any'`` (external land, not self-bounce like
      Rootha / Shigeki / Bilbo which tuck themselves)

    Two deduped tiers:

    - ``land_bounce_extra_drops``: ``static.Continuous`` whose raw
      line contains ``AdjustLandPlays``. ~38 cards — Azusa, Explore,
      Exploration, Oracle of Mul Daya, Dryad of the Ilysian Grove,
      Fastbond, Ghirapur Orrery, Rites of Flourishing, Burgeoning,
      Flubs (the Fool), Hugs, Aesi. Turning the land-bounce into a
      neutral tempo play (replay the bounced land, still land drop).
    - ``land_bounce_gy_recur``: ``effect.ChangeZone`` with
      ``zone_origin='Graveyard'`` and ``valid_filter`` containing
      ``Land``, rejecting opponent-targeting. ~56 cards — Crucible of
      Worlds, Ramunap Excavator, Splendid Reclamation, World Shaper,
      Life from the Loam, Emeria Shepherd, Lord Windgrace, Molderhulk,
      Deathrite Shaman (mode), Groundskeeper, Lodestone Bauble. Turns
      destroyed/sacrificed lands back into resources, compounding
      with the bounce loop.
    """
    has_land_return_cost = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "cost":
            continue
        if (p.get("event_class") or "").strip() != "return":
            continue
        if (p.get("cost_target") or "").strip() != "any":
            continue
        cst = (p.get("cost_subtype") or "").strip()
        # cost_subtype is "N/Type" (Forge pattern) — accept "Land" or
        # "Land/land" (typed-and-tagged variants both occur).
        parts = cst.split("/")
        if len(parts) >= 2 and parts[1] == "Land":
            has_land_return_cost = True
            break
    if not has_land_return_cost:
        return []

    # Exclude commanders whose PRIMARY axis is not land-focused: big-
    # hand (scales_with.ValidHand Card.YouOwn — Soramaro) and X-cost
    # spell-payoff (scales_with.xPaid — Tameshi whose bounce cost
    # enables a flicker/recast of a non-creature permanent). For these
    # commanders the land-return is an incidental cost, not the engine,
    # and feeding them AdjustLandPlays / GY-land-recur displaces their
    # real archetype picks (audit 2026-04-20: Soramaro -0.139 NDCG,
    # Tameshi -0.056 on the initial rule without this exclusion).
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "scales_with":
            continue
        ec = (p.get("event_class") or "").strip()
        if ec == "xPaid":
            return []
        if ec.startswith("ValidHand "):
            return []

    extra_drops_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'static' "
            "AND event_class = 'Continuous' "
            "AND raw_line LIKE '%AdjustLandPlays%'"
        )
    }

    gy_recur_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' "
            "AND event_class = 'ChangeZone' "
            "AND zone_origin = 'Graveyard' "
            "AND valid_filter LIKE '%Land%' "
            "AND raw_line NOT LIKE '%Opponent%'"
        )
    }

    tier_priority = (
        ("land_bounce_extra_drops", extra_drops_set),
        ("land_bounce_gy_recur", gy_recur_set),
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
                    rule_id="land_bounce_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="land_bounce_cost",
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


#: ``replacement_result`` values that AMPLIFY damage (DmgTwice → x2,
#: DmgPlus2 → +2, etc). Excludes Prevent / DmgMinus* / DmgHalf* /
#: redirector results that don't increase damage. Used as the gate
#: for the damage-doubler axis on both commander and candidate sides.
_DAMAGE_AMP_RESULTS: frozenset[str] = frozenset(
    {
        "DmgTwice",
        "DmgTriple",
        "DmgPlus",
        "DmgPlus1",
        "DmgPlus2",
        "Dmg2",
        "Dmg3",
        "HarshDmg",
    }
)

#: ``trigger.event_class`` values that fire repeatedly during a normal
#: turn cycle (every cast spell, every upkeep, every land drop).
#: A candidate carrying one of these triggers AND a DealDamage effect
#: targeting an opponent is a "ping engine" worth doubling.
#: Excludes ``Attacks`` / ``DamageDone`` / ``Blocks`` (combat-only —
#: those route through ``combat_enhancer`` instead).
_PING_TRIGGER_EVENTS: frozenset[str] = frozenset(
    {
        "SpellCast",
        "Phase",
        "LandPlayed",
        "TapsForMana",
        "Drawn",
        "Discarded",
        "Sacrificed",
        "ChangesZone",
    }
)


def _find_damage_doubler_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find synergies for damage-amplifier replacement-effect commanders.

    Torbran's ``+2`` static, Gisela / Solphim's ``double`` statics, and
    Tor Wauki / Raphael / Wolverine's similar replacements all share a
    mechanical shape: ``replacement.DamageDone`` with a damage-amp
    ``replacement_result`` (DmgTwice / DmgTriple / DmgPlus*) targeting
    opponent / opponent-permanent. The static is the commander's
    finisher — every damage source becomes more lethal.

    Two tiers:

    - ``damage_amp_stack``: candidates carrying their own opponent-
      facing damage-amp replacement (Furnace of Rath, Fiery
      Emancipation, Dictate of the Twin Gods, Curse of Bloodletting,
      Angrath's Marauders, City on Fire). Multiplicative stacking
      makes each pair of doublers worth disproportionately more than
      either alone — the highest-priority match for these commanders.
    - ``damage_pinger``: candidates with a non-combat repeating
      trigger (SpellCast / Phase / LandPlayed / TapsForMana / Drawn /
      Discarded / Sacrificed / ChangesZone) AND an effect=DealDamage
      port targeting Player / Opponent. Guttersnipe, Firebrand
      Archer, Thermo-Alchemist, Manabarbs, Sulfuric Vortex,
      Kessig Flamebreather, Storm-Kiln Artist. The amplifier turns
      each ping into a serious damage clock.

    Rejected commander shapes:

    - Pure prevention statics (Iroas, Tajic, Emmara, Frodo) — their
      replacement_result is ``Prevent`` or has ``PreventionEffect:
      True`` / ``Prevent: True``. Different mechanical axis.
    - Self-target replacements (Dralnu's "damage to me → sacrifice
      permanents", Polukranos's "damage to me → counters") — the
      ``ValidTarget`` is ``Card.Self`` / ``You`` / ``Permanent.YouCtrl``
      with no opponent-facing target. These are damage-routing
      commanders, not amplifiers.
    - Damage-decreasing replacements (DmgMinus1, DmgHalfDown) — same
      rejection as preventers.
    """
    has_amp = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "replacement":
            continue
        if (p.get("event_class") or "").strip() != "DamageDone":
            continue
        result = (p.get("replacement_result") or "").strip()
        if result not in _DAMAGE_AMP_RESULTS:
            continue
        raw = str(p.get("raw_line") or "")
        # Reject any prevention flag — amp + prevent never coexist on
        # the same port, but defensive check keeps the gate clean.
        if "'Prevent': 'True'" in raw or "'PreventionEffect': 'True'" in raw:
            continue
        # Require an opponent-facing ValidTarget — self-only targets
        # describe damage routing (Dralnu / Polukranos), not amps.
        if not _replacement_targets_opponent(raw):
            continue
        has_amp = True
        break
    if not has_amp:
        return []

    amp_placeholders = ",".join("?" * len(_DAMAGE_AMP_RESULTS))
    amp_list = sorted(_DAMAGE_AMP_RESULTS)
    amp_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'replacement' "
            "AND event_class = 'DamageDone' "
            f"AND replacement_result IN ({amp_placeholders}) "
            "AND raw_line NOT LIKE ? "
            "AND raw_line NOT LIKE ?",
            (*amp_list, "%'Prevent': 'True'%", "%'PreventionEffect': 'True'%"),
        )
    }

    ping_placeholders = ",".join("?" * len(_PING_TRIGGER_EVENTS))
    ping_list = sorted(_PING_TRIGGER_EVENTS)
    ping_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT cp_eff.card_name "
            "FROM card_ports cp_eff "
            "JOIN card_ports cp_trig ON cp_trig.card_name = cp_eff.card_name "
            "WHERE cp_eff.port_type = 'effect' AND cp_eff.event_class = 'DealDamage' "
            "AND (cp_eff.valid_filter LIKE '%Opponent%' "
            "     OR cp_eff.valid_filter LIKE '%Player%' "
            "     OR cp_eff.valid_filter LIKE '%Each%') "
            "AND cp_trig.port_type = 'trigger' "
            f"AND cp_trig.event_class IN ({ping_placeholders})",
            ping_list,
        )
    }

    tier_priority = (
        ("damage_amp_stack", amp_set),
        ("damage_pinger", ping_set),
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
                    rule_id="damage_doubler_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="damage_amp",
                    cand_event=cand_event,
                )
            )
    return results


#: ``ValidTarget`` clause values whose opponent-facing intent is
#: unambiguous. Anything containing one of these substrings counts as
#: opponent-targeting; all else is treated as self-only / ambiguous.
_OPPONENT_TARGET_SUBSTRINGS: tuple[str, ...] = (
    "Opponent",
    "OppCtrl",
    "Player.Opp",
)


def _replacement_targets_opponent(raw_line: str) -> bool:
    """True iff the replacement port's ``ValidTarget`` clause names
    an opponent or opponent-controlled permanent.
    """
    m = re.search(r"'ValidTarget':\s*'([^']+)'", raw_line)
    if not m:
        # No ValidTarget clause = unrestricted (Furnace of Rath shape:
        # all damage anywhere). Treat as opponent-targeting too — the
        # candidate-side query below will still narrow by replacement
        # result.
        return True
    val = m.group(1)
    return any(sub in val for sub in _OPPONENT_TARGET_SUBSTRINGS)


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
