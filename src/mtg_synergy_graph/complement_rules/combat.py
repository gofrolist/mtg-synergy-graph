"""Combat-related complement matchers."""

from __future__ import annotations

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


def _find_combat_enhancers(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find extra-combat and double-strike cards for DamageDone commanders.

    Saskia triggers on combat damage -> wants Aurelia (extra combat),
    Gisela (damage doubling), creatures with double strike.

    Dedicated IDF group (~212 cards) lifts these above the 1875-card
    trigger_effect pool where they'd be buried.
    """
    has_damage_trigger = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev == "DamageDone":
            vf = p.get("valid_filter") or ""
            if not _trigger_only_matches_self(vf):
                has_damage_trigger = True
                break

    if not has_damage_trigger:
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

    Commanders with ``ChangesZone`` triggers on ``Battlefield -> Graveyard``
    (dying creatures) want candidates with sacrifice costs -- these enable
    the commander to profit from creature deaths on demand.

    Marchesa triggers when creatures with +1/+1 counters die. Meren
    triggers when creatures die. Both want Viscera Seer, Ashnod's Altar,
    etc.
    """
    # Check if commander has a ChangesZone death trigger
    has_death_trigger = False
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
        # Death = going to graveyard (from battlefield)
        if zd == "Graveyard":
            has_death_trigger = True
            break

    if not has_death_trigger:
        return []

    # Find all cards with sacrifice costs, loading cost metadata for
    # sub-IDF grouping (free_outlet / paid_outlet / self_sac).
    cur = conn.execute(
        "SELECT card_name, event_class, cost_target, raw_line FROM card_ports "
        "WHERE port_type = 'cost' AND event_class = 'sacrifice'"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        seen.add(card)
        fg = _cost_filter_group(dict(r))
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
