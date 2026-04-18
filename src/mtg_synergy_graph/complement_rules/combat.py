"""Combat-related complement matchers."""

from __future__ import annotations

import re
import sqlite3

from ..graph_engine import _trigger_only_matches_self
from ..penalties import CandidateCache
from .core import (
    PortComplement,
    PortRow,
    _commander_subtypes_from_ports,
    _cost_filter_group,
)

#: Card types used for zone-resonance matching.
_PRIMARY_TYPES: frozenset[str] = frozenset({"Creature", "Artifact", "Enchantment", "Land", "Planeswalker"})

#: Generic subjects that don't constrain candidates — a trigger
#: filtering on ``Card`` or ``Permanent`` accepts any permanent type,
#: so no subject-based narrowing applies.
_GENERIC_SUBJECTS: frozenset[str] = frozenset({"Card", "Permanent"})

#: Matches the ``Sac<N/<subject>[/descr]>`` token inside a cost's raw_line.
#: Captures the subject main type (up to `.` or `/` or `>`).
_SAC_TARGET_RE = re.compile(r"Sac<[^>]*?/([A-Za-z]+)(?:[./>])")

#: Matches ``'SacValid': '<Type>`` in a Sacrifice effect's raw_line
#: (Scapeshift, Barter in Blood, Lotus Field). Captures the main type.
_SAC_VALID_RE = re.compile(r"'SacValid':\s*'([A-Za-z]+)")

#: Matches ``'ChangeType': '<Type>`` in a ChangeZoneAll effect's raw_line
#: (Splendid Reclamation, Living Death). Captures the main type.
_CHANGE_TYPE_RE = re.compile(r"'ChangeType':\s*'([A-Za-z]+)")

#: Matches ``'Defined': '<Scope>`` in any effect's raw_line. Used to
#: reject opponent-forcing effects (``Defined: Opponent``) that don't
#: feed a YouCtrl-scoped trigger.
_DEFINED_RE = re.compile(r"'Defined':\s*'([A-Za-z.]+)")


def _cmdr_trigger_subject_types(cmdr_ports: list[PortRow]) -> frozenset[str]:
    """Return the set of main subject types across the commander's
    ChangesZone death triggers.

    Extracts the first token of each OR-alternative in the valid_filter
    (e.g. ``Land.YouCtrl`` → ``Land``, ``Creature.Other+YouCtrl`` →
    ``Creature``). Generic subjects (``Card`` / ``Permanent``) and
    self-only (``Self``) are excluded — they don't narrow candidates.
    Returns empty when no qualifying trigger exists or all filters are
    generic, signalling the caller to fall back to broad matching.
    """
    subjects: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() != "ChangesZone":
            continue
        if (p.get("zone_destination") or "").strip() != "Graveyard":
            continue
        vf = (p.get("valid_filter") or "").strip()
        if not vf or _trigger_only_matches_self(vf):
            continue
        for alt in vf.split(","):
            tokens = alt.replace("+", ".").split(".")
            if not tokens:
                continue
            main = tokens[0].lstrip("!").strip()
            if not main or main == "Self":
                continue
            if main in _GENERIC_SUBJECTS:
                continue
            subjects.add(main)
    return frozenset(subjects)


def _sac_target_subject(cost_raw_line: str) -> str:
    """Extract the sacrifice target subject from a cost port's raw_line.

    ``Sac<1/Land>`` → ``Land``
    ``Sac<1/CARDNAME/this creature>`` → ``CARDNAME`` (self-sac)
    ``2 G Sac<1/Land>`` → ``Land``
    ``Sac<1/Creature.YouCtrl>`` → ``Creature``
    """
    if not cost_raw_line:
        return ""
    m = _SAC_TARGET_RE.search(cost_raw_line)
    return m.group(1) if m else ""


#: Effect event classes that unambiguously signal an "attack = engine"
#: pattern where extra combats translate to a repeated material
#: payoff. A single one of these on the commander is sufficient.
_ATTACKS_ENGINE_EFFECTS: frozenset[str] = frozenset(
    {
        "AddPhase",  # Scourge of the Throne, Moraug
        "Dig",  # Etali (exile top + cast)
        "Play",  # Etali's chain paired with Dig
        "Mana",  # Neheb-style mana engines on attack
        "Token",  # Tovolar, Krenko-on-attack creators
        "DealDamage",  # Ping-on-attack commanders
        "Discard",  # Attack-into-discard (Rakdos, Raiyuu)
        "Mill",  # Attack-mill payoffs
    }
)


