"""Graveyard-related complement matchers."""

from __future__ import annotations

import re
import sqlite3

from ..attributes import CARD_TYPES, CONTROLLER_QUALIFIERS, SUPERTYPES
from ..penalties import CandidateCache
from .core import PortComplement, PortRow

#: Card types that are spells (not permanents). Used by _find_graveyard_sac_value
#: to skip commanders that only replay instants/sorceries (e.g. Kess).
_SPELL_ONLY_TYPES: frozenset[str] = frozenset({"Instant", "Sorcery"})

#: Recast-from-GY types that are NOT creature-adjacent. If a
#: commander's MayPlay-from-GY ``Affected`` set is a subset of these,
#: they don't want creature-focused GY-fill / reanimator support.
_NON_CREATURE_RECAST_TYPES: frozenset[str] = frozenset(
    {"Instant", "Sorcery", "Artifact", "Enchantment", "Land", "Planeswalker", "Battle"}
)

#: Effect events that indicate a sacrifice cost has a valuable payoff.
_VALUABLE_SAC_EFFECTS: tuple[str, ...] = (
    "Mana",
    "Draw",
    "Destroy",
    "GainLife",
    "ChangeZone",
    "Token",
    "Mill",
    "DealDamage",
    "PutCounter",
)


#: Cast-from-GY recastable types (subset where spell_density density
#: signal makes sense — creatures are handled by other rules).
_CASTABLE_TYPES: frozenset[str] = frozenset({"Instant", "Sorcery"})


def _wants_graveyard(cmdr_ports: list[PortRow]) -> bool:
    """Return True if the commander reanimates/replays from graveyard."""
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "effect" and ev == "ChangeZone" and (p.get("zone_origin") or "").strip() == "Graveyard":
            return True
        if pt == "static" and ev == "Continuous":
            raw = str(p.get("raw_line") or "")
            if "'MayPlay'" in raw and "'Graveyard'" in raw:
                return True
        if pt == "scales_with" and ("Graveyard" in ev or "graveyard" in ev):
            return True
    return False


def _extract_recast_types(cmdr_ports: list[PortRow]) -> set[str]:
    """Extract card types the commander can recast from graveyard."""
    recast_types: set[str] = set()
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt != "static" or ev != "Continuous":
            continue
        raw = str(p.get("raw_line") or "")
        if "'MayPlay'" not in raw or "'Graveyard'" not in raw:
            continue
        m = re.search(r"'Affected':\s*'([^']+)'", raw)
        if not m:
            continue
        for alt in m.group(1).split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base and base[0].isupper() and base != "Card":
                recast_types.add(base)
    return recast_types


def _wants_gy_fill(cmdr_ports: list[PortRow]) -> bool:
    """Return True if the commander needs entomb/discard GY-fill.

    Fires for commanders that actively REANIMATE permanents
    (``effect: ChangeZone`` Graveyard→Battlefield) or RECAST creatures
    from the graveyard (MayPlay-from-GY static whose ``Affected``
    names ``Creature`` / ``Permanent`` or a creature subtype — Karador,
    Muldrotha, Gisa and Geralf, Chainer).

    Tergrid-style ``valid_filter='TriggeredCard'`` reanimation of an
    opponent's discarded card is rejected (irrelevant to own-side
    GY-fill). A bare ``cost: discard`` alone does NOT qualify —
    Borborygmos Enraged discards for retrace/Dredge without
    reanimation. Kess and Emry also stay False because their MayPlay
    scopes (Instant/Sorcery and Artifact respectively) are covered
    by ``_NON_CREATURE_RECAST_TYPES``.
    """
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if (
            pt == "effect"
            and ev == "ChangeZone"
            and (p.get("zone_origin") or "").strip() == "Graveyard"
            and (p.get("zone_destination") or "").strip() == "Battlefield"
        ):
            vf = (p.get("valid_filter") or "").strip()
            # Skip Tergrid-style "reanimate the card THEY just discarded"
            # — TriggeredCard with an opponent-scoped trigger means the
            # reanimation targets opponents' cards, not your own, so
            # own-side GY-fill (Buried Alive) is irrelevant.
            if vf == "TriggeredCard":
                continue
            return True
    # Recast-from-GY for creature-like types: Karador's
    # ``Creature.nonLand+YouCtrl``, Muldrotha's multi-type MayPlay,
    # Gisa and Geralf's ``Zombie.YouCtrl``. ``_extract_recast_types``
    # already strips ``Card``, so any creature subtype surfaces via
    # the ``recast - _NON_CREATURE_RECAST_TYPES`` difference.
    #
    # Note: a bare ``cost: discard`` doesn't qualify — Borborygmos
    # Enraged uses discard for retrace/Dredge, not reanimation.
    recast = _extract_recast_types(cmdr_ports)
    return "Creature" in recast or "Permanent" in recast or bool(recast - _NON_CREATURE_RECAST_TYPES)


