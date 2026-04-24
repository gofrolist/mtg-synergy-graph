"""Static ability complement matchers (cost reduction, graveyard play, edicts)."""

from __future__ import annotations

import re
import sqlite3

from ..graph_engine import _trigger_only_matches_self
from .core import PortComplement, PortRow

#: valid_filter values indicating edict-style sacrifice targeting.
_EDICT_FILTERS: tuple[str, ...] = (
    "Player",
    "Opponent",
    "Player.Opponent",
    "TriggeredPlayer",
    "TriggeredDefendingPlayer",
    "Player.Other",
)

#: Top-level card types that map cleanly to the ``card_types`` column.
#: Compound Affinity forms like ``Creature.Artifact:artifact``
#: (Urza, Chief Artificer) or ``Land.Snow:snow`` would not match
#: card_types as a substring; callers take the first type token only,
#: skip the rest.
_AFFINITY_RECOGNIZED_TYPES: frozenset[str] = frozenset(
    {"Artifact", "Creature", "Enchantment", "Land", "Planeswalker", "Instant", "Sorcery"}
)


def _find_cost_reduction_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find cost reducers for commanders with SpellCast triggers.

    Talrand triggers on Instant/Sorcery cast -> Goblin Electromancer
    reduces Instant/Sorcery costs, letting you cast more spells per turn.
    Jhoira triggers on Artifact cast -> Etherium Sculptor reduces
    Artifact costs.

    Only matches when the cost reducer's ValidCard type overlaps the
    commander's spell trigger filter type.
    """
    # Collect spell types the commander cares about
    wanted_types: set[str] = set()
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "trigger" and ev == "SpellCast":
            vf = p.get("valid_filter") or ""
            for alt in vf.split(","):
                base = alt.strip().split(".")[0].split("+")[0].strip()
                if base in ("Instant", "Sorcery", "Creature", "Artifact", "Enchantment", "Aura", "Equipment"):
                    wanted_types.add(base)
            # "nonCreature" -> Instant/Sorcery/Artifact/Enchantment
            if "nonCreature" in vf:
                wanted_types.update(("Instant", "Sorcery", "Artifact", "Enchantment"))

    if not wanted_types:
        return []

    # Find ReduceCost statics that affect the wanted types
    cur = conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'ReduceCost' "
        "AND raw_line NOT LIKE '%ValidCard%Card.Self%' "
        "AND raw_line NOT LIKE '%ValidTarget%Card.Self%'"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        name = r["card_name"]
        if name in cmdr_set or name in seen:
            continue
        raw = r["raw_line"] or ""
        # Extract ValidCard types
        m = re.search(r"'ValidCard':\s*'([^']+)'", raw)
        if not m:
            m = re.search(r"'ValidTarget':\s*'([^']+)'", raw)
        if not m:
            continue
        valid_card = m.group(1)
        # Check overlap: prefer specific type match, fall back to generic "Card"
        matched_type = next((wt for wt in sorted(wanted_types) if wt in valid_card), "")
        if not matched_type and valid_card.split(".")[0].split(",")[0].strip() == "Card":
            matched_type = next(iter(sorted(wanted_types)))
        if not matched_type:
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="cost_reducer",
                direction="synergy",
                candidate=name,
                cmdr_event=f"SpellCast_{matched_type}",
                cand_event=f"ReduceCost_{matched_type}",
                filter_group=matched_type,
            )
        )

    return results


def _find_graveyard_play_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find MayPlay-from-Graveyard enablers for landfall/GY commanders.

    Omnath triggers on Land entering -> Crucible of Worlds lets you play
    lands from graveyard (more landfall triggers from fetch lands).
    Muldrotha wants to cast permanents from GY -> Crucible covers the
    land slot.

    Narrow: only ~42 cards have MayPlay + Graveyard + Land.
    """
    # Detect landfall commanders
    has_landfall = False
    has_gy_scale = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        vf = p.get("valid_filter") or ""
        if pt == "trigger" and ev == "ChangesZone" and "Land" in vf:
            zd = (p.get("zone_destination") or "").strip() or "Battlefield"
            if zd == "Battlefield":
                has_landfall = True
        if pt == "scales_with" and ("Land" in ev or "land" in ev):
            has_gy_scale = True

    if not has_landfall and not has_gy_scale:
        return []

    # Find MayPlay + Graveyard + Land statics
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'Continuous' "
        "AND raw_line LIKE '%MayPlay%' AND raw_line LIKE '%Graveyard%' "
        "AND raw_line LIKE '%Land%'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="graveyard_play",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="landfall",
                    cand_event="MayPlay_Land_GY",
                )
            )

    return results