#: Effect event classes that are "value" but not individually engine-like.
#: A commander needs two or more of these on the same attack chain to
#: qualify for combat enhancement — Etali's Dig + Play pair, Scourge's
#: UntapAll + AddPhase. Single instances are voltron (Wyleth: Draw)
#: or enchantress (Zur: ChangeZone tutor) signals, handled by other
#: rules.
_ATTACKS_VALUE_EFFECTS_THRESHOLD: frozenset[str] = frozenset(
    {
        "Draw",
        "ChangeZone",
        "ChangeZoneAll",
        "PutCounter",
        "LoseLife",
        "GainLife",
        "Destroy",
    }
)


def _attacks_trigger_has_value_effect(cmdr_ports: list[PortRow]) -> bool:
    """True when an Attacks-Self trigger commander has an effect chain
    that converts extra combat steps into repeated material payoff.

    Two acceptance paths:

    1. **At least one engine effect** (``_ATTACKS_ENGINE_EFFECTS``) —
       AddPhase, Dig, Play, Mana, Token, DealDamage, Discard, Mill.
       Each implies the commander is trading attacks for resources,
       and extra combats multiply that.

    2. **Multiple value effects** (>=2) on the same card — Etali's
       Dig+Play chain, Scourge's UntapAll+AddPhase. A multi-effect
       on-attack engine rewards combat multipliers even if no single
       effect is on the engine list.

    Single-effect voltron triggers (Wyleth: Draw only; Zur:
    ChangeZone tutor only) fail both paths and stay out. Stacking
    attack-combat enhancers on them displaces their voltron /
    enchantress axes (Zur NDCG 0.27 → 0.15 when this gate was
    looser).
    """
    effect_count = 0
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        if pt != "effect":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev in _ATTACKS_ENGINE_EFFECTS:
            return True
        if ev in _ATTACKS_VALUE_EFFECTS_THRESHOLD:
            effect_count += 1
    return effect_count >= 2


def _find_combat_enhancers(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find extra-combat and double-strike cards for combat-trigger commanders.

    Two families of commanders benefit from combat enhancers:

    1. **DamageDone triggers** that watch arbitrary creatures (Saskia
       the Unyielding). Doubling damage or adding combat steps
       multiplies her payoff directly.

    2. **Attacks Card.Self triggers** with a valuable on-attack effect
       — Etali, Primal Storm's "exile top of each library and play
       it free" chain, Neheb the Eternal's mana-on-damage, Scourge of
       the Throne's ascend. Extra combat steps multiply the free
       casts / mana / damage-based effects; Double Strike pushes an
       otherwise single swing into two. Gating on "Attacks trigger +
       non-trivial effect chain" keeps vanilla attack-matters
       creatures out — they already get signal from the tribal /
       voltron axes.
    """
    has_damage_trigger = False
    has_self_attack_with_value = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev == "DamageDone":
            vf = p.get("valid_filter") or ""
            if _trigger_only_matches_self(vf):
                continue
            # The `is_combat` flag in card_ports is set iff the trigger's
            # raw ``CombatDamage`` clause is True — the only unambiguous
            # signal that the commander watches *combat* damage (Saskia,
            # Edward Kenway) rather than spell / ping damage (Imodane,
            # Ghyrson Starn). Extra combat steps and Double Strike only
            # multiply the payoff when the trigger is combat-gated.
            if not p.get("is_combat"):
                continue
            has_damage_trigger = True
            break
        if ev == "Attacks":
            vf = p.get("valid_filter") or ""
            if _trigger_only_matches_self(vf):
                has_self_attack_with_value = _attacks_trigger_has_value_effect(cmdr_ports)

    if not has_damage_trigger and not has_self_attack_with_value:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Extra combat steps
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'effect' AND event_class = 'AddPhase'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="combat_enhancer",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="DamageDone",
                    cand_event="AddPhase",
                )
            )

    # Double Strike keywords
    cur2 = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'keyword' AND event_class = 'Double Strike'"
    )
    for r in cur2.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="combat_enhancer",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="DamageDone",
                    cand_event="DoubleStrike",
                )
            )

    return results


def _find_evasion_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find self-unblockable creatures for combat-damage trigger commanders.

    Yuriko, Saskia, Derevi, etc. trigger on combat damage to players.
    Creatures with CantBlockBy (unblockable) statics reliably connect
    for damage triggers.

    Only matches self-unblockable (ValidAttacker contains Self) -- not
    cards that grant unblockable to other creatures.
    """
    # Only fire for commanders whose combat damage trigger applies to
    # OTHER creatures (Yuriko: Ninja.YouCtrl, Saskia: Creature.YouCtrl,
    # Derevi: Creature.YouCtrl). Skip self-damage triggers (Brago,
    # Lathril -- they only care about their OWN damage, not evasion on others).
    has_combat_damage = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() != "DamageDone":
            continue
        raw = str(p.get("raw_line") or "")
        vf = p.get("valid_filter") or ""
        if "'CombatDamage': 'True'" in raw and not _trigger_only_matches_self(vf):
            has_combat_damage = True
            break

    if not has_combat_damage:
        return []

    # Skip for tribal commanders -- they already get evasive tribe members
    # from tribal_density. Adding generic unblockable creatures dilutes
    # a focused tribal strategy (e.g., Yuriko's Ninjas).
    subtypes = _commander_subtypes_from_ports(conn, list(cmdr_set), cmdr_ports)
    if subtypes:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'CantBlockBy' "
        "AND (raw_line LIKE '%Creature.Self%' OR raw_line LIKE '%Card.Self%')"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="evasion",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="DamageDone_combat",
                    cand_event="unblockable",
                )
            )

    return results