def _emit_filler(
    rows: list[sqlite3.Row],
    cand_event: str,
    cmdr_set: set[str],
    seen: set[str],
) -> list[PortComplement]:
    """Build filler complements from a row iterable, mutating ``seen``."""
    out: list[PortComplement] = []
    for r in rows:
        name = r["card_name"]
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        out.append(
            PortComplement(
                rule_id="trigger_effect",
                direction="synergy",
                candidate=name,
                cmdr_event="graveyard_filler",
                cand_event=cand_event,
            )
        )
    return out


def _query_self_mill(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Self-mill effects (Mill / DigUntil / Surveil targeting you)."""
    return conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class IN ('Mill', 'DigUntil', 'Surveil') "
        "AND (valid_filter LIKE '%YouCtrl%' OR valid_filter LIKE '%YouOwn%' "
        "OR valid_filter = 'You' OR valid_filter LIKE 'You.%')"
    ).fetchall()


def _query_entomb(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Entomb effects: ChangeZone Library→Graveyard.  N≈49."""
    return conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class IN ('ChangeZone', 'ChangeZoneAll') "
        "AND zone_origin = 'Library' AND zone_destination = 'Graveyard'"
    ).fetchall()


def _query_self_discard(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Self-discard effects (Faithless Looting fills GY).  N≈423."""
    return conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Discard' "
        "AND (valid_filter LIKE '%You%' OR valid_filter = '' OR valid_filter IS NULL "
        "     OR valid_filter LIKE '%Each%') "
        "AND valid_filter NOT LIKE '%Opp%'"
    ).fetchall()


def _query_discard_cost(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Discard-as-cost (Cathartic Reunion: pay discard → draw).  N≈490."""
    return conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'cost' AND event_class = 'discard'"
    ).fetchall()


def _emit_recast_density(
    conn: sqlite3.Connection,
    card_type: str,
    cmdr_set: set[str],
    seen: set[str],
) -> list[PortComplement]:
    """Emit spell_density complements for cards matching a recastable type."""
    # card_type is from _CASTABLE_TYPES frozenset — safe, but escape
    # LIKE wildcards for defense-in-depth.
    safe_ct = card_type.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT name FROM cards WHERE card_types LIKE ? ESCAPE '\\'",
        (f"%{safe_ct}%",),
    ).fetchall()
    out: list[PortComplement] = []
    for r in rows:
        name = r["name"]
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        out.append(
            PortComplement(
                rule_id="spell_density",
                direction="synergy",
                candidate=name,
                cmdr_event="graveyard_cast",
                cand_event=card_type,
            )
        )
    return out


