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
    # Recognized top-level card types that map cleanly to the `card_types`
    # column. Compound Affinity forms like "Creature.Artifact:artifact"
    # (Urza, Chief Artificer) or "Land.Snow:snow" would not match card_types
    # as a substring; take the first type token only, skip the rest.
    _RECOGNIZED_TYPES: frozenset[str] = frozenset(
        {"Artifact", "Creature", "Enchantment", "Land", "Planeswalker", "Instant", "Sorcery"}
    )
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
            if base in _RECOGNIZED_TYPES:
                affinity_type = base
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


def _find_yard_caster(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Reanimator package matching for Chainer-style 'cast from graveyard'
    commanders.

    Gate: commander must have an effect whose raw_line mentions
    ``STYardCast`` — Forge's tag for "you may cast a card from your
    graveyard" static abilities (Chainer, Nightmare Adept; hypothetical
    similar commanders). Chainer pays a Discard cost and gets to cast
    one creature from the graveyard each turn.

    Matches four reanimator-archetype candidate patterns, each in its
    own IDF group (cand_event) so narrow categories score higher:

    - ``mill_to_gy``: Library→Graveyard effects (Buried Alive,
      Gravebreaker Lamia, Entomb). Small pool, high IDF.
    - ``reanimate``: Graveyard→Battlefield creature-targeting effects
      (Animate Dead, Victimize, Living Death, Doomed Necromancer).
    - ``self_recur``: creatures with a self-return effect
      Graveyard→Hand (Squee, Goblin Nabob; Squee, the Immortal).
    - ``loot_outlet``: cards with both Discard-player and Draw effects
      (Faithless Looting, Wheel of Fortune, Jace's Archivist).
    """
    has_yard_cast = any("STYardCast" in str(p.get("raw_line") or "") for p in cmdr_ports)
    if not has_yard_cast:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    def _add(name: str, cand_event: str) -> None:
        if name in cmdr_set or name in seen:
            return
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="yard_caster",
                direction="synergy",
                candidate=name,
                cmdr_event="cast_from_gy",
                cand_event=cand_event,
            )
        )

    # mill_to_gy: effects that put cards from Library into Graveyard
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class IN ('ChangeZone', 'ChangeZoneAll') "
        "AND zone_origin = 'Library' AND zone_destination = 'Graveyard'"
    )
    for r in cur.fetchall():
        _add(r["card_name"], "mill_to_gy")

    # reanimate: Graveyard → Battlefield creature-targeting effects
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class IN ('ChangeZone', 'ChangeZoneAll') "
        "AND zone_origin = 'Graveyard' AND zone_destination = 'Battlefield' "
        "AND (valid_filter LIKE '%Creature%' "
        "     OR valid_filter IN ('', 'Enchanted', 'Remembered') "
        "     OR valid_filter LIKE '%YouCtrl%' OR valid_filter LIKE '%YouOwn%')"
    )
    for r in cur.fetchall():
        _add(r["card_name"], "reanimate")

    # self_recur: creature effect returning Self from Graveyard to Hand
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class = 'ChangeZone' "
        "AND zone_origin = 'Graveyard' AND zone_destination = 'Hand' "
        "AND valid_filter IN ('Self', 'Card.Self')"
    )
    for r in cur.fetchall():
        _add(r["card_name"], "self_recur")

    # loot_outlet: cards with both Draw and Discard-player effects
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Discard' "
        "AND valid_filter IN ('You', 'Player') "
        "AND card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'effect' AND event_class = 'Draw'"
        ")"
    )
    for r in cur.fetchall():
        _add(r["card_name"], "loot_outlet")

    return results


def _find_edict_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find edict effects for death-trigger commanders.

    Meren triggers on creatures dying -> Plaguecrafter's ETB forces
    each player to sacrifice a creature, feeding Meren's trigger.
    Korvold triggers on Sacrificed -> Grave Pact forces opponents
    to sacrifice when your creatures die.

    Edicts are Sacrifice/SacrificeAll effects targeting Player/Opponent/Each.
    N ≈ 100-200 (edict effects in Magic).
    """
    # Detect death-trigger commanders
    has_death_trigger = False
    has_sacrificed_trigger = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt != "trigger":
            continue
        if ev == "ChangesZone":
            zd = (p.get("zone_destination") or "").strip()
            vf = p.get("valid_filter") or ""
            if zd == "Graveyard" and not _trigger_only_matches_self(vf):
                has_death_trigger = True
        if ev == "Sacrificed":
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