def _find_sacrifice_outlets(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find sacrifice outlet candidates for commanders with death triggers.

    A commander's ``ChangesZone`` trigger on ``Battlefield → Graveyard``
    with a ``valid_filter`` like ``Land.YouCtrl`` or ``Creature.Other``
    only fires when a permanent of the filtered subject type dies. This
    rule extracts the subject type(s) (Land, Creature, Artifact, Zombie,
    …) and hard-filters candidate sacrifice costs so only Sac<subject>
    targets that align with the commander's filter pass through. When
    the commander's subject is generic (``Card`` / ``Permanent``) every
    sac target qualifies. The hard filter is the real signal improver
    — tagging candidate subjects into ``filter_group`` was considered
    and rejected because it shattered the IDF pool into ~30 tiny
    buckets with inflated per-card weight; the emitted complement
    keeps the legacy ``free_outlet`` / ``paid_outlet`` / ``self_sac``
    cost-payment structure in ``filter_group``.

    A second narrowing applies to triggers gated on ``counters_GE_P1P1``
    (Marchesa): self-sac costs only qualify when the card also has a
    counter mechanic (Persist / Undying / Modular / etbCounter /
    PutCounter P1P1) — a self-sac without a counter can't satisfy the
    trigger.
    """
    has_death_trigger = False
    counters_gated = False
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev != "ChangesZone":
            continue
        vf = p.get("valid_filter") or ""
        if _trigger_only_matches_self(vf):
            continue
        zd = (p.get("zone_destination") or "").strip()
        if zd == "Graveyard":
            has_death_trigger = True
            if "counters_" in vf and "P1P1" in vf:
                counters_gated = True
            if not counters_gated:
                continue
            break

    if not has_death_trigger:
        return []

    # Extract commander trigger's subject-type constraint. When empty /
    # generic, every sac target qualifies. Otherwise, candidate sac
    # subjects must align. ``Creature`` also matches sacs of creature
    # subtypes (Zombie, Goblin, …) which Forge writes as e.g. Sac<Zombie>.
    subject_constraint = _cmdr_trigger_subject_types(cmdr_ports)

    cur = conn.execute(
        "SELECT card_name, event_class, cost_target, raw_line FROM card_ports "
        "WHERE port_type = 'cost' AND event_class = 'sacrifice'"
    )

    counter_mechanic_cards: set[str] = set()
    if counters_gated:
        counter_mechanic_cards = {
            row["card_name"]
            for row in conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE (port_type = 'keyword' AND ("
                "      event_class = 'Persist' "
                "   OR event_class = 'Undying' "
                "   OR event_class = 'Modular' "
                "   OR event_class LIKE 'etbCounter:P1P1%'"
                "  )) "
                "OR (port_type = 'effect' "
                "   AND event_class IN ('PutCounter', 'PutCounterAll') "
                "   AND counter_type = 'P1P1')"
            )
        }

    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        seen.add(card)
        fg = _cost_filter_group(dict(r))
        if counters_gated and fg == "self_sac" and card not in counter_mechanic_cards:
            continue

        # Subject-type narrowing. Extract the candidate's sac target
        # from raw_line; if the commander has a specific subject
        # constraint and the candidate's target is a DIFFERENT concrete
        # subject, skip. Empty / missing raw_line keeps the card
        # (benefit of the doubt — can't verify mismatch). ``CARDNAME``
        # is a self-sac placeholder; we keep it too since the dying
        # card's type can't be read from raw_line alone. Non-Creature
        # specific subjects (Land, Artifact) further drop CARDNAME
        # self-sacs — those are predominantly creatures, not lands.
        sac_subject = _sac_target_subject(r["raw_line"] or "")
        if subject_constraint and sac_subject:
            if sac_subject == "CARDNAME":
                # Self-sac placeholder: drop unless the commander's
                # constraint is creature-family (most self-sacs are
                # creatures). Counter-gated flows already filter these
                # via counter_mechanic_cards above.
                creature_constraint = "Creature" in subject_constraint or any(
                    s not in _PRIMARY_TYPES and s not in _GENERIC_SUBJECTS for s in subject_constraint
                )
                if not creature_constraint:
                    continue
            elif sac_subject not in subject_constraint and not _subject_alignment(subject_constraint, sac_subject):
                continue

        # filter_group kept as the legacy cost-payment structure
        # (free_outlet / paid_outlet / self_sac). Tagging the candidate
        # sac subject too would shatter the IDF pool into ~30 tiny
        # buckets with inflated per-card weight; the hard subject filter
        # above is the real win because it removes non-matching sacs
        # from the pool entirely.
        results.append(
            PortComplement(
                rule_id="cost_feeds_trigger",
                direction="synergy",
                candidate=card,
                cmdr_event="ChangesZone_death",
                cand_event="sacrifice",
                filter_group=fg,
            )
        )

    return results


def _find_subject_zone_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """General subject-axis feeders for commanders with a death trigger
    on a specific subject type.

    A commander with a ``ChangesZone Battlefield→Graveyard`` trigger on
    ``Land.YouCtrl`` (Titania), ``Creature.Other+YouCtrl`` (Meren),
    ``Zombie.Other+YouCtrl`` (Wilhelt) cares about the subject type —
    her trigger only fires when cards of that subject die. Two
    candidate-side effect classes feed that axis:

    - ``sac_type_effect`` (cand_event): ``effect=Sacrifice`` with
      ``SacValid=<subject>`` — Scapeshift (sac all Lands), Lotus Field's
      ETB-sac of 2 Lands, Barter in Blood (sac 2 Creatures). Triggers
      the commander once per sacrificed permanent.
    - ``mass_return_to_battlefield`` (cand_event): ``effect=ChangeZoneAll``
      with ``ChangeType=<subject>``, ``Origin=Graveyard``,
      ``Destination=Battlefield`` — Splendid Reclamation for Lands,
      Living Death for Creatures. The subsequent sacrifice-for-value
      cycle is the archetype's kill loop.

    The same subject constraint as ``_find_sacrifice_outlets`` applies:
    ``Card`` / ``Permanent`` are too generic and never activate this
    rule; creature-subtypes (Zombie, Goblin) align with ``Creature``.
    """
    subject_constraint = _cmdr_trigger_subject_types(cmdr_ports)
    if not subject_constraint:
        return []

    # Restrict to non-Creature primary types. Creature-subject commanders
    # (Meren, Wilhelt, Slimefoot, Judith) already surface individual
    # creature-recursion via ``cost_feeds_trigger`` (Sac<Creature>),
    # ``etb_sac_target`` (Plaguecrafter-style), ``gy_loader`` (Entomb-
    # style), and ``dies_drain`` (Blood Artist-style). Adding mass-sac
    # and mass-return effects on top displaces their tribal-lord and
    # aristocrat-payoff Hi-Syn picks (Death Baron, Cemetery Reaper,
    # Hateful Eidolon) with generic mass effects (Accursed Centaur,
    # Custodi Lich). Land / Artifact / Enchantment commanders have no
    # comparable toolkit so the mass axis is the archetype's core.
    non_creature_subjects = {s for s in subject_constraint if s in _PRIMARY_TYPES and s != "Creature"}
    if not non_creature_subjects:
        return []
    subject_constraint = frozenset(non_creature_subjects)

    sac_effect_rows = conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Sacrifice' "
        "AND raw_line LIKE '%SacValid%'"
    ).fetchall()

    mass_return_rows = conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'ChangeZoneAll' "
        "AND raw_line LIKE '%''ChangeType''%' "
        "AND raw_line LIKE '%''Origin'': ''Graveyard%' "
        "AND raw_line LIKE '%''Destination'': ''Battlefield%'"
    ).fetchall()

    def _targets_your_side(raw_line: str) -> bool:
        """Return True if the sacrifice/return effect acts on YOUR
        permanents (``Defined`` is You, Player — each player — or
        unspecified, which defaults to activator). Opponent-only
        effects (Goremand, Butcher of Malakir, Eradicator Valkyrie)
        sacrifice the opponent's creatures and don't feed a
        YouCtrl-scoped death trigger."""
        m = _DEFINED_RE.search(raw_line or "")
        if not m:
            return True  # unspecified → activator default
        defined = m.group(1)
        # Reject Opponent / Player.Opponent / Opponents
        return not defined.startswith("Opponent") and not defined.startswith("Player.Opp")

    results: list[PortComplement] = []
    seen: set[str] = set()

    def _emit(name: str, cand_event: str) -> None:
        if name in cmdr_set or name in seen:
            return
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="subject_zone_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="ChangesZone_death",
                cand_event=cand_event,
            )
        )

    for r in sac_effect_rows:
        raw = r["raw_line"] or ""
        m = _SAC_VALID_RE.search(raw)
        if not m:
            continue
        subj = m.group(1)
        if not (subj in subject_constraint or _subject_alignment(subject_constraint, subj)):
            continue
        if not _targets_your_side(raw):
            continue
        _emit(r["card_name"], "sac_type_effect")

    for r in mass_return_rows:
        m = _CHANGE_TYPE_RE.search(r["raw_line"] or "")
        if not m:
            continue
        subj = m.group(1)
        if subj in subject_constraint:
            _emit(r["card_name"], "mass_return_to_battlefield")

    return results


def _subject_alignment(constraint: frozenset[str], sac_subject: str) -> bool:
    """Return True when a candidate's sac target subject aligns with any
    subject the commander trigger filters on.

    Handles the common ``Creature`` ↔ creature-subtype case: a Korvold
    commander filtering on ``Dragon`` should accept Sac<Creature>, and
    a Meren on ``Creature`` accepts Sac<Zombie> / Sac<Goblin>. Land,
    Artifact, and Enchantment align only to themselves.
    """
    if sac_subject in constraint:
        return True
    # Creature-family alignment: if commander filters on Creature or a
    # creature subtype, accept sacrifices of any creature-family subject.
    creature_constraint = "Creature" in constraint or any(
        s not in _PRIMARY_TYPES and s not in _GENERIC_SUBJECTS for s in constraint
    )
    return creature_constraint and (
        sac_subject == "Creature" or (sac_subject not in _PRIMARY_TYPES and sac_subject not in _GENERIC_SUBJECTS)
    )


def _find_changeszone_resonance(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Zone-filtered ChangesZone resonance: find candidates that trigger
    on the same zone transition as the commander.

    Tatyova triggers on ``ChangesZone|Land`` -> find other cards that
    also trigger on lands entering (landfall payoffs). Omnath triggers
    on ``ChangesZone|Elemental`` -> find elemental-ETB payoffs.
    """
    # Collect (type, zone_destination) axes from commander triggers
    cmdr_axes: set[tuple[str, str]] = set()
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev != "ChangesZone":
            continue
        vf = p.get("valid_filter") or ""
        if not vf or _trigger_only_matches_self(vf):
            continue
        zd = (p.get("zone_destination") or "").strip() or "Battlefield"
        for alt in vf.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base in _PRIMARY_TYPES:
                cmdr_axes.add((base, zd))

    if not cmdr_axes:
        return []

    cmdr_types = {t for t, _ in cmdr_axes}

    cur = conn.execute(
        "SELECT card_name, valid_filter, zone_destination, branch_kind "
        "FROM card_ports "
        "WHERE port_type = 'trigger' AND event_class = 'ChangesZone' "
        "AND valid_filter IS NOT NULL AND valid_filter != ''"
    )
    results: list[PortComplement] = []
    seen: set[tuple[str, str]] = set()  # (card, type_zd) to allow multi-axis
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set:
            continue
        vf = r["valid_filter"] or ""
        if _trigger_only_matches_self(vf):
            continue
        cand_zd = (r["zone_destination"] or "").strip() or "Battlefield"
        for alt in vf.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base not in cmdr_types:
                continue
            # Match zone_destination: ETB↔ETB, death↔death
            if (base, cand_zd) not in cmdr_axes:
                continue
            dedup_key = (card, f"{base}_{cand_zd}")
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            results.append(
                PortComplement(
                    rule_id="zone_resonance",
                    direction="synergy",
                    candidate=card,
                    cmdr_event="ChangesZone",
                    cand_event=f"{base}_{cand_zd}",
                    filter_group=base,
                    branch_kind=r["branch_kind"] or "root",
                )
            )
            break

    return results


def _has_creature_attack_trigger(cmdr_ports: list[PortRow]) -> bool:
    """Return True iff the commander AMPLIFIES other creature-attack
    triggers via an Isshin-style ``Panharmonicon`` static.

    Narrow by design. Commanders that *have* an Attacks trigger
    themselves (Edgar Markov, Adeline, Rafiq, The Ur-Dragon) are
    already served by existing ``trigger_resonance`` /
    ``combat_enhancer`` rules plus their tribal / voltron axes —
    stacking ``attack_payoffs`` on top promoted generic attack
    creatures over the tribal / Aura picks that their EDHREC Hi-Syn
    actually values (Rafiq NDCG dropped from 0.16 to 0.02 when the
    broader gate was in place). Only the Panharmonicon shape
    consistently benefits: Isshin has no trigger of his own, so a
    dedicated attack-payoff pool is additive rather than displacing.
    """
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "static" and ev == "Panharmonicon":
            # Normalise whitespace in the raw dict repr so tests
            # don't have to replicate Forge's exact pretty-printing.
            raw = "".join((p.get("raw_line") or "").split())
            if "Attacks" in raw and "'ValidCause':'Creature'" in raw:
                return True
    return False


def _find_attack_payoffs(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Match attack-trigger payoff cards for combat-trigger commanders.

    Parallel to ``_find_dies_drain``. Isshin doubles every creature-
    attack trigger; Adeline, Rafiq, Edgar Markov care about attack
    events directly. The Hi-Syn EDHREC cards for these commanders
    (Krenko Tin Street, Captain Lannery Storm, Adeline, Mardu
    Ascendancy, Sword of the Animist) all share a single distinctive
    port pair: ``Attacks``/``AttackersDeclared`` trigger + a
    value-producing effect (Token / +1/+1 / Draw / Mana / land cheat).

    The existing ``panharmonicon`` rule already matches these cards
    for Isshin, but with a 1000+ card pool the IDF is tiny and they
    tie with pure staples on CMC. This dedicated rule gives them a
    narrower pool (~400) plus a 1.5× quality multiplier so the real
    attack-trigger payoffs crack top-30.

    Gate: ``_has_creature_attack_trigger`` — Panharmonicon static over
    Attacks+Creature only. Commanders that themselves have an Attacks
    trigger (Edgar Markov, Rafiq) are intentionally *not* gated in;
    see ``_has_creature_attack_trigger`` for the rationale.
    """
    if not _has_creature_attack_trigger(cmdr_ports):
        return []

    if candidate_cache is not None:
        pool: frozenset[str] = candidate_cache.attack_payoff_cards
    else:
        from ..penalties import _bulk_load_attack_payoff_cards

        pool = _bulk_load_attack_payoff_cards(conn)

    return [
        PortComplement(
            rule_id="attack_payoffs",
            direction="synergy",
            candidate=name,
            cmdr_event="creature_attacks",
            cand_event="attack_payoff",
        )
        for name in pool
        if name not in cmdr_set
    ]