def _find_graveyard_fillers(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find graveyard fillers for commanders that reanimate or cast from GY.

    Commanders with ChangeZone effects from Graveyard (Meren, Marchesa)
    or static MayPlay from Graveyard (Karador, Muldrotha, Kess) want
    cards that fill the graveyard: self-mill, dredge, self-discard, and
    creatures that put themselves in the graveyard (evoke, self-sacrifice).
    """
    if not _wants_graveyard(cmdr_ports):
        return []

    recast_types = _extract_recast_types(cmdr_ports)
    results: list[PortComplement] = []
    seen: set[str] = set()

    # Self-mill always fires for GY commanders (broad gate)
    results.extend(_emit_filler(_query_self_mill(conn), "self_mill", cmdr_set, seen))

    # Entomb / self-discard / discard-cost only for reanimators
    if _wants_gy_fill(cmdr_ports):
        results.extend(_emit_filler(_query_entomb(conn), "entomb", cmdr_set, seen))
        results.extend(_emit_filler(_query_self_discard(conn), "self_discard", cmdr_set, seen))
        results.extend(_emit_filler(_query_discard_cost(conn), "discard_cost", cmdr_set, seen))

    # Recast-type density: Kess wants instants/sorceries.
    for card_type in recast_types & _CASTABLE_TYPES:
        results.extend(_emit_recast_density(conn, card_type, cmdr_set, seen))

    return results


def _find_artifact_recursion(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find self-sacrificing and ETB artifacts for artifact recursion commanders.

    Osgir copies exiled artifacts -> wants Ichor Wellspring (sac for value,
    then copy from exile). Daretti returns artifacts from graveyard ->
    wants artifacts that put themselves in the graveyard.
    """
    wants_artifact_gy = False
    wants_artifact_copy = False

    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        vf = p.get("valid_filter") or ""
        raw = str(p.get("raw_line") or "")

        if pt == "effect" and ev == "ChangeZone":
            zo = (p.get("zone_origin") or "").strip()
            if zo == "Graveyard" and "Artifact" in vf:
                wants_artifact_gy = True
        if pt == "effect" and ev == "CopyPermanent" and ("Artifact" in vf or "Artifact" in raw):
            wants_artifact_copy = True

    if not wants_artifact_gy and not wants_artifact_copy:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Artifacts with sacrifice cost (self-recycling)
    cur = conn.execute(
        "SELECT DISTINCT cp.card_name FROM card_ports cp "
        "JOIN cards c ON cp.card_name = c.name "
        "WHERE cp.port_type = 'cost' AND cp.event_class = 'sacrifice' "
        "AND c.card_types LIKE '%Artifact%'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="artifact_recursion",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="graveyard_artifact",
                    cand_event="sac_artifact",
                )
            )

    # Artifacts with self-ETB + valuable effects (for recursion AND copy).
    # Ichor Wellspring draws on ETB + LTB — great for Daretti (recur from
    # GY) and Osgir (copy from exile).  The early-return above guarantees
    # at least one of wants_artifact_gy / wants_artifact_copy is True.
    cmdr_ev = "graveyard_artifact" if wants_artifact_gy else "copy_artifact"
    cur2 = conn.execute(
        "SELECT DISTINCT cp.card_name FROM card_ports cp "
        "JOIN cards c ON cp.card_name = c.name "
        "WHERE cp.port_type = 'trigger' AND cp.event_class = 'ChangesZone' "
        "AND cp.valid_filter LIKE '%Card.Self%' "
        "AND cp.zone_destination = 'Battlefield' "
        "AND c.card_types LIKE '%Artifact%' "
        "AND cp.card_name IN ("
        "  SELECT card_name FROM card_ports WHERE port_type = 'effect' "
        "  AND event_class IN ('Draw', 'Destroy', 'Token', 'DealDamage', 'Mana')"
        ")"
    )
    for r in cur2.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="artifact_recursion",
                    direction="synergy",
                    candidate=name,
                    cmdr_event=cmdr_ev,
                    cand_event="etb_artifact",
                )
            )

    # Artifact lands: free artifacts that increase artifact count
    # for recursion and copy commanders.  N≈22, high IDF.
    cur3 = conn.execute("SELECT name FROM cards WHERE card_types LIKE '%Artifact%' AND card_types LIKE '%Land%'")
    for r in cur3.fetchall():
        name = r["name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="artifact_recursion",
                    direction="synergy",
                    candidate=name,
                    cmdr_event=cmdr_ev,
                    cand_event="artifact_land",
                )
            )

    return results