def _find_affinity_archetype(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Affinity-keyword commanders want many permanents of the affinity
    type. Emry, Lurker of the Loch has Affinity:Artifact -> she benefits
    from cheap artifacts, artifact lands, and artifact-cost reducers.

    Narrow gate: commander has a ``Affinity:<Type>`` keyword port.

    Three cand_event buckets with distinct IDF:
    - ``typed_land``: Land cards of the affinity type (Seat of the Synod,
      Darksteel Citadel for Artifact). ~22 cards, very high IDF.
    - ``cost_reducer``: ReduceCost statics targeting the type (Etherium
      Sculptor, Foundry Inspector).
    - ``cheap_typed``: CMC 0-1 non-land permanents of the type (Mishra's
      Bauble, Lotus Petal, Chromatic Star). ~300 cards, medium IDF.

    The affinity-type subtype isn't used as a filter directly — the
    common case is Affinity:Artifact, so we read the keyword suffix
    and match `card_types LIKE '%<Type>%'`. For non-Artifact Affinity
    (e.g. Equipment) the rule works the same way.
    """
    affinity_type = ""
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "keyword":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev.startswith("Affinity:"):
            raw_type = ev.split(":", 1)[1].strip()
            # Split on "." to drop compound subtype qualifiers and keep the
            # base type ("Creature.Artifact:artifact" → "Creature").
            base = raw_type.split(".", 1)[0].strip()
            if base in _AFFINITY_RECOGNIZED_TYPES:
                affinity_type = base
                break

    # Extended gate for Artifact: commanders without Affinity:Artifact but
    # whose core mechanic involves artifacts (Sharuum reanimates artifacts,
    # Breya sacrifices artifacts, Osgir sacs artifacts for +X/+0, Urza taps
    # artifacts for mana) share the same canonical support pool — cheap
    # artifacts, artifact lands, cost reducers.
    #
    # Require the cost/effect to be PURELY artifact — Mondrak's
    # ``Sac<2/Artifact.Other;Creature.Other>`` mixes artifacts with
    # creatures and isn't an artifact-matters commander. The ``;``
    # separator in Forge notation indicates alternative types.
    if not affinity_type:
        for p in cmdr_ports:
            pt = (p.get("port_type") or "").strip()
            ev = (p.get("event_class") or "").strip()
            vf = p.get("valid_filter") or ""
            raw = p.get("raw_line") or ""
            # Reanimate/cast-from-GY artifact (Sharuum, Emry)
            if pt == "effect" and ev in ("ChangeZone", "Effect") and "Artifact" in vf and ";" not in vf:
                affinity_type = "Artifact"
                break
            # Sacrifice/tap-type with only-Artifact cost (Breya, Osgir, Urza)
            if pt == "cost" and ev in ("sacrifice", "tap_type"):
                m = re.search(r"<\d+/([^/>]+)(?:/[^>]*)?>", raw)
                if m and "Artifact" in m.group(1) and ";" not in m.group(1):
                    affinity_type = "Artifact"
                    break
    if not affinity_type:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    type_pattern = f"%{affinity_type}%"

    def _add(name: str, cand_event: str) -> None:
        if name in cmdr_set or name in seen:
            return
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="affinity_archetype",
                direction="synergy",
                candidate=name,
                cmdr_event=f"Affinity_{affinity_type}",
                cand_event=cand_event,
            )
        )

    # typed_land: Land cards of the affinity type
    cur = conn.execute(
        "SELECT name FROM cards WHERE card_types LIKE '%Land%' AND card_types LIKE ?",
        (type_pattern,),
    )
    for r in cur.fetchall():
        _add(r["name"], "typed_land")

    # cost_reducer: ReduceCost statics whose ValidCard hits the type
    cur = conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports WHERE port_type = 'static' AND event_class = 'ReduceCost'"
    )
    for r in cur.fetchall():
        raw = r["raw_line"] or ""
        if f"'ValidCard': '{affinity_type}" in raw or f"'Type': '{affinity_type}" in raw:
            _add(r["card_name"], "cost_reducer")

    # cheap_typed: non-land permanents of the type at CMC <= 1
    cur = conn.execute(
        "SELECT name FROM cards WHERE card_types LIKE ? AND card_types NOT LIKE '%Land%' AND cmc <= 1",
        (type_pattern,),
    )
    for r in cur.fetchall():
        _add(r["name"], "cheap_typed")

    return results


#: Trigger-filter fragments that narrow the firing population enough
#: that edict effects (forcing opponents to sacrifice creatures) no
#: longer reliably feed the trigger. Audit 2026-04-24 showed rules
#: with these filters hitting edict_feeder's 153-touched set but ALL
#: in the displacement tail — Toxrill (``counters_GE1_SLIME``),
#: Koll (``+enchanted``/``+equipped``), Donatello (``HasCounters``),
#: Jaws (``Artifact.nonCreature``), Astrid (``Food``/``Clue``).
#: Generic edicts produce deaths of plain creatures without these
#: modifiers, so the triggers don't actually fire.
_EDICT_GATE_REJECTING_FRAGMENTS: tuple[str, ...] = (
    "counters_GE",
    "counters_EQ",
    "counters_LE",
    "+enchanted",
    "+equipped",
    "HasCounters",
    "Food",
    "Clue",
    "Treasure",
    "Blood",
)


def _edict_benefits_from_trigger(vf: str) -> bool:
    """Return True if a trigger with valid_filter ``vf`` would plausibly
    fire when an opponent is forced to sacrifice a creature by an
    edict effect.

    Rejects narrow mechanical filters that depend on conditions edicts
    don't produce (specific counters, attached auras/equipment,
    non-creature artifact subtypes).
    """
    if any(frag in vf for frag in _EDICT_GATE_REJECTING_FRAGMENTS):
        return False
    # Edicts target creatures — reject filters whose main type is
    # Artifact / Enchantment *without* Creature as a separate type.
    # Substring "Creature" inside "nonCreature" doesn't count as the
    # type being present (Jaws, Relentless Predator has
    # ``Artifact.nonCreature`` — edicts don't sac non-creature
    # artifacts).
    has_creature_type = "Creature" in vf and "nonCreature" not in vf
    if vf.startswith("Artifact") and not has_creature_type:
        return False
    return not (vf.startswith("Enchantment") and not has_creature_type)


def _find_edict_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find edict effects for death-trigger commanders.

    Meren triggers on creatures dying -> Plaguecrafter's ETB forces
    each player to sacrifice a creature, feeding Meren's trigger.
    Tergrid triggers on opponent Sacrificed -> edicts force opponents
    to sacrifice, handing Tergrid permanents from the graveyard.

    Edicts are Sacrifice/SacrificeAll effects targeting Player/Opponent/Each.
    N ≈ 100-200 (edict effects in Magic).

    Gate narrowing (2026-04-24): the trigger filter must plausibly
    fire from an opponent-creature sacrifice. Excludes narrow
    mechanical filters that edicts don't satisfy — counter-gated
    triggers (Toxrill/Donatello), enchanted/equipped-only filters
    (Koll), and non-creature artifact subtypes (Jaws/Astrid).
    See ``_edict_benefits_from_trigger`` for the full predicate.
    """
    # Detect death-trigger commanders with edict-compatible filters
    has_death_trigger = False
    has_sacrificed_trigger = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt != "trigger":
            continue
        vf = p.get("valid_filter") or ""
        if ev == "ChangesZone":
            zd = (p.get("zone_destination") or "").strip()
            if zd == "Graveyard" and not _trigger_only_matches_self(vf) and _edict_benefits_from_trigger(vf):
                has_death_trigger = True
        if ev == "Sacrificed" and _edict_benefits_from_trigger(vf):
            has_sacrificed_trigger = True

    if not has_death_trigger and not has_sacrificed_trigger:
        return []

    cmdr_event = "Sacrificed" if has_sacrificed_trigger else "ChangesZone_death"

    # Find edict effects: Sacrifice/SacrificeAll targeting opponents/each player.
    # Safety: placeholders are "?,?,..." from len() of a module-level constant;
    # all values bound via params — no user input enters the SQL string.
    placeholders = ",".join("?" * len(_EDICT_FILTERS))
    cur = conn.execute(
        f"SELECT DISTINCT card_name FROM card_ports "
        f"WHERE port_type = 'effect' "
        f"AND event_class IN ('Sacrifice', 'SacrificeAll') "
        f"AND valid_filter IN ({placeholders})",
        _EDICT_FILTERS,
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="edict_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event=cmdr_event,
                    cand_event="Sacrifice_edict",
                )
            )

    return results
