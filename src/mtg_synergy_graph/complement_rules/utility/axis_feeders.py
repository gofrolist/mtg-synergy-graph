"""Axis feeders (counter, modified, cardpower, tap-type, hand-size) and cost payoff."""

from __future__ import annotations

import re
import sqlite3

from ..core import (
    PortComplement,
    PortRow,
)
from ._shared import (
    _CHECK_SVAR_RE,
    _HAND_SVAR_BINDING_RE,
    _SVAR_COMPARE_RE,
)

_COUNTER_GATE_RE = re.compile(r"counters_GE\d*_([A-Z0-9]+)")


_VALID_COUNTER_TYPE_RE = re.compile(r"^[A-Z0-9]+$")


_MODIFIED_QUALIFIER_RE = re.compile(r"(?<![A-Za-z])modified(?![A-Za-z])")


_MODIFIED_CLAUSE_RE = re.compile(r"'([^']+)':\s*'([^']*\bmodified\b[^']*)'")


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


_ADD_POWER_RE = re.compile(r"'AddPower':\s*'([XYZ]|\d+)'")


_UNTAP_SELF_ONLY_MARKER = "'ValidCard': 'Card.Self'"


_TAP_TYPE_SUBJECT_RE = re.compile(r"tapXType<[^/]+/([^/>]+)")


_ARTIFACT_SUBJECT_HEADS: frozenset[str] = frozenset({"Artifact", "Food"})


_SMALL_HAND_COMPARES: frozenset[str] = frozenset({"LE0", "LE1", "EQ0", "GE2", "GE3"})


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