def _find_copy_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find good copy targets for CopyPermanent commanders.

    Ghired (populate) -> token producers give more targets to populate.
    Riku (creature copy) -> creatures with valuable ETBs are best copy targets.
    """
    has_populate = False
    has_creature_copy = False

    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt != "effect" or ev != "CopyPermanent":
            continue
        raw = str(p.get("raw_line") or "")
        if "'Populate'" in raw:
            has_populate = True
        vf = p.get("valid_filter") or ""
        if "Creature" in vf or "Creature" in raw:
            has_creature_copy = True

    if not has_populate and not has_creature_copy:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Populate: find token producers (more targets to copy)
    if has_populate:
        cur = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'effect' AND event_class = 'Token'"
        )
        for r in cur.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="copy_synergy",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="CopyPermanent_populate",
                        cand_event="Token",
                    )
                )

        # Other populate spells stack with the commander's own populate
        # (Sundering Growth, Rootborn Defenses, Growing Ranks, Second
        # Harvest, Song of the Worldsoul). Narrow pool (~26 cards) so
        # these canonical EDHREC picks rise above the 280+ broad Token-
        # producer pool. Emitted with a distinct rule_id so the
        # ``populate_stack`` quality multiplier doesn't lift Riku's
        # (creature-copy) matches. Tolerate both dict-repr variants
        # (spaced and compact) for resilience to importer formatting.
        cur2 = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' AND event_class = 'CopyPermanent' "
            "AND (raw_line LIKE '%''Populate'': ''True''%' "
            "     OR raw_line LIKE '%''Populate'':''True''%')"
        )
        for r in cur2.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="populate_stack",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="CopyPermanent_populate",
                        cand_event="populate_stack",
                    )
                )

    # Creature copy: find creatures with self-ETB + valuable effects
    if has_creature_copy:
        _VALUABLE_LIST = (
            "Draw",
            "Destroy",
            "DestroyAll",
            "Token",
            "GainControl",
            "DealDamage",
            "ChangeZone",
            "Mana",
        )
        _val_ph = ",".join("?" * len(_VALUABLE_LIST))
        cur2 = conn.execute(
            "SELECT DISTINCT cp.card_name FROM card_ports cp "
            "JOIN cards c ON cp.card_name = c.name "
            "WHERE cp.port_type = 'trigger' AND cp.event_class = 'ChangesZone' "
            "AND cp.valid_filter LIKE '%Card.Self%' "
            "AND cp.zone_destination = 'Battlefield' "
            "AND c.card_types LIKE '%Creature%' "
            "AND cp.card_name IN ("
            "  SELECT card_name FROM card_ports WHERE port_type = 'effect' "
            f"  AND event_class IN ({_val_ph})"
            ")",
            _VALUABLE_LIST,
        )
        for r in cur2.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="copy_synergy",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="CopyPermanent_creature",
                        cand_event="etb_creature",
                    )
                )

    return results


def _wants_graveyard_recursion(
    cmdr_ports: list[PortRow],
) -> bool:
    """Check if the commander recurs its own cards from graveyard.

    True when the commander has a ChangeZone effect from Graveyard
    (Meren, Karador) or a MayPlay-from-Graveyard static (Kess, Muldrotha).

    False for steal-only commanders like Tergrid whose ChangeZone from
    Graveyard targets ``TriggeredCard`` (opponent's permanents). If a
    commander has both self-recursion AND steal, self-recursion wins.
    """
    has_self_recursion = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "effect" and ev == "ChangeZone":
            zo = (p.get("zone_origin") or "").strip()
            vf = p.get("valid_filter") or ""
            # TriggeredCard = steal from opponent (Tergrid), skip
            if zo == "Graveyard" and "TriggeredCard" not in vf:
                has_self_recursion = True
        if pt == "static" and ev == "Continuous":
            raw = str(p.get("raw_line") or "")
            if "'MayPlay'" in raw and "'Graveyard'" in raw:
                has_self_recursion = True
    return has_self_recursion


def _find_graveyard_sac_value(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find self-sacrificing permanents for graveyard-replay commanders.

    Muldrotha replays permanents from GY.  Spore Frog (sac: fog),
    Seal of Primordium (sac: destroy), Sakura-Tribe Elder (sac: ramp)
    are reusable removal/ramp/protection when replayed every turn.

    The existing ``_find_etb_sac_targets`` requires BOTH self-ETB AND
    sacrifice cost.  This rule is broader: any permanent with sacrifice
    cost + valuable effect.  Cards matching both rules get a multi-rule
    boost (correct — Plaguecrafter with ETB+sac+effect > Spore Frog
    with sac+effect only).

    N ≈ 1602, IDF ≈ 0.093.
    """
    if not _wants_graveyard_recursion(cmdr_ports):
        return []

    # Only fire for commanders that replay PERMANENTS from GY.
    # Kess replays Instant/Sorcery — sacrifice permanents are irrelevant.
    # Check: if the MayPlay static restricts to Instant/Sorcery, skip.
    replays_permanents = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "effect" and ev == "ChangeZone":
            zo = (p.get("zone_origin") or "").strip()
            vf = p.get("valid_filter") or ""
            if zo == "Graveyard" and "TriggeredCard" not in vf:
                # Meren-style: reanimates creatures (permanents) ✓
                replays_permanents = True
        if pt == "static" and ev == "Continuous":
            raw = str(p.get("raw_line") or "")
            if "'MayPlay'" in raw and "'Graveyard'" in raw:
                # Parse Affected types — skip if spell-only
                m = re.search(r"'Affected':\s*'([^']+)'", raw)
                if m:
                    affected = {alt.strip().split(".")[0].split("+")[0].strip() for alt in m.group(1).split(",")}
                    # If ALL affected types are spell-only, skip
                    non_spell = affected - _SPELL_ONLY_TYPES - {"Card", ""}
                    if non_spell:
                        replays_permanents = True
                else:
                    # No Affected filter → replays anything (Muldrotha)
                    replays_permanents = True

    if not replays_permanents:
        return []

    ph = ",".join("?" * len(_VALUABLE_SAC_EFFECTS))
    # Use IN-subquery instead of JOIN to avoid a costly self-join on
    # the 184k-row card_ports table (JOIN was ~58s, subquery is ~0.02s).
    # Load the best (rarest) effect type per candidate for IDF sub-grouping.
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'cost' AND event_class = 'sacrifice' "
        "AND card_name IN ("
        "  SELECT card_name FROM card_ports "
        f"  WHERE port_type = 'effect' AND event_class IN ({ph})"
        ")",
        _VALUABLE_SAC_EFFECTS,
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
                    cmdr_event="replays_from_gy",
                    cand_event="self_sacrifice",
                )
            )
    return results


def _find_etb_sac_targets(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find creatures with self-ETB + sacrifice cost for reanimation commanders.

    Meren recurs creatures from graveyard -> wants Plaguecrafter (ETB forces
    sacrifice, then sacrifices itself), Spore Frog (sac to Fog), Sakura-Tribe
    Elder (sac to fetch land). These are "sac-recur" creatures.

    Narrow intersection: self-ETB trigger AND sacrifice cost -> ~200-400 cards.
    """
    if not _wants_graveyard_recursion(cmdr_ports):
        return []

    cur = conn.execute(
        "SELECT DISTINCT a.card_name FROM card_ports a "
        "WHERE a.port_type = 'trigger' AND a.event_class = 'ChangesZone' "
        "AND a.valid_filter LIKE '%Card.Self%' "
        "AND a.zone_destination = 'Battlefield' "
        "AND a.card_name IN ("
        "  SELECT card_name FROM card_ports "
        "  WHERE port_type = 'cost' AND event_class = 'sacrifice'"
        ")"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="etb_sac_target",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="graveyard_reanimate",
                    cand_event="etb_sac_creature",
                )
            )

    return results


def _has_creature_dies_trigger(cmdr_ports: list[PortRow]) -> bool:
    """Return True iff the commander itself triggers on another creature
    dying, OR amplifies such triggers via a Panharmonicon-style static.

    Strict gate for ``_find_dies_drain``. Accepts:

    1. A raw ``ChangesZone Battlefield→Graveyard`` trigger whose
       ``valid_filter`` names ``Creature`` / ``Permanent`` **or** a
       creature subtype (Zombie, Saproling, Elemental, …) — so Meren,
       Wilhelt (``Zombie.Other``) and Slimefoot (``Saproling.YouCtrl``)
       qualify alongside plain ``Creature.Other`` commanders. A
       solitary ``Card.Self`` filter is rejected (~680 "when I die"
       cards that don't actually pay off other creature deaths).

    2. A ``static: Panharmonicon`` port whose ``raw_line`` describes
       a creature-dies doubler — ``Origin: Battlefield``,
       ``Destination: Graveyard``, and ``ValidCause: Creature`` — so
       Teysa Karlov qualifies even though she has no trigger of her
       own. Doubling a creature-dies trigger is mechanically
       equivalent to having one for the purpose of aristocrat-payoff
       synergies.
    """
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        if pt == "static" and (p.get("event_class") or "").strip() == "Panharmonicon":
            # Normalise whitespace in the raw dict repr so the check is
            # tolerant of Forge's pretty-printing.
            raw = "".join((p.get("raw_line") or "").split())
            if (
                "'Origin':'Battlefield'" in raw
                and "'Destination':'Graveyard'" in raw
                and "'ValidCause':'Creature'" in raw
            ):
                return True
            continue
        if pt != "trigger":
            continue
        if (p.get("event_class") or "").strip() != "ChangesZone":
            continue
        if (p.get("zone_origin") or "").strip() != "Battlefield":
            continue
        if (p.get("zone_destination") or "").strip() != "Graveyard":
            continue
        vf = (p.get("valid_filter") or "").strip()
        if not vf:
            continue
        # `Card.Self,Creature.Other+YouCtrl` → split on `,` for OR-alts,
        # then `+`/`.` within each alt. The first token of an alt is the
        # main type (`Card`, `Creature`, `Zombie`, …); qualifiers follow.
        for alt in vf.split(","):
            tokens = alt.replace("+", ".").split(".")
            if not tokens:
                continue
            main = tokens[0].lstrip("!").strip()
            if not main:
                continue
            if main == "Card":
                # Marchesa: `Card.YouCtrl+counters_GE1_P1P1`. The `Card` main
                # token is too broad on its own, but the +1/+1-counter qualifier
                # restricts the trigger to creatures in practice (P1P1 counters
                # only live on creatures). `Card.Self+counters_*` (Ochre Jelly)
                # is rejected — it's a self-only trigger, not a "any creature
                # you control dies" payoff axis.
                qualifiers = [t.lstrip("!").strip() for t in tokens[1:]]
                if "Self" in qualifiers:
                    continue
                if any("counters_" in q and "P1P1" in q for q in qualifiers):
                    return True
                continue
            if main in ("Creature", "Permanent"):
                return True
            # Any other main-type token that isn't a built-in Forge
            # classifier (type/supertype/controller) is a creature subtype.
            if main not in CARD_TYPES and main not in SUPERTYPES and main not in CONTROLLER_QUALIFIERS:
                return True
    return False


def _find_dies_drain(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Match aristocrats-payoff cards for creature-dies commanders.

    Cards like Blood Artist, Zulaport Cutthroat, Cruel Celebrant, Pitiless
    Plunderer, Grim Haruspex, Midnight Reaper and Elenda all share a
    single distinctive port: a ``ChangesZone Battlefield→Graveyard``
    trigger with a ``Creature``-referencing ``valid_filter``. They are
    universal payoffs for any commander whose own strategy kills their
    creatures repeatedly (Meren, Judith, Wilhelt, Slimefoot, Omnath).

    The existing ``sacrifice_cluster`` rule operates on event names
    ``Sacrificed`` / ``LifeLost``, so it misses these cards whose trigger
    is raw ``ChangesZone`` — hence a dedicated rule.

    Gate: strict. Commander must itself have a creature-dies trigger
    (``_has_creature_dies_trigger``). Commanders that merely *double*
    death triggers (Teysa Karlov's ``Panharmonicon`` static) or sac
    permanents broadly (Korvold's ``Sacrificed`` trigger) do not qualify
    — those axes are covered by other rules.
    """
    if not _has_creature_dies_trigger(cmdr_ports):
        return []

    if candidate_cache is not None:
        payoff_cards: frozenset[str] = candidate_cache.dies_drain_cards
    else:
        from ..penalties import _bulk_load_dies_drain_cards

        payoff_cards = _bulk_load_dies_drain_cards(conn)

    return [
        PortComplement(
            rule_id="dies_drain",
            direction="synergy",
            candidate=name,
            cmdr_event="creature_dies",
            cand_event="dies_payoff",
        )
        for name in payoff_cards
        if name not in cmdr_set
    ]


def _find_gy_loader(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Match Library→Graveyard tutors for reanimator commanders.

    Buried Alive, Entomb, Jarad's Orders, Final Parting, Vile Entomber
    and friends are EDHREC-top-synergy for every reanimator commander
    (Meren Hi-Syn #7 @ 0.52, Karador, Muldrotha, …) because they *set
    up* the engine: tutor a creature into the graveyard, then reanimate
    it. This shape is already queried by ``_find_graveyard_fillers``
    (which emits under ``rule_id='trigger_effect'``), but a tutor
    sorcery typically matches only that single rule, so it scored
    ~0.18 and sat below rank ~150 — well out of top-30.

    Emitting the same cards again under a distinct ``gy_loader``
    rule_id gives them two rule matches, a breadth bonus, and the 1.5×
    quality multiplier (see universal_scorer), lifting them into the
    range where they can compete.

    Gate: ``_wants_gy_fill`` — reanimators / recast-from-GY commanders
    only. Kess (Instant/Sorcery MayPlay) still returns False.
    """
    if not _wants_gy_fill(cmdr_ports):
        return []

    if candidate_cache is not None:
        tutor_cards: frozenset[str] = candidate_cache.gy_loader_cards
    else:
        from ..penalties import _bulk_load_gy_loader_cards

        tutor_cards = _bulk_load_gy_loader_cards(conn)

    return [
        PortComplement(
            rule_id="gy_loader",
            direction="synergy",
            candidate=name,
            cmdr_event="reanimator",
            cand_event="library_to_gy_tutor",
        )
        for name in tutor_cards
        if name not in cmdr_set
    ]
